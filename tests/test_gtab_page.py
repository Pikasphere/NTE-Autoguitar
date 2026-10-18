"""GTAB 页面的构建测试（不弹窗口，只验证控件树能搭起来）。

运行：``.venv\\Scripts\\python.exe tests/test_gtab_page.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import flet as ft  # noqa: E402

from gtab import EV_END, EV_STRING_NOTE_OFF, EV_STRING_NOTE_ON  # noqa: E402
from ui.gtab_doc import EventRow, GtabDocument  # noqa: E402
from ui.pages.gtab_page import GtabPage, _parse_beats  # noqa: E402


def _build_page() -> GtabPage:
    # page=None：构建阶段不需要页面上下文，事件回调才需要
    return GtabPage(page=None)


def _load_demo(view: GtabPage) -> None:
    view.doc = GtabDocument.from_path(GtabPage.DEMO_PATH)
    view.path = GtabPage.DEMO_PATH
    view._render_all()


def test_page_builds() -> None:
    view = _build_page()
    root = view.build()
    assert isinstance(root, ft.Control)
    assert not hasattr(view, "_f_timebase"), "time_base 不应再出现在界面上"
    assert view._notes_list is not None and view._events_list is not None


def test_renders_demo_document_without_note_off_rows() -> None:
    view = _build_page()
    _load_demo(view)

    # 音符区：一行一个音（NoteOn + 音长），NoteOff 不单独占行
    assert len(view._notes_list.controls) == len(view.doc.note_pairs()) == 2
    # 其他事件区：只剩 End
    assert len(view._events_list.controls) == 1
    assert view.doc.events[-1].event_type == EV_END
    assert view._file_label.value.endswith("demo.gtab")
    assert "2 音符" in view._stats_label.value


def test_renders_empty_document() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    view._render_all()
    # 空文档时给一句提示，而不是空列表
    assert len(view._notes_list.controls) == 1
    assert "还没有音符" in (view._notes_list.controls[0].content.value or "")
    assert len(view._events_list.controls) == 1
    assert "没有其他事件" in (view._events_list.controls[0].content.value or "")


def test_beat_axis() -> None:
    view = _build_page()
    _load_demo(view)

    # 1 拍 = 480 tick；示例谱正好 1 拍长
    assert view._note_blocks() == [(0, 480, 6, 0), (240, 480, 5, 2)]
    assert abs(view.doc.duration_beats - 1.0) < 1e-9

    assert view._format_beats(1) == "1.00"
    assert view._format_beats(2.5) == "2.50"
    assert _parse_beats("2.5") == 2.5
    assert _parse_beats("") is None
    assert _parse_beats("abc") is None

    assert view._timeline_host.content is not None
    assert view._timeline_host.height > 0
    assert view._timeline_width > 0


def test_note_duration_and_start_editing() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    view._render_all()

    view._on_add_note()
    entry = view.doc.note_pairs()[0]
    assert (entry.start_tick, entry.duration_ticks) == (0, 480), "新音符默认 1 拍、从 0 拍开始"
    assert len(view._notes_list.controls) == 1

    # 音长 2 拍 → NoteOff 移到 2 拍，但不在界面里单独显示
    view._on_note_duration_change(entry, "2.0")
    assert entry.off.tick == 960
    assert len([ev for ev in view.doc.events if ev.event_type == EV_STRING_NOTE_OFF]) == 1
    assert len(view._notes_list.controls) == 1, "改音长不该多出一行"

    # 移动整音：On/Off 一起动，音长不变
    view._on_note_start_change(entry, "1.0")
    assert (entry.on.tick, entry.off.tick) == (480, 1440)

    # 弦 / 品 / voice 必须同时改 On 和 Off，否则配不上
    view._on_note_field_change(entry, "string", "4")
    view._on_note_field_change(entry, "fret", "7")
    view._on_note_field_change(entry, "voice", "3")
    assert (entry.string, entry.fret, entry.voice) == (4, 7, 3)
    assert (entry.off.string, entry.off.fret, entry.off.voice) == (4, 7, 3)

    # 删除音 = On + Off 一起删
    view._on_delete_note(entry)
    assert view.doc.events == []


def test_duration_is_clamped_by_same_key_next_note() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    view._render_all()
    first = view.doc.add_note(0, 6, 0)
    second = view.doc.add_note(960, 6, 0)
    view._render_events()

    view._on_note_duration_change(first, "5.0")
    assert first.end_tick == second.start_tick == 960, "同键下一个音会限制前一个音的长度"

    field = ft.TextField(value="5.0")
    view._on_note_duration_change(first, "5.0", field)
    assert field.value == "2.00", "被夹短时输入框要显示真实音长"


def test_note_off_is_hidden_but_missing_off_warns() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    view.doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([6, 0]))]
    view._render_all()

    assert len(view._notes_list.controls) == 1
    assert view._events_warning.visible
    assert "NoteOff" in (view._events_warning.value or "")

    # 保存（模型层）会自动补 Off；补完后警告应能消失
    view.doc.to_bytes()
    view._render_events()
    assert not view._events_warning.visible
    assert view.doc.note_pairs()[0].missing_off is False


def test_orphan_note_off_is_shown_not_silently_dropped() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    view.doc.events = [EventRow(tick=0, event_type=EV_STRING_NOTE_OFF, data=bytes([1, 0]))]
    view._render_all()

    assert len(view._events_list.controls) == 1, "孤立 NoteOff 必须显示出来"
    assert view._events_warning.visible
    assert "孤立" in (view._events_warning.value or "")


def test_bpm_does_not_move_events() -> None:
    view = _build_page()
    view.doc = GtabDocument()
    ev = EventRow(tick=960, event_type=EV_STRING_NOTE_ON, data=bytes([1, 0]))
    view.doc.events = [ev]
    view._render_events()

    assert view._format_beats(view.doc.beats_at(ev.tick)) == "2.00"

    # BPM 只影响播放速度，节拍位置不动
    view._f_bpm.value = "60"
    view._on_bpm_change()
    assert view.doc.bpm == 60
    assert ev.tick == 960
    assert view._format_beats(view.doc.beats_at(ev.tick)) == "2.00"


def test_preview_offline_renders_and_reports() -> None:
    view = _build_page()
    _load_demo(view)

    view._on_preview()
    assert not view._playing
    assert "已合成" in (view._preview_label.value or "")
    assert view._playhead.visible is False


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
