"""gct 格式与文档模型的测试（不需要界面，也不会创建手柄）。

覆盖：读写往返、自动补 End、头部/CRC 校验、和弦表约束、Tempo 时间换算。

运行：``.venv\\Scripts\\python.exe tests/test_gct_doc.py``
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gct import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    EV_CHORD_STRUM,
    EV_END,
    EV_TEMPO,
    GctError,
    GctReader,
    GctWriter,
)
from ui.gct_doc import (  # noqa: E402
    ChordRow,
    GctDocument,
    demo_document,
    seconds_to_tick,
    tick_to_seconds,
)


def test_demo_roundtrip() -> None:
    doc = demo_document()
    blob = doc.to_bytes()
    again = GctDocument.from_bytes(blob)

    assert [entry.name for entry in again.chords] == [entry.name for entry in doc.chords]
    assert len(again.events) == len(doc.events)
    assert again.counts() == doc.counts()
    assert again.counts()["Strum"] == 9
    assert again.default_tempo_us == 500_000
    assert again.time_base_value == 480
    assert again.string_count == 6


def test_header_layout_and_magic() -> None:
    doc = demo_document()
    blob = doc.to_bytes()

    assert blob[:8] == b"GCTAB\0\0\0"
    (header_size,) = struct.unpack_from("<I", blob, 12)
    (chord_count,) = struct.unpack_from("<I", blob, 28)
    (chord_table_size,) = struct.unpack_from("<I", blob, 32)
    (meta_size,) = struct.unpack_from("<I", blob, 36)
    (event_count,) = struct.unpack_from("<I", blob, 40)

    assert header_size == 48 + chord_table_size + meta_size
    assert chord_count == len(doc.chords)
    assert event_count == len(doc.events)


def test_to_bytes_adds_end_and_is_idempotent() -> None:
    doc = GctDocument()
    chord = doc.add_chord("G")
    doc.events = [
        ChordRow(0, EV_CHORD_STRUM, 0, chord.to_bytes(2, "little") + bytes([DIR_DOWN]))
    ]

    first = doc.to_bytes()
    second = doc.to_bytes()      # 幂等：不能因为补过 End 又补一个
    assert first == second
    assert len(doc.events) == 1, "to_bytes 不应该改文档"

    parsed = GctDocument.from_bytes(first)
    assert [ev.event_type for ev in parsed.events] == [EV_CHORD_STRUM, EV_END]


def test_reader_rejects_corrupted_header() -> None:
    blob = bytearray(demo_document().to_bytes())
    blob[20] ^= 0xFF      # 改 time_base_value → CRC 对不上
    try:
        GctDocument.from_bytes(bytes(blob))
    except GctError as exc:
        assert "CRC" in str(exc)
    else:
        raise AssertionError("头被改坏却没有报错")


def test_reader_rejects_missing_end() -> None:
    doc = GctDocument()
    chord = doc.add_chord("G")
    payload = chord.to_bytes(2, "little") + bytes([DIR_DOWN])
    # End 和上一个事件同 tick，这样 End 事件正好占 5 字节：varint(0)+type+voice+len
    doc.events = [
        ChordRow(0, EV_CHORD_STRUM, 0, payload),
        ChordRow(0, EV_END),
    ]
    blob = doc.to_bytes()
    broken = blob[:-5]
    try:
        GctDocument.from_bytes(broken)
    except GctError as exc:
        assert "End" in str(exc)
    else:
        raise AssertionError("缺少 End 却没有报错")


def test_reader_rejects_unknown_chord_id() -> None:
    writer = GctWriter()
    writer.add_chord("G")          # 只有 id 1
    # 绕过 writer 的校验直接写事件：模拟别人手工拼出来的坏文件
    writer.add_event(0, EV_CHORD_STRUM, 0, (2).to_bytes(2, "little") + b"\0")
    try:
        GctDocument.from_bytes(writer.build())
    except GctError as exc:
        assert "和弦表" in str(exc)
    else:
        raise AssertionError("引用了不存在的 chord_id 却没有报错")


def test_document_rejects_duplicate_chord_names() -> None:
    doc = GctDocument()
    doc.add_chord("G")
    doc.chords.append(type(doc.chords[0])(chord_id=99, name="G"))
    try:
        doc.to_bytes()
    except GctError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复和弦名没有被拒绝")


def test_document_rejects_event_with_unknown_chord() -> None:
    doc = GctDocument()
    doc.add_chord("G")
    doc.events = [ChordRow(0, EV_CHORD_STRUM, 0, (99).to_bytes(2, "little") + b"\0")]
    try:
        doc.to_bytes()
    except GctError as exc:
        assert "和弦表" in str(exc)
    else:
        raise AssertionError("事件引用了不存在的和弦却没有报错")


def test_tempo_events_affect_playback_seconds() -> None:
    doc = GctDocument(default_tempo_us=500_000)     # 120 BPM
    chord = doc.add_chord("G")
    doc.events = [
        ChordRow(0, EV_CHORD_STRUM, 0, chord.to_bytes(2, "little") + bytes([DIR_DOWN])),
        # 第 1 拍起改成 60 BPM（1 拍 = 1s）
        ChordRow(480, EV_TEMPO, 0, (1_000_000).to_bytes(4, "little")),
        ChordRow(960, EV_CHORD_STRUM, 0, chord.to_bytes(2, "little") + bytes([DIR_UP])),
        ChordRow(1440, EV_END),
    ]
    blob = doc.to_bytes()
    parsed = GctDocument.from_bytes(blob)

    # 0–1 拍：0.5s；1–2 拍：1s（Tempo 生效）
    assert abs(tick_to_seconds(parsed, 480) - 0.5) < 1e-9
    assert abs(tick_to_seconds(parsed, 960) - 1.5) < 1e-9
    assert seconds_to_tick(parsed, 1.5) == 960


def test_chord_events_sorted_and_filtered() -> None:
    doc = GctDocument()
    chord = doc.add_chord("G")
    payload = chord.to_bytes(2, "little") + bytes([DIR_DOWN])
    doc.events = [
        ChordRow(960, EV_CHORD_STRUM, 0, payload),
        ChordRow(0, EV_CHORD_STRUM, 0, payload),
        ChordRow(480, EV_TEMPO, 0, (500_000).to_bytes(4, "little")),
        ChordRow(1200, EV_END),
    ]
    assert [ev.tick for ev in doc.chord_events()] == [0, 960], "只留和弦事件并按 tick 排序"
    assert doc.counts() == {"Strum": 2, "Tempo": 1, "End": 1}


def test_demo_has_up_strum_and_set() -> None:
    doc = demo_document()
    dirs = [ev.direction for ev in doc.chord_events() if ev.event_type == EV_CHORD_STRUM]
    assert DIR_UP in dirs and DIR_DOWN in dirs, "示例要覆盖上扫和下扫"
    labels = doc.counts()
    assert labels["Set"] == 1


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
