"""手柄页的 gct 和弦回放测试（不弹窗口，只验证控件与回放逻辑）。

覆盖：
- 载入 gct → ``kind`` / 文件行 / 轮盘切成「六扇区和弦名」；
- ``_play_loop`` 真跑（假 backend）：扫弦推右摇杆、不按弦键，轮盘六根弦按顺序亮，
  只计 1 个和弦，结束复位；
- 下扫/上扫的右摇杆方向与亮灯顺序；
- 切回 gtab 时轮盘恢复成品号；
- 热键路径的 ``_has_playable``。

运行：``.venv\\Scripts\\python.exe tests/test_gct_page.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from gct import DIR_DOWN, DIR_UP, EV_CHORD_STRUM, EV_END  # noqa: E402
from ui.chords import load_chord_names  # noqa: E402
from ui.gamepad_backend import STRUM_DOWN, STRUM_UP  # noqa: E402
from ui.gct_backend import build_chord_schedule  # noqa: E402
from ui.gct_doc import ChordRow, GctDocument, demo_document  # noqa: E402
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.pages.gamepad_page import GamepadPage  # noqa: E402


class FakeBackend:
    """记录调用顺序的假手柄（不连 ViGEmBus）。"""

    def __init__(self) -> None:
        self.connected = True
        self.calls: list[str] = []

    def connect(self) -> None:
        self.connected = True

    def set_stick(self, *_args) -> None:
        self.calls.append("stick")

    def set_right_stick(self, x: int, y: int) -> None:
        self.calls.append(f"rstick {x} {y}")

    def press(self, button: str) -> None:
        self.calls.append(f"press {button}")

    def release(self, button: str) -> None:
        self.calls.append(f"release {button}")

    def update(self) -> None:
        pass

    def release_all(self) -> None:
        self.calls.append("release_all")

    def presses(self) -> list[str]:
        return [call for call in self.calls if call.startswith("press ")]

    def rstricks(self) -> list[str]:
        return [call for call in self.calls if call.startswith("rstick ")]


class FakePage:
    """只实现手柄页用到的那几个 Page 方法。"""

    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple]] = []
        self.dialogs: list[object] = []
        self.updates = 0

    def run_task(self, handler, *args) -> None:
        self.tasks.append((handler, args))

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def update(self) -> None:
        self.updates += 1


def _one_chord_doc(name: str = "G", direction: int = DIR_DOWN) -> GctDocument:
    """一个和弦、一次扫弦的最小谱面（1 拍），用来跑真回放循环。"""
    doc = GctDocument(default_tempo_us=500_000)   # 120 BPM，1 拍 = 0.5s
    chord_id = doc.add_chord(name)
    doc.events = [
        ChordRow(
            tick=0,
            event_type=EV_CHORD_STRUM,
            data=chord_id.to_bytes(2, "little") + bytes([direction]),
        ),
        ChordRow(tick=doc.time_base_value, event_type=EV_END),
    ]
    return doc


def _load_gct(view: GamepadPage, doc: GctDocument) -> None:
    view.path = None
    view._set_gct_doc(doc, "和弦示例（内存）")


# ---------------------------------------------------------------- 页面状态


def test_builds_and_starts_in_gtab_mode() -> None:
    view = GamepadPage(None)
    assert view.kind == "gtab"
    assert view.gct_doc is None
    assert view._wheel.chord_mode is False


def test_load_gct_switches_wheel_to_chord_names() -> None:
    view = GamepadPage(None)
    doc = demo_document()
    _load_gct(view, doc)

    assert view.kind == "gct"
    assert view.gct_doc is doc and view.doc is None
    assert "和弦示例（内存）" in view._file_text.value
    assert "个和弦事件" in view._file_text.value
    assert "和弦" in view._progress_text.value

    assert view._wheel.chord_mode is True
    names = load_chord_names()
    assert view._wheel.chord_names == tuple(names), "轮盘要有完整的 24 个和弦"
    # 第 1 页 = chord.json 前 6 个，顺时针从正上方开始
    assert [label.value for label in view._wheel._labels] == list(names[:6])
    assert "和弦位" in view._wheel._caption.value
    # 还没播放：中心是占位符
    assert view._wheel._center_big.value == "—"


def test_switching_back_to_gtab_restores_fret_labels() -> None:
    view = GamepadPage(None)
    _load_gct(view, demo_document())
    assert view._wheel.chord_mode is True

    gtab_doc = GtabDocument.from_path(GamepadPage.DEMO_PATH)
    view._set_doc(gtab_doc, GamepadPage.DEMO_PATH.name)

    assert view.kind == "gtab"
    assert view.gct_doc is None
    assert view._wheel.chord_mode is False
    assert [label.value for label in view._wheel._labels] == ["1", "2", "3", "4", "5", "6"]
    assert "摇杆方向" in view._wheel._caption.value


def test_has_playable_for_gct() -> None:
    view = GamepadPage(None)
    assert view._has_playable() is False
    _load_gct(view, _one_chord_doc())
    assert view._has_playable() is True

    # 只有 End 的空谱不算能播
    empty = GctDocument(default_tempo_us=500_000)
    empty.events.append(ChordRow(tick=0, event_type=EV_END))
    _load_gct(view, empty)
    assert view._has_playable() is False


# ---------------------------------------------------------------- 回放循环


def _run_play_loop(view: GamepadPage, doc: GctDocument) -> FakeBackend:
    schedule = build_chord_schedule(doc)
    view._playing = True
    view._pressed = 0
    view._total_notes = schedule.note_count
    view._unit_label = schedule.unit_label
    view._drifts.clear()
    asyncio.run(view._play_loop(schedule.actions))
    return view.backend


def test_down_strum_moves_right_stick_and_lights_six_to_one() -> None:
    view = GamepadPage(None)
    fake = FakeBackend()
    view.backend = fake
    doc = _one_chord_doc()
    _load_gct(view, doc)

    seen: list[tuple[int, dict[int, int]]] = []
    original = view._wheel.apply

    def spy(action) -> None:
        original(action)
        if action.kind == "light":
            seen.append((action.string, dict(view._wheel.state.held)))

    view._wheel.apply = spy
    _run_play_loop(view, doc)

    assert fake.presses() == [], "扫弦不再按 6 个弦键"
    assert fake.rstricks()[0] == f"rstick {STRUM_DOWN[0]} {STRUM_DOWN[1]}", "向下推 = 下扫"
    assert fake.rstricks()[-1] == "rstick 0 0", "推完要回中"
    assert "stick" in fake.calls, "左摇杆先摆和弦位"

    assert [string for string, _held in seen] == [6, 5, 4, 3, 2, 1], "六根弦按顺序亮"
    assert seen[0][1] == {6: 1}, "第一根亮起时只有 6 弦"
    assert len(seen[-1][1]) == 6, "扫到最后六根都亮着（纯显示，不会中途灭）"

    assert view._pressed == 1, "一次扫弦只算 1 个和弦"
    assert view._total_notes == 1
    assert "个和弦" in view._progress_text.value
    assert "1/1" in view._progress_text.value
    assert view._wheel.state.page == 0, "回放结束轮盘复位"
    assert view._wheel.state.held == {}


def test_up_strum_moves_right_stick_up_and_lights_one_to_six() -> None:
    view = GamepadPage(None)
    fake = FakeBackend()
    view.backend = fake
    doc = _one_chord_doc("C", DIR_UP)
    _load_gct(view, doc)

    lights: list[int] = []
    original = view._wheel.apply

    def spy(action) -> None:
        original(action)
        if action.kind == "light":
            lights.append(action.string)

    view._wheel.apply = spy
    _run_play_loop(view, doc)

    assert lights == [1, 2, 3, 4, 5, 6]
    assert fake.rstricks()[0] == f"rstick {STRUM_UP[0]} {STRUM_UP[1]}", "向上推 = 上扫"


def test_play_loop_moves_wheel_to_chord_page() -> None:
    view = GamepadPage(None)
    view.backend = FakeBackend()
    # 第 4 页最后一个和弦：要翻 3 页、摇杆转到第 6 个方向
    name = load_chord_names()[-1]
    doc = _one_chord_doc(name)
    _load_gct(view, doc)

    seen: list[dict] = []
    original = view._wheel.apply

    def spy(action) -> None:
        original(action)
        if action.kind == "light":
            seen.append(
                {
                    "page": view._wheel.state.page,
                    "fret": view._wheel.state.fret,
                    "chord": view._wheel.state.chord,
                    "held": dict(view._wheel.state.held),
                }
            )

    view._wheel.apply = spy
    _run_play_loop(view, doc)

    assert seen, "扫弦时轮盘要跟着动"
    assert seen[-1]["page"] == 4
    assert seen[-1]["fret"] == 6
    assert seen[-1]["chord"] == name
    assert seen[-1]["held"] == {6: 6, 5: 6, 4: 6, 3: 6, 2: 6, 1: 6}


# ---------------------------------------------------------------- 播放入口


def test_on_play_builds_chord_schedule_and_sets_units() -> None:
    page = FakePage()
    view = GamepadPage(page)
    view.backend = FakeBackend()
    _load_gct(view, demo_document())

    asyncio.run(view._on_play())

    assert view._playing is True
    assert len(page.tasks) == 1, "播放要丢回事件循环，不能在调用线程里睡"
    handler, args = page.tasks[0]
    assert handler == view._play_loop
    assert args and len(args[0]) > 0
    # 示例谱：8 个下扫 + 末拍 1 个上扫 = 9 个和弦；Set 事件不按键
    assert view._total_notes == 9
    assert view._unit_label == "和弦"
    assert "9" in view._progress_text.value
    view.stop()
    assert view._playing is False


def test_hotkey_play_does_not_auto_connect_gamepad() -> None:
    """连接必须是手动的：热键路径也不许偷偷 connect()（用户 2026-10-18 要求）。"""

    class NoConnectBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.connected = False
            self.connect_calls = 0

        def connect(self) -> None:
            self.connect_calls += 1
            super().connect()

    page = FakePage()
    view = GamepadPage(page)
    backend = NoConnectBackend()
    view.backend = backend
    _load_gct(view, demo_document())

    asyncio.run(view._play_from_hotkey())

    assert backend.connect_calls == 0, "热键不能自动连接手柄"
    assert view._playing is False
    assert "连接手柄" in view._progress_text.value


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
