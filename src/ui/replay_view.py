"""手柄页左侧的「回放轮盘」：实时画出回放正在发的摇杆方向和按下的弦。

- **轮盘**：六个 60° 扇区 = 当前页的六个品。正上方是页内第 1 个品，顺时针每
  60° 一个，和 ``gamepad_backend.fret_direction()`` 完全一致；扇区上写的是
  **绝对品号**（第 2 页就是 7–12），空弦页六个方向都是「空」。
- **中心**：当前绝对品号；空弦显示「空弦」。
- **六根弦**：6 弦在最上、1 弦在最下（和吉他谱一致），正在按的那根会高亮，
  右侧写出它的品号。

``WheelState`` 只存数据、不依赖 Flet，可以直接单测；``ReplayWheel`` 是控件。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import flet as ft
import flet.canvas as cv

from ui.components import MUTED
from ui.gamepad_backend import OPEN_PAGE, PAGE_SIZE, page_for

__all__ = [
    "ReplayWheel",
    "STRING_ORDER",
    "WHEEL_SIZE",
    "WheelState",
    "chord_labels",
    "describe",
    "sector_angle",
    "sector_labels",
    "sector_of",
]

# 弦的显示顺序：6 弦在上、1 弦在下（和吉他谱一致）
STRING_ORDER: tuple[int, ...] = (6, 5, 4, 3, 2, 1)

# 轮盘尺寸按「和弦名」留位置：gtab 的品号用同一套几何，只是字短一些
WHEEL_SIZE = 280
_CENTER = WHEEL_SIZE / 2
_R_OUTER = 120.0
_R_LABEL = 84.0     # 标签靠外圈放，别让指针穿过文字
_R_HUB = 44.0
_R_NEEDLE_IN = 48.0   # 指针从中心圆外沿开始（内圆会盖住它）
_R_NEEDLE = 72.0      # 到标签之前收住
_LABEL_W = 56.0       # 和弦名最长 5–6 个字符（Cmaj9 / Am7/G / D/F#）
_LABEL_H = 20.0

ACCENT = ft.Colors.INDIGO_400
ACCENT_STRONG = ft.Colors.INDIGO_600
OUTLINE = ft.Colors.OUTLINE_VARIANT
SURFACE = ft.Colors.SURFACE_CONTAINER_LOWEST

# 6 弦最粗、1 弦最细，看起来像一把琴的弦
_STRING_THICK: dict[int, int] = {6: 4, 5: 4, 4: 3, 3: 3, 2: 2, 1: 2}


# ------------------------------------------------------------------ 纯逻辑


def sector_of(fret: int) -> int | None:
    """方向序号（fret = 方向序号 + 1）→ 扇区 0–5；空弦 / 未知返回 ``None``。"""
    if fret <= 0:
        return None
    return (fret - 1) % PAGE_SIZE


def sector_angle(index: int) -> float:
    """扇区中心的角度（度）。0° = 正右方，顺时针为正，-90° = 正上方。"""
    return -90.0 + index * 60.0


def sector_labels(page: int, fret: int) -> list[str]:
    """六个扇区上写什么：绝对品号；空弦（第 5 页）六个方向都是「空」。"""
    if page <= 0:
        return [str(index + 1) for index in range(PAGE_SIZE)]
    if fret <= 0:
        return ["空"] * PAGE_SIZE
    base = (page - 1) * PAGE_SIZE
    return [str(base + index + 1) for index in range(PAGE_SIZE)]


def chord_labels(page: int, names: tuple[str, ...] | list[str]) -> list[str]:
    """gct 模式：第 ``page`` 页六个方向上分别是哪个和弦（没有就留空）。"""
    page = page if page >= 1 else 1
    base = (page - 1) * PAGE_SIZE
    result: list[str] = []
    for index in range(PAGE_SIZE):
        slot = base + index
        result.append(names[slot] if 0 <= slot < len(names) else "")
    return result


@dataclass
class WheelState:
    """轮盘要显示的状态：页号、当前品/方向、正在按住的弦。"""

    page: int = 0                 # 0 = 还没开始
    fret: int = -1                # -1 = 未知；0 = 空弦；gct 里是「方向序号 + 1」
    held: dict[int, int] = field(default_factory=dict)   # 弦号 → 品/方向
    chord: str = ""               # gct：当前和弦名（gtab 恒为空）

    def reset(self) -> None:
        self.page = 0
        self.fret = -1
        self.held.clear()

    def apply(self, action: Any) -> None:
        """按一条 ``ScheduleAction`` 推进状态（翻页、摇杆、按下、松开、扫弦亮灯）。"""
        kind = getattr(action, "kind", "")
        page = int(getattr(action, "page", 0) or 0)
        fret = int(getattr(action, "fret", 0) or 0)
        string = int(getattr(action, "string", 0) or 0)

        chord = getattr(action, "chord", "") or ""

        if kind == "stick":
            self.fret = fret
            self.page = page or page_for(fret)
        elif kind == "press":
            if page:                     # 翻页/扫弦动作也带页号，先让页号跟着变
                self.page = page
            if string:
                self.held[string] = fret
                self.fret = fret
                if not page:
                    self.page = page_for(fret)
        elif kind == "release" and string:
            self.held.pop(string, None)
        elif kind in ("light", "dark") and string:
            # gct 扫弦：右摇杆推一下，轮盘上的六根弦按顺序亮/灭（纯显示）
            if page:
                self.page = page
            if kind == "light":
                self.held[string] = fret
                self.fret = fret
            else:
                self.held.pop(string, None)
        elif kind == "page":
            # 翻页也是右摇杆推一下（左 = 下一页 / 右 = 上一页），只改页号
            if page:
                self.page = page
        elif kind == "rstick":
            # 右摇杆只表示扫弦手势/回中，不改变按弦状态
            pass
        if chord:
            self.chord = chord


def describe(state: WheelState, chord_mode: bool = False) -> str:
    """轮盘下面那行状态文字。"""
    if state.page <= 0:
        return "就绪：点「播放」开始回放"
    held = [f"{string} 弦" for string in STRING_ORDER if string in state.held]
    if chord_mode:
        head = f"第 {state.page} 页 · {state.chord or '—'}"
        return head + (" · 正在弹 " + "、".join(held) if held else " · 未按弦")
    if state.fret <= 0:
        head = f"第 {OPEN_PAGE} 页 · 空弦"
    else:
        head = f"第 {state.page} 页 · {state.fret} 品"
    if held:
        return head + " · 按住 " + "、".join(held)
    return head + " · 未按弦"


# ------------------------------------------------------------------ 控件


class ReplayWheel:
    """轮盘控件：``apply(action)`` / ``reset()`` 后由页面统一 ``page.update()``。"""

    def __init__(self, chord_mode: bool = False) -> None:
        self.state = WheelState()
        # gct 模式：扇区写和弦名、中心写当前和弦；gtab 模式写品号
        self.chord_mode = chord_mode
        self.chord_names: tuple[str, ...] = ()
        self._labels: list[ft.Text] = []
        self._rows: dict[int, tuple[ft.Container, ft.Text, ft.Container, ft.Text]] = {}
        self._build_canvas()
        self._root = self._build()
        self.refresh()

    def set_chord_mode(self, enabled: bool, names: tuple[str, ...] | None = None) -> None:
        """切换 gtab（品号）/ gct（和弦名）显示。``names`` 是 chord.json 的顺序。"""
        self.chord_mode = bool(enabled)
        if names is not None:
            self.chord_names = tuple(names)
        self.refresh()

    # -------------------------------------------------------------- 组装

    def build(self) -> ft.Control:
        return self._root

    def _build_canvas(self) -> None:
        cx = cy = _CENTER
        shapes: list[cv.Shape] = []

        # 高亮扇区（用透明色代替 visible —— canvas 的 Shape 没有 visible 字段）
        self._fill = cv.Arc(
            cx - _R_OUTER,
            cy - _R_OUTER,
            _R_OUTER * 2,
            _R_OUTER * 2,
            start_angle=0,
            sweep_angle=0,
            use_center=True,
            paint=ft.Paint(color=ft.Colors.TRANSPARENT, style=ft.PaintingStyle.FILL),
        )
        shapes.append(self._fill)

        # 6 条分界线：从正上方 ±30° 起，每 60° 一条
        for index in range(PAGE_SIZE):
            angle = math.radians(sector_angle(index) + 30.0)
            shapes.append(
                cv.Line(
                    cx,
                    cy,
                    cx + _R_OUTER * math.cos(angle),
                    cy + _R_OUTER * math.sin(angle),
                    paint=ft.Paint(color=OUTLINE, stroke_width=1),
                )
            )

        shapes.append(
            cv.Circle(
                cx,
                cy,
                _R_OUTER,
                paint=ft.Paint(
                    color=OUTLINE,
                    style=ft.PaintingStyle.STROKE,
                    stroke_width=1.5,
                ),
            )
        )

        self._needle = cv.Line(
            cx,
            cy,
            cx,
            cy - _R_NEEDLE,
            paint=ft.Paint(color=ft.Colors.TRANSPARENT, stroke_width=3),
        )
        shapes.append(self._needle)

        self._hub = cv.Circle(
            cx,
            cy,
            _R_HUB,
            paint=ft.Paint(color=SURFACE, style=ft.PaintingStyle.FILL),
        )
        shapes.append(self._hub)
        shapes.append(
            cv.Circle(
                cx,
                cy,
                _R_HUB,
                paint=ft.Paint(
                    color=OUTLINE,
                    style=ft.PaintingStyle.STROKE,
                    stroke_width=1.5,
                ),
            )
        )
        self._canvas = cv.Canvas(shapes=shapes, width=WHEEL_SIZE, height=WHEEL_SIZE)

    def _build(self) -> ft.Control:
        cx = cy = _CENTER
        pieces: list[ft.Control] = [self._canvas]

        # 六个品号 / 和弦名：按扇区中心的角度绝对定位到 Stack 上
        for index in range(PAGE_SIZE):
            angle = math.radians(sector_angle(index))
            x = cx + _R_LABEL * math.cos(angle)
            y = cy + _R_LABEL * math.sin(angle)
            label = ft.Text("", size=11, color=MUTED, text_align=ft.TextAlign.CENTER)
            self._labels.append(label)
            pieces.append(
                ft.Container(
                    content=label,
                    left=x - _LABEL_W / 2,
                    top=y - _LABEL_H / 2,
                    width=_LABEL_W,
                    height=_LABEL_H,
                    alignment=ft.Alignment.CENTER,
                )
            )

        self._center_big = ft.Text(
            "—",
            size=18,
            weight=ft.FontWeight.BOLD,
            color=MUTED,
            text_align=ft.TextAlign.CENTER,
        )
        self._center_small = ft.Text(
            "就绪", size=10, color=MUTED, text_align=ft.TextAlign.CENTER
        )
        pieces.append(
            ft.Container(
                content=ft.Column(
                    [self._center_big, self._center_small],
                    spacing=0,
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                left=cx - _R_HUB,
                top=cy - _R_HUB,
                width=_R_HUB * 2,
                height=_R_HUB * 2,
                alignment=ft.Alignment.CENTER,
            )
        )

        wheel = ft.Stack(pieces, width=WHEEL_SIZE, height=WHEEL_SIZE)

        # 六根弦：6 弦在上、1 弦在下
        rows: list[ft.Control] = []
        for string in STRING_ORDER:
            name = ft.Text(f"{string} 弦", size=11, color=MUTED, width=28)
            line = ft.Container(
                height=_STRING_THICK[string],
                expand=True,
                bgcolor=OUTLINE,
                border_radius=2,
            )
            fret_text = ft.Text(
                "", size=11, color=MUTED, width=44, text_align=ft.TextAlign.RIGHT
            )
            box = ft.Container(
                content=ft.Row(
                    [name, line, fret_text],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.Padding.symmetric(vertical=3, horizontal=6),
                border_radius=6,
                bgcolor=ft.Colors.TRANSPARENT,
            )
            rows.append(box)
            self._rows[string] = (box, name, line, fret_text)

        self._state_text = ft.Text("", size=11, color=MUTED, text_align=ft.TextAlign.CENTER)
        self._caption = ft.Text(
            "轮盘 = 摇杆方向（品），六根弦 = 正在按的弦（6 弦在上）",
            size=11,
            color=MUTED,
            text_align=ft.TextAlign.CENTER,
        )

        return ft.Column(
            [
                self._caption,
                ft.Row([wheel], alignment=ft.MainAxisAlignment.CENTER),
                ft.Column(rows, spacing=3),
                self._state_text,
            ],
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    # -------------------------------------------------------------- 刷新

    def apply(self, action: Any) -> None:
        self.state.apply(action)
        self.refresh()

    def reset(self) -> None:
        self.state.reset()
        self.refresh()

    def refresh(self) -> None:
        state = self.state
        sector = sector_of(state.fret)
        if self.chord_mode:
            labels = chord_labels(state.page or 1, self.chord_names)
        else:
            labels = sector_labels(state.page, state.fret)

        # 和弦名比品号长：字体小一号、标签框已经按最长的和弦名留了位置
        for index, text in enumerate(self._labels):
            text.value = labels[index]
            active = sector == index
            text.color = ACCENT_STRONG if active else MUTED
            text.weight = ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL
            text.size = 10 if self.chord_mode else 11

        if sector is None:
            self._fill.paint = ft.Paint(
                color=ft.Colors.TRANSPARENT, style=ft.PaintingStyle.FILL
            )
            self._needle.paint = ft.Paint(
                color=ft.Colors.TRANSPARENT, stroke_width=3
            )
            is_open = state.fret == 0
            self._hub.paint = ft.Paint(
                color=(
                    ft.Colors.with_opacity(0.16, ACCENT) if is_open else SURFACE
                ),
                style=ft.PaintingStyle.FILL,
            )
            self._center_big.value = "空弦" if is_open else "—"
            self._center_big.size = 18
            self._center_small.value = (
                f"第 {OPEN_PAGE} 页" if is_open else ("就绪" if state.page <= 0 else "")
            )
        else:
            start = math.radians(sector_angle(sector) - 30.0)
            self._fill.start_angle = start
            self._fill.sweep_angle = math.radians(60.0)
            self._fill.paint = ft.Paint(
                color=ft.Colors.with_opacity(0.16, ACCENT),
                style=ft.PaintingStyle.FILL,
            )

            angle = math.radians(sector_angle(sector))
            self._needle.x1 = _CENTER + _R_NEEDLE_IN * math.cos(angle)
            self._needle.y1 = _CENTER + _R_NEEDLE_IN * math.sin(angle)
            self._needle.x2 = _CENTER + _R_NEEDLE * math.cos(angle)
            self._needle.y2 = _CENTER + _R_NEEDLE * math.sin(angle)
            self._needle.paint = ft.Paint(
                color=ACCENT_STRONG,
                stroke_width=3,
                stroke_cap=ft.StrokeCap.ROUND,
            )

            self._hub.paint = ft.Paint(color=SURFACE, style=ft.PaintingStyle.FILL)
            if self.chord_mode:
                name = state.chord or labels[sector]
                self._center_big.value = name or "—"
                self._center_big.size = 15 if len(name) <= 3 else 12
            else:
                self._center_big.value = str(state.fret)
                self._center_big.size = 18
            self._center_small.value = (
                f"第 {state.page} 页" if self.chord_mode else "品"
            )

        for string in STRING_ORDER:
            box, name, line, fret_text = self._rows[string]
            if string in state.held:
                box.bgcolor = ft.Colors.with_opacity(0.10, ACCENT)
                name.color = ACCENT_STRONG
                name.weight = ft.FontWeight.BOLD
                line.bgcolor = ACCENT_STRONG
                line.height = _STRING_THICK[string] + 2
                fret_text.value = "" if self.chord_mode else f"{state.held[string]} 品"
                fret_text.color = ACCENT_STRONG
                fret_text.weight = ft.FontWeight.BOLD
            else:
                box.bgcolor = ft.Colors.TRANSPARENT
                name.color = MUTED
                name.weight = ft.FontWeight.NORMAL
                line.bgcolor = OUTLINE
                line.height = _STRING_THICK[string]
                fret_text.value = ""
                fret_text.color = MUTED
                fret_text.weight = ft.FontWeight.NORMAL

        if self.chord_mode:
            self._caption.value = "轮盘 = 和弦位（4 页 × 6 方向），六根弦 = 正在扫/拨的弦"
        else:
            self._caption.value = "轮盘 = 摇杆方向（品），六根弦 = 正在按的弦（6 弦在上）"
        self._state_text.value = describe(state, self.chord_mode)
