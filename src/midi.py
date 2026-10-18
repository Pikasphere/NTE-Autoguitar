"""标准 MIDI 文件（SMF）的最小读写（只用标准库，不依赖 mido / pretty_midi）。

只做「把 MIDI 里的音符按时间拿出来」这一件事，够 MIDI 映射页和测试用：

- 支持 format 0 / 1（format 2 按 1 处理），PPQ 时间基准（SMPTE 时间基准直接报错）；
- 支持 running status、note on/off（``velocity == 0`` 的 note on 当 note off）、
  tempo meta（``FF 51 03``）、track name（``FF 03``）、End of Track（``FF 2F``）；
- 其他事件（控制轮、弯音、sysex、aftertouch…）**跳过但正确计算长度**，
  不会把后面的字节读串；
- 同一个 ``(channel, note)`` 上的多次按下按先来先配对（FIFO），音尾没关的按曲尾收掉。

写文件只用于生成示例 / 测试数据（``build``）：写出 format 0、单轨、可被本模块读回。

格式细节见 https://www.music.meps.ntua.gr/Portals/0/Docs/MIDI/SMF.pdf 的鸟瞰，
这里不复制完整规范，只保留本程序依赖的部分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MidiError",
    "MidiFile",
    "RawNote",
    "TempoChange",
    "build",
    "from_path",
    "parse",
]


class MidiError(ValueError):
    """MIDI 文件损坏 / 不支持的格式。"""


@dataclass(frozen=True)
class TempoChange:
    tick: int
    tempo_us: int  # 每四分音符的微秒数（500000 = 120 BPM）


@dataclass(frozen=True)
class RawNote:
    start: int
    end: int
    note: int  # MIDI 音高，60 = C4
    velocity: int
    channel: int


@dataclass
class MidiFile:
    format: int = 1
    tracks: int = 1
    division: int = 480  # 每四分音符的 tick 数
    notes: list[RawNote] = field(default_factory=list)
    tempos: list[TempoChange] = field(default_factory=list)
    end_tick: int = 0
    name: str = ""
    #: 音尾没有对应 note off、被曲尾强制收掉的音数（容错提示用）
    unclosed: int = 0


# ---------------------------------------------------------------- 读


def _read_varlen(data: bytes, pos: int) -> tuple[int, int]:
    """SMF 的变长整数（每字节 7 位，最高位是「还有后续」）。"""
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiError("变长整数被截断")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiError("变长整数超过 4 字节（文件损坏）")


def _write_varlen(value: int) -> bytes:
    value = max(0, int(value))
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def _parse_track(
    body: bytes, notes: list[RawNote], tempos: list[TempoChange], result: MidiFile
) -> None:
    pos = 0
    tick = 0
    running: tuple[int, int] | None = None  # (命令高 4 位, 通道)
    opened: dict[tuple[int, int], list[tuple[int, int]]] = {}
    last_tick = 0

    while pos < len(body):
        delta, pos = _read_varlen(body, pos)
        tick += delta
        if pos >= len(body):
            raise MidiError("事件被截断")
        status = body[pos]

        # ---- meta / sysex
        if status == 0xFF:
            pos += 1
            if pos >= len(body):
                raise MidiError("meta 事件被截断")
            meta_type = body[pos]
            pos += 1
            length, pos = _read_varlen(body, pos)
            payload = body[pos : pos + length]
            if len(payload) < length:
                raise MidiError("meta 事件长度越界")
            pos += length
            if meta_type == 0x51 and length >= 3:
                tempos.append(TempoChange(tick, int.from_bytes(payload[:3], "big")))
            elif meta_type == 0x03 and payload and not result.name:
                result.name = payload.decode("utf-8", "replace").strip("\0 ")
            elif meta_type == 0x2F:
                last_tick = max(last_tick, tick)
                break
            continue
        if status in (0xF0, 0xF7):
            pos += 1
            length, pos = _read_varlen(body, pos)
            pos += length
            if pos > len(body):
                raise MidiError("sysex 长度越界")
            continue

        # ---- 通道消息（含 running status）
        first: int | None = None
        if status & 0x80:
            command = status & 0xF0
            channel = status & 0x0F
            pos += 1
            running = (command, channel)
        else:
            if running is None:
                raise MidiError("running status 没有前置状态字节")
            command, channel = running
            first = status
            pos += 1  # 这个数据字节就是第一个参数，别再读一遍

        need = 1 if command in (0xC0, 0xD0) else 2
        args: list[int] = [first] if first is not None else []
        while len(args) < need:
            if pos >= len(body):
                raise MidiError("通道消息被截断")
            args.append(body[pos])
            pos += 1

        if command == 0x90 and args[1] > 0:
            opened.setdefault((channel, args[0]), []).append((tick, args[1]))
        elif command in (0x80, 0x90):
            queue = opened.get((channel, args[0]))
            if queue:
                start, velocity = queue.pop(0)
                notes.append(
                    RawNote(
                        start=start,
                        end=max(tick, start + 1),
                        note=args[0],
                        velocity=velocity,
                        channel=channel,
                    )
                )
        # 其他通道消息（CC / 弯音 / 触后 / 音色）不影响音符，跳过
        last_tick = max(last_tick, tick)

    # 音尾缺失：按曲尾收掉（MIDI 是容错读取；gtab 那种严格校验不适用于通用 MIDI 文件）
    for (channel, note), queue in opened.items():
        for start, velocity in queue:
            result.unclosed += 1
            notes.append(
                RawNote(
                    start=start,
                    end=max(last_tick, start + 1),
                    note=note,
                    velocity=velocity,
                    channel=channel,
                )
            )
    result.end_tick = max(result.end_tick, last_tick)


def parse(data: bytes) -> MidiFile:
    """把 SMF 字节流解析成 :class:`MidiFile`。"""
    if len(data) < 14 or data[0:4] != b"MThd":
        raise MidiError("不是 MIDI 文件（缺 MThd 头）")
    header_len = int.from_bytes(data[4:8], "big")
    if header_len < 6 or len(data) < 8 + header_len:
        raise MidiError("MIDI 头长度不对")
    result = MidiFile(
        format=int.from_bytes(data[8:10], "big"),
        tracks=int.from_bytes(data[10:12], "big"),
        division=int.from_bytes(data[12:14], "big"),
    )
    if result.division & 0x8000:
        raise MidiError("不支持 SMPTE 时间基准的 MIDI（只支持 PPQ）")
    if result.division <= 0:
        raise MidiError("MIDI 时间基准为 0")

    pos = 8 + header_len
    notes: list[RawNote] = []
    tempos: list[TempoChange] = []
    while pos + 8 <= len(data):
        if data[pos : pos + 4] != b"MTrk":
            break  # 不认识的 chunk：停止（不猜）
        length = int.from_bytes(data[pos + 4 : pos + 8], "big")
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length:
            raise MidiError("track 长度越界（文件被截断）")
        pos += 8 + length
        _parse_track(body, notes, tempos, result)

    if not notes and not tempos:
        raise MidiError("MIDI 里没有任何音符")
    notes.sort(key=lambda note: (note.start, note.note))
    tempos.sort(key=lambda tempo: tempo.tick)
    result.notes = notes
    result.tempos = tempos or [TempoChange(0, 500_000)]
    if result.end_tick <= 0:
        result.end_tick = max((note.end for note in notes), default=0)
    return result


def from_path(path: str | Path) -> MidiFile:
    return parse(Path(path).read_bytes())


# ---------------------------------------------------------------- 写（示例 / 测试）


def build(
    notes: list[tuple[int, int, int, int]],
    *,
    tempo_us: int = 500_000,
    division: int = 480,
    name: str = "demo",
    tempos: list[tuple[int, int]] | None = None,
) -> bytes:
    """用 ``(start_tick, end_tick, note, velocity)`` 列表生成 format 0 的 MIDI 字节。"""
    events: list[tuple[int, int, bytes]] = []
    if name:
        payload = name.encode("utf-8")
        events.append((0, 0, b"\xff\x03" + _write_varlen(len(payload)) + payload))
    for tick, value in sorted(tempos or [(0, tempo_us)]):
        events.append((tick, 0, b"\xff\x51\x03" + int(value).to_bytes(3, "big")))
    for start, end, note, velocity in notes:
        start = max(0, int(start))
        end = max(start + 1, int(end))
        events.append((start, 2, bytes([0x90, int(note) & 0x7F, max(1, min(127, int(velocity)))])))
        events.append((end, 1, bytes([0x80, int(note) & 0x7F, 0])))
    last = max((tick for tick, _, _ in events), default=0)
    events.append((last, 9, b"\xff\x2f\x00"))
    events.sort(key=lambda event: (event[0], event[1]))

    track = bytearray()
    previous = 0
    for tick, _order, payload in events:
        track += _write_varlen(tick - previous)
        track += payload
        previous = tick

    header = (
        b"MThd"
        + (6).to_bytes(4, "big")
        + (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + int(division).to_bytes(2, "big")
    )
    return header + b"MTrk" + len(track).to_bytes(4, "big") + bytes(track)
