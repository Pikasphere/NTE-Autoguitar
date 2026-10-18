# gtab.py
import struct
import zlib
import io
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"GTAB\0\0\0\0"
HEADER_SIZE = 48

# 固定头前 44 字节（不含 header_crc32）
HEADER_FMT = "<8sHHIBBBBIIIIQ"
HEADER_BEFORE_CRC = 44
assert struct.calcsize(HEADER_FMT) == HEADER_BEFORE_CRC

EV_STRING_NOTE_ON = 0x01
EV_STRING_NOTE_OFF = 0x02
EV_TEMPO = 0x03
EV_MARKER = 0x04
EV_SNAPSHOT = 0x05
EV_END = 0x06

FLAG_EVENT_CRC = 0x01

# ---------------- varint ----------------

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

def decode_varint_from(f, max_bytes=10):
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

# ---------------- 数据结构 ----------------

@dataclass
class Event:
    tick: int
    event_type: int
    voice: int
    data: bytes

@dataclass
class Header:
    major: int
    minor: int
    header_size: int
    time_base_type: int
    string_count: int
    max_fret: int
    flags: int
    time_base_value: int
    default_tempo_us: int
    meta_offset: int
    meta_size: int
    event_count: int

# ---------------- Writer ----------------

class GtabWriter:
    def __init__(self, string_count=6, max_fret=24,
                 time_base_type=0, time_base_value=480,
                 default_tempo_us=500000, flags=0):
        if string_count < 1 or string_count > 255:
            raise ValueError("string_count out of range")
        if max_fret < 1 or max_fret > 255:
            raise ValueError("max_fret out of range")
        if time_base_value < 1:
            raise ValueError("time_base_value must be > 0")
        self.string_count = string_count
        self.max_fret = max_fret
        self.time_base_type = time_base_type
        self.time_base_value = time_base_value
        self.default_tempo_us = default_tempo_us
        self.flags = flags

        self.meta = bytearray()
        self.events = bytearray()
        self.event_count = 0
        self.current_tick = 0

    # ---- 元数据 ----
    def add_meta(self, meta_type: int, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        if len(value) > 0xFFFF:
            raise ValueError("meta value too long")
        self.meta += struct.pack("<HH", meta_type, len(value)) + value

    # ---- 事件 ----
    def add_event(self, delta: int, event_type: int, voice: int = 0, data: bytes = b"") -> None:
        """写一个事件：delta(varint) + 类型(u8) + voice(u8) + 数据长度(u16) + 数据。

        ``voice`` 是事件头里固定存在的 1 字节字段。本格式没有规定它代表什么，
        项目里只把它当「声部号」用于 NoteOn/NoteOff 配对
        （见 ``ui.gtab_doc.GtabDocument.note_blocks`` 的配对键）。
        """
        if delta < 0:
            raise ValueError("delta must be >= 0")
        if len(data) > 0xFFFF:
            raise ValueError("event data too long")
        delta_bytes = encode_varint(delta)
        head = struct.pack("<BBH", event_type, voice, len(data))
        payload = delta_bytes + head + data
        if self.flags & FLAG_EVENT_CRC:
            payload += struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
        self.events += payload
        self.event_count += 1
        self.current_tick += delta

    def note_on(self, delta: int, string: int, fret: int, voice: int = 0) -> None:
        self._check_string_fret(string, fret)
        self.add_event(delta, EV_STRING_NOTE_ON, voice, bytes([string, fret]))

    def note_off(self, delta: int, string: int, fret: int, voice: int = 0) -> None:
        self._check_string_fret(string, fret)
        self.add_event(delta, EV_STRING_NOTE_OFF, voice, bytes([string, fret]))

    def tempo(self, delta: int, tempo_us: int, voice: int = 0) -> None:
        if tempo_us <= 0:
            raise ValueError("tempo_us must be > 0")
        self.add_event(delta, EV_TEMPO, voice, struct.pack("<I", tempo_us))

    def end(self, delta: int = 0, voice: int = 0) -> None:
        self.add_event(delta, EV_END, voice, b"")

    def _check_string_fret(self, string: int, fret: int) -> None:
        if not (1 <= string <= self.string_count):
            raise ValueError(f"string {string} out of range 1..{self.string_count}")
        if not (0 <= fret <= self.max_fret):
            raise ValueError(f"fret {fret} out of range 0..{self.max_fret}")

    # ---- 构建 ----
    def build(self) -> bytes:
        meta_size = len(self.meta)
        meta_offset = HEADER_SIZE if meta_size else 0
        header_size = HEADER_SIZE + meta_size

        before_crc = struct.pack(
            HEADER_FMT,
            MAGIC, 1, 0, header_size,
            self.time_base_type, self.string_count, self.max_fret, self.flags,
            self.time_base_value, self.default_tempo_us,
            meta_offset, meta_size, self.event_count,
        )
        crc = zlib.crc32(before_crc + bytes(self.meta)) & 0xFFFFFFFF
        header = before_crc + struct.pack("<I", crc)
        return header + bytes(self.meta) + bytes(self.events)

    def write(self, path: str) -> None:
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(self.build())

# ---------------- Reader ----------------

class GtabReader:
    def __init__(self, source, strict=True):
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
        self.header = None
        self.meta = {}
        self._parse_header()

    def close(self):
        if self._close:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 固定头 ----
    def _parse_header(self):
        raw = self._f.read(HEADER_BEFORE_CRC)
        if len(raw) < HEADER_BEFORE_CRC:
            raise ValueError("file too short")
        fields = struct.unpack(HEADER_FMT, raw)
        (magic, major, minor, header_size,
         time_base_type, string_count, max_fret, flags,
         time_base_value, default_tempo_us,
         meta_offset, meta_size, event_count) = fields

        if magic != MAGIC:
            raise ValueError("bad magic")
        if major != 1:
            raise ValueError(f"unsupported major version {major}")
        if header_size < HEADER_SIZE:
            raise ValueError("header_size too small")
        if time_base_value == 0:
            raise ValueError("time_base_value must be > 0")
        if string_count == 0:
            raise ValueError("string_count must be > 0")
        if max_fret == 0:
            raise ValueError("max_fret must be > 0")
        if meta_size > 0:
            if meta_offset < HEADER_SIZE:
                raise ValueError("bad meta_offset")
            if meta_offset + meta_size > header_size:
                raise ValueError("meta area out of header_size")
        else:
            if meta_offset != 0:
                raise ValueError("meta_offset must be 0 when meta_size is 0")

        crc_raw = self._f.read(4)
        if len(crc_raw) < 4:
            raise ValueError("truncated header crc")
        stored_crc = struct.unpack("<I", crc_raw)[0]

        meta = b""
        if meta_size:
            self._f.seek(meta_offset)
            meta = self._f.read(meta_size)
            if len(meta) < meta_size:
                raise ValueError("truncated meta area")

        calc_crc = zlib.crc32(raw + meta) & 0xFFFFFFFF
        if calc_crc != stored_crc:
            raise ValueError("header crc mismatch")

        self.header = Header(
            major=major, minor=minor, header_size=header_size,
            time_base_type=time_base_type, string_count=string_count,
            max_fret=max_fret, flags=flags,
            time_base_value=time_base_value,
            default_tempo_us=default_tempo_us,
            meta_offset=meta_offset, meta_size=meta_size,
            event_count=event_count,
        )
        self._parse_meta(meta)
        self._f.seek(header_size)

    def _parse_meta(self, blob: bytes):
        i = 0
        while i + 4 <= len(blob):
            t, ln = struct.unpack_from("<HH", blob, i)
            i += 4
            if i + ln > len(blob):
                if self.strict:
                    raise ValueError("meta TLV out of range")
                break
            self.meta.setdefault(t, []).append(blob[i:i+ln])
            i += ln

    # ---- 事件 ----
    def events(self):
        f = self._f
        tick = 0
        active = set()
        while True:
            pos = f.tell()
            delta, delta_raw = decode_varint_from(f)

            head = f.read(4)
            if len(head) < 4:
                if self.strict and len(head) != 0:
                    raise ValueError("truncated event header")
                break
            event_type, voice, data_len = struct.unpack("<BBH", head)
            data = f.read(data_len)
            if len(data) < data_len:
                raise ValueError("truncated event data")

            if self.header.flags & FLAG_EVENT_CRC:
                crc_raw = f.read(4)
                if len(crc_raw) < 4:
                    raise ValueError("truncated event crc")
                stored = struct.unpack("<I", crc_raw)[0]
                calc = zlib.crc32(delta_raw + head + data) & 0xFFFFFFFF
                if calc != stored:
                    raise ValueError(f"event crc mismatch at offset {pos}")

            tick += delta
            ev = Event(tick, event_type, voice, data)

            # 校验
            if event_type == EV_STRING_NOTE_ON:
                self._check_note_data(data)
                key = (voice, data[0], data[1])
                if self.strict and key in active:
                    raise ValueError(f"duplicate note on at tick {tick}: {key}")
                active.add(key)
            elif event_type == EV_STRING_NOTE_OFF:
                self._check_note_data(data)
                key = (voice, data[0], data[1])
                if self.strict and key not in active:
                    raise ValueError(f"note off without note on at tick {tick}: {key}")
                active.discard(key)
            elif event_type == EV_TEMPO:
                if len(data) != 4:
                    raise ValueError("bad tempo event length")
            elif event_type == EV_MARKER:
                if len(data) != 8:
                    raise ValueError("bad marker event length")

            yield ev

            if event_type == EV_END:
                return

        if self.strict:
            raise ValueError("missing EndOfTrack")

    def _check_note_data(self, data: bytes):
        if len(data) != 2:
            raise ValueError("note event must be 2 bytes")
        string, fret = data[0], data[1]
        if not (1 <= string <= self.header.string_count):
            raise ValueError(f"string {string} out of range")
        if not (0 <= fret <= self.header.max_fret):
            raise ValueError(f"fret {fret} out of range")