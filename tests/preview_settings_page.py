"""开发用：单独打开「设置」页，方便看排版 / 重拍截图。

环境变量：
- ``PREVIEW_WIDTH`` / ``PREVIEW_HEIGHT``：窗口逻辑尺寸；App 真实内容宽度是 1144。
- ``PREVIEW_STATE=on``：把两个开关**画成**打开的样子（只改控件值，不写 settings.json，
  也不触发回调）——截图里能同时看到「开」的样式。

运行：``.venv\\Scripts\\python.exe tests/preview_settings_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402

from ui.pages.settings_page import SettingsPage  # noqa: E402


def main(page: ft.Page) -> None:
    page.title = "设置页面预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = SettingsPage(page)
    if os.environ.get("PREVIEW_STATE") == "on":
        # 只摆视觉状态：不动 settings.json，也不触发页面回调
        view._sw_crc.value = True
        view._sw_project.value = True
        view._status.value = "已保存"
        view._status.color = ft.Colors.ON_SURFACE_VARIANT

    page.add(view.build())
    page.update()


if __name__ == "__main__":
    ft.run(main)
