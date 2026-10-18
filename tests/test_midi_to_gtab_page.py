"""MIDI→GTAB 页面的构建测试（不弹窗口，只验证控件树和离线转换路径）。

运行：``.venv\\Scripts\\python.exe tests/test_midi_to_gtab_page.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from ui import settings as settings_mod  # noqa: E402
from ui.midi_doc import MidiDocument, demo_document  # noqa: E402
from ui.pages.gtab_page import GtabPage  # noqa: E402
from ui.pages.midi_to_gtab_page import MidiToGtabPage  # noqa: E402

# 测试固定跑「默认设置」：用户本机的 src/settings.json 不该影响结果
settings_mod.set_settings(settings_mod.AppSettings(), persist=False)


def _view() -> MidiToGtabPage:
    # page=None：构建阶段不需要页面上下文，只有文件对话框/提示才需要
    return MidiToGtabPage(page=None)


def test_page_builds_with_default_options() -> None:
    view = _view()
    root = view.build()

    assert isinstance(root, ft.Control)
    assert view._sw_top.value is True, "默认同时起音只留最高音"
    assert view._sw_percussion.value is True, "默认跳过 MIDI 通道 10（鼓）"
    assert view._btn_save.disabled and view._btn_send.disabled
    assert view._preview_list is not None


def test_demo_converts_and_renders_preview() -> None:
    view = _view()
    view.load_document(demo_document())

    assert view.result is not None
    assert view.result.stats.output_notes == len(view.result.notes) > 0
    assert len(view._preview_list.controls) == min(
        len(view.result.notes), view.PREVIEW_LIMIT
    )
    assert "输出" in view._stats_text.value
    assert not view._btn_save.disabled
    assert view.path is None, "内存示例没有磁盘路径"


def test_switch_off_keeps_simultaneous_notes() -> None:
    view = _view()
    view.load_document(demo_document())
    top_only = len(view.result.notes)

    view._sw_top.value = False
    view._on_option_change()
    assert len(view.result.notes) > top_only, "关掉后和弦的低音也应保留"
    assert view.result.stats.dropped_simultaneous == 0

    view._sw_top.value = True
    view._on_option_change()
    assert len(view.result.notes) == top_only


def test_empty_document_disables_actions() -> None:
    view = _view()
    view.load_document(MidiDocument(division=480, notes=[]))

    assert view.result is not None
    assert view.result.notes == []
    assert view._btn_save.disabled and view._btn_send.disabled


def test_demo_button_loads_in_memory_document() -> None:
    view = _view()
    view._on_demo()

    assert view.doc is not None
    assert view.path is None
    assert view.result is not None and view.result.notes


def test_send_to_gtab_editor_loads_unsaved_document() -> None:
    view = _view()
    view.load_document(demo_document())
    sent = []
    view.on_send = lambda doc: sent.append(doc)

    view._on_send()

    assert sent and sent[0] is view.result.doc

    gtab = GtabPage(page=None)
    gtab.load_document(sent[0])
    assert gtab.path is None, "送进编辑页的文档还没有磁盘文件"
    assert gtab.dirty is True
    assert len(gtab._notes_list.controls) == len(view.result.notes)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
