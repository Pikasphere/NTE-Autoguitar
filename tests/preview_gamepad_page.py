"""开发用：单独打开「手柄映射」页并载入示例谱，方便看界面。

运行：``.venv\\Scripts\\python.exe tests/preview_gamepad_page.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from branding import ICON_PATH  # noqa: E402

from gct import DIR_DOWN, EV_CHORD_STRUM  # noqa: E402
from ui.chords import load_chord_names  # noqa: E402
from ui.gct_backend import build_chord_schedule  # noqa: E402
from ui.gct_doc import ChordRow, GctDocument, demo_document  # noqa: E402
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.pages.gamepad_page import GamepadPage  # noqa: E402


def _last_page_document() -> GctDocument:
    """最后一页（第 4 页）的六个和弦做成一张谱：名字最长，用来查标签会不会顶出外圈。"""
    names = tuple(load_chord_names())[-6:]
    doc = GctDocument(default_tempo_us=500_000)   # 120 BPM
    ids = {name: doc.add_chord(name) for name in names}
    beat = doc.time_base_value
    for index, name in enumerate(names):
        doc.events.append(
            ChordRow(
                tick=index * beat,
                event_type=EV_CHORD_STRUM,
                data=ids[name].to_bytes(2, "little") + bytes([DIR_DOWN]),
            )
        )
    return doc


def _set_gct_example(view: GamepadPage, last_page: bool = False) -> None:
    """载入 gct 和弦示例（内存里生成的那张），和「示例 gct」按钮走同一条路。"""
    doc = _last_page_document() if last_page else demo_document()
    view.path = None
    view._set_gct_doc(doc, "和弦示例（内存，最后一页）" if last_page else "和弦示例（内存）")


def _pose_wheel(wheel, doc: GctDocument, lead: float | None = None) -> None:
    """把轮盘摆成「第一个和弦扫到第 5 根弦」，截图才能看到中心/指针/高亮。"""
    from ui.gct_backend import CHORD_STRUM_SWEEP

    schedule = build_chord_schedule(doc)
    lights = [
        action.time
        for action in schedule.actions
        if action.kind == "light" and action.string
    ]
    if not lights:
        return
    if lead is None:
        lead = 4 * CHORD_STRUM_SWEEP + 0.001   # 第 5 根弦刚好亮起
    cutoff = min(lights) + lead
    for action in schedule.actions:
        if action.time > cutoff:
            break
        wheel.apply(action)


def _pose_mid_play(view: GamepadPage) -> None:
    if view.gct_doc is not None:
        _pose_wheel(view._wheel, view.gct_doc)


def main(page: ft.Page) -> None:
    page.title = "手柄映射预览"
    page.window.icon = str(ICON_PATH)
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    # 默认按整窗 1280 截；设 PREVIEW_WIDTH=1144 可以模拟 App 里
    # 「左侧导航栏 96 + 分隔线」之后真正的内容宽度，检查工具栏会不会被挤。
    page.window.width = int(os.environ.get("PREVIEW_WIDTH", "1280"))
    page.window.height = int(os.environ.get("PREVIEW_HEIGHT", "840"))

    view = GamepadPage(page)
    score = os.environ.get("PREVIEW_SCORE", "gtab").strip().lower()
    if score == "gct":
        # 和弦谱：轮盘切成「六扇区和弦名」模式
        _set_gct_example(view, last_page=os.environ.get("PREVIEW_PAGE") == "4")
        if os.environ.get("PREVIEW_STATE", "").strip().lower() == "play":
            _pose_mid_play(view)
    else:
        view.doc = GtabDocument.from_path(GamepadPage.DEMO_PATH)
        view.path = GamepadPage.DEMO_PATH
        view._set_doc(view.doc, GamepadPage.DEMO_PATH.name)
    try:
        view.backend.connect()
    except Exception:  # noqa: BLE001
        pass
    view.refresh_status()

    page.add(view.build())
    page.update()


if __name__ == "__main__":
    ft.run(main)
