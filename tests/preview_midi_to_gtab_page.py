"""开发用：单独打开「MIDI→GTAB」页并载入内存示例，方便看排版 / 重拍截图。

环境变量：
- ``PREVIEW_WIDTH`` / ``PREVIEW_HEIGHT``：窗口逻辑尺寸；App 真实内容宽度是 1144；
- ``PREVIEW_STATE=keepall``：把「同时起音只保留最高音」关掉，截图里能看到和弦
  （默认是开，示例里的低音会被省略）。

运行：``.venv\\Scripts\\python.exe tests/preview_midi_to_gtab_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402

from ui.midi_doc import demo_document  # noqa: E402
from ui.pages.midi_to_gtab_page import MidiToGtabPage  # noqa: E402


def main(page: ft.Page) -> None:
    page.title = "MIDI→GTAB 预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = MidiToGtabPage(page)
    page.add(view.build())

    if os.environ.get("PREVIEW_STATE") == "keepall":
        view._sw_top.value = False
        view.keep_top_only = False
    view.load_document(demo_document())
    page.update()


if __name__ == "__main__":
    ft.run(main)
