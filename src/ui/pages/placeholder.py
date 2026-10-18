"""占位页面：只为演示主壳的页面切换，后续替换成真实页面。"""

from __future__ import annotations

import flet as ft

__all__ = ["PlaceholderPage"]


class PlaceholderPage:
    """一个居中的「建设中」提示页。"""

    def __init__(self, title: str, description: str, icon=None) -> None:
        self.title = title
        self.description = description
        self.icon = icon

    def build(self) -> ft.Control:
        return ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                [
                    ft.Icon(self.icon or ft.Icons.CONSTRUCTION, size=48, color=ft.Colors.OUTLINE),
                    ft.Text(self.title, size=20, weight=ft.FontWeight.BOLD),
                    ft.Text(self.description, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("（占位页面，后续实现）", size=11, color=ft.Colors.OUTLINE),
                ],
                spacing=10,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
