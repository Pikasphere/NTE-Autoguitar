"""MIDI → GTAB 转换模型的测试（不启动 GUI）。

覆盖：定弦/页面换算、同时音只留最高、把位沿用、翻页统计、音域过滤、
tempo/时间轴换算、同键重叠截断、以及生成文件能被严格回读。

运行：``.venv\\Scripts\\python.exe tests/test_midi_to_gtab.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gtab import EV_TEMPO  # noqa: E402
from ui.gamepad_backend import page_for as gamepad_page_for  # noqa: E402
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.midi_doc import MidiDocument, NoteEvent, TempoMark, demo_document  # noqa: E402
from ui.midi_to_gtab import (  # noqa: E402
    GTAB_DIVISION,
    ConvertOptions,
    convert,
    page_for,
    positions_for,
)


def _doc(notes: list[NoteEvent], *, division: int = 480, tempos=None, end_tick=None):
    return MidiDocument(
        division=division,
        notes=notes,
        tempos=tempos or [TempoMark(0, 500_000)],
        end_tick=end_tick
        if end_tick is not None
        else max((note.end for note in notes), default=0),
    )


def _one(pitch: int, start: int = 0, length: int = 240) -> NoteEvent:
    return NoteEvent(start, start + length, pitch)


# ---------------------------------------------------------------- 定弦 / 页面


def test_page_for_matches_gamepad_backend() -> None:
    """页面换算必须和手柄回放用的一模一样，否则转换出来会多翻/少翻页。"""
    for fret in range(0, 25):
        assert page_for(fret) == gamepad_page_for(fret)


def test_positions_cover_standard_tuning() -> None:
    assert [(p.string, p.fret) for p in positions_for(40)] == [(6, 0)]
    assert positions_for(39) == []
    assert positions_for(89) == []
    e4 = [(p.string, p.fret) for p in positions_for(64)]
    assert (1, 0) in e4 and (2, 5) in e4
    assert [(p.string, p.fret) for p in positions_for(88)] == [(1, 24)]


# ---------------------------------------------------------------- 同时音


def test_highest_only_drops_lower_simultaneous_notes() -> None:
    chord = [NoteEvent(0, 480, pitch) for pitch in (60, 64, 67)]
    result = convert(_doc(chord))

    assert len(result.notes) == 1
    assert result.notes[0].pitch == 67, "默认只弹同一 tick 上最高的音"
    assert result.stats.dropped_simultaneous == 2
    assert any("同时起音" in text for text in result.warnings)


def test_keep_all_puts_chord_on_one_page_with_distinct_strings() -> None:
    chord = [NoteEvent(0, 480, pitch) for pitch in (60, 64, 67)]
    result = convert(_doc(chord), ConvertOptions(keep_top_only=False))

    assert [note.pitch for note in result.notes] == [67, 64, 60]
    assert len({note.string for note in result.notes}) == 3, "和弦不能两条音挤同一根弦"
    assert {note.page for note in result.notes} == {1}, "整组应落在同一页"
    assert result.stats.page_steps == 0


# ---------------------------------------------------------------- 把位


def test_prefers_previous_page_over_open_string() -> None:
    """C4 之后弹 E4：应走 2 弦 5 品（第 1 页），而不是 1 弦空弦（第 5 页）。"""
    result = convert(_doc([_one(60), _one(64, start=240)]))
    first, second = result.notes

    assert (first.string, first.fret, first.page) == (2, 1, 1)
    assert (second.string, second.fret, second.page) == (2, 5, 1)
    assert result.stats.page_steps == 0


def test_page_switch_only_when_pitch_is_not_on_current_page() -> None:
    result = convert(_doc([_one(67), _one(72, start=240)]))
    first, second = result.notes

    assert first.page == 1
    assert second.page == 2, "G4 之后 C5 在第 1 页没有位置，只能去第 2 页"
    assert result.stats.page_changes == 1
    assert result.stats.page_steps == 1


def test_start_moves_to_open_page_when_note_requires_it() -> None:
    result = convert(_doc([_one(40)]))
    assert result.notes[0].page == 5
    assert result.stats.page_steps == 4, "从开局第 1 页到空弦第 5 页要推 4 次"


# ---------------------------------------------------------------- 音域 / 时间轴


def test_out_of_range_notes_are_skipped_and_warned() -> None:
    # 四个音错开起点，否则会被当成“同时起音”只留最高的那个
    doc = _doc(
        [
            _one(39, start=0),
            _one(40, start=240),
            _one(88, start=480),
            _one(89, start=720),
        ]
    )
    result = convert(doc)

    assert [note.pitch for note in result.notes] == [40, 88]
    assert result.stats.skipped_out_of_range == 2
    assert any("超出" in text for text in result.warnings)


def test_percussion_channel_is_skipped_by_default() -> None:
    doc = _doc(
        [
            NoteEvent(0, 240, 60, 100, channel=0),
            NoteEvent(240, 480, 45, 100, channel=9),  # GM 通道 10 = 鼓
        ]
    )
    result = convert(doc)

    assert [note.pitch for note in result.notes] == [60]
    assert result.stats.skipped_percussion == 1
    assert any("打击乐" in text for text in result.warnings)

    kept = convert(doc, ConvertOptions(skip_percussion=False))
    assert [note.pitch for note in kept.notes] == [60, 45]
    assert kept.stats.skipped_percussion == 0


def test_tempo_and_ticks_are_rescaled_to_480() -> None:
    doc = _doc(
        [NoteEvent(960, 1920, 60)],
        division=960,
        tempos=[TempoMark(0, 600_000), TempoMark(1920, 300_000)],
    )
    result = convert(doc)

    assert result.doc.time_base_value == GTAB_DIVISION
    assert result.doc.default_tempo_us == 600_000
    assert result.doc.bpm == 100.0

    note = result.notes[0]
    assert (note.start, note.duration) == (480, 480)

    tempo_events = [ev for ev in result.doc.events if ev.event_type == EV_TEMPO]
    assert [ev.tick for ev in tempo_events] == [960]
    assert tempo_events[0].tempo_us == 300_000


def test_durations_are_preserved() -> None:
    result = convert(_doc([NoteEvent(0, 360, 60)]))
    assert result.notes[0].duration == 360


def test_same_start_same_pitch_keeps_longest() -> None:
    doc = _doc([NoteEvent(0, 240, 60), NoteEvent(0, 480, 60)])
    result = convert(doc)

    assert len(result.notes) == 1
    assert result.notes[0].duration == 480
    assert result.stats.dropped_simultaneous == 0, "同音去重不算“省略同时音”"


# ---------------------------------------------------------------- 重叠 / 回读


def test_same_key_overlap_is_trimmed_and_roundtrips() -> None:
    # E2 只有 6 弦空弦一个位置，两个音压在一起时只能截断前一个
    doc = _doc([NoteEvent(0, 1000, 40), NoteEvent(500, 1500, 40)])
    result = convert(doc)

    assert [(n.start, n.end) for n in result.notes] == [(0, 500), (500, 1500)]
    assert result.stats.trimmed_overlaps == 1

    reloaded = GtabDocument.from_bytes(result.doc.to_bytes())
    assert reloaded.note_blocks() == [(0, 500, 6, 0), (500, 1500, 6, 0)]


def test_demo_conversion_roundtrips_without_orphan_offs() -> None:
    result = convert(demo_document())
    blob = result.doc.to_bytes()
    reloaded = GtabDocument.from_bytes(blob)

    assert len(result.notes) > 0
    assert len(reloaded.note_pairs()) == len(result.notes)
    assert reloaded.orphan_note_offs() == []
    assert reloaded.time_base_value == GTAB_DIVISION


def test_empty_midi_gives_empty_but_valid_document() -> None:
    result = convert(MidiDocument(division=480, notes=[]))

    assert result.notes == []
    assert result.stats.output_notes == 0
    assert any("没有可转换" in text for text in result.warnings)
    assert GtabDocument.from_bytes(result.doc.to_bytes()).note_pairs() == []


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
