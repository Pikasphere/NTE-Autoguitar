"""手柄页「回放轮盘」的测试：扇区映射、状态推进、离线构建。

不创建虚拟手柄、不打开窗口；``ReplayWheel`` 在没有 ``page`` 时也能构建。

运行：``.venv\\Scripts\\python.exe tests/test_replay_view.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ui.gamepad_backend import (  # noqa: E402
    OPEN_PAGE,
    ScheduleAction,
    build_schedule,
    page_for,
)
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.replay_view import (  # noqa: E402
    STRING_ORDER,
    ReplayWheel,
    WheelState,
    describe,
    sector_angle,
    sector_labels,
    sector_of,
)

DEMO = SRC / "MusicList" / "gtabfile" / "demo.gtab"


def test_string_order_is_six_on_top() -> None:
    assert STRING_ORDER == (6, 5, 4, 3, 2, 1), "6 弦在上、1 弦在下"


def test_sector_of_maps_fret_to_six_sectors() -> None:
    assert sector_of(1) == 0, "1 品在正上方"
    assert sector_of(6) == 5
    assert sector_of(7) == 0, "第 2 页的 7 品又回到正上方"
    assert sector_of(12) == 5
    assert sector_of(13) == 0
    assert sector_of(0) is None, "空弦没有扇区"
    assert sector_of(-1) is None


def test_sector_angle_starts_at_top_clockwise() -> None:
    assert sector_angle(0) == -90.0, "0° 是正右方，所以正上方是 -90°"
    assert sector_angle(1) == -30.0
    assert sector_angle(2) == 30.0
    assert sector_angle(3) == 90.0, "第 4 个品在正下方"
    assert sector_angle(5) == 210.0


def test_sector_labels_use_absolute_frets() -> None:
    assert sector_labels(1, 1) == ["1", "2", "3", "4", "5", "6"]
    assert sector_labels(2, 8) == ["7", "8", "9", "10", "11", "12"]
    assert sector_labels(3, 13) == ["13", "14", "15", "16", "17", "18"]
    assert sector_labels(OPEN_PAGE, 0) == ["空"] * 6, "第 5 页六个方向都是空弦"
    assert sector_labels(0, -1) == ["1", "2", "3", "4", "5", "6"], "未开始时先显示 1–6"


def test_state_follows_stick_action() -> None:
    state = WheelState()
    state.apply(ScheduleAction(time=0.0, kind="stick", stick=(0, 32767), string=5, fret=8, page=2))
    assert state.fret == 8
    assert state.page == 2
    assert sector_of(state.fret) == 1
    assert "8 品" in describe(state)


def test_press_and_release_track_held_strings() -> None:
    state = WheelState()
    state.apply(ScheduleAction(time=0.0, kind="press", button="A", string=5, fret=8, page=2))
    assert state.held == {5: 8}
    assert "按住 5 弦" in describe(state)

    state.apply(ScheduleAction(time=0.1, kind="press", button="X", string=6, fret=0, page=5))
    assert state.held == {5: 8, 6: 0}
    assert state.page == 5

    state.apply(ScheduleAction(time=0.2, kind="release", button="A", string=5, fret=8, page=2))
    assert 5 not in state.held, "松开只去掉那一根弦，其余保持"
    assert 6 in state.held
    assert state.fret == 0, "摇杆仍停在空弦位置"


def test_page_flip_updates_page_without_touching_held() -> None:
    state = WheelState()
    state.apply(
        ScheduleAction(
            time=0.0, kind="page", stick=(-32767, 0), fret=0, page=OPEN_PAGE
        )
    )
    assert state.page == OPEN_PAGE
    assert state.held == {}, "翻页不是音符，不该点亮任何一根弦"
    assert state.fret == -1, "翻页不改变摇杆位置"


def test_open_string_is_page_five_and_no_sector() -> None:
    state = WheelState()
    state.apply(ScheduleAction(time=0.0, kind="stick", stick=(0, 32767), string=6, fret=0))
    assert state.page == OPEN_PAGE, "空弦固定在第 5 页"
    assert sector_of(state.fret) is None
    assert "空弦" in describe(state)


def test_light_and_dark_actions_only_touch_wheel() -> None:
    # gct 扫弦：右摇杆只表示方向（不改位置），六根弦由 light/dark 点亮/熄灭
    state = WheelState()
    state.apply(ScheduleAction(time=0.0, kind="rstick", stick=(0, -32767), fret=1, page=1))
    assert state.page == 0 and state.fret == -1, "右摇杆不改变页号/品"

    state.apply(
        ScheduleAction(time=0.01, kind="light", string=6, fret=1, page=1, chord="G")
    )
    state.apply(
        ScheduleAction(time=0.03, kind="light", string=5, fret=1, page=1, chord="G")
    )
    assert state.held == {6: 1, 5: 1}
    assert state.chord == "G"
    assert "第 1 页 · G" in describe(state, True)

    state.apply(
        ScheduleAction(time=0.2, kind="dark", string=6, fret=1, page=1, chord="G")
    )
    assert state.held == {5: 1}, "熄灭只去掉那一根弦"
    assert state.fret == 1, "熄灭不改变和弦位"


def test_reset_returns_to_idle() -> None:
    state = WheelState()
    state.apply(ScheduleAction(time=0.0, kind="press", button="X", string=6, fret=3, page=1))
    state.reset()
    assert state.page == 0 and state.fret == -1 and state.held == {}
    assert describe(state).startswith("就绪")


def test_state_follows_whole_demo_schedule() -> None:
    # 走完示例谱的整条排期：最后一个音是 5 弦 2 品，结束时所有弦都已松开
    doc = GtabDocument.from_path(DEMO)
    schedule = build_schedule(doc)
    state = WheelState()
    for action in schedule.actions:
        state.apply(action)
    assert state.page == page_for(2)
    assert state.fret == 2
    assert state.held == {}, "排期最后会松开所有弦"

    # 第二个音按下时，轮盘应该同时显示「5 弦」和「2 品」；
    # 6 弦空弦还按着（重叠音保持到自己的 NoteOff），所以两根弦都亮
    mid = WheelState()
    for action in schedule.actions:
        mid.apply(action)
        if action.kind == "press" and action.string == 5:
            break
    assert mid.held == {6: 0, 5: 2}
    assert mid.fret == 2
    assert mid.page == 1


def test_wheel_builds_offline_and_paints_state() -> None:
    wheel = ReplayWheel()          # page=None 的离线构建，和页面测试一致
    assert wheel.state.page == 0
    assert wheel._center_big.value == "—"
    assert wheel._labels[0].value == "1"

    # 第 2 页 8 品：扇区 1 高亮、标签写绝对品号 7–12、中心写 8
    wheel.apply(ScheduleAction(time=0.0, kind="press", button="A", string=5, fret=8, page=2))
    assert wheel._labels[1].value == "8"
    assert wheel._center_big.value == "8"
    assert wheel._center_small.value == "品"
    assert "第 2 页 · 8 品" in wheel._state_text.value
    assert wheel._rows[5][3].value == "8 品", "5 弦那行右侧写出品号"

    # 松开后弦不再高亮，但摇杆位置留在 8 品
    wheel.apply(ScheduleAction(time=0.1, kind="release", button="A", string=5, fret=8, page=2))
    assert wheel._rows[5][3].value == ""
    assert wheel._center_big.value == "8"

    # 空弦：中心写「空弦」、六个扇区都写「空」
    wheel.apply(ScheduleAction(time=0.2, kind="stick", stick=(0, 32767), string=6, fret=0, page=5))
    assert wheel._center_big.value == "空弦"
    assert wheel._labels[0].value == "空"

    wheel.reset()
    assert wheel._center_big.value == "—"
    assert wheel._state_text.value.startswith("就绪")


def test_play_loop_drives_the_wheel() -> None:
    # 不用真手柄：把 backend 换成记录用的假对象，跑真正的回放循环，
    # 确认轮盘确实跟着回放走（而不是只在我单独调 apply 时才动）。
    import asyncio

    from gtab import EV_STRING_NOTE_OFF, EV_STRING_NOTE_ON
    from ui.gtab_doc import EventRow
    from ui.pages.gamepad_page import GamepadPage

    doc = GtabDocument(default_tempo_us=500_000)   # 120 BPM，1 拍 = 0.5s
    doc.events = [
        EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([6, 1])),
        EventRow(tick=240, event_type=EV_STRING_NOTE_ON, data=bytes([5, 3])),
        EventRow(tick=480, event_type=EV_STRING_NOTE_OFF, data=bytes([6, 1])),
        EventRow(tick=480, event_type=EV_STRING_NOTE_OFF, data=bytes([5, 3])),
    ]
    doc.events.sort(key=lambda ev: ev.tick)

    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def set_stick(self, *_args) -> None:
            self.calls.append("stick")

        def press(self, button: str) -> None:
            self.calls.append(f"press {button}")

        def release(self, button: str) -> None:
            self.calls.append(f"release {button}")

        def update(self) -> None:
            pass

        def release_all(self) -> None:
            self.calls.append("release_all")

    view = GamepadPage(None)
    fake = FakeBackend()
    view.backend = fake
    view.doc = doc

    seen: list[dict] = []
    original = view._wheel.apply

    def spy(action) -> None:
        original(action)
        if action.kind != "release":
            seen.append(
                {
                    "string": action.string,
                    "fret": view._wheel.state.fret,
                    "page": view._wheel.state.page,
                    "held": dict(view._wheel.state.held),
                }
            )

    view._wheel.apply = spy
    schedule = build_schedule(doc)
    view._playing = True
    view._total_notes = schedule.note_count
    asyncio.run(view._play_loop(schedule.actions))

    assert "press X" in fake.calls and "press A" in fake.calls
    second = [step for step in seen if step["string"] == 5][-1]
    assert second["fret"] == 3, "第二个音的品要出现在轮盘上"
    assert second["page"] == 1
    assert second["held"] == {6: 1, 5: 3}, "两个重叠音都在按着"
    assert view._wheel.state.page == 0, "回放结束轮盘复位"


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
