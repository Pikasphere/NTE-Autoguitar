"""跨页面复用的小控件。"""

from __future__ import annotations

import flet as ft

__all__ = ["card", "MUTED"]

MUTED = ft.Colors.ON_SURFACE_VARIANT


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
