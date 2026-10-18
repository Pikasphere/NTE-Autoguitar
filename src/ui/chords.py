"""chord.json → 手柄上的和弦位（4 页 × 6 个摇杆方向 = 24 个）。

游戏里和弦不是按「品」弹的，而是按**位置**：摇杆指到某一页的某一个方向，
再扫/拨弦。所以 24 个和弦就是 4 页 × 6 方向。``src/chord.json`` 里的顺序
就是分配顺序：

- 第 1 个（``G``）= 第 1 页正上方，之后顺时针：2 右上、3 右下、4 下、5 左下、6 左上；
- 第 7 个（``Cmaj7``）= 第 2 页正上方，依此类推；
- 最多 24 个，超出的不会用到（游戏只有 4 页）。

.gct 文件里存的是**和弦名**（自己的和弦表），页号/方向由这里的名字查出来，
所以同一个名字在任何 .gct 里都落在同一个位置。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

__all__ = [
    "CHORD_FILE",
    "PER_PAGE",
    "SLOTS",
    "TOTAL_PAGES",
    "ChordError",
    "chord_dir_index",
    "chord_page",
    "load_chord_names",
    "name_at",
    "position_of",
    "slot_of",
    "unknown_names",
]

PER_PAGE = 6
TOTAL_PAGES = 4
SLOTS = PER_PAGE * TOTAL_PAGES

CHORD_FILE = Path(__file__).resolve().parents[1] / "chord.json"


class ChordError(ValueError):
    """chord.json 缺失/不合法，或谱面用到了表里没有的和弦。"""


@lru_cache(maxsize=4)
def _read(path: str) -> tuple[str, ...]:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ChordError(f"读不到和弦表 {file}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ChordError(f"和弦表 {file.name} 不是合法 JSON：{exc}") from exc
    return parse_chord_names(raw, file.name)


def parse_chord_names(raw: object, source: str = "chord.json") -> tuple[str, ...]:
    """校验并整理和弦名列表（``raw`` 是 JSON 解析后的对象）。"""
    names = raw.get("CHORD_NAMES") if isinstance(raw, dict) else None
    if not isinstance(names, list) or not names:
        raise ChordError(f"{source} 里没有非空的 CHORD_NAMES 列表")

    cleaned: list[str] = []
    for index, name in enumerate(names, start=1):
        if not isinstance(name, str) or not name.strip():
            raise ChordError(f"{source} 第 {index} 个和弦不是非空字符串：{name!r}")
        cleaned.append(name.strip())
    if len(set(cleaned)) != len(cleaned):
        duplicates = sorted({n for n in cleaned if cleaned.count(n) > 1})
        raise ChordError(f"{source} 里有重复的和弦名：{'、'.join(duplicates)}")
    if len(cleaned) > SLOTS:
        raise ChordError(
            f"{source} 有 {len(cleaned)} 个和弦，但只有 {TOTAL_PAGES} 页 × "
            f"{PER_PAGE} 个方向 = {SLOTS} 个位置"
        )
    return tuple(cleaned)


def load_chord_names(path: str | Path | None = None) -> tuple[str, ...]:
    """读取和弦名列表（按 chord.json 的顺序，带缓存）。"""
    return _read(str(CHORD_FILE if path is None else Path(path)))


def slot_of(name: str, names: tuple[str, ...] | None = None) -> int | None:
    """和弦名 → 0 开始的位置序号（0–23）；表里没有就返回 ``None``。"""
    table = load_chord_names() if names is None else names
    try:
        return table.index(name)
    except ValueError:
        return None


def position_of(name: str, names: tuple[str, ...] | None = None) -> tuple[int, int] | None:
    """和弦名 → ``(页号 1–4, 方向序号 0–5)``；表里没有就返回 ``None``。"""
    slot = slot_of(name, names)
    if slot is None:
        return None
    return slot // PER_PAGE + 1, slot % PER_PAGE


def chord_page(name: str, names: tuple[str, ...] | None = None) -> int | None:
    position = position_of(name, names)
    return position[0] if position else None


def chord_dir_index(name: str, names: tuple[str, ...] | None = None) -> int | None:
    position = position_of(name, names)
    return position[1] if position else None


def name_at(page: int, index: int, names: tuple[str, ...] | None = None) -> str:
    """第 ``page`` 页（1–4）第 ``index`` 个方向（0–5）上是哪个和弦；没有就返回空串。"""
    table = load_chord_names() if names is None else names
    slot = (page - 1) * PER_PAGE + index
    if 0 <= slot < len(table):
        return table[slot]
    return ""


def unknown_names(names, path: str | Path | None = None) -> list[str]:
    """``names`` 里哪些不在和弦表里（排期前用来报错）。"""
    table = load_chord_names(path)
    return [name for name in names if name not in table]
