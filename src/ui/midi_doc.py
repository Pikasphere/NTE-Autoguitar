"""MIDI 文档模型：把 ``src/midi.py`` 读出来的原始事件整理成「能画能放」的时间轴。

和 ``gtab_doc`` / ``gct_doc`` 一样，这一层不依赖 Flet，可以直接单测：

- 音符按 tick 存，界面一律用「拍」（1 拍 = 四分音符 = ``division`` tick）；
- 时间换算 ``seconds_at`` 会把 **tempo 变化** 算进去（MIDI 的 tempo 单位是
  微秒/四分音符，和 gct 的 Tempo 事件一个意思，但事件编码不同）；
- 音名用科学记法：MIDI 60 = C4（和 ``src/midi.py`` 的注释一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import midi

__all__ = [
    "DEFAULT_TEMPO_US",
    "MidiDocument",
    "NoteEvent",
    "TempoMark",
    "demo_document",
    "note_name",
]

DEFAULT_TEMPO_US = 500_000  # 120 BPM

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(note: int) -> str:
    """MIDI 音高 → 音名（60 = C4）。"""
    value = int(note)
    return f"{_NOTE_NAMES[value % 12]}{value // 12 - 1}"


@dataclass(frozen=True)
class NoteEvent:
    start: int
    end: int
    note: int
    velocity: int = 100
    channel: int = 0

    @property
    def name(self) -> str:
        return note_name(self.note)

    @property
    def duration(self) -> int:
        return max(1, self.end - self.start)


@dataclass(frozen=True)
class TempoMark:
    tick: int
    tempo_us: int

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.tempo_us if self.tempo_us > 0 else 0.0


@dataclass
class MidiDocument:
    """一个可读的 MIDI：音符 + tempo 变化 + 头部信息。"""

    format: int = 1
    tracks: int = 1
    division: int = 480
    name: str = ""
    notes: list[NoteEvent] = field(default_factory=list)
    tempos: list[TempoMark] = field(default_factory=list)
    end_tick: int = 0
    unclosed: int = 0

    # ---------------------------------------------------------------- 读取

    @classmethod
    def from_bytes(cls, blob: bytes) -> "MidiDocument":
        try:
            raw = midi.parse(blob)
        except midi.MidiError as exc:
            raise midi.MidiError(f"不是有效的 MIDI：{exc}") from exc
        return cls.from_raw(raw)

    @classmethod
    def from_path(cls, path: str | Path) -> "MidiDocument":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_raw(cls, raw: midi.MidiFile) -> "MidiDocument":
        return cls(
            format=raw.format,
            tracks=raw.tracks,
            division=max(1, raw.division),
            name=raw.name,
            notes=[
                NoteEvent(
                    start=item.start,
                    end=max(item.end, item.start + 1),
                    note=item.note,
                    velocity=item.velocity,
                    channel=item.channel,
                )
                for item in raw.notes
            ],
            tempos=sorted(
                (TempoMark(tick=item.tick, tempo_us=item.tempo_us) for item in raw.tempos),
                key=lambda mark: mark.tick,
            )
            or [TempoMark(0, DEFAULT_TEMPO_US)],
            end_tick=max(0, raw.end_tick),
            unclosed=raw.unclosed,
        )

    # ---------------------------------------------------------------- 时间轴

    def beats_at(self, tick: int) -> float:
        return max(0, int(tick)) / max(1, self.division)

    def tick_at(self, beats: float) -> int:
        return max(0, round(float(beats) * max(1, self.division)))

    @property
    def duration_beats(self) -> float:
        return self.beats_at(max(self.end_tick, max((n.end for n in self.notes), default=0)))

    def seconds_at(self, tick: int) -> float:
        """tick → 秒（把 tempo 变化算进去）。"""
        target = max(0, int(tick))
        seconds = 0.0
        previous = 0
        # MIDI 的默认速度是 120 BPM：第一个 tempo 事件之前都按它算。
        # （tempo 不一定从 tick 0 开始，别拿 tempos[0] 当整首的基准）
        tempo_us = DEFAULT_TEMPO_US
        for mark in self.tempos:
            if mark.tick > target:
                break
            seconds += (mark.tick - previous) * (tempo_us / 1_000_000) / max(1, self.division)
            previous = mark.tick
            tempo_us = mark.tempo_us or DEFAULT_TEMPO_US
        seconds += (target - previous) * (tempo_us / 1_000_000) / max(1, self.division)
        return seconds

    @property
    def duration_seconds(self) -> float:
        return self.seconds_at(max(self.end_tick, max((n.end for n in self.notes), default=0)))

    def tick_at_seconds(self, seconds: float) -> int:
        """秒 → tick（``seconds_at`` 的反函数，播放游标用）。"""
        target = max(0.0, float(seconds))
        tick = 0
        elapsed = 0.0
        tempo_us = DEFAULT_TEMPO_US
        division = max(1, self.division)
        for mark in self.tempos:
            if mark.tick <= tick:
                tempo_us = mark.tempo_us or DEFAULT_TEMPO_US
                continue
            span = (mark.tick - tick) * (tempo_us / 1_000_000) / division
            if elapsed + span > target:
                break
            elapsed += span
            tick = mark.tick
            tempo_us = mark.tempo_us or DEFAULT_TEMPO_US
        remaining = target - elapsed
        tick += int(round(remaining / ((tempo_us / 1_000_000) / division)))
        return max(0, tick)

    def tempo_at(self, tick: int) -> int:
        """``tick`` 处生效的 tempo（微秒/四分音符）；第一个事件前是 120 BPM。"""
        value = DEFAULT_TEMPO_US
        target = max(0, int(tick))
        for mark in self.tempos:
            if mark.tick > target:
                break
            value = mark.tempo_us or DEFAULT_TEMPO_US
        return value

    def to_bytes(self) -> bytes:
        """写回 SMF（format 0、单轨）：示例文件 / 测试数据用。"""
        return midi.build(
            [(note.start, note.end, note.note, note.velocity) for note in self.notes],
            tempos=[(mark.tick, mark.tempo_us) for mark in self.tempos],
            division=max(1, self.division),
            name=self.name,
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_bytes(self.to_bytes())
        return target

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.tempo_at(0)

    @property
    def note_range(self) -> tuple[int, int]:
        if not self.notes:
            return (0, 0)
        return (min(n.note for n in self.notes), max(n.note for n in self.notes))

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for note in self.notes:
            result[note.name] = result.get(note.name, 0) + 1
        return result


# ---------------------------------------------------------------- 示例


def demo_document() -> MidiDocument:
    """示例：右手一段音阶 + 左手两个三和弦（覆盖重叠音与跨八度）。

    120 BPM、480 tick/拍；第 8 拍起改成 180 BPM，用来验证 tempo 换算。
    """
    division = 480
    beat = division
    notes: list[NoteEvent] = []

    scale = [60, 62, 64, 65, 67, 69, 71, 72]  # C4 D4 E4 F4 G4 A4 B4 C5
    for index, pitch in enumerate(scale):
        start = index * beat
        notes.append(NoteEvent(start, start + beat // 2, pitch, 100, 0))

    # 左手：C 大三和弦（C3 E3 G3）、G 大三和弦（G3 B3 D4）
    for offset, chord in enumerate(((48, 52, 55), (55, 59, 62))):
        start = offset * 4 * beat
        for pitch in chord:
            notes.append(NoteEvent(start, start + 4 * beat, pitch, 80, 1))

    notes.sort(key=lambda note: (note.start, note.note))
    return MidiDocument(
        format=0,
        tracks=1,
        division=division,
        name="示例（音阶 + 和弦）",
        notes=notes,
        tempos=[TempoMark(0, DEFAULT_TEMPO_US), TempoMark(8 * beat, 333_333)],
        end_tick=9 * beat,
    )
