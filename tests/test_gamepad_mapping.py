"""gtab → 手柄按键/摇杆排期的测试（不需要真实手柄，也不会创建虚拟设备）。

运行：``.venv\\Scripts\\python.exe tests/test_gamepad_mapping.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gtab import EV_STRING_NOTE_OFF, EV_STRING_NOTE_ON  # noqa: E402
from ui.gamepad_backend import (  # noqa: E402
    AIM_GAP,
    LEAD_IN,
    PAGE_STICK_NEXT,
    PAGE_STICK_PREV,
    REPLUCK_GAP,
    STRING_BUTTONS,
    build_schedule,
    fret_direction,
    page_for,
)
from ui.gtab_doc import EventRow, GtabDocument  # noqa: E402

DEMO = SRC / "MusicList" / "gtabfile" / "demo.gtab"


def _note_doc(*notes: tuple[int, int, int, int]) -> GtabDocument:
    """notes: (start_tick, end_tick, string, fret)"""
    doc = GtabDocument()
    events: list[EventRow] = []
    for start, end, string, fret in notes:
        events.append(EventRow(tick=start, event_type=EV_STRING_NOTE_ON, data=bytes([string, fret])))
        events.append(EventRow(tick=end, event_type=EV_STRING_NOTE_OFF, data=bytes([string, fret])))
    doc.events = sorted(events, key=lambda ev: ev.tick)
    return doc


def test_string_buttons() -> None:
    # X/A/上/右/左/下 = 6/5/4/3/2/1 弦
    assert STRING_BUTTONS == {
        6: "X",
        5: "A",
        4: "DPAD_UP",
        3: "DPAD_RIGHT",
        2: "DPAD_LEFT",
        1: "DPAD_DOWN",
    }


def test_page_for() -> None:
    assert page_for(0) == 5, "空弦在第 5 页"
    assert page_for(1) == 1
    assert page_for(6) == 1
    assert page_for(7) == 2
    assert page_for(12) == 2
    assert page_for(13) == 3
    assert page_for(24) == 4


def test_fret_direction() -> None:
    # 正上 = 1 品
    assert fret_direction(1) == (0, 32767)
    # 顺时针 60° = 2 品
    x, y = fret_direction(2)
    assert abs(x - 28378) <= 1
    assert abs(y - 16384) <= 1
    # 顺时针 180° = 4 品
    assert fret_direction(4) == (0, -32767)
    # 300° = 6 品
    x6, y6 = fret_direction(6)
    assert x6 < 0 and y6 > 0
    # 第 2 页同一品复用一个方向
    assert fret_direction(7) == fret_direction(1)
    # 空弦任取正上
    assert fret_direction(0) == (0, 32767)


def test_single_page_schedule() -> None:
    doc = _note_doc((0, 240, 6, 1), (240, 480, 5, 3))
    schedule = build_schedule(doc)

    presses = [a for a in schedule.actions if a.kind == "press"]
    assert [a.button for a in presses] == ["X", "A"], "6 弦→X、5 弦→A"
    assert not any(a.kind == "page" for a in schedule.actions), "同页不应翻页"
    assert abs(presses[0].time - LEAD_IN) < 1e-9
    assert abs(presses[1].time - (LEAD_IN + 0.25)) < 1e-9

    stick = [a for a in schedule.actions if a.kind == "stick"]
    assert len(stick) == 2
    assert stick[0].stick == fret_direction(1)
    assert stick[1].stick == fret_direction(3)
    assert not schedule.conflicts


def test_page_switch_schedule() -> None:
    doc = _note_doc((0, 240, 6, 7))  # 第 2 页
    schedule = build_schedule(doc)

    pages = [a for a in schedule.actions if a.kind == "page"]
    assert len(pages) == 1
    assert pages[0].stick == PAGE_STICK_NEXT, "下一页 = 右摇杆向左"
    assert pages[0].time < LEAD_IN, "翻页要在音符之前完成"
    # 推完要回中，否则下一次推没有新的边沿
    assert any(a.kind == "rstick" and a.stick == (0, 0) for a in schedule.actions)
    assert not schedule.conflicts


def test_open_string_uses_fifth_page() -> None:
    doc = _note_doc((0, 240, 3, 0))  # 空弦
    schedule = build_schedule(doc)
    pages = [a for a in schedule.actions if a.kind == "page"]
    assert len(pages) == 4, "从第 1 页到第 5 页需要向左推 4 次"
    assert all(a.stick == PAGE_STICK_NEXT for a in pages)
    assert all(a.time < LEAD_IN for a in pages)
    assert any(a.kind == "stick" and a.stick == (0, 32767) for a in schedule.actions)
    assert not schedule.conflicts


def test_page_back_uses_right_stick_right() -> None:
    # 第 5 页回到第 1 页：先向左推 4 次到第 5 页（第一个音），再向右推 4 次回第 1 页
    doc = _note_doc((0, 240, 6, 0), (240, 480, 6, 1))
    schedule = build_schedule(doc)
    pages = [a.stick for a in schedule.actions if a.kind == "page"]
    assert pages[:4] == [PAGE_STICK_NEXT] * 4
    assert pages[4:] == [PAGE_STICK_PREV] * 4


def test_overlap_is_hint_not_conflict() -> None:
    # 同一时间两个不同品的音：按住已锁定，摇杆切过去再按另一根弦是正常演奏
    doc = _note_doc((0, 480, 6, 1), (0, 480, 5, 3))
    schedule = build_schedule(doc)
    assert not schedule.conflicts, "按住即锁音，重叠换摇杆不算冲突"
    assert schedule.hints, "应当给出重叠提示"
    assert any("保持按住" in h for h in schedule.hints)


def test_overlap_holds_long_note_instead_of_tap() -> None:
    # 6 弦空弦 0~480 与 5 弦 2 品（240~480）重叠：
    # 点按模式下长音也要保持到自己的 NoteOff，摇杆照常切到新音
    doc = _note_doc((0, 480, 6, 0), (240, 480, 5, 2))
    schedule = build_schedule(doc, tap_notes=True)

    releases = {a.button: a.time for a in schedule.actions if a.kind == "release" and a.string}
    assert abs(releases["X"] - (LEAD_IN + 0.5)) < 1e-9, "长音应保持到 NoteOff"
    assert abs(releases["A"] - (LEAD_IN + 0.5)) < 1e-9

    sticks = [a for a in schedule.actions if a.kind == "stick"]
    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    assert len(sticks) == 2
    assert abs(sticks[0].time - (LEAD_IN - AIM_GAP)) < 1e-9
    # 第二个音要等 4 次「右摇杆向右」翻页完（0.71s）；按键随后推迟一点，保证摇杆先到位
    assert abs((presses[1].time - sticks[1].time) - AIM_GAP) < 1e-9
    assert sticks[1].stick == fret_direction(2)


def test_same_string_pluck_releases_before_next_note() -> None:
    # 同一根弦上两个重叠的音（1 品 0~480、3 品 240~720）：
    # 第一个音在新音前 REPLUCK_GAP 松开，第二个音重拨并保持到自己的 NoteOff
    doc = _note_doc((0, 480, 6, 1), (240, 720, 6, 3))
    schedule = build_schedule(doc, tap_notes=True)

    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    releases = [a for a in schedule.actions if a.kind == "release" and a.string]
    assert [a.button for a in presses] == ["X", "X"], "同一根弦要重拨"
    assert abs(presses[0].time - LEAD_IN) < 1e-9
    assert abs(presses[1].time - (LEAD_IN + 0.25)) < 1e-9
    assert abs(releases[0].time - (LEAD_IN + 0.25 - REPLUCK_GAP)) < 1e-9
    assert abs(releases[1].time - (LEAD_IN + 0.75)) < 1e-9


def test_custom_repluck_gap() -> None:
    # 重拨间隔可以由界面覆盖：0.08s 松开，给游戏更多时间采样
    doc = _note_doc((0, 480, 6, 1), (240, 720, 6, 3))
    schedule = build_schedule(doc, repluck_gap=0.08)
    releases = [a for a in schedule.actions if a.kind == "release" and a.string]
    assert abs(releases[0].time - (LEAD_IN + 0.25 - 0.08)) < 1e-9


def test_same_string_overlap_is_hint() -> None:
    # 同弦重叠：必须松开重拨，这是预期处理，不是错误
    doc = _note_doc((0, 480, 6, 1), (240, 720, 6, 3))
    schedule = build_schedule(doc)
    assert not schedule.conflicts
    assert any("重拨" in h for h in schedule.hints)


def test_adjacent_same_string_notes_still_tap() -> None:
    # 首尾相接（不是重叠）：仍然是两次独立的 80ms 点按
    doc = _note_doc((0, 240, 6, 1), (240, 480, 6, 3))
    schedule = build_schedule(doc, tap_notes=True)
    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    releases = [a for a in schedule.actions if a.kind == "release" and a.string]
    assert len(presses) == 2 and len(releases) == 2
    assert abs(releases[0].time - (LEAD_IN + 0.08)) < 1e-9


def test_demo_open_string_held_across_overlap() -> None:
    # 示例谱：6 弦空弦 0~480 与 5 弦 2 品 240~480 重叠，空弦要保持到 NoteOff
    doc = GtabDocument.from_path(DEMO)
    schedule = build_schedule(doc, tap_notes=True)
    releases = {a.button: a.time for a in schedule.actions if a.kind == "release" and a.string}
    assert abs(releases["X"] - (LEAD_IN + 0.5)) < 1e-9
    assert abs(releases["A"] - (LEAD_IN + 0.5)) < 1e-9


def test_tap_and_hold_modes() -> None:
    doc = _note_doc((0, 480, 6, 1))  # 0 拍 ~ 1 拍，120BPM → 0.5s

    tap = build_schedule(doc, tap_notes=True)
    release_tap = [a for a in tap.actions if a.kind == "release" and a.button == "X"]
    assert abs(release_tap[0].time - (LEAD_IN + 0.08)) < 1e-9

    hold = build_schedule(doc, tap_notes=False)
    release_hold = [a for a in hold.actions if a.kind == "release" and a.button == "X"]
    assert abs(release_hold[0].time - (LEAD_IN + 0.5)) < 1e-9


def test_demo_reports_page_conflict() -> None:
    # 示例谱：空弦（第 5 页）紧跟着 2 品（第 1 页），4 次翻页来不及
    doc = GtabDocument.from_path(DEMO)
    schedule = build_schedule(doc)
    assert schedule.note_count == 2
    assert any("翻页" in c for c in schedule.conflicts), "应提示翻页时间不足"
    assert any("保持按住" in h for h in schedule.hints), "长音重叠应是提示"


def test_page_switch_never_clobbers_previous_note() -> None:
    # 第 1 页的音（1 品）之后紧接第 5 页的空弦：翻页不能早于前一个音按下
    doc = _note_doc((0, 240, 6, 1), (240, 480, 5, 0))
    schedule = build_schedule(doc)

    x_press = [a for a in schedule.actions if a.kind == "press" and a.button == "X"][0]
    pages = [a for a in schedule.actions if a.kind == "page"]
    assert pages, "空弦在第 5 页，需要翻页"
    assert all(a.time >= x_press.time for a in pages), "翻页不能早于前一个音按下"
    assert any("翻页" in c for c in schedule.conflicts)


def test_aim_gap_moves_stick_before_press() -> None:
    # 摇杆要比按弦早 aim_gap 秒到位；按键本身仍在音符时刻，不拖节奏
    doc = _note_doc((0, 240, 6, 1), (240, 480, 5, 3))
    schedule = build_schedule(doc, aim_gap=0.08)

    sticks = [a for a in schedule.actions if a.kind == "stick"]
    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    assert abs(sticks[0].time - (LEAD_IN - 0.08)) < 1e-9
    assert abs(sticks[1].time - (LEAD_IN + 0.25 - 0.08)) < 1e-9
    assert abs(presses[0].time - LEAD_IN) < 1e-9
    assert abs(presses[1].time - (LEAD_IN + 0.25)) < 1e-9
    assert not any("摇杆到位到按键" in h for h in schedule.hints)


def test_aim_gap_zero_keeps_stick_and_press_together() -> None:
    doc = _note_doc((0, 240, 6, 1), (240, 480, 5, 3))
    schedule = build_schedule(doc, aim_gap=0.0)
    sticks = [a for a in schedule.actions if a.kind == "stick"]
    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    assert abs(sticks[0].time - presses[0].time) < 1e-9
    assert abs(sticks[1].time - presses[1].time) < 1e-9


def test_same_direction_is_not_repeated() -> None:
    # 两个音都是 1 品：摇杆不用动，也不该为第二条音多等 aim_gap
    doc = _note_doc((0, 240, 6, 1), (240, 480, 5, 1))
    schedule = build_schedule(doc)
    sticks = [a for a in schedule.actions if a.kind == "stick"]
    assert len(sticks) == 1, "方向没变就不要重复发摇杆动作"
    assert not any("摇杆到位到按键" in h for h in schedule.hints)


def test_aim_gap_delays_press_when_previous_note_too_close() -> None:
    # 第二个音只比第一个晚 25ms，摇杆来不及提前一个 aim_gap：把按键推迟，
    # 保证摇杆到位间隔（最多推迟到下一个音开始前）。
    doc = _note_doc((0, 240, 6, 1), (24, 240, 5, 3))
    schedule = build_schedule(doc, aim_gap=AIM_GAP)

    sticks = [a for a in schedule.actions if a.kind == "stick"]
    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    assert abs(sticks[1].time - (presses[0].time + 0.001)) < 1e-9, "摇杆必须在前一个音按下之后"
    assert abs((presses[1].time - sticks[1].time) - AIM_GAP) < 1e-9, "推迟按键来保证间隔"
    assert presses[1].time > LEAD_IN + 0.025, "按弦确实推迟了"
    assert not any("摇杆到位到按键" in h for h in schedule.hints), "间隔补足后不需要警告"


def test_aim_gap_delay_is_capped_by_next_note() -> None:
    # 三个音挤在 ~10ms 内：第二、三个音的按键推迟不能抢到后一个音前面
    doc = _note_doc((0, 240, 6, 1), (5, 240, 5, 3), (10, 240, 4, 5))
    schedule = build_schedule(doc, aim_gap=0.05)

    presses = [a for a in schedule.actions if a.kind == "press" and a.string]
    assert abs(presses[1].time - (LEAD_IN + 10 / 480 * 0.5)) < 1e-9, "推迟上限 = 下一个音开始"
    assert presses[0].time <= presses[1].time <= presses[2].time, "按键顺序不能乱"
    assert any("摇杆到位到按键" in h for h in schedule.hints), "间隔不够要提示"


def test_page_switch_finishes_before_stick_move() -> None:
    # 空弦在第 5 页：必须等 4 次翻页都推完，左摇杆才到位，再隔 AIM_GAP 按弦
    doc = _note_doc((0, 240, 3, 0))
    schedule = build_schedule(doc)
    page_pushes = [a.time for a in schedule.actions if a.kind == "page"]
    sticks = [a for a in schedule.actions if a.kind == "stick"]
    assert sticks, "空弦也要把摇杆放到正上方"
    assert sticks[0].time >= max(page_pushes), "摇杆不能早于翻页完成"
    assert abs(sticks[0].time - (LEAD_IN - AIM_GAP)) < 1e-9


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
