"""试听合成的测试（不播放、不启动 GUI）。

运行：``.venv\\Scripts\\python.exe tests/test_audio_preview.py``
"""

from __future__ import annotations

import sys
import wave
import io
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gtab import EV_TEMPO  # noqa: E402
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
        EventRow(tick=0, event_type=EV_TEMPO, data=(250_000).to_bytes(4, "little"))
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
