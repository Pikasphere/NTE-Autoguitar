"""gtab 文档模型的测试（不启动 GUI）。

运行：``.venv\\Scripts\\python.exe tests/test_gtab_doc.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gtab import EV_END, EV_STRING_NOTE_OFF, EV_STRING_NOTE_ON, EV_TEMPO  # noqa: E402
from ui.gtab_doc import (  # noqa: E402
    EventRow,
    GtabDocument,
    GtabError,
    MetaEntry,
    NoteEntry,
    seconds_to_tick,
    tick_to_seconds,
)

DEMO = SRC / "MusicList" / "gtabfile" / "demo.gtab"


def test_demo_roundtrip_is_byte_identical() -> None:
    doc = GtabDocument.from_path(DEMO)
    assert doc.string_count == 6
    assert doc.max_fret == 24
    assert doc.time_base_value == 480
    assert doc.bpm == 120.0
    assert [m.meta_type for m in doc.meta] == [1, 2, 4]
    assert len(doc.events) == 5
    assert doc.to_bytes() == DEMO.read_bytes(), "读入再写出应逐字节一致"


def test_edit_then_reload() -> None:
    doc = GtabDocument.from_path(DEMO)
    doc.events.append(EventRow(tick=600, event_type=EV_STRING_NOTE_ON, data=bytes([4, 3])))

    reloaded = GtabDocument.from_bytes(doc.to_bytes())

    # 只加了 NoteOn：保存时自动补了 1/4 拍的 NoteOff
    assert len(reloaded.events) == 7
    assert reloaded.events[-1].event_type == EV_END
    assert reloaded.events[-1].tick == 720, "End 应被推到（自动补的）最后一个事件之后"

    note = reloaded.events[-2]
    assert note.event_type == EV_STRING_NOTE_OFF
    assert (note.string, note.fret) == (4, 3), "自动补的 NoteOff 要沿用 On 的弦/品"
    assert reloaded.note_blocks()[-1] == (600, 720, 4, 3)


def test_unsorted_events_are_sorted_on_save() -> None:
    doc = GtabDocument()
    doc.events = [
        EventRow(tick=480, event_type=EV_STRING_NOTE_ON, data=bytes([2, 0])),
        EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([1, 0])),
    ]
    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    ticks = [ev.tick for ev in reloaded.events]
    assert ticks == sorted(ticks)
    assert ticks[0] == 0


def test_meta_value_text_and_hex() -> None:
    assert MetaEntry.parse_value("standard") == b"standard"
    assert MetaEntry.parse_value("hex:0001") == b"\x00\x01"
    assert MetaEntry.parse_value("hex:00 01") == b"\x00\x01"
    assert MetaEntry(4, b"\x00").display_value() == "hex:00"
    assert MetaEntry(1, b"standard").display_value() == "standard"
    # 文本 <-> hex 往返
    for entry in (MetaEntry(4, b"\x00"), MetaEntry(2, "Demo Song".encode())):
        assert MetaEntry.parse_value(entry.display_value()) == entry.data


def test_bpm_affects_header() -> None:
    doc = GtabDocument()
    doc.bpm = 90
    assert doc.default_tempo_us == round(60_000_000 / 90)
    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    assert reloaded.default_tempo_us == doc.default_tempo_us


def test_beats_conversion() -> None:
    doc = GtabDocument()  # 1 拍 = 480 tick
    assert doc.beats_at(0) == 0.0
    assert doc.beats_at(480) == 1.0
    assert doc.beats_at(240) == 0.5
    assert doc.tick_at(1.0) == 480
    assert doc.tick_at(2.5) == 1200
    # 负数夹到 0
    assert doc.beats_at(-10) == 0.0
    assert doc.tick_at(-1) == 0

    # 换 time_base 后，tick 与拍的关系跟着变
    other = GtabDocument(time_base_value=960)
    assert other.beats_at(960) == 1.0
    assert other.tick_at(1.0) == 960


def test_tick_to_seconds_for_playback() -> None:
    doc = GtabDocument()  # 480 tick/拍，120 BPM → 1 拍 = 0.5s
    assert abs(tick_to_seconds(doc, 0) - 0.0) < 1e-9
    assert abs(tick_to_seconds(doc, 480) - 0.5) < 1e-9
    assert abs(tick_to_seconds(doc, 960) - 1.0) < 1e-9

    # 0 拍处改成 240 BPM，后面时间减半
    doc.events = [EventRow(tick=0, event_type=EV_TEMPO, data=(250_000).to_bytes(4, "little"))]
    assert abs(tick_to_seconds(doc, 480) - 0.25) < 1e-9

    # 反函数：试听进度用
    assert seconds_to_tick(doc, 0.0) == 0
    assert seconds_to_tick(doc, 0.25) == 480
    assert seconds_to_tick(doc, 0.5) == 960
    assert seconds_to_tick(doc, -1.0) == 0

    # 有 Tempo 变化时也要能来回换算
    doc.events = [
        EventRow(tick=0, event_type=EV_TEMPO, data=(500_000).to_bytes(4, "little")),
        EventRow(tick=480, event_type=EV_TEMPO, data=(250_000).to_bytes(4, "little")),
    ]
    # 0–1 拍：0.5s；1–3 拍：2 拍 × 0.25s = 0.5s → 3 拍 = 1.0s
    assert abs(tick_to_seconds(doc, 1440) - 1.0) < 1e-9
    assert seconds_to_tick(doc, 1.0) == 1440
    assert abs(tick_to_seconds(doc, seconds_to_tick(doc, 0.7)) - 0.7) < 1e-9


def test_document_duration_in_beats() -> None:
    doc = GtabDocument.from_path(DEMO)
    assert abs(doc.duration_beats - 1.0) < 1e-9


def test_invalid_values_raise() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_OFF, data=bytes([1, 0]))]
    try:
        doc.to_bytes()
    except GtabError as exc:
        assert "NoteOff" in str(exc) or "note" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("悬空的 NoteOff 应当报错")

    bad = GtabDocument(string_count=6, max_fret=24)
    bad.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([9, 0]))]
    try:
        bad.to_bytes()
    except GtabError:
        pass
    else:  # pragma: no cover
        raise AssertionError("弦号越界应当报错")


def test_tempo_event_kept() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=EV_TEMPO, data=(400_000).to_bytes(4, "little"))]
    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    tempo = [ev for ev in reloaded.events if ev.event_type == EV_TEMPO]
    assert len(tempo) == 1
    assert tempo[0].tempo_us == 400_000


def test_note_pairs_keep_on_off_rows() -> None:
    doc = GtabDocument.from_path(DEMO)
    entries = doc.note_pairs()

    assert len(entries) == 2
    first, second = entries
    assert isinstance(first, NoteEntry)
    assert (first.start_tick, first.duration_ticks, first.string, first.fret) == (0, 480, 6, 0)
    assert (second.start_tick, second.duration_ticks, second.string, second.fret) == (240, 240, 5, 2)

    # 条目里拿到的就是文档里的那几个 EventRow（引用，不是拷贝）
    assert any(ev is first.on for ev in doc.events)
    assert any(ev is first.off for ev in doc.events)
    assert first.missing_off is False
    assert doc.orphan_note_offs() == []


def test_duration_edit_moves_note_off() -> None:
    doc = GtabDocument()
    entry = doc.add_note(tick=0, string=6, fret=0)
    assert entry.off is not None
    assert entry.duration_ticks == 480, "默认音长应为 1 拍"

    applied = doc.set_note_duration(entry, 960)
    assert applied == 960
    assert entry.end_tick == 960
    assert entry.off.tick == 960
    assert doc.note_blocks() == [(0, 960, 6, 0)]

    # 同一 key 上后面还有音时，音长被夹到它前面，避免重叠
    later = doc.add_note(tick=1440, string=6, fret=0)
    applied = doc.set_note_duration(entry, 2000)
    assert applied == 1440, "应夹到后一个同键音的起点"
    assert entry.end_tick == 1440
    assert later.start_tick == 1440

    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    assert reloaded.note_blocks() == [(0, 1440, 6, 0), (1440, 1920, 6, 0)]


def test_missing_off_is_filled_on_save() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([3, 5]))]

    entry = doc.note_pairs()[0]
    assert entry.missing_off is True
    assert entry.duration_ticks == 120, "缺 Off 时按 1/4 拍占位显示"

    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    assert reloaded.note_blocks() == [(0, 120, 3, 5)]
    assert len([ev for ev in reloaded.events if ev.event_type == EV_STRING_NOTE_OFF]) == 1
    # 已经补过之后再保存是幂等的
    assert doc.ensure_note_offs() == 0


def test_move_and_delete_note_keep_pair_together() -> None:
    doc = GtabDocument()
    entry = doc.add_note(tick=0, string=2, fret=3, voice=7, duration_ticks=240)

    doc.move_note(entry, 480)
    assert entry.on.tick == 480 and entry.off.tick == 720
    assert entry.duration_ticks == 240, "移动整音时音长不变"

    doc.set_note_fields(entry, string=4, fret=9, voice=2)
    assert (entry.on.string, entry.on.fret, entry.on.voice) == (4, 9, 2)
    assert (entry.off.string, entry.off.fret, entry.off.voice) == (4, 9, 2), "On/Off 必须同步改"

    doc.delete_note(entry)
    assert doc.events == []
    assert doc.note_pairs() == []


def test_orphan_note_off_is_kept_and_reported() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_OFF, data=bytes([1, 0]))]

    assert len(doc.orphan_note_offs()) == 1
    try:
        doc.to_bytes()
    except GtabError as exc:
        assert "NoteOff" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("孤立的 NoteOff 应当报错，而不是被静默删掉")
    assert len(doc.events) == 1, "报错时不能改动文档"


def test_same_key_overlap_is_rejected_with_clear_error() -> None:
    doc = GtabDocument()
    doc.events = [
        EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([6, 0])),
        EventRow(tick=240, event_type=EV_STRING_NOTE_ON, data=bytes([6, 0])),
        EventRow(tick=480, event_type=EV_STRING_NOTE_OFF, data=bytes([6, 0])),
        EventRow(tick=960, event_type=EV_STRING_NOTE_OFF, data=bytes([6, 0])),
    ]
    try:
        doc.to_bytes()
    except GtabError as exc:
        assert "重叠" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("同一个键上重叠的两个音应当报错")


def test_adjacent_same_key_notes_roundtrip() -> None:
    doc = GtabDocument()
    doc.add_note(tick=0, string=6, fret=0, duration_ticks=480)
    doc.add_note(tick=480, string=6, fret=0, duration_ticks=480)

    reloaded = GtabDocument.from_bytes(doc.to_bytes())
    assert reloaded.note_blocks() == [(0, 480, 6, 0), (480, 960, 6, 0)]
    # 同一 tick 必须先写 NoteOff 再写 NoteOn
    off_index = next(
        i for i, ev in enumerate(reloaded.events)
        if ev.event_type == EV_STRING_NOTE_OFF and ev.tick == 480
    )
    on_index = next(
        i for i, ev in enumerate(reloaded.events)
        if ev.event_type == EV_STRING_NOTE_ON and ev.tick == 480
    )
    assert off_index < on_index


def test_duration_beats_uses_note_end() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([6, 0]))]
    assert abs(doc.duration_beats - 0.25) < 1e-9, "缺 Off 时按占位长度算总长"


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
