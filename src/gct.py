"""gct.py — GCT 和弦谱二进制格式（与 gtab 同源，但音是「和弦」而不是「单音」）。

文件布局（全部小端）::

    +0   8  bytes  magic           b"GCTAB\\0\\0\\0"
    +8   2  bytes  major           = 1
    +10  2  bytes  minor           = 0
    +12  4  bytes  header_size     = 48 + chord_table_size + meta_size
    +16  1  byte   time_base_type  （同 gtab，固定 0）
    +17  1  byte   string_count    弦数（6）
    +18  1  byte   flags           bit0 = 每个事件带 CRC32
    +19  1  byte   reserved        = 0
    +20  4  bytes  time_base_value 一拍的 tick 数（新文件 480）
    +24  4  bytes  default_tempo_us 默认速度（微秒/拍，500000 = 120 BPM）
    +28  4  bytes  chord_count     和弦表条目数
    +32  4  bytes  chord_table_size 和弦表字节数
    +36  4  bytes  meta_size       meta 区字节数
    +40  4  bytes  event_count     事件数
    +44  4  bytes  header_crc32    CRC32(前 44 字节 + 和弦表 + meta)
    +48  …         chord_table     chord_count 条：u16 id, u8 name_len, name(utf-8)
    …              meta            TLV：u16 type, u16 len, value
    …              events          见下

事件流：每个事件 = ``varint(delta_tick) + u8 type + u8 voice + u16 data_len + data``，
若 ``flags & 1`` 再跟 4 字节 CRC32(整条事件，含 delta 和头)。``delta_tick`` 是相对
上一个事件的 tick。

事件类型：

======  ========================================================  =============
type    含义                                                        data
======  ========================================================  =============
0x01    ChordStrum 扫弦：把整把琴扫一遍                              u16 chord_id, u8 direction
0x02    ChordPluck 单弦拨奏：只弹一根弦                              u16 chord_id, u8 string
0x03    ChordSet   只切换和弦（摆好指位、不出声）                     u16 chord_id
0x04    Tempo      变速                                             u32 tempo_us
0x05    End        结束（必须存在）                                  空
======  ========================================================  =============

``direction``：0 = 下扫（6 弦 → 1 弦），1 = 上扫（1 弦 → 6 弦）。
``chord_id`` 指向和弦表；和弦表里的 **名字** 才是跨文件稳定的（页号/方向由
``chord.json`` 的名字顺序决定，见 ``ui/chords.py``）。

写入端会自动保证文件以 End 结尾（``build()`` 里补，幂等）。
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "DIR_DOWN",
    "DIR_UP",
    "EV_CHORD_PLUCK",
    "EV_CHORD_SET",
    "EV_CHORD_STRUM",
    "EV_END",
    "EV_TEMPO",
    "EVENT_LABELS",
    "FLAG_EVENT_CRC",
    "GctError",
    "GctReader",
    "GctWriter",
    "Event",
    "Header",
    "decode_varint_from",
    "encode_varint",
]

MAGIC = b"GCTAB\0\0\0"
HEADER_SIZE = 48
HEADER_BEFORE_CRC = 44

# 固定头前 44 字节，44..47 是 header_crc32
HEADER_FMT = "<8sHHIBBBBIIIIII"
assert struct.calcsize(HEADER_FMT) == HEADER_BEFORE_CRC

EV_CHORD_STRUM = 0x01
EV_CHORD_PLUCK = 0x02
EV_CHORD_SET = 0x03
EV_TEMPO = 0x04
EV_END = 0x05

EVENT_LABELS: dict[int, str] = {
    EV_CHORD_STRUM: "Strum",
    EV_CHORD_PLUCK: "Pluck",
    EV_CHORD_SET: "Set",
    EV_TEMPO: "Tempo",
    EV_END: "End",
}

FLAG_EVENT_CRC = 0x01

DIR_DOWN = 0
DIR_UP = 1

DIRECTION_LABELS: dict[int, str] = {DIR_DOWN: "下扫", DIR_UP: "上扫"}


class GctError(ValueError):
    """gct 数据不合法，或文件无法解析。"""


# ---------------------------------------------------------------- varint


def encode_varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint_from(f, max_bytes: int = 10) -> tuple[int, bytes]:
    """从流里读一个 varint；EOF 抛 ``EOFError``（调用方负责判断是不是正常结尾）。"""
    raw = bytearray()
    shift = 0
    result = 0
    while True:
        b = f.read(1)
        if not b:
            raise EOFError("truncated varint")
        raw += b
        byte = b[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, bytes(raw)
        shift += 7
        if shift > 63 or len(raw) > max_bytes:
            raise ValueError("varint too long")


# ---------------------------------------------------------------- 数据结构


@dataclass
class Event:
    """一条事件；``tick`` 是绝对 tick（相对开头的累计值）。"""

    tick: int
    event_type: int
    voice: int
    data: bytes

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.event_type, f"0x{self.event_type:02X}")

    @property
    def chord_id(self) -> int:
        """Strum / Pluck / Set 事件里的 chord_id（其它事件返回 0）。"""
        if self.event_type in (EV_CHORD_STRUM, EV_CHORD_PLUCK):
            return int.from_bytes(self.data[:2], "little")
        if self.event_type == EV_CHORD_SET:
            return int.from_bytes(self.data[:2], "little")
        return 0

    @property
    def direction(self) -> int:
        """Strum 的方向（0 下扫 / 1 上扫）；其它事件返回 0。"""
        if self.event_type == EV_CHORD_STRUM and len(self.data) >= 3:
            return self.data[2]
        return DIR_DOWN

    @property
    def string(self) -> int:
        """Pluck 的弦号；其它事件返回 0。"""
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

    def copy(self) -> "Event":
        return Event(self.tick, self.event_type, self.voice, bytes(self.data))


@dataclass
class Header:
    major: int
    minor: int
    header_size: int
    time_base_type: int
    string_count: int
    flags: int
    time_base_value: int
    default_tempo_us: int
    chord_count: int
    chord_table_size: int
    meta_size: int
    event_count: int
    reserved: int = 0


# ---------------------------------------------------------------- Writer


class GctWriter:
    """按顺序写事件；``add_event`` 的 delta 是相对上一个事件的 tick 差。"""

    def __init__(
        self,
        string_count: int = 6,
        time_base_type: int = 0,
        time_base_value: int = 480,
        default_tempo_us: int = 500000,
        flags: int = 0,
    ):
        if not (1 <= string_count <= 255):
            raise ValueError("string_count out of range 1..255")
        if time_base_value < 1:
            raise ValueError("time_base_value must be > 0")
        if default_tempo_us <= 0:
            raise ValueError("default_tempo_us must be > 0")
        self.string_count = string_count
        self.time_base_type = time_base_type
        self.time_base_value = time_base_value
        self.default_tempo_us = default_tempo_us
        self.flags = flags

        # chords: list[(chord_id, name_bytes)]
        self.chords: list[tuple[int, bytes]] = []
        self._next_chord_id = 1
        self._name_to_id: dict[str, int] = {}

        self.meta = bytearray()
        self.events = bytearray()
        self.event_count = 0
        self.current_tick = 0
        # 最后一个事件是不是 End（build() 用它决定要不要补）
        self._last_is_end = False
        self._end_seen = False

    # ---- 和弦表 ----
    def add_chord(self, name: str, chord_id: Optional[int] = None) -> int:
        if chord_id is None:
            chord_id = self._next_chord_id
        if chord_id == 0:
            raise ValueError("chord_id 0 is reserved")
        if not (0 < chord_id <= 0xFFFF):
            raise ValueError("chord_id out of range")
        if any(c[0] == chord_id for c in self.chords):
            raise ValueError(f"duplicate chord_id {chord_id}")
        if name in self._name_to_id:
            raise ValueError(f"duplicate chord name {name!r}")
        name_bytes = name.encode("utf-8")
        if not (0 < len(name_bytes) <= 255):
            raise ValueError("chord name length must be 1..255 bytes")
        self.chords.append((chord_id, name_bytes))
        self._name_to_id[name] = chord_id
        self._next_chord_id = max(self._next_chord_id, chord_id + 1)
        return chord_id

    def chord_id_of(self, name: str) -> int:
        return self._name_to_id[name]

    def _resolve_chord(self, chord) -> int:
        if isinstance(chord, int):
            if not any(c[0] == chord for c in self.chords):
                raise ValueError(f"chord_id {chord} not in chord table")
            return chord
        if chord not in self._name_to_id:
            raise ValueError(f"chord name {chord!r} not in chord table")
        return self._name_to_id[chord]

    # ---- 元数据 ----
    def add_meta(self, meta_type: int, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        if len(value) > 0xFFFF:
            raise ValueError("meta value too long")
        if not (0 <= meta_type <= 0xFFFF):
            raise ValueError("meta_type out of range 0..0xFFFF")
        self.meta += struct.pack("<HH", meta_type, len(value)) + value

    # ---- 事件 ----
    def add_event(self, delta: int, event_type: int, voice: int, data: bytes) -> None:
        if delta < 0:
            raise ValueError("delta must be >= 0")
        if len(data) > 0xFFFF:
            raise ValueError("event data too long")
        if not (0 <= event_type <= 0xFF):
            raise ValueError("event_type out of range 0..255")
        if not (0 <= voice <= 0xFF):
            raise ValueError("voice out of range 0..255")
        if self._end_seen:
            raise ValueError("End 之后不能再写事件")
        delta_bytes = encode_varint(delta)
        head = struct.pack("<BBH", event_type, voice, len(data))
        payload = delta_bytes + head + data
        if self.flags & FLAG_EVENT_CRC:
            payload += struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
        self.events += payload
        self.event_count += 1
        self.current_tick += delta
        self._last_is_end = event_type == EV_END
        self._end_seen = self._end_seen or self._last_is_end

    def strum(self, delta: int, chord, direction: int, voice: int = 0) -> None:
        cid = self._resolve_chord(chord)
        if direction not in (DIR_DOWN, DIR_UP):
            raise ValueError("direction must be 0 (down) or 1 (up)")
        self.add_event(delta, EV_CHORD_STRUM, voice, struct.pack("<HB", cid, direction))

    def pluck(self, delta: int, chord, string: int, voice: int = 0) -> None:
        cid = self._resolve_chord(chord)
        if not (1 <= string <= self.string_count):
            raise ValueError(f"string {string} out of range 1..{self.string_count}")
        self.add_event(delta, EV_CHORD_PLUCK, voice, struct.pack("<HB", cid, string))

    def set_chord(self, delta: int, chord, voice: int = 0) -> None:
        cid = self._resolve_chord(chord)
        self.add_event(delta, EV_CHORD_SET, voice, struct.pack("<H", cid))

    def tempo(self, delta: int, tempo_us: int, voice: int = 0) -> None:
        if tempo_us <= 0:
            raise ValueError("tempo_us must be > 0")
        self.add_event(delta, EV_TEMPO, voice, struct.pack("<I", tempo_us))

    def end(self, delta: int = 0, voice: int = 0) -> None:
        self.add_event(delta, EV_END, voice, b"")

    # ---- 构建 ----
    def _build_chord_table(self) -> bytes:
        out = bytearray()
        for cid, name in self.chords:
            out += struct.pack("<HB", cid, len(name)) + name
        return bytes(out)

    def build(self) -> bytes:
        # 末尾没有 End 就补一个：严格读取要求必须有，补上后重复调用是幂等的
        if not self._last_is_end:
            self.end(0)
        chord_table = self._build_chord_table()
        meta = bytes(self.meta)
        header_size = HEADER_SIZE + len(chord_table) + len(meta)

        before_crc = struct.pack(
            HEADER_FMT,
            MAGIC,
            1,
            0,
            header_size,
            self.time_base_type,
            self.string_count,
            self.flags,
            0,
            self.time_base_value,
            self.default_tempo_us,
            len(self.chords),
            len(chord_table),
            len(meta),
            self.event_count,
        )
        crc = zlib.crc32(before_crc + chord_table + meta) & 0xFFFFFFFF
        return (
            before_crc
            + struct.pack("<I", crc)
            + chord_table
            + meta
            + bytes(self.events)
        )

    def write(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.build())


# ---------------------------------------------------------------- Reader


class GctReader:
    """解析 .gct；``strict=True`` 时任何不一致都抛 :class:`GctError`。"""

    def __init__(self, source, strict: bool = True):
        if isinstance(source, (bytes, bytearray)):
            self._f = io.BytesIO(bytes(source))
            self._close = False
        elif isinstance(source, str):
            self._f = open(source, "rb")
            self._close = True
        else:
            self._f = source
            self._close = False
        self.strict = strict
        self.header: Optional[Header] = None
        self.chords: dict[int, str] = {}   # chord_id -> name
        self.meta: dict[int, list[bytes]] = {}
        self._parse_header()

    def close(self):
        if self._close:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 固定头 + 和弦表 + 元数据 ----
    def _parse_header(self):
        raw = self._f.read(HEADER_BEFORE_CRC)
        if len(raw) < HEADER_BEFORE_CRC:
            raise GctError("文件太短，读不到 44 字节固定头")

        (
            magic,
            major,
            minor,
            header_size,
            time_base_type,
            string_count,
            flags,
            reserved,
            time_base_value,
            default_tempo_us,
            chord_count,
            chord_table_size,
            meta_size,
            event_count,
        ) = struct.unpack(HEADER_FMT, raw)

        if magic != MAGIC:
            raise GctError(f"不是 gct 文件（magic={magic!r}）")
        if major != 1:
            raise GctError(f"不支持的版本 {major}.{minor}")
        if header_size < HEADER_SIZE:
            raise GctError("header_size 太小")
        if time_base_value == 0:
            raise GctError("time_base_value 必须 > 0")
        if string_count == 0:
            raise GctError("string_count 必须 > 0")
        if default_tempo_us <= 0:
            raise GctError("default_tempo_us 必须 > 0")
        if chord_table_size > 0 and chord_count == 0:
            raise GctError("chord_table_size > 0 但 chord_count == 0")
        if chord_count > 0 and chord_table_size == 0:
            raise GctError("chord_count > 0 但 chord_table_size == 0")
        expected_header_size = HEADER_SIZE + chord_table_size + meta_size
        if header_size != expected_header_size:
            raise GctError(
                f"header_size {header_size} != 48 + 和弦表 + meta "
                f"({expected_header_size})"
            )

        crc_raw = self._f.read(4)
        if len(crc_raw) < 4:
            raise GctError("头部 CRC 被截断")
        stored_crc = struct.unpack("<I", crc_raw)[0]

        chord_table = self._f.read(chord_table_size)
        if len(chord_table) < chord_table_size:
            raise GctError("和弦表被截断")

        meta = self._f.read(meta_size)
        if len(meta) < meta_size:
            raise GctError("meta 区被截断")

        calc_crc = zlib.crc32(raw + chord_table + meta) & 0xFFFFFFFF
        if calc_crc != stored_crc:
            raise GctError("头部 CRC 不匹配（文件被改坏或不完整）")

        self._parse_chord_table(chord_table, chord_count)
        self._parse_meta(meta)

        self.header = Header(
            major=major,
            minor=minor,
            header_size=header_size,
            time_base_type=time_base_type,
            string_count=string_count,
            flags=flags,
            time_base_value=time_base_value,
            default_tempo_us=default_tempo_us,
            chord_count=chord_count,
            chord_table_size=chord_table_size,
            meta_size=meta_size,
            event_count=event_count,
            reserved=reserved,
        )
        self._f.seek(header_size)

    def _parse_chord_table(self, blob: bytes, expected_count: int):
        i = 0
        count = 0
        while i < len(blob):
            if i + 3 > len(blob):
                raise GctError("和弦表条目头被截断")
            cid, name_len = struct.unpack_from("<HB", blob, i)
            i += 3
            if i + name_len > len(blob):
                raise GctError("和弦名被截断")
            raw_name = blob[i : i + name_len]
            try:
                name = raw_name.decode("utf-8")
            except UnicodeDecodeError as exc:
                if self.strict:
                    raise GctError(f"和弦名不是合法 UTF-8：{raw_name!r}") from exc
                name = raw_name.decode("utf-8", "replace")
            i += name_len

            if cid == 0:
                raise GctError("chord_id 0 保留，不能使用")
            if cid in self.chords:
                raise GctError(f"chord_id {cid} 重复")
            if not name:
                raise GctError(f"chord_id {cid} 的名字为空")
            self.chords[cid] = name
            count += 1

        if count != expected_count:
            raise GctError(
                f"和弦表有 {count} 条，头部写的是 {expected_count} 条"
            )

    def _parse_meta(self, blob: bytes):
        i = 0
        while i + 4 <= len(blob):
            t, ln = struct.unpack_from("<HH", blob, i)
            i += 4
            if i + ln > len(blob):
                if self.strict:
                    raise GctError("meta TLV 越界")
                break
            self.meta.setdefault(t, []).append(blob[i : i + ln])
            i += ln
        if self.strict and i != len(blob):
            raise GctError("meta 区尾部有残余字节")

    # ---- 事件 ----
    def events(self):
        """逐条 yield :class:`Event`，读到 End 就停（End 也会 yield）。"""
        f = self._f
        tick = 0
        count = 0

        while True:
            # 先看还有没有字节：正常结尾是读到 End 就返回；没有 End 才是错
            first = f.read(1)
            if not first:
                if self.strict:
                    raise GctError("缺少 EndOfTrack（End 事件）")
                return
            # 把读掉的那个字节接回去，交给 decode_varint_from
            f.seek(-1, io.SEEK_CUR)
            delta, delta_raw = decode_varint_from(f)

            head = f.read(4)
            if len(head) < 4:
                raise GctError("事件头被截断")
            event_type, voice, data_len = struct.unpack("<BBH", head)
            data = f.read(data_len)
            if len(data) < data_len:
                raise GctError("事件数据被截断")

            if self.header.flags & FLAG_EVENT_CRC:
                crc_raw = f.read(4)
                if len(crc_raw) < 4:
                    raise GctError("事件 CRC 被截断")
                stored = struct.unpack("<I", crc_raw)[0]
                calc = zlib.crc32(delta_raw + head + data) & 0xFFFFFFFF
                if calc != stored:
                    raise GctError(f"事件 CRC 不匹配（offset {f.tell()}）")

            tick += delta

            if event_type == EV_CHORD_STRUM:
                if len(data) != 3:
                    raise GctError("Strum 数据必须是 3 字节")
                cid, direction = struct.unpack("<HB", data)
                self._require_chord(cid)
                if direction not in (DIR_DOWN, DIR_UP):
                    raise GctError(f"扫弦方向不合法：{direction}")
            elif event_type == EV_CHORD_PLUCK:
                if len(data) != 3:
                    raise GctError("Pluck 数据必须是 3 字节")
                cid, string = struct.unpack("<HB", data)
                self._require_chord(cid)
                if not (1 <= string <= self.header.string_count):
                    raise GctError(f"拨弦的弦号 {string} 超出 1..{self.header.string_count}")
            elif event_type == EV_CHORD_SET:
                if len(data) != 2:
                    raise GctError("Set 数据必须是 2 字节")
                self._require_chord(struct.unpack("<H", data)[0])
            elif event_type == EV_TEMPO:
                if len(data) != 4:
                    raise GctError("Tempo 数据必须是 4 字节")
                if struct.unpack("<I", data)[0] == 0:
                    raise GctError("Tempo 不能为 0")
            elif event_type == EV_END:
                if data:
                    raise GctError("End 事件不应带数据")
            # 未知事件：原样 yield（向后兼容），由上层决定怎么处理

            count += 1
            yield Event(tick, event_type, voice, bytes(data))

            if event_type == EV_END:
                if self.strict and count != self.header.event_count:
                    raise GctError(
                        f"实际读到 {count} 个事件，头部写的是 {self.header.event_count}"
                    )
                return

    def _require_chord(self, cid: int):
        if cid not in self.chords:
            raise GctError(f"chord_id {cid} 不在和弦表里")
