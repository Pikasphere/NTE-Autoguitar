"""全局偏好设置：读写 ``src/settings.json``，以及「全局事件 CRC」的落地函数。

只有两个开关，都**默认关**：

- ``global_event_crc``：所有 gtab / gct 文档保存时都强制带事件 CRC
  （两个编辑页已没有单独的「事件 CRC」开关，这里是唯一控制点）；
- ``midi_project_octave``：MIDI 音高超出键位窗口时按八度投影（±12 半音直到落进窗口），
  而不是跳过并报错。

设置文件是**用户偏好**，不进版本库（``.gitignore`` 里有 ``src/settings.json``）。
文件不存在、坏 JSON、字段类型不对都一律回默认值，**不抛异常**——设置坏了不该让程序起不来。

模型层不依赖界面，可以直接单测；页面上改开关走 :func:`set_settings`（默认立即写盘）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from paths import settings_path

__all__ = [
    "DEFAULTS",
    "SETTINGS_FILE",
    "AppSettings",
    "force_event_crc",
    "get_settings",
    "load_settings",
    "reload_settings",
    "save_settings",
    "set_settings",
    "update_settings",
]

#: 设置文件位置：源码运行是 ``src/settings.json``；打包后是
#: ``%APPDATA%\\NTE-Autoguitar\\settings.json``（单文件 exe 的资源目录退出即删）。
SETTINGS_FILE = settings_path()


@dataclass(frozen=True)
class AppSettings:
    """两个全局开关。字段名就是 JSON 的键名。"""

    global_event_crc: bool = False
    midi_project_octave: bool = False


DEFAULTS = AppSettings()

#: 缓存的设置；``None`` 表示还没读过（或刚被 :func:`reload_settings` 清掉）。
_cache: AppSettings | None = None


def _as_bool(value: object, fallback: bool) -> bool:
    """只认真正的布尔；别的类型（含 ``"true"`` 字符串）一律用默认值。"""
    return value if isinstance(value, bool) else fallback


def load_settings(path: str | Path | None = None) -> AppSettings:
    """读设置文件；不存在 / 坏 JSON / 不是对象 → 返回默认值（不抛）。"""
    file = Path(path) if path is not None else SETTINGS_FILE
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULTS
    if not isinstance(raw, dict):
        return DEFAULTS
    return AppSettings(
        global_event_crc=_as_bool(raw.get("global_event_crc"), DEFAULTS.global_event_crc),
        midi_project_octave=_as_bool(
            raw.get("midi_project_octave"), DEFAULTS.midi_project_octave
        ),
    )


def save_settings(settings: AppSettings, path: str | Path | None = None) -> Path:
    """把设置写盘（紧凑 JSON + 换行）；返回写入的文件路径。

    打包运行时会写到 ``%APPDATA%\\NTE-Autoguitar``，目录可能还不存在，这里顺手建出来。
    """
    file = Path(path) if path is not None else SETTINGS_FILE
    payload = {
        "global_event_crc": bool(settings.global_event_crc),
        "midi_project_octave": bool(settings.midi_project_octave),
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return file


def get_settings() -> AppSettings:
    """取当前设置（带缓存；第一次调用才读盘）。"""
    global _cache
    if _cache is None:
        _cache = load_settings()
    return _cache


def set_settings(settings: AppSettings, persist: bool = True) -> AppSettings:
    """更新缓存（``persist=True`` 时同时写盘），返回写入后的设置。

    写盘失败（只读目录 / 磁盘满）会抛 ``OSError``，**但缓存已经更新**——
    本次运行仍然按新设置走，界面上应提示「没保存下来」。
    """
    global _cache
    _cache = settings
    if persist:
        save_settings(settings)
    return settings


def reload_settings() -> AppSettings:
    """丢掉缓存重新读盘（外部改了 settings.json 之后用）。"""
    global _cache
    _cache = None
    return get_settings()


def force_event_crc(doc, settings: AppSettings | None = None) -> bool:
    """全局 CRC 打开时，把 ``doc`` 的事件 CRC 置为开；返回是否真的改了。

    鸭子类型：优先用文档的 ``event_crc_enabled`` 属性（gtab / gct 两个模型都有），
    没有该属性时退回直接改 ``flags`` 的 bit0。``doc`` 为 ``None`` 时返回 ``False``。
    """
    if doc is None:
        return False
    active = get_settings() if settings is None else settings
    if not active.global_event_crc:
        return False

    if hasattr(type(doc), "event_crc_enabled"):
        doc.event_crc_enabled = True
        return True

    flags = getattr(doc, "flags", None)
    if not isinstance(flags, int):
        return False
    doc.flags = flags | 0x01
    return True


def update_settings(**changes) -> AppSettings:
    """按字段改设置（未知字段忽略），写盘并返回新设置。"""
    allowed = {field: value for field, value in changes.items() if hasattr(DEFAULTS, field)}
    return set_settings(replace(get_settings(), **allowed))
