"""试听合成的测试（不播放、不启动 GUI）。

覆盖 gtab 的音符合成，以及 GCT 编辑页的和弦合成（`chord_preview`）：
扫弦弦序 / 静音弦 / 表外和弦 / Tempo 对长度的影响。

运行：``.venv\\Scripts\\python.exe tests/test_audio_preview.py``
"""

from __future__ import annotations

import sys
import wave
import io
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gct import DIR_DOWN, DIR_UP, EV_CHORD_PLUCK, EV_CHORD_STRUM, EV_TEMPO  # noqa: E402
from gtab import EV_TEMPO as GTAB_TEMPO  # noqa: E402
from ui.audio_preview import (  # noqa: E402
    LEAD_IN_SECONDS,
    SAMPLE_RATE,
    TAIL_SECONDS,
    fret_frequency,
    note_timeline,
    preview_seconds,
    render_wav,
    wav_duration,
)
from ui.chord_preview import (  # noqa: E402
    RING_SECONDS,
    STRUM_GAP,
    chord_notes,
    preview_seconds as chord_preview_seconds,
    render_wav as render_chord_wav,
)
from ui.chords import ChordTable, load_chord_table  # noqa: E402
from ui.gct_doc import ChordRow, GctDocument, demo_document  # noqa: E402
from ui.gtab_doc import EventRow, GtabDocument  # noqa: E402

DEMO = SRC / "MusicList" / "gtabfile" / "demo.gtab"


def _read_wav(blob: bytes):
    with wave.open(io.BytesIO(blob), "rb") as wav:
        return {
            "channels": wav.getnchannels(),
            "width": wav.getsampwidth(),
            "rate": wav.getframerate(),
            "frames": wav.getnframes(),
            "pcm": wav.readframes(wav.getnframes()),
        }


def test_fret_frequency_standard_tuning() -> None:
    assert abs(fret_frequency(6, 0) - 82.41) < 0.01
    assert abs(fret_frequency(1, 0) - 329.63) < 0.01
    # 每加一品升高一个半音（×2^(1/12)）
    assert abs(fret_frequency(5, 2) - 110.00 * 2 ** (2 / 12)) < 0.01
    assert abs(fret_frequency(6, 12) - 164.82) < 0.02
    # 负品按空弦处理，未知弦号退回 6 弦
    assert fret_frequency(6, -3) == fret_frequency(6, 0)
    assert fret_frequency(99, 0) == fret_frequency(6, 0)


def test_note_timeline_from_document() -> None:
    doc = GtabDocument.from_path(DEMO)
    timeline = note_timeline(doc)
    assert len(timeline) == 2
    start, end, string, fret = timeline[0]
    assert (string, fret) == (6, 0)
    assert abs(start) < 1e-9 and abs(end - 0.5) < 1e-9
    start, end, string, fret = timeline[1]
    assert (string, fret) == (5, 2)
    assert abs(start - 0.25) < 1e-9 and abs(end - 0.5) < 1e-9


def test_render_wav_header_and_length() -> None:
    doc = GtabDocument.from_path(DEMO)
    blob = render_wav(doc)
    info = _read_wav(blob)

    assert blob[:4] == b"RIFF"
    assert info["channels"] == 1
    assert info["width"] == 2
    assert info["rate"] == SAMPLE_RATE

    expected = LEAD_IN_SECONDS + 0.5 + TAIL_SECONDS
    assert abs(wav_duration(blob) - expected) < 0.01
    assert abs(preview_seconds(doc) - expected) < 1e-9

    pcm = info["pcm"]
    assert len(pcm) == info["frames"] * 2
    # 有声音，且没有溢出成静音
    assert any(byte != 0 for byte in pcm)
    loudest = max(abs(int.from_bytes(pcm[i : i + 2], "little", signed=True)) for i in range(0, len(pcm), 2))
    assert 1000 < loudest <= 32767


def test_bpm_changes_preview_length() -> None:
    doc = GtabDocument.from_path(DEMO)
    doc.bpm = 240  # 1 拍从 0.5s 变成 0.25s
    expected = LEAD_IN_SECONDS + 0.25 + TAIL_SECONDS
    assert abs(preview_seconds(doc) - expected) < 1e-9
    assert abs(wav_duration(render_wav(doc)) - expected) < 0.01


def test_tempo_event_changes_preview_length() -> None:
    doc = GtabDocument.from_path(DEMO)
    doc.events.append(
        EventRow(tick=0, event_type=GTAB_TEMPO, data=(250_000).to_bytes(4, "little"))
    )
    # 240 BPM 从 0 拍开始 → 整段减半
    assert abs(preview_seconds(doc) - (LEAD_IN_SECONDS + 0.25 + TAIL_SECONDS)) < 1e-9


def test_empty_document_renders_silence() -> None:
    blob = render_wav(GtabDocument())
    info = _read_wav(blob)
    assert info["frames"] > 0
    assert all(byte == 0 for byte in info["pcm"]), "空文档应当是静音"


def test_missing_note_off_uses_placeholder_length() -> None:
    doc = GtabDocument()
    doc.events = [EventRow(tick=0, event_type=0x01, data=bytes([6, 0]))]
    # 缺 Off 时按 1/4 拍（0.125s @120BPM）占位
    assert abs(preview_seconds(doc) - (LEAD_IN_SECONDS + 0.125 + TAIL_SECONDS)) < 1e-9


# ---------------------------------------------------------------- GCT 和弦试听


def _strum_data(chord_id: int, direction: int = DIR_DOWN) -> bytes:
    return int(chord_id).to_bytes(2, "little") + bytes([direction])


def _pluck_data(chord_id: int, string: int) -> bytes:
    return int(chord_id).to_bytes(2, "little") + bytes([string])


def test_chord_notes_use_fingering_for_down_strum() -> None:
    notes, skipped = chord_notes(demo_document(), load_chord_table())
    assert skipped == []

    # 示例第一拍是 G 下扫：按 6 → 1 弦依次响，品来自 CHORDS_MAPPINGS
    first = [note for note in notes if note[0] < 0.15]
    assert [(note[2], note[3]) for note in first] == [
        (6, 3),
        (5, 0),
        (4, 0),
        (3, 0),
        (2, 2),
        (1, 3),
    ], "G 的指法是 1 弦 → 6 弦 = 3 2 0 0 0 3，下扫要倒着来"
    assert abs((first[1][0] - first[0][0]) - STRUM_GAP) < 1e-9, "六根弦依次响"
    assert abs((first[0][1] - first[0][0]) - RING_SECONDS) < 1e-9


def test_chord_notes_up_strum_reverses_string_order() -> None:
    notes, _ = chord_notes(demo_document(), load_chord_table())
    # 示例最后一拍（第 8 拍 = 4.0s）是上扫：1 → 6 弦
    up = [note for note in notes if 4.0 <= note[0] < 4.2]
    assert [note[2] for note in up] == [1, 2, 3, 4, 5, 6]


def test_chord_notes_skip_muted_strings() -> None:
    table = ChordTable(names=("D",), mappings={"D": (2, 3, 2, 0, -1, -1)})
    doc = GctDocument()
    chord_id = doc.add_chord("D")
    doc.events = [ChordRow(0, EV_CHORD_STRUM, 0, _strum_data(chord_id))]

    notes, skipped = chord_notes(doc, table)
    assert skipped == []
    assert [note[2] for note in notes] == [4, 3, 2, 1], "5/6 弦是 -1，不发声"
    assert [note[3] for note in notes] == [0, 2, 3, 2]


def test_chord_notes_skip_unknown_chord() -> None:
    table = ChordTable(names=("G",), mappings={"G": (3, 2, 0, 0, 0, 3)})
    doc = GctDocument()
    chord_id = doc.add_chord("Xyz")
    doc.events = [ChordRow(0, EV_CHORD_STRUM, 0, _strum_data(chord_id))]

    notes, skipped = chord_notes(doc, table)
    assert notes == []
    assert skipped == ["Xyz"], "表里没有指法的和弦要报出来，不能静默"


def test_chord_notes_pluck_only_that_string() -> None:
    table = ChordTable(names=("G",), mappings={"G": (3, 2, 0, 0, 0, 3)})
    doc = GctDocument()
    chord_id = doc.add_chord("G")
    doc.events = [ChordRow(0, EV_CHORD_PLUCK, 0, _pluck_data(chord_id, 4))]

    notes, _ = chord_notes(doc, table)
    assert [(note[2], note[3]) for note in notes] == [(4, 0)], "拨 4 弦 = 空弦"


def test_chord_preview_seconds_follow_tempo() -> None:
    doc = demo_document()
    expected = LEAD_IN_SECONDS + 4.0 + RING_SECONDS + TAIL_SECONDS
    assert abs(chord_preview_seconds(doc) - expected) < 1e-9

    # Tempo 从 0 拍改成 240 BPM：整段减半（最后一拍 4.0s → 2.0s）
    doc.events.append(
        ChordRow(tick=0, event_type=EV_TEMPO, data=(250_000).to_bytes(4, "little"))
    )
    expected = LEAD_IN_SECONDS + 2.0 + RING_SECONDS + TAIL_SECONDS
    assert abs(chord_preview_seconds(doc) - expected) < 1e-9


def test_render_chord_wav_header_and_sound() -> None:
    doc = demo_document()
    blob = render_chord_wav(doc, load_chord_table())
    info = _read_wav(blob)

    assert blob[:4] == b"RIFF"
    assert info["channels"] == 1 and info["width"] == 2
    assert info["rate"] == SAMPLE_RATE
    assert abs(wav_duration(blob) - chord_preview_seconds(doc)) < 0.01

    pcm = info["pcm"]
    loudest = max(
        abs(int.from_bytes(pcm[i : i + 2], "little", signed=True))
        for i in range(0, len(pcm), 2)
    )
    assert 1000 < loudest <= 32767, "和弦扫弦要有声音"


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
