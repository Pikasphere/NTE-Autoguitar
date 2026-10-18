"""和弦表（chord.json）+ gct → 手柄排期的测试。

覆盖：24 个和弦 → 4 页 × 6 方向、名字查位置、扫弦顺序/间隔、单弦拨奏、
只切指位、翻页与摇杆提前量、计数单位、未知和弦的处理。

运行：``.venv\\Scripts\\python.exe tests/test_gct_mapping.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gct import DIR_DOWN, DIR_UP  # noqa: E402
from ui.chords import (  # noqa: E402
    SLOTS,
    ChordError,
    load_chord_names,
    name_at,
    parse_chord_names,
    position_of,
    slot_of,
    unknown_names,
)
from ui.gamepad_backend import (  # noqa: E402
    AIM_GAP,
    LEAD_IN,
    PAGE_STICK_NEXT,
    STRUM_DOWN,
    STRUM_UP,
)
from ui.gct_backend import (  # noqa: E402
    CHORD_HOLD,
    CHORD_LIGHT_HOLD,
    CHORD_STRUM_FLICK,
    CHORD_STRUM_SWEEP,
    build_chord_schedule,
)
from ui.gct_doc import ChordRow, GctDocument, demo_document  # noqa: E402

EV_STRUM = 0x01
EV_PLUCK = 0x02
EV_SET = 0x03


def _doc(*events: tuple[int, str, int, int]) -> GctDocument:
    """events: (tick, chord_name, event_type, extra) —— extra 是方向或弦号。"""
    doc = GctDocument(default_tempo_us=500_000)
    ids: dict[str, int] = {}
    rows: list[ChordRow] = []
    for tick, name, event_type, extra in events:
        if name not in ids:
            ids[name] = doc.add_chord(name)
        cid = ids[name]
        if event_type == EV_STRUM:
            data = cid.to_bytes(2, "little") + bytes([extra])
        elif event_type == EV_PLUCK:
            data = cid.to_bytes(2, "little") + bytes([extra])
        else:
            data = cid.to_bytes(2, "little")
        rows.append(ChordRow(tick, event_type, 0, data))
    rows.append(ChordRow(max((row.tick for row in rows), default=0) + 480, 0x05))
    doc.events = sorted(rows, key=lambda row: row.tick)
    return doc


def _lights(schedule):
    """扫弦时轮盘依次点亮的六根弦（纯显示动作）。"""
    return [a for a in schedule.actions if a.kind == "light" and a.string]


def _string_presses(schedule):
    """真正按下的弦键（现在只有单弦拨奏会按）。"""
    return [a for a in schedule.actions if a.kind == "press" and a.string]


def _rsticks(schedule):
    return [a for a in schedule.actions if a.kind == "rstick"]


def _sticks(schedule):
    return [a for a in schedule.actions if a.kind == "stick"]


# ---------------------------------------------------------------- 和弦表


def test_chord_table_has_24_names() -> None:
    names = load_chord_names()
    assert len(names) == 24 == SLOTS
    assert names[0] == "G"
    assert names[-1] == "Dm"


def test_positions_are_four_pages_of_six() -> None:
    assert position_of("G") == (1, 0), "第 1 个 = 第 1 页正上方"
    assert position_of("Am") == (1, 1)
    assert position_of("Em") == (1, 5)
    assert position_of("Cmaj7") == (2, 0), "第 7 个 = 第 2 页正上方"
    assert position_of("G7") == (2, 3)
    assert position_of("Cmaj9") == (3, 4)
    assert position_of("A") == (4, 4)
    assert position_of("Dm") == (4, 5), "第 24 个 = 第 4 页最后一个方向"


def test_slot_and_name_roundtrip() -> None:
    names = load_chord_names()
    for index, name in enumerate(names):
        assert slot_of(name) == index
        page, dir_index = position_of(name)
        assert name_at(page, dir_index) == name
    assert slot_of("F") is None, "不存在的和弦"
    assert name_at(5, 0) == "", "只有 4 页"


def test_unknown_names_lists_missing() -> None:
    assert unknown_names(["G", "F", "Cmaj7"]) == ["F"]


def test_bad_chord_json_is_rejected() -> None:
    try:
        parse_chord_names({"CHORD_NAMES": [f"C{i}" for i in range(25)]}, "many.json")
    except ChordError as exc:
        assert "24" in str(exc)
    else:
        raise AssertionError("25 个和弦应该被拒绝（只有 4 页）")

    try:
        parse_chord_names({"CHORD_NAMES": ["G", "G"]}, "dup.json")
    except ChordError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复和弦名应该被拒绝")

    try:
        parse_chord_names({"CHORD_NAMES": []}, "empty.json")
    except ChordError as exc:
        assert "CHORD_NAMES" in str(exc)
    else:
        raise AssertionError("空列表应该被拒绝")


# ---------------------------------------------------------------- 排期


def test_strum_down_uses_right_stick_and_lights_six_to_one() -> None:
    schedule = build_chord_schedule(_doc((0, "G", EV_STRUM, DIR_DOWN)))
    lights = _lights(schedule)

    assert [a.string for a in lights] == [6, 5, 4, 3, 2, 1], "下扫从 6 弦到 1 弦"
    assert abs(lights[0].time - LEAD_IN) < 1e-9, "第一个弦仍在和弦时刻亮起"
    for index in range(1, 6):
        assert abs((lights[index].time - lights[index - 1].time) - CHORD_STRUM_SWEEP) < 1e-9

    # 扫弦不再按 6 个弦键，而是把右摇杆向下推一下
    assert _string_presses(schedule) == [], "扫弦不应该再按弦键"
    rstricks = _rsticks(schedule)
    assert len(rstricks) == 2, "推到位 + 回中"
    assert rstricks[0].stick == STRUM_DOWN, "向下推 = 下扫"
    assert abs(rstricks[0].time - LEAD_IN) < 1e-9
    assert rstricks[1].stick == (0, 0)
    assert abs(rstricks[1].time - (LEAD_IN + CHORD_STRUM_FLICK)) < 1e-9

    sticks = _sticks(schedule)
    assert len(sticks) == 1
    assert abs(sticks[0].time - (LEAD_IN - AIM_GAP)) < 1e-9, "左摇杆提前到位"
    assert sticks[0].page == 1

    # 六根弦亮完保持 CHORD_LIGHT_HOLD 后一起熄灭
    darks = [a for a in schedule.actions if a.kind == "dark" and a.string]
    assert [a.string for a in darks] == [6, 5, 4, 3, 2, 1]
    assert abs(darks[0].time - (LEAD_IN + CHORD_LIGHT_HOLD)) < 1e-9
    assert all(abs(a.time - darks[0].time) < 1e-9 for a in darks)


def test_strum_up_uses_right_stick_and_lights_one_to_six() -> None:
    schedule = build_chord_schedule(_doc((0, "G", EV_STRUM, DIR_UP)))
    assert [a.string for a in _lights(schedule)] == [1, 2, 3, 4, 5, 6]
    assert _rsticks(schedule)[0].stick == STRUM_UP, "向上推 = 上扫"


def test_pluck_presses_only_one_string() -> None:
    schedule = build_chord_schedule(_doc((0, "G", EV_PLUCK, 4)))
    presses = _string_presses(schedule)
    assert [a.string for a in presses] == [4]
    assert schedule.note_count == 1
    assert _rsticks(schedule) == [], "单弦拨奏不用右摇杆扫"
    releases = [a for a in schedule.actions if a.kind == "release" and a.string]
    assert abs(releases[0].time - (presses[0].time + CHORD_HOLD)) < 1e-9


def test_set_only_moves_stick() -> None:
    # 只切指位（Set）不出声；后面的扫弦如果方向没变就不用再动摇杆
    schedule = build_chord_schedule(
        _doc((0, "G", EV_SET, 0), (480, "G", EV_STRUM, DIR_DOWN))
    )
    sticks = _sticks(schedule)
    assert len(sticks) == 1, "Set 已经把摇杆摆好，扫弦不需要重复发"
    assert abs(sticks[0].time - LEAD_IN) < 1e-9, "Set 在自己那个时刻摆摇杆"
    assert abs(_lights(schedule)[0].time - (LEAD_IN + 0.5)) < 1e-9


def test_unit_counts_chord_events_not_strings() -> None:
    schedule = build_chord_schedule(
        _doc((0, "G", EV_STRUM, DIR_DOWN), (480, "C", EV_STRUM, DIR_DOWN))
    )
    assert len(_lights(schedule)) == 12, "两次扫弦共亮 12 次弦"
    assert _string_presses(schedule) == [], "扫弦不按弦键"
    assert schedule.note_count == 2, "但只算 2 个和弦"
    assert schedule.unit_label == "和弦"
    units = [a for a in schedule.actions if a.unit]
    assert [a.chord for a in units] == ["G", "C"]


def test_all_24_chords_map_without_conflict() -> None:
    names = load_chord_names()
    schedule = build_chord_schedule(
        _doc(*[(index * 480, name, EV_STRUM, DIR_DOWN) for index, name in enumerate(names)])
    )
    assert schedule.note_count == 24
    assert not schedule.conflicts, schedule.conflicts
    lights = _lights(schedule)
    pages = {a.page for a in lights}
    assert pages == {1, 2, 3, 4}, "24 个和弦要铺满 4 页"
    by_page = {}
    for action in lights:
        by_page.setdefault(action.page, set()).add(action.fret)
    for page, frets in by_page.items():
        assert frets == {1, 2, 3, 4, 5, 6}, f"第 {page} 页的六个方向都要用到"


def test_page_four_chord_flips_before_stick() -> None:
    # Dm 在第 4 页：要向右摇杆向左推 3 次，左摇杆必须等翻页完成后才动
    schedule = build_chord_schedule(_doc((0, "Dm", EV_STRUM, DIR_DOWN)))
    pages = [a for a in schedule.actions if a.kind == "page"]
    sticks = _sticks(schedule)
    assert len(pages) == 3
    assert all(a.stick == PAGE_STICK_NEXT for a in pages)
    assert sticks[0].page == 4
    assert sticks[0].time >= max(a.time for a in pages), "摇杆不能早于翻页完成"
    light = _lights(schedule)[0]
    assert abs((light.time - sticks[0].time) - AIM_GAP) < 1e-9


def test_dark_lights_do_not_overlap_next_chord() -> None:
    # 两个和弦挨得近（0.25s）时，前一个的熄灭不能把后一个刚亮的弦灭掉
    schedule = build_chord_schedule(
        _doc((0, "G", EV_STRUM, DIR_DOWN), (240, "C", EV_STRUM, DIR_DOWN))
    )
    lights = _lights(schedule)
    darks = [a for a in schedule.actions if a.kind == "dark"]
    second_first = [a for a in lights if a.chord == "C"][0]
    first_darks = [a for a in darks if a.chord == "G"]
    assert all(a.time <= second_first.time for a in first_darks)


def test_unknown_chord_is_a_conflict() -> None:
    schedule = build_chord_schedule(_doc((0, "Xyz", EV_STRUM, DIR_DOWN)))
    assert schedule.note_count == 0
    assert schedule.conflicts and "chord.json" in schedule.conflicts[0]


def test_tempo_changes_chord_timing() -> None:
    doc = _doc((0, "G", EV_STRUM, DIR_DOWN))
    # 第 1 拍起改 60 BPM：第二个扫弦应该晚 1 秒而不是 0.5 秒
    doc.events.insert(1, ChordRow(480, 0x04, 0, (1_000_000).to_bytes(4, "little")))
    doc.events.append(
        ChordRow(
            960,
            EV_STRUM,
            0,
            doc.chord_id("G").to_bytes(2, "little") + bytes([DIR_DOWN]),
        )
    )
    schedule = build_chord_schedule(doc)
    units = [a for a in schedule.actions if a.unit]
    assert len(units) == 2
    assert abs((units[1].time - units[0].time) - (0.5 + 1.0)) < 1e-9


def test_demo_progression_schedule() -> None:
    schedule = build_chord_schedule(demo_document())
    assert schedule.note_count == 9, "8 个下扫 + 1 个上扫"
    assert not schedule.conflicts, schedule.conflicts
    assert schedule.unit_label == "和弦"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
