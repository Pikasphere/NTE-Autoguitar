"""开发用：单独看「回放轮盘」，并把它摆成回放中的样子（截图用）。

默认摆 gtab 示例谱第二音按下那一刻：摇杆在第 1 页 2 品，6 弦空弦还按着、5 弦刚按下。

环境变量：
- ``PREVIEW_SCORE=gct``：切到和弦模式，用最后一页的和弦（名字最长）摆成下扫第 5 根；
- ``PREVIEW_SCALE=1.6``：把整块放大（截图核对和弦名会不会顶出外圈 / 压住指针）。

运行：``.venv\\Scripts\\python.exe tests/preview_replay_wheel.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from ui.chords import load_chord_names  # noqa: E402
from ui.components import card  # noqa: E402
from ui.gamepad_backend import ScheduleAction  # noqa: E402
from ui.replay_view import ReplayWheel  # noqa: E402

from preview_gamepad_page import _last_page_document, _pose_wheel  # noqa: E402


def main(page: ft.Page) -> None:
    page.title = "回放轮盘预览"
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    scale = float(os.environ.get("PREVIEW_SCALE", "1") or 1)
    # 窗口按内容给到刚好：400x640 的轮盘卡片 + 内边距，别留一大片空背景（截图会被
    # 直接放进 README，空白只会把图片拉长）。
    if scale == 1:
        page.window.width = 448
        page.window.height = 632
    else:
        page.window.width = int(400 * scale + 32)
        page.window.height = int(640 * scale + 32)
    chord_mode = os.environ.get("PREVIEW_SCORE", "").strip().lower() == "gct"
    wheel = ReplayWheel(chord_mode=chord_mode)
    if chord_mode:
        wheel.set_chord_mode(True, load_chord_names())
        _pose_wheel(wheel, _last_page_document())
    else:
        # 示例谱第二个音：空弦（第 5 页）先按着，然后摇杆切到第 1 页 2 品、5 弦按下
        wheel.apply(ScheduleAction(time=0.0, kind="stick", stick=(0, 32767), string=6, fret=0, page=5))
        wheel.apply(ScheduleAction(time=0.1, kind="press", button="X", string=6, fret=0, page=5))
        wheel.apply(ScheduleAction(time=0.3, kind="stick", stick=(18385, 27152), string=5, fret=2, page=1))
        wheel.apply(ScheduleAction(time=0.4, kind="press", button="A", string=5, fret=2, page=1))

    holder = ft.Container(width=400, content=card("回放轮盘", wheel.build()))
    if scale == 1:
        body: ft.Control = ft.Container(padding=24, content=holder)
    else:
        # 放大是把整块按中心拉大：Stack 里先按 400 宽排版，再把左上角挪
        # 200*(scale-1)，这样放大后正好从 (0,0) 铺满 400*scale 的方框，
        # 不会像直接 scale 那样向左/向上溢出被窗口裁掉。
        holder.scale = ft.Scale(scale)
        holder.left = 200 * (scale - 1)
        holder.top = 200 * (scale - 1)
        body = ft.Container(
            padding=16,
            content=ft.Stack(
                [holder], width=400 * scale, height=640 * scale
            ),
        )

    page.add(body)
    page.update()


if __name__ == "__main__":
    ft.run(main)
