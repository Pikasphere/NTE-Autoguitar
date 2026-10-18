"""把 gct 和弦谱合成成 WAV，供 GCT 编辑页试听（复用 ``audio_preview`` 的合成器）。

和 gtab 不一样，**gct 里没有音长**：一个和弦响多久完全由播放器定，所以试听这里用
一个固定余音 ``RING_SECONDS``。扫弦的六根弦之间隔 ``STRUM_GAP`` 依次发声
（**下扫 6 → 1 弦，上扫 1 → 6 弦**，和游戏里的扫弦方向一致），单弦拨奏只发那一根，
``Set``（只换指位）不出声。

音高来自 ``chord.json`` 的 ``CHORDS_MAPPINGS``（按 **1 弦 → 6 弦** 排列，``-1`` =
这根弦不发声）。表里没有指法的和弦**跳过**并出现在返回的 ``skipped`` 里——
这只影响试听，手柄回放的位置仍由名字顺序决定，不受影响。

时间换算走 ``gct_doc.tick_to_seconds``（gct 的 Tempo 是 ``0x04``，和 gtab 不同）。
"""

from __future__ import annotations

from ui import audio_preview
from ui.chords import MUTED, ChordTable, load_chord_table
from ui.gct_doc import DIR_UP, EV_CHORD_PLUCK, EV_CHORD_SET, EV_CHORD_STRUM, GctDocument
from ui.gct_doc import tick_to_seconds

__all__ = [
    "RING_SECONDS",
    "STRUM_GAP",
    "chord_notes",
    "preview_seconds",
    "render_wav",
]

#: 一个和弦（或一根弦）响多久。gct 文件里没有音长，试听只能给个固定余音；
#: 相邻和弦会自然叠一点，听起来才像连着的和弦进行。
RING_SECONDS = 0.8
#: 扫弦时相邻两根弦的间隔（和游戏里 CHORD_STRUM_SWEEP=20ms 一个意思）
STRUM_GAP = 0.02


def chord_notes(
    doc: GctDocument, table: ChordTable | None = None
) -> tuple[list[tuple[float, float, int, int]], list[str]]:
    """和弦事件 → ``(start_s, end_s, 弦, 品)`` 列表，以及被跳过的和弦名。

    返回的时间轴已经按开始时间排好，可以直接喂给 ``audio_preview.render_notes``。
    """
    table = table if table is not None else load_chord_table()
    notes: list[tuple[float, float, int, int]] = []
    skipped: list[str] = []

    for ev in doc.chord_events():
        name = doc.chord_name(ev.chord_id)
        fingering = table.fingering(name) if name else None
        if fingering is None:
            label = name or f"#{ev.chord_id}"
            if label not in skipped:
                skipped.append(label)
            continue

        start = tick_to_seconds(doc, ev.tick)
        if ev.event_type == EV_CHORD_STRUM:
            # 下扫 6 → 1 弦；上扫 1 → 6 弦。静音的弦（-1）跳过。
            strings = range(1, 7) if ev.direction == DIR_UP else range(6, 0, -1)
            for index, string in enumerate(strings):
                fret = fingering[string - 1]
                if fret == MUTED:
                    continue
                offset = index * STRUM_GAP
                notes.append(
                    (start + offset, start + offset + RING_SECONDS, string, fret)
                )
        elif ev.event_type == EV_CHORD_PLUCK:
            string = max(1, min(6, ev.string or 1))
            fret = fingering[string - 1]
            if fret != MUTED:
                notes.append((start, start + RING_SECONDS, string, fret))
        elif ev.event_type == EV_CHORD_SET:
            continue  # 只换指位，不出声

    notes.sort(key=lambda item: item[0])
    return notes, skipped


def preview_seconds(doc: GctDocument) -> float:
    """整段试听的长度（秒）：最后一个和弦的起点 + 余音 + 尾部留白。"""
    last = max(
        (tick_to_seconds(doc, ev.tick) for ev in doc.chord_events()), default=0.0
    )
    return min(
        audio_preview.MAX_SECONDS,
        audio_preview.LEAD_IN_SECONDS + last + RING_SECONDS + audio_preview.TAIL_SECONDS,
    )


def render_wav(
    doc: GctDocument,
    table: ChordTable | None = None,
    sample_rate: int = audio_preview.SAMPLE_RATE,
) -> bytes:
    """和弦谱 → 16-bit 单声道 WAV 字节（没有可发声的和弦时是一段静音）。"""
    notes, _ = chord_notes(doc, table)
    return audio_preview.render_notes(notes, preview_seconds(doc), sample_rate)
