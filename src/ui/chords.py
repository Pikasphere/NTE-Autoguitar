"""chord.json → 手柄上的和弦位（4 页 × 6 个摇杆方向 = 24 个）+ 指法表。

游戏里和弦不是按「品」弹的，而是按**位置**：摇杆指到某一页的某一个方向，
再扫/拨弦。所以 24 个和弦就是 4 页 × 6 方向。``src/chord.json`` 里的顺序
就是分配顺序：

- 第 1 个（``G``）= 第 1 页正上方，之后顺时针：2 右上、3 右下、4 下、5 左下、6 左上；
- 第 7 个（``Cmaj7``）= 第 2 页正上方，依此类推；
- 最多 24 个，超出的不会用到（游戏只有 4 页）。

同一份文件里的 ``CHORDS_MAPPINGS`` 是每个和弦的**指法**（验证用）：

- 值固定 6 项，按 **1 弦 → 6 弦** 排列；
- 数字是品位（0 = 空弦，1–24 品）；
- ``-1`` 表示**这根弦不发声**，界面里显示成 ``x``（例如 ``Dm`` = ``1 3 2 0 x x``）。

指法只用于界面显示 / 人工核对**（包括 GCT 编辑页的和弦表）**，回放时的
页号 + 摇杆方向仍然只看 ``CHORD_NAMES`` 的顺序 —— 改指法不会影响手柄排期。

``chord.json`` 是**运行时读取**的：GCT 编辑页有「重载和弦表」按钮，
切回该页时也会重读一次，所以改完 json 不用重启程序。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "CHORD_FILE",
    "MAX_FRET",
    "MUTED",
    "MUTED_TEXT",
    "PER_PAGE",
    "SLOTS",
    "TOTAL_PAGES",
    "ChordError",
    "ChordTable",
    "chord_dir_index",
    "chord_fingering",
    "chord_page",
    "fingering_text",
    "load_chord_mappings",
    "load_chord_names",
    "load_chord_table",
    "name_at",
    "parse_chord_mappings",
    "parse_chord_names",
    "parse_chord_table",
    "position_of",
    "reload_chord_table",
    "slot_of",
    "unknown_names",
]

PER_PAGE = 6
TOTAL_PAGES = 4
SLOTS = PER_PAGE * TOTAL_PAGES

#: 指法里的「这根弦不发声」（界面显示成 ``x``）
MUTED = -1
MUTED_TEXT = "x"

#: 指法允许的最大品（和 gtab 的 max_fret 默认值一致；超出的当填写错误）
MAX_FRET = 24

CHORD_FILE = Path(__file__).resolve().parents[1] / "chord.json"


class ChordError(ValueError):
    """chord.json 缺失/不合法，或谱面用到了表里没有的和弦。"""


@dataclass(frozen=True)
class ChordTable:
    """chord.json 的内容：名字顺序（= 位置） + 名字 → 6 弦指法。"""

    names: tuple[str, ...]
    mappings: dict[str, tuple[int, ...]]

    def fingering(self, name: str) -> tuple[int, ...] | None:
        return self.mappings.get(name)

    def fingering_text(self, name: str) -> str:
        mapping = self.mappings.get(name)
        return "" if mapping is None else fingering_text(mapping)

    def missing_mappings(self) -> list[str]:
        """CHORD_NAMES 里有哪些还没有指法。"""
        return [name for name in self.names if name not in self.mappings]

    def unknown_mappings(self) -> list[str]:
        """CHORDS_MAPPINGS 里有哪些名字不在 CHORD_NAMES 里（拼错/多余）。"""
        return sorted(name for name in self.mappings if name not in self.names)


# ---------------------------------------------------------------- 解析 / 校验


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


def parse_chord_mappings(raw: object, source: str = "chord.json") -> dict[str, tuple[int, ...]]:
    """校验 ``CHORDS_MAPPINGS``：每个名字 6 个整数，按 1–6 弦；-1 = 不发声。"""
    if not isinstance(raw, dict):
        raise ChordError(f"{source} 顶层必须是对象")
    table = raw.get("CHORDS_MAPPINGS", {})
    if table is None:
        table = {}
    if not isinstance(table, dict):
        raise ChordError(f"{source} 的 CHORDS_MAPPINGS 必须是对象（和弦名 → 6 个品位）")

    mappings: dict[str, tuple[int, ...]] = {}
    for name, value in table.items():
        if not isinstance(name, str) or not name.strip():
            raise ChordError(f"{source} 的 CHORDS_MAPPINGS 里有非法和弦名：{name!r}")
        chord = name.strip()
        if not isinstance(value, (list, tuple)):
            raise ChordError(f"{source} 里和弦 {chord} 的指法必须是数组")
        if len(value) != 6:
            raise ChordError(
                f"{source} 里和弦 {chord} 的指法要有 6 项（1–6 弦），现在是 {len(value)} 项"
            )
        frets: list[int] = []
        for index, fret in enumerate(value, start=1):
            if isinstance(fret, bool) or not isinstance(fret, int):
                raise ChordError(
                    f"{source} 里和弦 {chord} 第 {index} 弦的品位不是整数：{fret!r}"
                )
            if fret != MUTED and not (0 <= fret <= MAX_FRET):
                raise ChordError(
                    f"{source} 里和弦 {chord} 第 {index} 弦的品位要在 0–{MAX_FRET}，"
                    f"或用 -1 表示不发声；现在是 {fret}"
                )
            frets.append(fret)
        mappings[chord] = tuple(frets)
    return mappings


def parse_chord_table(raw: object, source: str = "chord.json") -> ChordTable:
    """名字 + 指法一起校验（缺指法/多余名字不算错，由界面提示）。"""
    names = parse_chord_names(raw, source)
    mappings = parse_chord_mappings(raw, source)
    return ChordTable(names, mappings)


@lru_cache(maxsize=8)
def _read_table(path: str) -> ChordTable:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ChordError(f"读不到和弦表 {file}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ChordError(f"和弦表 {file.name} 不是合法 JSON：{exc}") from exc
    return parse_chord_table(raw, file.name)


def load_chord_table(path: str | Path | None = None) -> ChordTable:
    """读取整张表（带缓存；改完 json 调 ``reload_chord_table()``）。"""
    return _read_table(str(CHORD_FILE if path is None else Path(path)))


def reload_chord_table() -> None:
    """丢掉缓存，下次 ``load_chord_table`` 会重新读 json（改完指法不用重启）。"""
    _read_table.cache_clear()


def load_chord_names(path: str | Path | None = None) -> tuple[str, ...]:
    """读取和弦名列表（按 chord.json 的顺序，带缓存）。"""
    return load_chord_table(path).names


def load_chord_mappings(path: str | Path | None = None) -> dict[str, tuple[int, ...]]:
    """读取「和弦名 → 6 弦指法」；``-1`` 表示该弦不发声。"""
    return dict(load_chord_table(path).mappings)


def fingering_text(mapping: tuple[int, ...] | list[int]) -> str:
    """指法 → 一行文本：``-1`` 显示 ``x``，1 弦在左、6 弦在右。"""
    return " ".join(
        MUTED_TEXT if int(fret) == MUTED else str(int(fret)) for fret in mapping
    )


def chord_fingering(
    name: str, table: ChordTable | None = None
) -> tuple[int, ...] | None:
    """和弦名 → 指法；没填指法返回 ``None``。"""
    return (load_chord_table() if table is None else table).fingering(name)


# ---------------------------------------------------------------- 位置换算


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
