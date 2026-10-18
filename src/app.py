"""主 GUI 外壳：左侧导航 + 右侧页面容器。

``main.py`` 只负责启动，界面组织都在这里。
新增一个页面 = 在 ``_entries()`` 里加一行；页面本身只要能返回一个
``ft.Control`` 即可（见 ui/pages/gtab_page.py）。
"""

from __future__ import annotations

import flet as ft

from ui.pages.gamepad_page import GamepadPage
from ui.pages.gtab_page import GtabPage
from ui.pages.placeholder import PlaceholderPage

__all__ = ["APP_NAME", "main"]

APP_NAME = "NTE Autoguitar"
APP_SUBTITLE = "吉他谱工具集"

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 840
WINDOW_MIN_WIDTH = 1020
WINDOW_MIN_HEIGHT = 640


def main(page: ft.Page) -> None:
    """Flet 入口：搭好外壳，把 GTAB 页面作为其中一页挂上去。"""
    page.title = f"{APP_NAME} · {APP_SUBTITLE}"
    page.padding = 0
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)

    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    page.window.min_width = WINDOW_MIN_WIDTH
    page.window.min_height = WINDOW_MIN_HEIGHT

    gtab_page = GtabPage(page)
    gamepad_page = GamepadPage(page)

    entries: list[tuple[str, object, ft.Control]] = [
        ("GTAB 编辑", ft.Icons.MUSIC_NOTE, gtab_page.build()),
        ("手柄测试", ft.Icons.SPORTS_ESPORTS, gamepad_page.build()),
        (
            "MIDI 转换",
            ft.Icons.GRAPHIC_EQ,
            PlaceholderPage(
                "MIDI 转换", "把 gtab 事件导出成 MIDI，或把 MIDI 读回来。"
            ).build(),
        ),
        (
            "设置",
            ft.Icons.SETTINGS,
            PlaceholderPage("设置", "音色、默认定弦之类的偏好设置。").build(),
        ),
    ]

    body = ft.Container(content=entries[0][2], expand=True)

    rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=96,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        destinations=[
            ft.NavigationRailDestination(icon=icon, label=label)
            for label, icon, _ in entries
        ],
    )

    def _on_select(event) -> None:
        index = event.control.selected_index or 0
        # 只有停在本页时才注册全局热键 F9/F10；切走就注销，避免抢占游戏按键
        gamepad_page.set_active(index == 1)
        if index != 1:
            # 离开手柄页时停止回放，避免虚拟手柄一直按着
            gamepad_page.stop()
        if index != 0:
            # 离开 GTAB 页时停掉试听，避免音频在后台继续响
            gtab_page.stop_preview()
        body.content = entries[index][2]
        page.update()

    rail.on_change = _on_select

    page.add(
        ft.Row(
            [rail, ft.VerticalDivider(width=1), body],
            expand=True,
            spacing=0,
        )
    )
