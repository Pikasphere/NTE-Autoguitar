"""MIDI 音符 → 键盘按键：键位表、排期、以及 Windows ``SendInput`` 发送。

键位表在 ``src/midi_keys.json``（运行时读取，改完点「重载键位」即可）：

- ``BASE_NOTE``：``KEYS[0]`` 对应的 MIDI 音高（默认 52 = E3）；
- ``KEYS``：从 ``BASE_NOTE`` 起**每个半音一个键**，默认 36 个 = 三个八度，
  按键盘三行排（每行 12 个半音，从 E 到 D#）：
  底行 ``a s d f g h j k l ; ' enter``（E3–D#4）、
  中行 ``q w e r t y u i o p [ ]``（E4–D#5）、
  顶行 ``1 2 3 4 5 6 7 8 9 0 - =``（E5–D#6）。
  音高超出这段范围的和弦/音符默认会被**跳过**并报出来，用 MIDI 页的 Capo（0–12 品，
  N 品 = 整首升高 N 个半音）把整首移进这个窗口即可；
  设置页打开「MIDI 音域超出后按八度投影」时，改为把每个超窗的音**按 ±12 半音移进窗口**
  （:func:`project_note`），节奏不变、音名会变（:func:`build_key_schedule` 的
  ``project_octave=True``）。
- ``KEYS`` 的每一项通常是**单个字符**；``enter`` 是唯一允许的键名（Enter 键没有字符），
  显示时用 :func:`key_label` 缩写成 ``Ent``。

排期（``build_key_schedule``）和手柄那边一样是**纯函数**：输入文档 + 键位表，
输出按时间排序的按下/松开动作，不碰硬件，可以直接单测。

``KeyboardBackend`` 用 ``SendInput`` 发**扫描码**（很多游戏不认单纯注入的虚拟键，
认扫描码），所以：

- 必须以**普通权限或管理员**运行——低权限进程（沙盒 / CI）发不进权限更高的游戏；
- 键位表里不允许出现需要 Shift 的键（例如 ``!``），需要的话请换成普通键。
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ui.midi_doc import MidiDocument, note_name

__all__ = [
    "KEY_FILE",
    "KEY_REPRESS_GAP",
    "LEAD_IN",
    "KeyAction",
    "KeySchedule",
    "KeyTable",
    "KeyboardBackend",
    "build_key_schedule",
    "key_for_note",
    "key_label",
    "load_key_table",
    "parse_key_table",
    "project_note",
    "reload_key_table",
]

KEY_FILE = Path(__file__).resolve().parents[1] / "midi_keys.json"

LEAD_IN = 0.4          # 开场留一点时间（游戏在前台、手离开热键）
KEY_REPRESS_GAP = 0.04  # 同一个键要再按时，先松开至少这么久（和手柄重拨同理）
MIN_PRESS = 0.01

#: 键位表里允许的「键名」→ 虚拟键码。Enter 没有对应字符，只能用名字写。
NAMED_KEYS = {"enter": 0x0D}
#: 键名在界面上的缩写（单个字符的键原样显示）。
KEY_LABELS = {"enter": "Ent"}


def key_label(key: str) -> str:
    """键位表里的键 → 界面上显示的文字（``enter`` → ``Ent``）。"""
    return KEY_LABELS.get(key, key)


# ---------------------------------------------------------------- 键位表


class KeyTableError(ValueError):
    """键位表内容不合法。"""


@dataclass(frozen=True)
class KeyTable:
    """``keys`` 从 ``base_note`` 起每个半音一个键。"""

    base_note: int = 60
    keys: tuple[str, ...] = ()

    @property
    def span(self) -> int:
        return len(self.keys)

    @property
    def last_note(self) -> int:
        return self.base_note + max(0, len(self.keys) - 1)

    def key_for(self, note: int, transpose: int = 0) -> str | None:
        index = int(note) + int(transpose) - self.base_note
        if 0 <= index < len(self.keys):
            return self.keys[index]
        return None

    def note_for(self, key: str) -> int | None:
        try:
            return self.base_note + self.keys.index(key)
        except ValueError:
            return None


def parse_key_table(mapping: object, source: str = "midi_keys.json") -> KeyTable:
    if not isinstance(mapping, dict):
        raise KeyTableError(f"{source}：顶层应该是一个对象")
    base = mapping.get("BASE_NOTE", 60)
    keys = mapping.get("KEYS")
    if isinstance(base, bool) or not isinstance(base, int):
        raise KeyTableError(f"{source}：BASE_NOTE 应该是整数")
    if not 0 <= base <= 127:
        raise KeyTableError(f"{source}：BASE_NOTE 要在 0–127 之间")
    if not isinstance(keys, list) or not keys:
        raise KeyTableError(f"{source}：KEYS 应该是非空数组")
    cleaned: list[str] = []
    for item in keys:
        if not isinstance(item, str) or not item:
            raise KeyTableError(
                f"{source}：KEYS 里每一项必须是单个字符或键名（enter），收到 {item!r}"
            )
        if len(item) == 1:
            token = item
        else:
            token = item.lower()
            if token not in NAMED_KEYS:
                raise KeyTableError(
                    f"{source}：KEYS 里每一项必须是单个字符或键名（enter），收到 {item!r}"
                )
        cleaned.append(token)
    if len(set(cleaned)) != len(cleaned):
        raise KeyTableError(f"{source}：KEYS 里有重复的键")
    return KeyTable(base_note=base, keys=tuple(cleaned))


@lru_cache(maxsize=4)
def _read_table(path: str) -> KeyTable:
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyTableError(f"找不到键位表 {file}") from exc
    except json.JSONDecodeError as exc:
        raise KeyTableError(f"{file} 不是合法 JSON：{exc}") from exc
    return parse_key_table(data, file.name)


def load_key_table(path: str | Path | None = None) -> KeyTable:
    return _read_table(str(KEY_FILE if path is None else Path(path)))


def reload_key_table() -> None:
    """清缓存：改完 ``midi_keys.json`` 之后必须调一次（或点「重载键位」）。"""
    _read_table.cache_clear()


def key_for_note(note: int, transpose: int = 0, table: KeyTable | None = None) -> str | None:
    return (table or load_key_table()).key_for(note, transpose)


# ---------------------------------------------------------------- 排期


@dataclass(frozen=True)
class KeyAction:
    """一次键盘动作。``time`` 是相对播放起点的秒数。"""

    time: float
    kind: str  # "press" / "release"
    key: str
    note: int = 0
    name: str = ""
    tick: int = 0
    # 是否算一个「单位」（一个 MIDI 音），进度统计用它
    unit: bool = False


@dataclass
class KeySchedule:
    actions: list[KeyAction] = field(default_factory=list)
    #: 音高超出键位窗口、被跳过的音名（去重，按音高排序）
    skipped: list[str] = field(default_factory=list)
    #: 超出窗口但被八度投影拉回窗口的音（``"C3→C4"``，只在 project_octave 时有内容）
    projected: list[str] = field(default_factory=list)
    unit_label: str = "按键"

    @property
    def note_count(self) -> int:
        return sum(1 for action in self.actions if action.unit)


def project_note(
    note: int, table: KeyTable | None = None, transpose: int = 0
) -> int | None:
    """把超窗的音按八度移到键位窗口里；返回移动后的音高（已含移调），落不进去返回 None。

    只在 ±12 半音的整数倍上找，取|移动量|最小的那个；窗口 >= 12 个半音时一定有解。
    """
    table = table or load_key_table()
    if table.span <= 0:
        return None
    pitch = int(note) + int(transpose)
    base = table.base_note
    last = table.last_note
    if base <= pitch <= last:
        return pitch
    best: int | None = None
    for octave in range(-11, 12):
        candidate = pitch + 12 * octave
        if not base <= candidate <= last:
            continue
        if best is None or abs(candidate - pitch) < abs(best - pitch):
            best = candidate
    return best


def build_key_schedule(
    doc: MidiDocument,
    table: KeyTable | None = None,
    *,
    transpose: int = 0,
    tap_notes: bool = True,
    tap_seconds: float = 0.1,
    lead_in: float = LEAD_IN,
    repress_gap: float = KEY_REPRESS_GAP,
    project_octave: bool = False,
) -> KeySchedule:
    """MIDI 音符 → 按键动作序列（按时间排序）。

    - 每个音默认**点按** ``tap_seconds``（键盘游戏通常在按下那一刻判定）；
      ``tap_notes=False`` 时按住到音尾（长按类游戏用）；
    - **同一个键要再按时，先松开至少 ``repress_gap``**，否则游戏收不到第二次边沿；
    - 音高不在 ``[base_note, base_note + len(keys)) + transpose`` 里的音：
      ``project_octave=True`` 时按八度移到窗口里（记进 ``projected``），
      否则记进 ``skipped``，都不静默丢。
    """
    table = table or load_key_table()
    gap = max(0.005, min(0.2, float(repress_gap)))
    tap = max(0.01, min(2.0, float(tap_seconds)))
    shift = int(transpose)

    actions: list[KeyAction] = []
    skipped: list[str] = []
    projected: list[str] = []
    held_until: dict[str, float] = {}

    for note in sorted(doc.notes, key=lambda item: (item.start, item.note)):
        key = table.key_for(note.note, shift)
        if key is None and project_octave:
            landed = project_note(note.note, table, shift)
            if landed is not None:
                key = table.keys[landed - table.base_note]
                label = f"{note_name(note.note)}→{note_name(landed)}"
                if label not in projected:
                    projected.append(label)
        if key is None:
            name = note_name(note.note)
            if name not in skipped:
                skipped.append(name)
            continue

        press = lead_in + doc.seconds_at(note.start)
        end = lead_in + doc.seconds_at(note.end)
        hold = min(tap, max(MIN_PRESS, end - press)) if tap_notes else max(MIN_PRESS, end - press)

        previous = held_until.get(key)
        if previous is not None and press < previous + gap:
            press = previous + gap  # 同一个键不能按着再按
        release = press + hold

        actions.append(
            KeyAction(
                time=press,
                kind="press",
                key=key,
                note=note.note,
                name=note_name(note.note),
                tick=note.start,
                unit=True,
            )
        )
        actions.append(
            KeyAction(
                time=release,
                kind="release",
                key=key,
                note=note.note,
                name=note_name(note.note),
                tick=note.start,
            )
        )
        held_until[key] = release

    skipped.sort(key=_name_to_note)
    # 投影列表按原音高排序（标签形如 "C3→C4"，取箭头前那段来比）
    projected.sort(key=lambda item: _name_to_note(item.split("→", 1)[0]))
    priority = {"release": 0, "press": 1}
    actions.sort(key=lambda action: (round(action.time, 6), priority.get(action.kind, 2)))
    return KeySchedule(actions=actions, skipped=skipped, projected=projected)


def _name_to_note(name: str) -> int:
    """``C4`` → 60（``skipped`` 排序用；解析不了就当 0）。"""
    if not name:
        return 0
    try:
        octave = int(name[-1])
        step = name[:-1]
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        return (octave + 1) * 12 + names.index(step)
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------- 发送


if sys.platform == "win32":
    import ctypes.wintypes as wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("ki", _KEYBDINPUT),
            ("mi", _MOUSEINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    _user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(_INPUT),
        ctypes.c_int,
    )
    _user32.SendInput.restype = wintypes.UINT
    _user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
    _user32.VkKeyScanW.restype = ctypes.c_short
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _user32.MapVirtualKeyW.restype = wintypes.UINT


@lru_cache(maxsize=256)
def _scan_code(key: str) -> int:
    """键位表里的键 → 扫描码；需要 Shift 的键直接拒绝。"""
    if sys.platform != "win32":
        raise RuntimeError("只有 Windows 支持发送按键")
    named = NAMED_KEYS.get(key.lower()) if key.lower() in NAMED_KEYS else None
    if named is not None:
        vk = named
    elif len(key) == 1:
        vk_scan = _user32.VkKeyScanW(key)
        if vk_scan == -1:
            raise ValueError(f"无法把 {key!r} 转成虚拟键")
        vk = vk_scan & 0xFF
        if vk_scan >> 8:
            raise ValueError(f"{key!r} 需要 Shift/Alt 组合键，不支持")
    else:
        raise ValueError(f"键位表里只能写单个字符或 enter，收到 {key!r}")
    scan = _user32.MapVirtualKeyW(vk, 0)
    if not scan:
        raise ValueError(f"{key!r} 拿不到扫描码")
    return int(scan)


def _send(scan: int, up: bool) -> None:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    event = _INPUT(type=INPUT_KEYBOARD)
    event.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    sent = _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
    if sent != 1:
        error = ctypes.get_last_error()
        raise OSError(f"SendInput 失败（错误码 {error}）")


class KeyboardBackend:
    """``SendInput`` 的薄封装：按字符发按下/松开，带引用计数。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._held: dict[str, int] = {}
        self._error = "" if sys.platform == "win32" else "只有 Windows 支持发送按键"

    @property
    def available(self) -> bool:
        return not self._error

    @property
    def error(self) -> str:
        return self._error

    @property
    def connected(self) -> bool:
        return self.available

    def press(self, key: str) -> None:
        if not self.available:
            raise RuntimeError(self._error)
        scan = _scan_code(key)
        with self._lock:
            count = self._held.get(key, 0)
            self._held[key] = count + 1
            if count == 0:
                _send(scan, up=False)

    def release(self, key: str) -> None:
        if not self.available:
            return
        scan = _scan_code(key)
        with self._lock:
            count = self._held.get(key, 0)
            if count <= 0:
                return
            if count == 1:
                self._held.pop(key, None)
                _send(scan, up=True)
            else:
                self._held[key] = count - 1

    def release_all(self) -> None:
        if not self.available:
            return
        with self._lock:
            keys = list(self._held)
            self._held.clear()
        for key in keys:
            try:
                _send(_scan_code(key), up=True)
            except Exception:  # noqa: BLE001 - 松开失败不该抛给界面
                pass

    @property
    def held(self) -> list[str]:
        with self._lock:
            return list(self._held)
