"""MIDI 映射：SMF 解析、tempo 换算、键位表、Capo、按键排期、页面离线构建。

和另外几个 ``test_*`` 一样不弹窗口、不真发按键：
- SMF 往返用 ``midi.build`` 现场生成字节（也测 running status / 缺音尾等手工字节）；
- 键位表用 ``tests/_midi_keys.bad.json`` 这类固定文件 + ``finally unlink``，
  **不要用 tempfile**（沙箱里 %TEMP% 不可写，见检查点坑表）；
- ``KeyboardBackend`` 只测扫描码换算，不调用 ``press``（会真的打进当前窗口）。

运行：``.venv\\Scripts\\python.exe tests/test_midi_doc.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import midi as midi_format  # noqa: E402
from ui.keyboard_backend import (  # noqa: E402
    KEY_REPRESS_GAP,
    LEAD_IN,
    KeyTableError,
    _scan_code,
    build_key_schedule,
    key_for_note,
    load_key_table,
    parse_key_table,
)
from ui.midi_doc import (  # noqa: E402
    DEFAULT_TEMPO_US,
    MidiDocument,
    NoteEvent,
    TempoMark,
    demo_document,
    note_name,
)
from ui.pages.midi_page import MidiPage  # noqa: E402
from ui import settings as settings_mod  # noqa: E402

# 测试固定跑「默认设置」：用户本机开着八度投影时，这里的「跳过」断言也必须成立
settings_mod.set_settings(settings_mod.AppSettings(), persist=False)

HERE = Path(__file__).resolve().parent
TICKS = 480  # 1 拍


class FakePage:
    """只实现 MIDI 页用到的那几个 Page 方法。"""

    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple]] = []
        self.dialogs: list[object] = []
        self.updates = 0

    def run_task(self, handler, *args) -> None:
        self.tasks.append((handler, args))

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def update(self) -> None:
        self.updates += 1


def _smf(track_body: bytes, division: int = TICKS, fmt: int = 0, tracks: int = 1) -> bytes:
    """手工拼一个 SMF（用来喂 running status / 缺音尾 / 坏格式）。"""
    return (
        b"MThd"
        + (6).to_bytes(4, "big")
        + fmt.to_bytes(2, "big")
        + tracks.to_bytes(2, "big")
        + division.to_bytes(2, "big")
        + b"MTrk"
        + len(track_body).to_bytes(4, "big")
        + track_body
    )


def _doc(notes: list[tuple[int, int, int]], tempos=None, division: int = TICKS) -> MidiDocument:
    return MidiDocument(
        format=0,
        tracks=1,
        division=division,
        notes=[NoteEvent(start, end, note) for start, end, note in notes],
        tempos=tempos or [TempoMark(0, DEFAULT_TEMPO_US)],
        end_tick=max((end for _start, end, _note in notes), default=0),
    )


# ---------------------------------------------------------------- SMF 解析


def test_note_name_is_scientific_pitch() -> None:
    assert note_name(60) == "C4"
    assert note_name(69) == "A4"
    assert note_name(0) == "C-1"
    assert note_name(127) == "G9"


def test_build_roundtrip_keeps_notes_and_division() -> None:
    blob = midi_format.build(
        [(0, 480, 60, 100), (480, 960, 64, 80)],
        division=TICKS,
        name="test",
    )
    doc = MidiDocument.from_bytes(blob)
    assert [(n.start, n.end, n.note) for n in doc.notes] == [(0, 480, 60), (480, 960, 64)]
    assert doc.division == TICKS
    assert doc.end_tick == 960
    assert doc.name == "test"
    assert doc.unclosed == 0


def test_running_status_and_velocity_zero_as_note_off() -> None:
    body = bytes(
        [
            0x00, 0x90, 0x3C, 0x64,          # tick 0: note on C4
            0x83, 0x60, 0x3C, 0x00,          # tick 480: running status C4 velocity 0 = off
            0x00, 0xFF, 0x2F, 0x00,          # End of Track
        ]
    )
    doc = MidiDocument.from_bytes(_smf(body))
    assert len(doc.notes) == 1
    note = doc.notes[0]
    assert (note.start, note.end, note.note, note.velocity) == (0, TICKS, 60, 100)
    assert doc.unclosed == 0


def test_unclosed_note_is_closed_at_track_end() -> None:
    body = bytes([0x00, 0x90, 0x3C, 0x64, 0x00, 0xFF, 0x2F, 0x00])
    doc = MidiDocument.from_bytes(_smf(body))
    assert len(doc.notes) == 1
    assert doc.unclosed == 1
    assert doc.notes[0].end > doc.notes[0].start


def test_rejects_garbage_and_smpte_and_empty() -> None:
    for blob, expected in (
        (b"not a midi file at all", "不是 MIDI"),
        (_smf(b"\x00\xff\x2f\x00", division=0xE728), "SMPTE"),
        (_smf(b"\x00\xff\x2f\x00", division=0), "时间基准"),
        (_smf(b"\x00\xff\x2f\x00"), "没有任何音符"),
    ):
        try:
            MidiDocument.from_bytes(blob)
        except midi_format.MidiError as exc:
            assert expected in str(exc), (expected, str(exc))
        else:
            raise AssertionError(f"应该拒绝：{expected}")


def test_tempo_before_first_mark_uses_120_bpm() -> None:
    # tempo 事件从第 1 拍才开始：0–1 拍仍按默认 120 BPM
    doc = _doc([(0, 960, 60)], tempos=[TempoMark(480, 250_000)])
    assert doc.bpm == 120.0, "第一个 tempo 之前的 BPM 是默认值"
    assert doc.tempo_at(0) == DEFAULT_TEMPO_US
    assert doc.tempo_at(480) == 250_000
    assert abs(doc.seconds_at(480) - 0.5) < 1e-9, "第 1 拍 0.5s"
    assert abs(doc.seconds_at(960) - 0.75) < 1e-9, "第 2 拍再 0.25s（240 BPM）"


def test_seconds_at_handles_tempo_changes() -> None:
    doc = demo_document()
    assert doc.bpm == 120.0
    assert abs(doc.seconds_at(8 * TICKS) - 4.0) < 1e-9, "8 拍 × 0.5s"
    # 第 8 拍起 180 BPM（333333us），再 1 拍
    expected = 4.0 + 333_333 / 1_000_000
    assert abs(doc.seconds_at(9 * TICKS) - expected) < 1e-6
    assert abs(doc.duration_seconds - expected) < 1e-6


def test_tick_at_seconds_is_inverse_and_clamped() -> None:
    doc = demo_document()
    for tick in (0, 480, 3840, 4320):
        assert abs(doc.tick_at_seconds(doc.seconds_at(tick)) - tick) <= 1
    assert doc.tick_at_seconds(-5) == 0
    assert doc.tick_at_seconds(0) == 0


def test_beats_and_duration() -> None:
    doc = demo_document()
    assert doc.beats_at(TICKS) == 1.0
    assert doc.tick_at(1.5) == 720
    assert doc.duration_beats == 9.0
    assert doc.note_range == (48, 72)
    assert doc.counts()["C4"] == 1


def test_demo_document_shape() -> None:
    doc = demo_document()
    assert doc.division == TICKS
    assert len(doc.notes) == 14
    assert len(doc.tempos) == 2
    assert doc.tempos[1].tick == 8 * TICKS
    assert doc.end_tick == 9 * TICKS


def test_to_bytes_roundtrip_with_save_and_unlink() -> None:
    doc = demo_document()
    path = HERE / "_midi_roundtrip.mid"
    try:
        doc.save(path)
        assert path.exists()
        again = MidiDocument.from_path(path)
    finally:
        path.unlink(missing_ok=True)
    assert [(n.start, n.end, n.note, n.velocity) for n in again.notes] == [
        (n.start, n.end, n.note, n.velocity) for n in doc.notes
    ]
    assert [(t.tick, t.tempo_us) for t in again.tempos] == [
        (t.tick, t.tempo_us) for t in doc.tempos
    ]


# ---------------------------------------------------------------- 键位表


def test_default_key_table_is_three_octaves_from_e3() -> None:
    table = load_key_table()
    assert table.base_note == 52, "E3"
    assert table.span == 36
    assert table.keys[0] == "a"
    assert table.keys[11] == "enter", "底行 a…enter = E3–D#4"
    assert table.keys[12] == "q" and table.keys[23] == "]"
    assert table.keys[24] == "1" and table.keys[35] == "="
    assert table.last_note == 87, "D#6"
    assert table.key_for(52) == "a"
    assert table.key_for(63) == "enter"
    assert table.key_for(64) == "q"
    assert table.key_for(76) == "1"
    assert table.key_for(87) == "="
    assert table.key_for(51) is None
    assert table.key_for(88) is None
    assert table.key_for(51, transpose=1) == "a"
    assert table.key_for(52, transpose=-1) is None
    assert table.note_for("q") == 64
    assert table.note_for("enter") == 63
    assert table.note_for("?") is None
    assert key_for_note(62, table=table) == "'"


def test_parse_key_table_rejects_bad_shapes() -> None:
    good = {"BASE_NOTE": 60, "KEYS": ["z", "s"]}
    assert parse_key_table(good).keys == ("z", "s")
    assert parse_key_table({"KEYS": ["z"]}).base_note == 60, "BASE_NOTE 可省"
    assert parse_key_table({"KEYS": ["z", "enter"]}).keys == ("z", "enter")
    assert parse_key_table({"KEYS": ["Enter"]}).keys == ("enter",), "键名不区分大小写"

    for bad, expected in (
        ([], "顶层"),
        ({}, "KEYS"),
        ({"KEYS": []}, "非空"),
        ({"KEYS": "zs"}, "非空"),
        ({"KEYS": ["zz"]}, "单个字符"),
        ({"KEYS": ["z", 1]}, "单个字符"),
        ({"KEYS": ["z", "z"]}, "重复"),
        ({"KEYS": ["enter", "Enter"]}, "重复"),
        ({"BASE_NOTE": True, "KEYS": ["z"]}, "整数"),
        ({"BASE_NOTE": "60", "KEYS": ["z"]}, "整数"),
        ({"BASE_NOTE": 200, "KEYS": ["z"]}, "0–127"),
    ):
        try:
            parse_key_table(bad)
        except KeyTableError as exc:
            assert expected in str(exc), (expected, str(exc))
        else:
            raise AssertionError(f"应该拒绝：{bad!r}")


def test_key_table_file_roundtrip_and_reload() -> None:
    path = HERE / "_midi_keys.test.json"
    try:
        path.write_text(
            json.dumps({"BASE_NOTE": 48, "KEYS": ["a", "b", "c"]}), encoding="utf-8"
        )
        table = load_key_table(path)
        assert (table.base_note, table.keys) == (48, ("a", "b", "c"))
    finally:
        path.unlink(missing_ok=True)


def test_scan_code_uses_scancodes_and_rejects_shifted_keys() -> None:
    assert _scan_code("z") > 0
    assert _scan_code("2") > 0
    assert _scan_code(";") > 0
    assert _scan_code("'") > 0
    assert _scan_code("=") > 0
    assert _scan_code("enter") == 28, "Enter 的扫描码（VK_RETURN）"
    assert _scan_code("Enter") == 28, "键名不区分大小写"
    for bad in ("!", "Z", "zz", "f1"):
        try:
            _scan_code(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} 应该被拒绝")


# ---------------------------------------------------------------- Capo / 排期


def test_capo_shifts_whole_song_up() -> None:
    """Capo N 品 = 整首 +N 半音：C3（48）用 Capo 4 正好进 E3（52）。"""
    view = MidiPage(None)
    view._set_doc(demo_document())
    assert view.capo == 0, "载入新文档时 Capo 回到 0，不自动移调"
    missing = [block for block in view._note_blocks() if not block[5]]
    assert len(missing) == 1 and missing[0][2] == "C3"

    view._dd_capo.value = "4"
    view._on_capo_change()
    assert view.capo == 4
    assert all(block[5] for block in view._note_blocks()), "Capo 4 后没有超窗的音"
    assert view._key_for(48) == "a", "C3 + 4 = E3 → 底行第一个键"


def test_capo_is_clamped_and_reset_on_new_doc() -> None:
    view = MidiPage(None)
    view._set_doc(demo_document())
    view._dd_capo.value = "99"
    view._on_capo_change()
    assert view.capo == 12, "下拉之外的脏值夹到 12"
    assert view._dd_capo.value == "12"

    view._set_doc(demo_document())
    assert view.capo == 0 and view._dd_capo.value == "0"


def test_tap_schedule_press_and_release() -> None:
    doc = _doc([(0, 480, 52)])
    schedule = build_key_schedule(doc, tap_seconds=0.1)
    press, release = schedule.actions
    assert (press.kind, press.key, press.unit) == ("press", "a", True)
    assert release.kind == "release" and release.unit is False
    assert abs(press.time - (LEAD_IN + 0.0)) < 1e-9
    assert abs(release.time - (LEAD_IN + 0.1)) < 1e-9
    assert schedule.note_count == 1
    assert schedule.skipped == []


def test_long_press_schedule_holds_to_note_end() -> None:
    doc = _doc([(0, 960, 60)])  # 2 拍 = 1s
    schedule = build_key_schedule(doc, tap_notes=False, tap_seconds=0.1)
    _press, release = schedule.actions
    assert abs(release.time - (LEAD_IN + 1.0)) < 1e-6


def test_short_tap_is_lifted_to_note_end_when_longer() -> None:
    # 音只有 0.05s，点按却要 0.2s：不能超过音尾（tap 是上限）
    doc = _doc([(0, 48, 60)])  # 48 tick = 0.1 拍 = 0.05s
    schedule = build_key_schedule(doc, tap_seconds=0.2)
    _press, release = schedule.actions
    assert abs(release.time - (LEAD_IN + 0.05)) < 1e-6


def test_out_of_window_notes_are_skipped_and_reported() -> None:
    doc = _doc([(0, 480, 30), (0, 480, 60)])
    schedule = build_key_schedule(doc)
    assert schedule.note_count == 1
    assert schedule.skipped == ["F#1"]


def test_same_key_repress_is_pushed_after_gap() -> None:
    doc = _doc([(0, 240, 60), (0, 240, 60)])  # 同一个音同时按两次
    schedule = build_key_schedule(doc, tap_seconds=0.1)
    presses = [action for action in schedule.actions if action.kind == "press"]
    releases = [action for action in schedule.actions if action.kind == "release"]
    assert len(presses) == 2
    # 不能「按着再按」：第二次按下离第一次松开至少 KEY_REPRESS_GAP
    assert presses[1].time - releases[0].time >= KEY_REPRESS_GAP - 1e-9
    assert presses[1].time > presses[0].time


def test_actions_are_sorted_and_release_comes_first_on_tie() -> None:
    doc = _doc([(0, 480, 60), (480, 960, 62)])
    schedule = build_key_schedule(doc)
    times = [action.time for action in schedule.actions]
    assert times == sorted(times)
    # 第一个音的 release（0.5）早于第二个音的 press（0.9）
    assert [action.kind for action in schedule.actions] == [
        "press",
        "release",
        "press",
        "release",
    ]


def test_transpose_moves_keys() -> None:
    doc = _doc([(0, 480, 48)])
    assert build_key_schedule(doc).skipped, "不移调时 C3 在窗口外"
    schedule = build_key_schedule(doc, transpose=4)
    assert schedule.skipped == []
    assert schedule.actions[0].key == "a"


# ---------------------------------------------------------------- 页面（离线）


def test_page_builds_without_document() -> None:
    view = MidiPage(None)
    assert view.doc is None
    assert view.table.span == 36
    assert view._note_blocks() == []
    assert view._timeline_width > 0
    assert "还没有载入" in view._info_column.controls[0].value


def test_page_set_doc_renders_blocks_and_labels() -> None:
    view = MidiPage(None)
    view._set_doc(demo_document())
    blocks = view._note_blocks()
    assert len(blocks) == 14
    assert view._file_label.value.startswith("示例（音阶 + 和弦）")
    assert "14 个音" in view._file_label.value
    # 不自动移调：默认 Capo 0 时只有 C3（48）在窗口外
    assert sum(1 for block in blocks if not block[5]) == 1


def test_page_offline_play_only_builds_schedule() -> None:
    view = MidiPage(None)
    view._set_doc(demo_document())
    asyncio.run(view._on_play())
    assert view._playing is False
    assert "离线" in view._progress.value
    assert "13" in view._progress.value, "Capo 0 时示例的 14 个音里 C3 发不出去，剩 13"


def test_page_play_with_fake_page_reports_skipped_and_stops() -> None:
    page = FakePage()
    view = MidiPage(page)
    view.doc = demo_document()

    asyncio.run(view._on_play())

    assert view._playing is True
    assert page.tasks and page.tasks[0][0] == view._play_loop
    assert view._warning.visible is True
    assert "C3" in view._warning.value
    assert "0/13" in view._progress.value, "14 个音里只有 C3 超窗，剩 13 个"

    view.stop()
    assert view._playing is False
    assert view._playhead.visible is False
    assert "已停止" in view._progress.value


def test_page_load_bad_file_keeps_current_document() -> None:
    page = FakePage()
    view = MidiPage(page)
    view._set_doc(demo_document())
    path = HERE / "_midi_bad.mid"
    try:
        path.write_bytes(b"definitely not midi")
        assert view.load_path(path) is False
    finally:
        path.unlink(missing_ok=True)
    assert view.doc is not None, "坏文件不能把已载入的谱面清空"
    assert len(page.dialogs) == 1, "要弹出错误提示"
    assert "打开失败" in page.dialogs[0].content.value


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
