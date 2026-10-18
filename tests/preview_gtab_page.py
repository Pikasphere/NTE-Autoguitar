"""开发用脚本：单独打开 GTAB 页面并载入示例谱，方便看排版效果。

环境变量：
- ``PREVIEW_WIDTH`` / ``PREVIEW_HEIGHT``：窗口逻辑尺寸；App 真实内容宽度是 1144；
- ``PREVIEW_STATE=play``：摆成「试听中」——露出红色游标和「试听中 x/y」进度文字
  （只摆状态，不真的合成/播音，避免截图时占住声卡）。

运行：``.venv\\Scripts\\python.exe tests/preview_gtab_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402
from ui.audio_preview import LEAD_IN_SECONDS, render_wav, wav_duration  # noqa: E402
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.pages.gtab_page import GtabPage  # noqa: E402


def _pose_playback(view: GtabPage) -> None:
    """把页面摆成「试听进行到 0.6s」：红线 + 进度文字，不真的播音。

    只改显示状态，不调 ``_on_preview``：截图脚本用 pythonw 启动，没有控制台，
    真播音会占住声卡还可能在截图期间结束、把游标收掉。
    """
    doc = view.doc
    total = wav_duration(render_wav(doc))
    elapsed = min(0.6, max(0.0, total - 0.1))
    # 和 _preview_ticker 一致：游标比进度文字慢一个开场留白（LEAD_IN_SECONDS）
    view._set_playhead_seconds(max(0.0, elapsed - LEAD_IN_SECONDS), doc)
    view._preview_label.value = f"试听中 {elapsed:.1f}s / {total:.1f}s"
    view._preview_label.color = ft.Colors.PRIMARY
    view._update()


def main(page: ft.Page) -> None:
    page.title = "GTAB 页面预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = GtabPage(page)
    view.doc = GtabDocument.from_path(GtabPage.DEMO_PATH)
    view.path = GtabPage.DEMO_PATH
    view._render_all()

    page.add(view.build())
    page.update()

    if os.environ.get("PREVIEW_STATE", "").strip().lower() == "play":
        _pose_playback(view)


if __name__ == "__main__":
    ft.run(main)
