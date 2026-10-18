"""开发用：单独打开「GCT 编辑」页并载入示例和弦谱，方便看排版。

环境变量：
- ``PREVIEW_WIDTH`` / ``PREVIEW_HEIGHT``：窗口逻辑尺寸；App 真实内容宽度是 1144。
- ``PREVIEW_STATE=play``：摆成「试听中」（把红色游标放在 ``PREVIEW_BEAT`` 拍、
  进度文字填上），**不真播音**——和 GTAB 页预览的 ``PREVIEW_STATE=play`` 一个意思。

运行：``.venv\\Scripts\\python.exe tests/preview_gct_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402

from ui.gct_doc import demo_document  # noqa: E402
from ui.components import PX_PER_BEAT  # noqa: E402
from ui.pages.gct_page import GctPage  # noqa: E402


def main(page: ft.Page) -> None:
    page.title = "GCT 页面预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = GctPage(page)
    view.doc = demo_document()
    view.path = None
    view._render_all()

    if os.environ.get("PREVIEW_STATE") == "play":
        # 只摆状态、不出声：游标 + 进度文字（截图脚本用 PREVIEW_STATE=play 选这张）
        beat = float(os.environ.get("PREVIEW_BEAT", "3"))
        view._playhead.left = beat * PX_PER_BEAT
        view._playhead.visible = True
        view._preview_label.value = "试听中 1.5s / 5.3s"
        view._preview_label.color = ft.Colors.PRIMARY

    page.add(view.build())
    page.update()


if __name__ == "__main__":
    ft.run(main)
