"""跨页面复用的小控件。"""

from __future__ import annotations

import flet as ft

__all__ = [
    "card",
    "MUTED",
    "PX_PER_BEAT",
    "LANE_HEIGHT",
    "RULER_HEIGHT",
    "BEATS_PER_BAR",
]

MUTED = ft.Colors.ON_SURFACE_VARIANT

# 节拍轴外观。GTAB 编辑页和 GCT 编辑页共用这几个数：
# 两页的刻度、行高、每小节拍数必须一致，否则同一个谱在两边看起来「对不上」。
PX_PER_BEAT = 120
LANE_HEIGHT = 22
RULER_HEIGHT = 24
BEATS_PER_BAR = 4


def card(title: str, body: ft.Control) -> ft.Control:
    """统一风格的卡片容器。"""
    return ft.Container(
        padding=16,
        border_radius=12,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Column(
            [ft.Text(title, size=14, weight=ft.FontWeight.BOLD), body],
            spacing=10,
        ),
    )
