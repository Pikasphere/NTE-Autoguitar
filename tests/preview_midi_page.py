"""开发用：单独打开「MIDI 映射」页并载入内存示例，方便看排版。

环境变量：
- ``PREVIEW_WIDTH`` / ``PREVIEW_HEIGHT``：窗口逻辑尺寸；App 真实内容宽度是 1144。
- ``PREVIEW_STATE=play``：摆成「发送中」（红色游标放在 ``PREVIEW_BEAT`` 拍、
  进度文字填上），**不真发按键**——和另外两个预览页的 ``PREVIEW_STATE=play`` 一个意思。

运行：``.venv\\Scripts\\python.exe tests/preview_midi_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402

from ui.components import PX_PER_BEAT  # noqa: E402
from ui.midi_doc import demo_document  # noqa: E402
from ui.pages.midi_page import MidiPage  # noqa: E402


def main(page: ft.Page) -> None:
    page.title = "MIDI 页面预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = MidiPage(page)
    view.path = None
    view._set_doc(demo_document())   # 进度文字 / 信息卡都走真实路径
    # Capo 默认 0（不自动移调）：grey 块 = 超出键位窗口，能看出提示
    view._render_all()

    if os.environ.get("PREVIEW_STATE") == "play":
        # 只摆状态、不发按键：游标 + 进度文字（截图脚本用 PREVIEW_STATE=play 选这张）
        beat = float(os.environ.get("PREVIEW_BEAT", "3"))
        view._set_playhead_seconds(view.doc.seconds_at(int(beat * view.doc.division)))
        view._playhead.left = beat * PX_PER_BEAT
        view._progress.value = "发送中… 8/13 个按键 · 当前误差 1.8 ms"
        view._warning.value = "⚠ 1 个音超出键位窗口，已跳过：C3（调 Capo，或在设置页打开八度投影）"
        view._warning.visible = True

    page.add(view.build())
    page.update()


if __name__ == "__main__":
    ft.run(main)
