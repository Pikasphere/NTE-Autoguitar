"""GCT 文档模型：解析 / 校验 / 序列化，不含任何界面代码。

GCT 是「和弦谱」：事件只说明**什么时候弹哪个和弦**（扫弦 / 单弦拨奏 / 只切指位），
不像 gtab 那样有 NoteOn/NoteOff 和音长。和弦的「页号 + 摇杆方向」由
``ui/chords.py`` 按 ``chord.json`` 里的名字顺序换算。

约定：
- 事件在模型里保存「绝对 tick」，写文件时换算成 delta；
- 保存时自动补 End（幂等），并回读一遍做严格校验；
- 和弦表按文件里的顺序保存（名字才是跨文件稳定的东西）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gct import (
    EV_CHORD_PLUCK,
    EV_CHORD_SET,
    EV_CHORD_STRUM,
    EV_END,
    EV_TEMPO,
    EVENT_LABELS,
    DIR_DOWN,
    DIR_UP,
    GctError,
    GctReader,
    GctWriter,
)
from ui.gtab_doc import MetaEntry

__all__ = [
    "CHORD_EVENT_TYPES",
    "ChordEntry",
    "ChordRow",
    "GctDocument",
    "GctError",
    "demo_document",
    "seconds_to_tick",
    "tick_to_seconds",
]

CHORD_EVENT_TYPES = frozenset({EV_CHORD_STRUM, EV_CHORD_PLUCK, EV_CHORD_SET})

DIRECTION_NAMES: dict[int, str] = {DIR_DOWN: "下扫", DIR_UP: "上扫"}


# ---------------------------------------------------------------- 事件


@dataclass
class ChordRow:
    """一条和弦事件；``tick`` 是绝对位置。"""

    tick: int = 0
    event_type: int = EV_CHORD_STRUM
    voice: int = 0
    data: bytes = b""

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.event_type, f"0x{self.event_type:02X}")

    @property
    def chord_id(self) -> int:
        if self.event_type in (EV_CHORD_STRUM, EV_CHORD_PLUCK, EV_CHORD_SET):
            return int.from_bytes(self.data[:2], "little")
        return 0

    def set_chord_id(self, chord_id: int) -> None:
        data = bytearray(self.data or b"\0\0")
        data[0:2] = int(chord_id).to_bytes(2, "little")
        self.data = bytes(data)

    @property
    def direction(self) -> int:
        if self.event_type == EV_CHORD_STRUM and len(self.data) >= 3:
            return self.data[2]
        return DIR_DOWN

    @property
    def string(self) -> int:
        if self.event_type == EV_CHORD_PLUCK and len(self.data) >= 3:
            return self.data[2]
        return 0

    @property
    def tempo_us(self) -> int:
        if self.event_type == EV_TEMPO:
            return int.from_bytes(self.data[:4].ljust(4, b"\0"), "little")
        return 0

    @property
    def hex_data(self) -> str:
        return self.data.hex()

    @property
    def is_chord_event(self) -> bool:
        return self.event_type in CHORD_EVENT_TYPES

    def copy(self) -> "ChordRow":
        return ChordRow(self.tick, self.event_type, self.voice, bytes(self.data))


# ---------------------------------------------------------------- 和弦表


@dataclass
class ChordEntry:
    """和弦表里的一条：id 只在文件内部有效，名字才跨文件稳定。"""

    chord_id: int = 1
    name: str = ""


# ---------------------------------------------------------------- 文档


@dataclass
class GctDocument:
    """一个可读写的 gct 和弦谱。"""

    string_count: int = 6
    time_base_type: int = 0
    time_base_value: int = 480
    default_tempo_us: int = 500_000
    flags: int = 0
    chords: list[ChordEntry] = field(default_factory=list)
    meta: list[MetaEntry] = field(default_factory=list)
    events: list[ChordRow] = field(default_factory=list)

    # ---------------- 读取 ----------------

    @classmethod
    def from_bytes(cls, blob: bytes) -> "GctDocument":
        try:
            reader = GctReader(bytes(blob), strict=True)
        except (ValueError, EOFError) as exc:
            raise GctError(f"不是有效的 gct 文件：{exc}") from exc

        try:
            header = reader.header
            events = [
                ChordRow(ev.tick, ev.event_type, ev.voice, bytes(ev.data))
                for ev in reader.events()
            ]
            meta = [
                MetaEntry(meta_type, bytes(value))
                for meta_type, values in reader.meta.items()
                for value in values
            ]
            chords = [ChordEntry(cid, name) for cid, name in reader.chords.items()]
        except (ValueError, EOFError) as exc:
            raise GctError(f"gct 事件流损坏：{exc}") from exc
        finally:
            reader.close()

        meta.sort(key=lambda entry: entry.meta_type)
        return cls(
            string_count=header.string_count,
            time_base_type=header.time_base_type,
            time_base_value=header.time_base_value,
            default_tempo_us=header.default_tempo_us,
            flags=header.flags,
            chords=chords,
            meta=meta,
            events=events,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "GctDocument":
        return cls.from_bytes(Path(path).read_bytes())

    # ---------------- 写入 ----------------

    def to_bytes(self, validate: bool = True) -> bytes:
        """编码成 gct 字节流；validate=True 时回读一遍确认能被严格解析。"""
        self._validate()

        writer = GctWriter(
            string_count=self.string_count,
            time_base_type=self.time_base_type,
            time_base_value=self.time_base_value,
            default_tempo_us=self.default_tempo_us,
            flags=self.flags,
        )
        for entry in self.chords:
            writer.add_chord(entry.name, entry.chord_id)
        for entry in self.meta:
            writer.add_meta(entry.meta_type, entry.data)

        prev_tick = 0
        for index, ev in enumerate(self.ordered_events(), start=1):
            delta = ev.tick - prev_tick
            if delta < 0:
                raise GctError(f"第 {index} 个事件的 Tick 早于前一个事件")
            self._write_event(writer, ev, delta, index)
            prev_tick = ev.tick

        blob = writer.build()
        if validate:
            self._validate_blob(blob)
        return blob

    def save(self, path: str | Path) -> bytes:
        blob = self.to_bytes()
        Path(path).write_bytes(blob)
        return blob

    def ordered_events(self) -> list[ChordRow]:
        """写入顺序：按 tick 稳定排序，End 永远排在最后。"""
        body = [ev for ev in self.events if ev.event_type != EV_END]
        body.sort(key=lambda ev: ev.tick)
        last_tick = max((ev.tick for ev in body), default=0)
        ends = [ev for ev in self.events if ev.event_type == EV_END]
        end = ends[0].copy() if ends else ChordRow(last_tick, EV_END)
        end.tick = max(end.tick, last_tick)
        return [*body, end]

    def _write_event(self, writer: GctWriter, ev: ChordRow, delta: int, index: int) -> None:
        where = f"第 {index} 个事件（{ev.label}）"
        try:
            if ev.event_type == EV_CHORD_STRUM:
                writer.strum(delta, ev.chord_id, ev.direction, ev.voice)
            elif ev.event_type == EV_CHORD_PLUCK:
                writer.pluck(delta, ev.chord_id, ev.string, ev.voice)
            elif ev.event_type == EV_CHORD_SET:
                writer.set_chord(delta, ev.chord_id, ev.voice)
            elif ev.event_type == EV_TEMPO:
                if len(ev.data) != 4:
                    raise ValueError("Tempo 事件需要 4 字节（微秒/拍）")
                writer.tempo(delta, ev.tempo_us, ev.voice)
            elif ev.event_type == EV_END:
                writer.end(delta, ev.voice)
            else:
                writer.add_event(delta, ev.event_type, ev.voice, ev.data)
        except ValueError as exc:
            raise GctError(f"{where}不合法：{exc}") from exc

    # ---------------- 校验 ----------------

    def _validate(self) -> None:
        if not (1 <= self.string_count <= 255):
            raise GctError("弦数必须在 1..255 之间")
        if self.time_base_value < 1:
            raise GctError("time_base_value 必须大于 0")
        if self.default_tempo_us <= 0:
            raise GctError("默认速度必须大于 0 微秒/拍")

        ids: set[int] = set()
        names: set[str] = set()
        for entry in self.chords:
            if entry.chord_id == 0:
                raise GctError("和弦 id 0 保留，不能使用")
            if not (0 < entry.chord_id <= 0xFFFF):
                raise GctError(f"和弦 id {entry.chord_id} 超出 1..65535")
            if entry.chord_id in ids:
                raise GctError(f"和弦 id {entry.chord_id} 重复")
            if not entry.name:
                raise GctError(f"和弦 id {entry.chord_id} 没有名字")
            if entry.name in names:
                raise GctError(f"和弦名 {entry.name!r} 重复")
            ids.add(entry.chord_id)
            names.add(entry.name)

        for ev in self.events:
            if ev.tick < 0:
                raise GctError("事件的 Tick 不能为负数")
            if ev.is_chord_event and ev.chord_id not in ids:
                raise GctError(f"{ev.label} 引用了不在和弦表里的 id {ev.chord_id}")

    @staticmethod
    def _validate_blob(blob: bytes) -> None:
        try:
            reader = GctReader(blob, strict=True)
            for _event in reader.events():
                pass
            reader.close()
        except (ValueError, EOFError) as exc:
            raise GctError(f"写出的 gct 回读失败：{exc}") from exc

    # ---------------- 和弦表辅助 ----------------

    def add_chord(self, name: str, chord_id: int | None = None) -> int:
        if not name:
            raise GctError("和弦名不能为空")
        if any(entry.name == name for entry in self.chords):
            return next(entry.chord_id for entry in self.chords if entry.name == name)
        if chord_id is None:
            chord_id = max((entry.chord_id for entry in self.chords), default=0) + 1
        self.chords.append(ChordEntry(chord_id, name))
        return chord_id

    def chord_name(self, chord_id: int) -> str:
        for entry in self.chords:
            if entry.chord_id == chord_id:
                return entry.name
        return ""

    def chord_id(self, name: str) -> int | None:
        for entry in self.chords:
            if entry.name == name:
                return entry.chord_id
        return None

    def chord_names(self) -> list[str]:
        return [entry.name for entry in self.chords]

    def chord_events(self) -> list[ChordRow]:
        """只返回和弦事件（不含 Tempo / End），按 tick 排序。"""
        return sorted(
            (ev for ev in self.events if ev.is_chord_event), key=lambda ev: ev.tick
        )

    # ---------------- 时间轴 ----------------

    def beats_at(self, tick: int) -> float:
        return max(0, int(tick)) / max(1, self.time_base_value)

    def tick_at(self, beats: float) -> int:
        return max(0, round(float(beats) * max(1, self.time_base_value)))

    @property
    def duration_beats(self) -> float:
        return max((self.beats_at(ev.tick) for ev in self.events), default=0.0)

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.default_tempo_us

    @bpm.setter
    def bpm(self, value: float) -> None:
        if value <= 0:
            raise GctError("BPM 必须大于 0")
        self.default_tempo_us = max(1, round(60_000_000 / value))

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for ev in self.events:
            result[ev.label] = result.get(ev.label, 0) + 1
        return result


# ---------------------------------------------------------------- 时间换算


def tick_to_seconds(doc: GctDocument, tick: int) -> float:
    """tick → 播放用的真实秒数（把 Tempo 事件算进去）。"""
    target = max(0, int(tick))
    seconds = 0.0
    prev_tick = 0
    tempo_us = doc.default_tempo_us
    ticks_per_beat = max(1, doc.time_base_value)
    tempo_events = sorted(
        (ev.tick, ev.tempo_us)
        for ev in doc.events
        if ev.event_type == EV_TEMPO and ev.tempo_us > 0
    )
    for start, block_tempo in tempo_events:
        if start > target:
            break
        seconds += (start - prev_tick) * (tempo_us / 1_000_000) / ticks_per_beat
        prev_tick, tempo_us = start, block_tempo
    seconds += (target - prev_tick) * (tempo_us / 1_000_000) / ticks_per_beat
    return seconds


def seconds_to_tick(doc: GctDocument, seconds: float) -> int:
    """``tick_to_seconds`` 的反函数。"""
    target = max(0.0, float(seconds))
    tick = 0
    elapsed = 0.0
    tempo_us = doc.default_tempo_us
    ticks_per_beat = max(1, doc.time_base_value)
    tempo_events = sorted(
        (ev.tick, ev.tempo_us)
        for ev in doc.events
        if ev.event_type == EV_TEMPO and ev.tempo_us > 0
    )
    for start, block_tempo in tempo_events:
        if start <= tick:
            tempo_us = block_tempo
            continue
        span = (start - tick) * (tempo_us / 1_000_000) / ticks_per_beat
        if elapsed + span > target:
            break
        elapsed += span
        tick = start
        tempo_us = block_tempo
    remaining = target - elapsed
    tick += int(round(remaining / ((tempo_us / 1_000_000) / ticks_per_beat)))
    return max(0, tick)


# ---------------------------------------------------------------- 示例


def demo_document() -> GctDocument:
    """示例谱：G C D Em Am Dm G7 C，一拍一个下扫，最后一拍上扫。

    覆盖第 1 / 2 / 4 页和两个扫弦方向，够试出翻页与扫弦。
    """
    doc = GctDocument(default_tempo_us=500_000)   # 120 BPM，1 拍 = 0.5s
    progression = ["G", "C", "D", "Em", "Am", "Dm", "G7", "C"]
    ids = {name: doc.add_chord(name) for name in dict.fromkeys(progression)}

    beat = doc.time_base_value
    for index, name in enumerate(progression):
        doc.events.append(
            ChordRow(
                tick=index * beat,
                event_type=EV_CHORD_STRUM,
                data=ids[name].to_bytes(2, "little") + bytes([DIR_DOWN]),
            )
        )
    # 最后一拍：先「只切指位」，再上扫（顺便试 Set 事件）
    last = ids["C"]
    doc.events.append(
        ChordRow(
            tick=len(progression) * beat - beat // 4,
            event_type=EV_CHORD_SET,
            data=last.to_bytes(2, "little"),
        )
    )
    doc.events.append(
        ChordRow(
            tick=len(progression) * beat,
            event_type=EV_CHORD_STRUM,
            data=last.to_bytes(2, "little") + bytes([DIR_UP]),
        )
    )
    doc.events.append(ChordRow(tick=(len(progression) + 1) * beat, event_type=EV_END))
    return doc
