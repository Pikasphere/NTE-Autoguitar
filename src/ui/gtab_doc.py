"""GTAB 文档模型：解析 / 校验 / 序列化，不含任何界面代码。

界面层（ui/pages/gtab_page.py）只与本模块打交道，因此以后把 GTAB 页面
挂到别的主界面、或者改成命令行工具时，读写逻辑都不用重写。

约定：
- 事件在模型里保存的是「绝对 tick」，写文件时由本模块换算成 delta。
- 元数据取值既可以是文本，也可以写成 ``hex:`` 开头的十六进制串。
- 音符以「NoteOn + 音长」的形式编辑：``note_pairs()`` 返回 On/Off 行的引用，
  保存时缺 NoteOff 会按 1/4 拍自动补（``to_bytes`` → ``ensure_note_offs``）；
  孤立 NoteOff 不会被静默删除，而是保存时报错指出位置。
- 保存时自动保证末尾有且只有一个 End 事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gtab import (
    EV_END,
    EV_MARKER,
    EV_SNAPSHOT,
    EV_STRING_NOTE_OFF,
    EV_STRING_NOTE_ON,
    EV_TEMPO,
    GtabReader,
    GtabWriter,
)

__all__ = [
    "DEFAULT_NOTE_DURATION_BEATS",
    "EVENT_LABELS",
    "NOTE_EVENTS",
    "TYPE_BY_LABEL",
    "EventRow",
    "GtabDocument",
    "GtabError",
    "MetaEntry",
    "NoteEntry",
    "default_event",
    "seconds_to_tick",
    "tick_to_seconds",
]

# 新音符的默认音长（拍）。界面里的「添加音符」和模型层的 add_note 都用它。
DEFAULT_NOTE_DURATION_BEATS = 1.0

EVENT_LABELS: dict[int, str] = {
    EV_STRING_NOTE_ON: "NoteOn",
    EV_STRING_NOTE_OFF: "NoteOff",
    EV_TEMPO: "Tempo",
    EV_MARKER: "Marker",
    EV_SNAPSHOT: "Snapshot",
    EV_END: "End",
}

TYPE_BY_LABEL: dict[str, int] = {label: t for t, label in EVENT_LABELS.items()}

NOTE_EVENTS = frozenset({EV_STRING_NOTE_ON, EV_STRING_NOTE_OFF})


class GtabError(ValueError):
    """gtab 数据不合法，或文件无法解析。"""


# ---------------------------------------------------------------- 元数据


@dataclass
class MetaEntry:
    """一条 meta TLV：类型 + 原始字节。"""

    meta_type: int = 0
    data: bytes = b""

    def display_value(self) -> str:
        """给界面看的取值：能当文本读就用文本，否则用 hex: 前缀。"""
        try:
            text = self.data.decode("utf-8")
        except UnicodeDecodeError:
            return "hex:" + self.data.hex()
        if text and all(ch.isprintable() for ch in text):
            return text
        return "hex:" + self.data.hex()

    @staticmethod
    def parse_value(text: str) -> bytes:
        """把界面输入还原成字节；``hex:`` 前缀表示十六进制。"""
        text = text.strip()
        if text.lower().startswith("hex:"):
            raw = text[4:].replace(" ", "")
            try:
                return bytes.fromhex(raw)
            except ValueError as exc:
                raise GtabError(f"不是合法的十六进制：{text!r}") from exc
        return text.encode("utf-8")

    def copy(self) -> "MetaEntry":
        return MetaEntry(self.meta_type, bytes(self.data))


# ---------------------------------------------------------------- 事件


@dataclass
class EventRow:
    """一个演奏事件；tick 是绝对位置。"""

    tick: int = 0
    event_type: int = EV_STRING_NOTE_ON
    # 事件头里的 1 字节「声部号」：格式没定义语义，只作为 NoteOn/NoteOff 的配对键
    voice: int = 0
    data: bytes = b""

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.event_type, f"0x{self.event_type:02X}")

    @property
    def is_note(self) -> bool:
        return self.event_type in NOTE_EVENTS

    @property
    def string(self) -> int:
        return self.data[0] if len(self.data) >= 1 else 1

    @property
    def fret(self) -> int:
        return self.data[1] if len(self.data) >= 2 else 0

    def set_note(self, string: int, fret: int) -> None:
        self.data = bytes([max(0, min(255, int(string))), max(0, min(255, int(fret)))])

    @property
    def tempo_us(self) -> int:
        return int.from_bytes(self.data[:4].ljust(4, b"\0"), "little")

    @property
    def hex_data(self) -> str:
        return self.data.hex()

    def copy(self) -> "EventRow":
        return EventRow(self.tick, self.event_type, self.voice, bytes(self.data))


def default_event(event_type: int, tick: int) -> EventRow:
    """新建某类型事件时给一个合理初值。"""
    if event_type == EV_STRING_NOTE_OFF:
        return EventRow(tick, event_type, 0, bytes([1, 0]))
    if event_type == EV_TEMPO:
        return EventRow(tick, event_type, 0, (500_000).to_bytes(4, "little"))
    if event_type in (EV_MARKER, EV_SNAPSHOT):
        return EventRow(tick, event_type, 0, bytes(8))
    if event_type == EV_END:
        return EventRow(tick, event_type, 0, b"")
    return EventRow(tick, event_type, 0, bytes([1, 0]))


# ---------------------------------------------------------------- 音符


@dataclass
class NoteEntry:
    """一个音 = NoteOn 行 + 配对的 NoteOff 行。

    界面只编辑「音长」，NoteOff 由模型维护；所以这里同时持有两半的
    :class:`EventRow` 引用（不是拷贝）：改这里就是改文档里的事件行。

    ``off is None`` 表示这个 NoteOn 还没有配对的 NoteOff（孤立 On）。
    显示时用 ``placeholder_ticks`` 给它一个占位长度，保存时 ``to_bytes()``
    会自动补一个真正的 NoteOff。
    """

    on: EventRow
    off: EventRow | None = None
    placeholder_ticks: int = 120  # 缺 NoteOff 时的占位长度（默认 1/4 拍）

    @property
    def key(self) -> tuple[int, int, int]:
        """配对键 ``(voice, 弦, 品)``，与 ``note_blocks()`` 一致。"""
        return (self.on.voice, self.on.string, self.on.fret)

    @property
    def voice(self) -> int:
        return self.on.voice

    @property
    def string(self) -> int:
        return self.on.string

    @property
    def fret(self) -> int:
        return self.on.fret

    @property
    def start_tick(self) -> int:
        return max(0, int(self.on.tick))

    @property
    def end_tick(self) -> int:
        if self.off is not None:
            return max(self.start_tick, int(self.off.tick))
        return self.start_tick + max(1, int(self.placeholder_ticks))

    @property
    def duration_ticks(self) -> int:
        return self.end_tick - self.start_tick

    @property
    def missing_off(self) -> bool:
        return self.off is None


# ---------------------------------------------------------------- 文档


@dataclass
class GtabDocument:
    """一个可编辑的 gtab 文档。"""

    string_count: int = 6
    max_fret: int = 24
    time_base_type: int = 0
    time_base_value: int = 480
    default_tempo_us: int = 500_000
    flags: int = 0
    meta: list[MetaEntry] = field(default_factory=list)
    events: list[EventRow] = field(default_factory=list)

    # ---------------- 读取 ----------------

    @classmethod
    def from_bytes(cls, blob: bytes) -> "GtabDocument":
        try:
            reader = GtabReader(bytes(blob), strict=True)
        except (ValueError, EOFError) as exc:
            raise GtabError(f"不是有效的 gtab 文件：{exc}") from exc

        try:
            header = reader.header
            events = [
                EventRow(ev.tick, ev.event_type, ev.voice, bytes(ev.data))
                for ev in reader.events()
            ]
            meta = [
                MetaEntry(meta_type, bytes(value))
                for meta_type, values in reader.meta.items()
                for value in values
            ]
        except (ValueError, EOFError) as exc:
            raise GtabError(f"gtab 事件流损坏：{exc}") from exc
        finally:
            reader.close()

        # 固定按类型排序，读进来的顺序与写出去的顺序保持一致
        meta.sort(key=lambda entry: entry.meta_type)

        return cls(
            string_count=header.string_count,
            max_fret=header.max_fret,
            time_base_type=header.time_base_type,
            time_base_value=header.time_base_value,
            default_tempo_us=header.default_tempo_us,
            flags=header.flags,
            meta=meta,
            events=events,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "GtabDocument":
        return cls.from_bytes(Path(path).read_bytes())

    # ---------------- 写入 ----------------

    def to_bytes(self, validate: bool = True) -> bytes:
        """编码成 gtab 字节流；validate=True 时回读一遍确认能被严格解析。

        写入前会先 :meth:`ensure_note_offs`：界面只编辑音长，缺 NoteOff 的
        NoteOn 在这里按占位长度自动补上（重复调用是幂等的）。孤立 NoteOff
        无法写进严格格式，会抛 :class:`GtabError`（不会静默删除）。
        """
        self._validate_header()
        self.ensure_note_offs()
        self._check_note_pairs()

        writer = GtabWriter(
            string_count=self.string_count,
            max_fret=self.max_fret,
            time_base_type=self.time_base_type,
            time_base_value=self.time_base_value,
            default_tempo_us=self.default_tempo_us,
            flags=self.flags,
        )
        for entry in self.meta:
            if not (0 <= entry.meta_type <= 0xFFFF):
                raise GtabError(f"元数据类型 0x{entry.meta_type:X} 超出 0..0xFFFF")
            writer.add_meta(entry.meta_type, entry.data)

        prev_tick = 0
        for index, ev in enumerate(self.ordered_events(), start=1):
            delta = ev.tick - prev_tick
            if delta < 0:
                raise GtabError(f"第 {index} 个事件的 Tick 早于前一个事件")
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

    def ordered_events(self) -> list[EventRow]:
        """写入顺序：按 tick 稳定排序，End 永远排在最后。

        同一 tick 上 NoteOff 排在 NoteOn 前面：前一个音结束、后一个音在同
        一拍开始时，必须先松开再按下，否则严格解析会认为同一键重复按下。
        """
        body = [ev for ev in self.events if ev.event_type != EV_END]
        body.sort(key=lambda ev: (ev.tick, 0 if ev.event_type == EV_STRING_NOTE_OFF else 1))
        last_tick = max((ev.tick for ev in body), default=0)

        ends = [ev for ev in self.events if ev.event_type == EV_END]
        end = ends[0].copy() if ends else EventRow(last_tick, EV_END)
        end.tick = max(end.tick, last_tick)
        return [*body, end]

    def _write_event(self, writer: GtabWriter, ev: EventRow, delta: int, index: int) -> None:
        where = f"第 {index} 个事件（{ev.label}）"
        try:
            if ev.event_type == EV_STRING_NOTE_ON:
                writer.note_on(delta, ev.string, ev.fret, ev.voice)
            elif ev.event_type == EV_STRING_NOTE_OFF:
                writer.note_off(delta, ev.string, ev.fret, ev.voice)
            elif ev.event_type == EV_TEMPO:
                if len(ev.data) != 4:
                    raise ValueError("Tempo 事件需要 4 字节（微秒/拍）")
                writer.tempo(delta, ev.tempo_us, ev.voice)
            elif ev.event_type == EV_END:
                writer.end(delta, ev.voice)
            else:
                if ev.event_type == EV_MARKER and len(ev.data) != 8:
                    raise ValueError("Marker 事件需要 8 字节数据")
                writer.add_event(delta, ev.event_type, ev.voice, ev.data)
        except ValueError as exc:
            raise GtabError(f"{where}不合法：{exc}") from exc

    # ---------------- 校验 ----------------

    def _validate_header(self) -> None:
        if not (1 <= self.string_count <= 255):
            raise GtabError("弦数必须在 1..255 之间")
        if not (1 <= self.max_fret <= 255):
            raise GtabError("最大品必须在 1..255 之间")
        if self.time_base_value < 1:
            raise GtabError("time_base_value 必须大于 0")
        if self.default_tempo_us <= 0:
            raise GtabError("默认速度必须大于 0 微秒/拍")
        for ev in self.events:
            if ev.tick < 0:
                raise GtabError("事件的 Tick 不能为负数")

    @staticmethod
    def _validate_blob(blob: bytes) -> None:
        try:
            with GtabReader(blob, strict=True) as reader:
                for _ in reader.events():
                    pass
        except (ValueError, EOFError) as exc:
            raise GtabError(f"生成的 gtab 未通过校验：{exc}") from exc

    # ---------------- 节拍换算 ----------------

    # ---------------- 音符编辑（音长 / NoteOff 自动维护） ----------------

    def note_pairs(self, placeholder_ticks: int | None = None) -> list[NoteEntry]:
        """把 NoteOn / NoteOff 配对成 :class:`NoteEntry`（保留 On/Off 行引用）。

        与 :meth:`note_blocks` 的区别：这里返回的是可编辑的「音」，
        带 NoteOn / NoteOff 的 :class:`EventRow` 引用，界面用它显示音长、
        改弦/品/voice、成对删除。配对键仍是 ``(voice, 弦, 品)``。

        - 同一个键被重复按下时，按先后顺序（FIFO）与 NoteOff 配对，
          多出来的 NoteOn 作为「缺 Off」的条目返回；
        - 找不到 NoteOn 的 NoteOff 不会丢：见 :meth:`orphan_note_offs`；
        - 缺 NoteOff 的音用 ``placeholder_ticks``（默认 1/4 拍）作为显示长度。
        """
        if placeholder_ticks is None:
            placeholder_ticks = max(1, self.time_base_value // 4)
        placeholder_ticks = max(1, int(placeholder_ticks))

        pending: dict[tuple[int, int, int], list[EventRow]] = {}
        entries: list[NoteEntry] = []
        for ev in sorted(self.events, key=lambda item: item.tick):
            if ev.event_type == EV_STRING_NOTE_ON:
                pending.setdefault((ev.voice, ev.string, ev.fret), []).append(ev)
            elif ev.event_type == EV_STRING_NOTE_OFF:
                queue = pending.get((ev.voice, ev.string, ev.fret))
                if not queue:
                    continue
                entries.append(NoteEntry(queue.pop(0), ev, placeholder_ticks))

        for queue in pending.values():
            for on in queue:
                entries.append(NoteEntry(on, None, placeholder_ticks))

        entries.sort(key=lambda entry: (entry.start_tick, entry.string, entry.fret))
        return entries

    def orphan_note_offs(self) -> list[EventRow]:
        """找不到配对 NoteOn 的 NoteOff 行（原样返回，不删除）。"""
        pending: dict[tuple[int, int, int], int] = {}
        orphans: list[EventRow] = []
        for ev in sorted(self.events, key=lambda item: item.tick):
            key = (ev.voice, ev.string, ev.fret)
            if ev.event_type == EV_STRING_NOTE_ON:
                pending[key] = pending.get(key, 0) + 1
            elif ev.event_type == EV_STRING_NOTE_OFF:
                if pending.get(key, 0) > 0:
                    pending[key] -= 1
                else:
                    orphans.append(ev)
        return orphans

    def add_note(
        self,
        tick: int,
        string: int,
        fret: int,
        voice: int = 0,
        duration_ticks: int | None = None,
    ) -> NoteEntry:
        """新建一个音（NoteOn + NoteOff），默认 1 拍，返回可编辑条目。"""
        tick = max(0, int(tick))
        if duration_ticks is None:
            duration_ticks = self.tick_at(DEFAULT_NOTE_DURATION_BEATS)
        duration_ticks = max(1, int(duration_ticks))

        on = EventRow(tick, EV_STRING_NOTE_ON, max(0, min(255, int(voice))))
        on.set_note(string, fret)
        off = EventRow(
            tick + duration_ticks,
            EV_STRING_NOTE_OFF,
            on.voice,
            bytes(on.data[:2]),
        )
        self.events.extend([on, off])
        return NoteEntry(on, off, max(1, self.time_base_value // 4))

    def _next_same_key_tick(self, entry: NoteEntry) -> int | None:
        """同一 ``(voice, 弦, 品)`` 上、排在这个音之后的最近起点。"""
        starts = [
            other.start_tick
            for other in self.note_pairs()
            if other.key == entry.key
            and other.on is not entry.on
            and other.start_tick > entry.start_tick
        ]
        return min(starts) if starts else None

    def set_note_duration(self, entry: NoteEntry, ticks: int, clamp: bool = True) -> int:
        """改音长（tick）；缺 NoteOff 时在这里补一个。返回实际生效的音长。

        ``clamp=True`` 时把结束点夹到「同键下一个音」的起点之前——同一个
        ``(voice, 弦, 品)`` 的两个音不允许重叠（严格格式会当成重复按下）。
        """
        ticks = max(1, int(ticks))
        if clamp:
            limit = self._next_same_key_tick(entry)
            if limit is not None:
                ticks = min(ticks, max(1, limit - entry.start_tick))

        end = entry.start_tick + ticks
        if entry.off is None:
            off = EventRow(
                end,
                EV_STRING_NOTE_OFF,
                entry.on.voice,
                bytes(entry.on.data[:2]),
            )
            self.events.append(off)
            entry.off = off
        else:
            entry.off.tick = end
        return end - entry.start_tick

    def move_note(self, entry: NoteEntry, tick: int) -> None:
        """移动整个音（On/Off 一起动，音长不变）。"""
        tick = max(0, int(tick))
        delta = tick - int(entry.on.tick)
        entry.on.tick = tick
        if entry.off is not None:
            entry.off.tick = max(tick, int(entry.off.tick) + delta)

    def set_note_fields(
        self,
        entry: NoteEntry,
        *,
        voice: int | None = None,
        string: int | None = None,
        fret: int | None = None,
    ) -> None:
        """改 voice / 弦 / 品，NoteOn 与 NoteOff 必须同步（否则配不上）。"""
        if voice is not None:
            entry.on.voice = max(0, min(255, int(voice)))
            if entry.off is not None:
                entry.off.voice = entry.on.voice
        if string is not None or fret is not None:
            new_string = entry.string if string is None else int(string)
            new_fret = entry.fret if fret is None else int(fret)
            entry.on.set_note(new_string, new_fret)
            if entry.off is not None:
                entry.off.set_note(new_string, new_fret)

    def delete_note(self, entry: NoteEntry) -> None:
        """成对删除一个音（On 和 Off 一起删）。"""
        doomed = {id(entry.on)}
        if entry.off is not None:
            doomed.add(id(entry.off))
        self.events = [ev for ev in self.events if id(ev) not in doomed]

    def ensure_note_offs(self) -> int:
        """给缺 NoteOff 的 NoteOn 补一个 Off，返回补了几个。

        保存时自动调用；界面也可以手动调用。补的长度用 ``placeholder_ticks``
        （1/4 拍），这样只是“能写出去”，真要长音请在界面上改音长。
        """
        created = 0
        for entry in self.note_pairs():
            if entry.off is None:
                self.set_note_duration(entry, entry.placeholder_ticks)
                created += 1
        return created

    def _check_note_pairs(self) -> None:
        """写入前的音符级检查：孤立 Off、同键重叠、Off 早于 On。"""
        orphans = self.orphan_note_offs()
        if orphans:
            detail = "、".join(
                f"第 {self.beats_at(ev.tick):.2f} 拍（{ev.string} 弦 {ev.fret} 品）"
                for ev in orphans[:4]
            )
            more = f" 等 {len(orphans)} 个" if len(orphans) > 4 else ""
            raise GtabError(
                f"有 {len(orphans)} 个 NoteOff 找不到配对的 NoteOn（{detail}{more}）。"
                "严格格式不允许孤立 NoteOff；请删掉这些行，或把它的音补成完整音符。"
            )

        groups: dict[tuple[int, int, int], list[NoteEntry]] = {}
        for entry in self.note_pairs():
            groups.setdefault(entry.key, []).append(entry)
        for (voice, string, fret), entries in groups.items():
            entries.sort(key=lambda item: item.start_tick)
            previous = entries[0]
            if previous.off is not None and previous.off.tick < previous.on.tick:
                raise GtabError(
                    f"第 {self.beats_at(previous.start_tick):.2f} 拍的 NoteOff 早于 NoteOn"
                )
            for current in entries[1:]:
                if current.start_tick < previous.end_tick:
                    raise GtabError(
                        "同一个 "
                        f"(voice={voice}, 弦={string}, 品={fret}) 上有两个音重叠："
                        f"第 {self.beats_at(previous.start_tick):.2f} 拍"
                        f"（长 {self.beats_at(previous.duration_ticks):.2f} 拍）与"
                        f"第 {self.beats_at(current.start_tick):.2f} 拍。"
                        "同一个键不能同时按住两次，请缩短前一个音或错开位置。"
                    )
                previous = current
            if previous.off is not None and previous.off.tick < previous.on.tick:
                raise GtabError(
                    f"第 {self.beats_at(previous.start_tick):.2f} 拍的 NoteOff 早于 NoteOn"
                )

    def note_blocks(self) -> list[tuple[int, int, int, int]]:
        """把 NoteOn / NoteOff 配对成 (start_tick, end_tick, string, fret)。

        配对键是 ``(voice, string, fret)``（voice 只是事件头里的声部号）。
        没配对的 NoteOn 给 1/4 拍的占位长度。
        """
        active: dict[tuple[int, int, int], int] = {}
        blocks: list[tuple[int, int, int, int]] = []
        for ev in sorted(self.events, key=lambda item: item.tick):
            key = (ev.voice, ev.string, ev.fret)
            if ev.event_type == EV_STRING_NOTE_ON:
                active[key] = ev.tick
            elif ev.event_type == EV_STRING_NOTE_OFF:
                start = active.pop(key, None)
                if start is not None:
                    blocks.append((start, max(start, ev.tick), ev.string, ev.fret))
        fallback = max(1, self.time_base_value // 4)
        for (_voice, string, fret), start in active.items():
            blocks.append((start, start + fallback, string, fret))
        return blocks

    def beats_at(self, tick: int) -> float:
        """tick → 节拍位置（1 拍 = time_base_value 个 tick）。"""
        return max(0, int(tick)) / max(1, self.time_base_value)

    def tick_at(self, beats: float) -> int:
        """节拍位置 → tick（四舍五入）。"""
        return max(0, round(float(beats) * max(1, self.time_base_value)))

    @property
    def duration_beats(self) -> float:
        ticks = [ev.tick for ev in self.events]
        ticks.extend(entry.end_tick for entry in self.note_pairs())
        return max((self.beats_at(tick) for tick in ticks), default=0.0)

    # ---------------- 便捷属性 ----------------

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.default_tempo_us

    @bpm.setter
    def bpm(self, value: float) -> None:
        if value <= 0:
            raise GtabError("BPM 必须大于 0")
        self.default_tempo_us = max(1, round(60_000_000 / value))

    @property
    def event_crc_enabled(self) -> bool:
        return bool(self.flags & 0x01)

    @event_crc_enabled.setter
    def event_crc_enabled(self, value: bool) -> None:
        self.flags = (self.flags | 0x01) if value else (self.flags & ~0x01)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for ev in self.events:
            result[ev.label] = result.get(ev.label, 0) + 1
        return result


def tick_to_seconds(doc: GtabDocument, tick: int) -> float:
    """把 tick 换算成播放用的真实秒数（会把 Tempo 事件算进去）。

    界面上的轴用节拍，只有“播放/回放”需要真实时间，所以这个函数只在
    手柄测试页这类需要按时间触发的场景用。
    """
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


def seconds_to_tick(doc: GtabDocument, seconds: float) -> int:
    """``tick_to_seconds`` 的反函数：真实秒数 → tick（试听进度条用）。

    同样会把 Tempo 事件算进去；超过结尾时返回结尾之后对应的 tick。
    """
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
