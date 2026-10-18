"""GCT 编辑页 + chord.json 指法表的测试（不弹窗口，只验证控件树 / 模型行为）。

覆盖：
- chord.json 的 ``CHORDS_MAPPINGS``（1–6 弦、-1 → ``x``、格式校验、重载）；
- GCT 页构建 / 示例渲染 / 添加、切换类型、换和弦、改方向、删除；
- Tempo 行、事件 CRC、保存（自动补 End）后回读；
- 节拍轴：事件按拍排开、扫弦/拨弦/指位三条泳道、Tempo 竖线、改拍后立即重画。

运行：``.venv\\Scripts\\python.exe tests/test_gct_editor.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from gct import DIR_DOWN, DIR_UP, EV_CHORD_PLUCK, EV_CHORD_SET, EV_CHORD_STRUM  # noqa: E402
from ui import audio_preview  # noqa: E402
from ui.chords import (  # noqa: E402

    MUTED,
    ChordError,
    ChordTable,
    fingering_text,
    load_chord_mappings,
    load_chord_table,
    parse_chord_mappings,
    parse_chord_table,
    reload_chord_table,
)
from ui.components import LANE_HEIGHT, PX_PER_BEAT, RULER_HEIGHT  # noqa: E402
from ui.gct_doc import ChordRow, GctDocument, demo_document  # noqa: E402
from ui.pages.gct_page import GctPage  # noqa: E402


def _page() -> GctPage:
    # page=None：构建阶段不需要页面上下文
    return GctPage(page=None)


# ---------------------------------------------------------------- chord.json 指法


def test_mappings_cover_all_24_names() -> None:
    table = load_chord_table()
    mappings = load_chord_mappings()
    assert len(table.names) == 24
    assert set(mappings) == set(table.names)
    assert table.missing_mappings() == []
    assert table.unknown_mappings() == []
    for name, frets in mappings.items():
        assert len(frets) == 6, f"{name} 不是 6 根弦"
        for fret in frets:
            assert fret == MUTED or 0 <= fret <= 24, f"{name} 有非法品位 {fret}"


def test_fingering_text_uses_x_for_muted_and_strings_one_to_six() -> None:
    assert fingering_text((2, 3, 2, 0, -1, -1)) == "2 3 2 0 x x", "D 的 5/6 弦不发声"
    assert fingering_text((0, 0, 2, 2, 1, 0)) == "0 0 2 2 1 0"
    assert load_chord_table().fingering_text("Dm") == "1 3 2 0 x x"
    # 顺序是 1 弦在左、6 弦在右：G 的 6 弦是 3 品
    assert load_chord_table().fingering("G") == (3, 2, 0, 0, 0, 3)


def test_bad_mappings_are_rejected() -> None:
    for bad, hint in [
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": {"G": [1, 2, 3]}}, "6 项"),
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": {"G": [1, 2, 3, 4, 5, 26]}}, "品位"),
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": {"G": [1, 2, 3, 4, 5, "x"]}}, "整数"),
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": {"G": [1, 2, 3, 4, 5, True]}}, "整数"),
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": {"G": "023200"}}, "数组"),
        ({"CHORD_NAMES": ["G"], "CHORDS_MAPPINGS": []}, "CHORDS_MAPPINGS"),
    ]:
        try:
            parse_chord_mappings(bad, "bad.json")
        except ChordError as exc:
            assert hint in str(exc), (hint, str(exc))
        else:
            raise AssertionError(f"应该拒绝：{bad}")


def test_missing_and_extra_mappings_are_tolerated() -> None:
    table = parse_chord_table(
        {
            "CHORD_NAMES": ["G", "Am"],
            "CHORDS_MAPPINGS": {
                "G": [3, 2, 0, 0, 0, 3],
                "Xyz": [0, 0, 0, 0, 0, 0],
            },
        },
        "partial.json",
    )
    assert table.missing_mappings() == ["Am"], "缺指法不阻止解析（由界面提示）"
    assert table.unknown_mappings() == ["Xyz"], "多余/拼错的名字也要能被界面看见"


def test_reload_picks_up_edited_json() -> None:
    # 直接写在 tests/ 下：受限环境里 %TEMP% 和「新建的临时子目录」可能不可写
    path = Path(__file__).resolve().parent / "_chord_reload_tmp.json"
    base = {
        "CHORD_NAMES": ["G", "D"],
        "CHORDS_MAPPINGS": {"G": [3, 2, 0, 0, 0, 3], "D": [2, 3, 2, 0, -1, -1]},
    }
    try:
        path.write_text(json.dumps(base), encoding="utf-8")
        assert load_chord_table(path).fingering("D") == (2, 3, 2, 0, -1, -1)

        base["CHORDS_MAPPINGS"]["D"] = [2, 3, 2, 0, 0, 2]
        path.write_text(json.dumps(base), encoding="utf-8")
        assert load_chord_table(path).fingering("D") == (2, 3, 2, 0, -1, -1), "有缓存，先不变"
        reload_chord_table()
        assert load_chord_table(path).fingering("D") == (2, 3, 2, 0, 0, 2), "重载后读到新值"
    finally:
        path.unlink(missing_ok=True)
        reload_chord_table()


# ---------------------------------------------------------------- 页面构建


def test_page_builds_with_chord_table() -> None:
    view = _page()
    root = view.build()
    assert isinstance(root, ft.Control)
    controls = view._chord_table_column.controls
    # 4 个「第 N 页」标题 + 每页 2 列 × 3 行 = 16 个控件，24 个和弦一屏看全
    assert len(controls) == 4 + 12
    assert view._events_list is not None and view._other_list is not None
    assert view._file_label.value == "未命名"

    names: list[str] = []
    fingers: dict[str, str] = {}
    for block in range(4):
        for line in range(3):
            row = controls[block * 4 + 1 + line]
            for cell in row.controls:
                name = cell.controls[1].value
                names.append(name)
                fingers[name] = cell.controls[2].value
    assert len(names) == 24
    assert names[0] == "G" and names[-1] == "Dm", "顺序 = 手柄位置"
    assert fingers["D"] == "2 3 2 0 x x", "-1（不发声）显示成 x"


def test_empty_document_shows_hints_and_end_row() -> None:
    view = _page()
    assert len(view._events_list.controls) == 1
    assert "还没有和弦事件" in (view._events_list.controls[0].content.value or "")
    assert len(view._other_list.controls) == 1, "空文档只有一行 End"
    assert view._stats_label.value.startswith("0 个和弦")


def test_demo_renders_chord_rows_and_stats() -> None:
    view = _page()
    view.doc = demo_document()
    view._render_all()
    assert len(view._events_list.controls) == 10, "8 个下扫 + 1 个 Set + 1 个上扫"
    assert len(view._other_list.controls) == 1, "示例没有 Tempo，只有 End 行"
    assert "10 个和弦" in view._stats_label.value
    assert "9.00 拍" in view._stats_label.value
    assert view._file_label.value == "未命名"


# ---------------------------------------------------------------- 事件编辑


def test_add_chord_appends_strum_in_next_beat() -> None:
    view = _page()
    view._on_add_chord()
    assert len(view.doc.events) == 1
    ev = view.doc.events[0]
    assert ev.event_type == EV_CHORD_STRUM
    assert ev.tick == 0
    assert ev.direction == DIR_DOWN
    assert view.doc.chord_name(ev.chord_id) == "G", "默认用和弦表第一个"
    assert len(view._events_list.controls) == 1

    view._on_add_chord()
    assert view.doc.events[1].tick == view.doc.time_base_value, "第二个接在 1 拍后"


def test_kind_change_rewrites_data() -> None:
    view = _page()
    view._on_add_chord()
    ev = view.doc.events[0]

    view._on_kind_change(ev, "拨弦")
    assert ev.event_type == EV_CHORD_PLUCK
    assert len(ev.data) == 3 and ev.string == 6, "拨弦默认 6 弦"

    view._on_kind_change(ev, "只换指位")
    assert ev.event_type == EV_CHORD_SET
    assert len(ev.data) == 2, "Set 只有 chord_id"

    view._on_kind_change(ev, "扫弦")
    assert ev.event_type == EV_CHORD_STRUM
    assert len(ev.data) == 3 and ev.direction == DIR_DOWN


def test_chord_change_keeps_direction_and_registers_chord() -> None:
    view = _page()
    view._on_add_chord()
    ev = view.doc.events[0]
    view._on_direction_change(ev, "上扫")
    assert ev.direction == DIR_UP

    view._on_chord_change(ev, "Dm")
    assert view.doc.chord_name(ev.chord_id) == "Dm"
    assert ev.direction == DIR_UP, "换和弦不应该丢掉方向"
    assert "Dm" in view.doc.chord_names()


def test_pluck_string_and_beat_edit() -> None:
    view = _page()
    view._on_add_chord()
    ev = view.doc.events[0]
    view._on_kind_change(ev, "拨弦")
    view._on_pluck_string_change(ev, "3")
    assert ev.string == 3

    view._on_beat_change(ev, "2.5")
    assert ev.tick == view.doc.tick_at(2.5) == 1200
    assert "2.50" in view._stats_label.value, "时长按拍更新"


def test_delete_chord_event() -> None:
    view = _page()
    view._on_add_chord()
    view._on_add_chord()
    victim = view.doc.events[0]
    view._on_delete_event(victim)
    assert len(view.doc.events) == 1
    assert victim not in view.doc.events
    assert len(view._events_list.controls) == 1


# ---------------------------------------------------------------- Tempo / CRC


def test_tempo_row_roundtrip() -> None:
    view = _page()
    view._on_add_tempo()
    tempo = view.doc.events[0]
    view._on_tempo_bpm_change(tempo, "60")
    assert tempo.tempo_us == 1_000_000
    assert len(view._other_list.controls) == 2, "Tempo 行 + End 行"

    view._on_tempo_beat_change(tempo, "4")
    assert tempo.tick == 4 * view.doc.time_base_value

    blob = view.doc.to_bytes()
    again = GctDocument.from_bytes(blob)
    assert again.events[0].tempo_us == 1_000_000


def test_crc_toggle_sets_flag_and_roundtrips() -> None:
    view = _page()
    view._sw_crc.value = True
    view._on_crc_change()
    assert view.doc.flags & 0x01

    view._on_add_chord()
    blob = view.doc.to_bytes()
    assert GctDocument.from_bytes(blob).flags & 0x01

    view._sw_crc.value = False
    view._on_crc_change()
    assert not (view.doc.flags & 0x01)


def test_save_appends_end_and_sorts_events() -> None:
    view = _page()
    view._on_add_chord()
    view._on_add_chord()
    first, second = view.doc.events
    first.tick, second.tick = 2 * view.doc.time_base_value, view.doc.time_base_value

    blob = view.doc.to_bytes()
    again = GctDocument.from_bytes(blob)
    assert again.events[-1].event_type == 0x05, "自动补 End"
    assert [ev.tick for ev in again.events[:-1]] == [480, 960], "保存时按拍排序"
    # 重复保存是幂等的（不会越补越多 End）
    assert again.to_bytes() == blob


# ---------------------------------------------------------------- 校验提示


def test_unknown_chord_in_score_warns() -> None:
    view = _page()
    doc = GctDocument()
    chord_id = doc.add_chord("Xyz")
    doc.events = [ChordRow(0, EV_CHORD_STRUM, 0, chord_id.to_bytes(2, "little") + bytes([DIR_DOWN]))]
    view.doc = doc
    view._render_all()
    assert view._warning.visible
    assert "Xyz" in view._warning.value
    assert view._warning.color == ft.Colors.RED_400, "谱面引用了表里没有的和弦要标红"


def test_missing_mapping_is_shown_as_undefined() -> None:
    view = _page()
    view.chord_table = ChordTable(names=("G", "Am"), mappings={"G": (3, 2, 0, 0, 0, 3)})
    view._render_all()
    assert view._chord_table_warning.visible
    assert "没填指法" in view._chord_table_warning.value

    view._on_add_chord()
    finger = view._events_list.controls[0].controls[5]
    assert finger.value == "3 2 0 0 0 3"

    view._on_chord_change(view.doc.events[0], "Am")
    finger = view._events_list.controls[0].controls[5]
    assert finger.value == "未定义"


def test_chord_json_broken_shows_error_instead_of_crashing() -> None:
    view = _page()
    view.chord_error = "boom"
    view.chord_table = ChordTable((), {})
    view._render_all()
    assert view._warning.visible and "boom" in view._warning.value
    assert view._chord_table_column.controls, "表坏了也要能画出空位/标题"


# ---------------------------------------------------------------- 示例载入


def test_load_demo_reads_file_when_present_else_memory() -> None:
    # 没实体文件 → 用内存里的 demo_document()（path=None，之后「保存」会转另存为）
    view = _page()
    view.DEMO_PATH = Path(__file__).resolve().parent / "_no_such_demo.gct"
    asyncio.run(view._on_load_demo())
    assert len(view.doc.chord_events()) == 10
    assert view.path is None
    assert view.dirty is False

    # 有实体文件 → 读它，并把 path 指过去（保存时直接写回这个文件）
    target = Path(__file__).resolve().parent / "_demo_branch.gct"
    try:
        demo_document().save(target)
        view2 = _page()
        view2.DEMO_PATH = target
        asyncio.run(view2._on_load_demo())
        assert view2.path == target, "有实体 demo.gct 时要记住路径"
        assert len(view2.doc.chord_events()) == 10
        assert view2.dirty is False
    finally:
        target.unlink(missing_ok=True)


# ---------------------------------------------------------------- 节拍轴


def test_timeline_puts_demo_events_on_beat_axis() -> None:
    view = _page()
    asyncio.run(view._on_load_demo())

    blocks = view._event_blocks()
    assert len(blocks) == len(view.doc.chord_events()) == 10, "每个和弦事件一个块"
    assert [block[0] for block in blocks][:3] == [0.0, 1.0, 2.0], "按拍排开"
    assert blocks[9][0] == 8.0, "示例最后一拍是上扫"
    assert blocks[0][1] == 0, "扫弦在第 1 条泳道"
    assert blocks[8][1] == 2, "只换指位在第 3 条泳道"
    assert view._timeline_width >= 8 * PX_PER_BEAT, "至少铺到 8 拍"
    # 末尾的 End 事件也算进 duration_beats（第 9 拍），再留 1 拍余量
    assert view._timeline_width == 10 * PX_PER_BEAT


def test_timeline_distinguishes_strum_directions() -> None:
    view = _page()
    asyncio.run(view._on_load_demo())
    blocks = view._event_blocks()
    down, up = blocks[0], blocks[9]
    assert (down[2], up[2]) == ("G", "C"), "块上写和弦名"
    assert (down[3], up[3]) == (ft.Colors.INDIGO_400, ft.Colors.TEAL_400), (
        "颜色要传 ft.Colors 原值：它是个枚举，str() 会变成非法颜色串，块会画成透明"
    )
    assert "下扫" in down[5] and "上扫" in up[5], "tooltip 里写清方向"


def test_timeline_shows_tempo_marks_and_pluck_lane() -> None:
    view = _page()
    view._on_add_tempo()
    tempo = view.doc.events[0]
    view._on_tempo_beat_change(tempo, "2")
    view._on_tempo_bpm_change(tempo, "90")
    marks = view._tempo_marks()
    assert len(marks) == 1
    assert marks[0][0] == 2.0, "竖线在第 2 拍"
    assert abs(marks[0][1] - 90) < 0.01

    view._on_add_chord()
    pluck = [ev for ev in view.doc.events if ev.event_type == EV_CHORD_STRUM][-1]
    view._on_kind_change(pluck, "拨弦")
    view._on_pluck_string_change(pluck, "3")
    block = next(block for block in view._event_blocks() if block[1] == 1)
    assert block[2].endswith("·3"), "拨弦块上带弦号"


def test_timeline_empty_document_still_draws_four_beats() -> None:
    view = _page()
    assert view._event_blocks() == []
    assert view._tempo_marks() == []
    assert view._timeline_width == 4 * PX_PER_BEAT, "空白谱也留 4 拍"

    stack = view._timeline_host.content.controls[1].controls[0]
    assert stack.height == RULER_HEIGHT + 3 * LANE_HEIGHT, "标尺 + 三条泳道"
    assert any(
        isinstance(control, ft.Text) and "还没有和弦事件" in control.value
        for control in stack.controls
    ), "空白时给一句提示"


def test_timeline_redraws_when_beat_and_type_change() -> None:
    view = _page()
    view._on_add_chord()
    ev = view.doc.events[0]

    view._on_beat_change(ev, "3.5")
    assert view._event_blocks()[0][0] == 3.5, "改拍后轴上的块立刻跟着走"

    view._on_direction_change(ev, "上扫")
    assert "上扫" in view._event_blocks()[0][5]

    view._on_chord_change(ev, "Dm")
    assert view._event_blocks()[0][2] == "Dm", "换和弦后块上的字也换"


# ---------------------------------------------------------------- 试听


def test_preview_synthesizes_offline_without_playing() -> None:
    view = _page()
    view.doc = demo_document()
    view._render_all()

    view._on_preview()

    assert view._playing is False, "离线只合成，不该留在播放状态"
    assert view._preview_label.value.startswith("已合成")
    assert "离线，不播放" in view._preview_label.value


def test_preview_needs_chord_events() -> None:
    view = _page()
    view._on_preview()
    assert view._playing is False
    assert view._preview_label.value == "", "空谱不合成，也不该显示进度"


def test_preview_skips_chords_missing_fingering() -> None:
    view = _page()
    view.chord_table = ChordTable(names=("G",), mappings={"G": (3, 2, 0, 0, 0, 3)})
    view.doc = demo_document()
    view._render_all()

    view._on_preview()

    assert view._playing is False
    assert view._preview_label.value.startswith("已合成"), "还有 G 能出声，不能整段没声音"


def test_playhead_follows_beat_position() -> None:
    view = _page()
    view.doc = demo_document()
    view._render_all()

    view._set_playhead_seconds(0.5, view.doc)  # 120 BPM：1 拍 = 0.5s
    assert view._playhead.visible is True
    assert abs(view._playhead.left - PX_PER_BEAT) < 1e-6
    assert view._playhead.height == 3 * LANE_HEIGHT + 2

    view._set_playhead_seconds(9999.0, view.doc)
    assert view._playhead.left == max(0.0, view._timeline_width - 3), "别跑出轴外"


def test_stop_preview_hides_playhead_and_clears_label() -> None:
    view = _page()
    view._playing = True
    view._preview_label.value = "试听中 1.0s / 5.3s"
    view._playhead.visible = True

    view.stop_preview()

    assert view._playing is False
    assert view._playhead.visible is False
    assert view._preview_label.value == ""
    assert view._preview_label.color == ft.Colors.ON_SURFACE_VARIANT


class _FakePage:
    """够用的假 page：``run_thread`` 同步执行，``run_task`` 收集成 task 交给测试 await。"""

    def __init__(self) -> None:
        self.tasks: list = []

    def run_thread(self, fn, *args) -> None:
        fn(*args)

    def run_task(self, fn, *args) -> None:
        self.tasks.append(asyncio.ensure_future(fn(*args)))

    def update(self) -> None:
        pass

    def show_dialog(self, _dialog) -> None:
        pass


def test_preview_thread_and_ticker_cycle() -> None:
    """真机才走到的「线程 + ticker」路径：用假 page 同步跑一遍（不真的出声）。"""
    played: list[bytes] = []
    original_play, original_stop = audio_preview.play_wav, audio_preview.stop_playback
    audio_preview.play_wav = played.append
    audio_preview.stop_playback = lambda: None
    try:
        page = _FakePage()
        view = _page()
        view.page = page
        view.doc = GctDocument()
        chord_id = view.doc.add_chord("G")
        view.doc.events = [
            ChordRow(0, EV_CHORD_STRUM, 0, chord_id.to_bytes(2, "little") + bytes([DIR_DOWN]))
        ]
        view._render_all()

        async def cycle() -> None:
            view._on_preview()
            assert view._playing is True
            assert played, "合成完要把 WAV 交给播放"
            await asyncio.sleep(0.15)  # 让 ticker 跑起来
            assert view._preview_label.value.startswith("试听中")
            assert view._playhead.visible is True
            for task in page.tasks:
                await task

            # 再点一次开始，然后「播放中再点试听」= 停止
            view._on_preview()
            assert view._playing is True
            view._on_preview()
            assert view._playing is False
            for task in page.tasks:
                await task

        asyncio.run(cycle())

        assert view._playing is False, "播完要复位"
        assert view._playhead.visible is False
        assert view._preview_label.value == ""
        assert float(view._playhead.left or 0.0) > 0.0, "游标要真的动过"
    finally:
        audio_preview.play_wav = original_play
        audio_preview.stop_playback = original_stop


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
