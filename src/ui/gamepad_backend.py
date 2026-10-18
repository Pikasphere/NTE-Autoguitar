"""虚拟手柄后端（vgamepad）+ gtab 谱面 → 手柄按键/摇杆的排期。

映射规则（按用户给定）：

- **弦 → 按键**：``X``=6 弦, ``A``=5 弦, 十字键 ``上``=4 弦, ``右``=3 弦,
  ``左``=2 弦, ``下``=1 弦；
- **品 → 左摇杆角度**：以「上」为 1 品，顺时针每 60° 一个品，
  一页 6 个品：上=1, 右上 60°=2, 右下 120°=3, 下=4, 左下 240°=5, 左上 300°=6；
- **翻页**：**右摇杆向左 = 下一页**（7–12 品起），向右 = 上一页
  （用户 2026-10-18 实测：从第 1 页到最后一页靠向左移动完成；不再用 RB/LB）；
- **第 5 页**：六个方向都表示空弦（0 品）。
- **扫弦（gct 和弦谱）→ 右摇杆**：向下推 = 下扫，向上推 = 上扫
  （用户 2026-10-18 指定；扫弦不再依次按 6 个弦键）。

``build_schedule`` 是纯函数：输入文档，输出带时间轴的按键/摇杆动作，
不碰硬件，可以直接单测。
"""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ui.gtab_doc import GtabDocument, tick_to_seconds

__all__ = [
    "AIM_GAP",
    "BUTTON_NAMES",
    "LEAD_IN",
    "OPEN_PAGE",
    "PAGE_SIZE",
    "PAGE_STICK_NEXT",
    "PAGE_STICK_PREV",
    "REPLUCK_GAP",
    "STRING_BUTTONS",
    "STRUM_DOWN",
    "STRUM_UP",
    "GamepadBackend",
    "GamepadStatus",
    "PositionPlanner",
    "Schedule",
    "ScheduleAction",
    "build_schedule",
    "direction_label",
    "fret_direction",
    "page_for",
]

# 弦号 -> 按键名（6 弦到 1 弦：X, A, 上, 右, 左, 下）
STRING_BUTTONS: dict[int, str] = {
    6: "X",
    5: "A",
    4: "DPAD_UP",
    3: "DPAD_RIGHT",
    2: "DPAD_LEFT",
    1: "DPAD_DOWN",
}

PAGE_SIZE = 6            # 每页 6 个品
OPEN_PAGE = 5            # 第 5 页：六处全为空弦

STICK_MAX = 32767
# 右摇杆扫弦方向：向下 = 下扫，向上 = 上扫（用户 2026-10-18 指定）
STRUM_DOWN = (0, -STICK_MAX)
STRUM_UP = (0, STICK_MAX)
# 翻页：右摇杆向左 = 下一页、向右 = 上一页（用户 2026-10-18 实测：向左能一路到最后一页）
PAGE_STICK_NEXT = (-STICK_MAX, 0)
PAGE_STICK_PREV = (STICK_MAX, 0)

PAGE_TAP_HOLD = 0.04     # 翻页时右摇杆推住时长
PAGE_TAP_GAP = 0.03      # 两次翻页之间的间隔（摇杆回中后再推）
PAGE_LEAD = 0.08         # 翻页要在音符前完成
LEAD_IN = 0.5            # 开场留一点时间做初始翻页

BUTTON_ATTRS: dict[str, str] = {
    "A": "XUSB_GAMEPAD_A",
    "B": "XUSB_GAMEPAD_B",
    "X": "XUSB_GAMEPAD_X",
    "Y": "XUSB_GAMEPAD_Y",
    "LB": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "RB": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "START": "XUSB_GAMEPAD_START",
    "BACK": "XUSB_GAMEPAD_BACK",
    "DPAD_UP": "XUSB_GAMEPAD_DPAD_UP",
    "DPAD_DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "DPAD_LEFT": "XUSB_GAMEPAD_DPAD_LEFT",
    "DPAD_RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
}

BUTTON_NAMES: tuple[str, ...] = tuple(BUTTON_ATTRS)

DRIVER_PATH = Path(r"C:\Windows\System32\drivers\ViGEmBus.sys")


# ------------------------------------------------------------------ 映射规则


def page_for(fret: int) -> int:
    """品 → 页号；空弦（0 品）固定在第 5 页。"""
    if fret <= 0:
        return OPEN_PAGE
    return (fret - 1) // PAGE_SIZE + 1


def fret_direction(fret: int) -> tuple[int, int]:
    """品 → 左摇杆 (x, y)，以「上」为 1 品、顺时针每 60° 一个品。

    空弦用正上方（第 5 页六个方向都算空弦）。
    """
    if fret <= 0:
        return (0, STICK_MAX)
    index = (fret - 1) % PAGE_SIZE
    angle = math.radians(index * 60)
    x = round(STICK_MAX * math.sin(angle))
    y = round(STICK_MAX * math.cos(angle))
    return (x, y)


def direction_label(fret: int) -> str:
    if fret <= 0:
        return "空弦"
    index = (fret - 1) % PAGE_SIZE
    names = ("上", "右上", "右下", "下", "左下", "左上")
    return f"{names[index]} {index * 60}°"


# ------------------------------------------------------------------ 状态对象


@dataclass
class GamepadStatus:
    installed: bool = False
    version: str = ""
    driver: bool = False
    error: str = ""


@dataclass(frozen=True)
class ScheduleAction:
    """一次要执行的动作。``time`` 是相对播放起点的秒数。"""

    time: float
    kind: str  # "press" / "release" / "stick" / "rstick" / "page" / "light" / "dark"
    button: str = ""
    stick: tuple[int, int] | None = None
    tick: int = 0
    string: int = 0
    fret: int = 0
    page: int = 0
    label: str = ""
    # 是否算一个「单位」（gtab 的一个音 / gct 的一个和弦事件），计数与误差统计用它；
    # 翻页、一次扫弦里的第 2–6 根弦都不算。
    unit: bool = False
    # gct 用：这一步对应哪个和弦（gtab 恒为空）
    chord: str = ""


@dataclass
class Schedule:
    actions: list[ScheduleAction] = field(default_factory=list)
    # 真正会弹错的问题：翻页来不及、弦没有映射
    conflicts: list[str] = field(default_factory=list)
    # 预期内的处理方式（重叠换摇杆、同弦重拨）：照常执行，不是错误
    hints: list[str] = field(default_factory=list)
    # 计数单位（界面显示「x/y 个<单位>」）
    unit_label: str = "音符"

    @property
    def note_count(self) -> int:
        """真正的单位数（gtab = 音符，gct = 和弦事件；翻页/扫弦里的第 2–6 根弦不算）。

        单位标记在 ``unit=True`` 的动作上：gtab 是按下那一下，gct 是和弦第一根弦点亮。
        """
        return sum(1 for action in self.actions if action.unit)


class GamepadBackend:
    """vgamepad 的薄封装，按键用「名字」而不是 vg 的常量。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vg = None
        self._pad = None
        self._held: dict[str, int] = {}
        self._error = ""
        try:
            import vgamepad as vg  # noqa: F401  （延迟导入，缺依赖也不炸）
            self._vg = vg
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- 状态

    @property
    def available(self) -> bool:
        return self._vg is not None

    @property
    def connected(self) -> bool:
        return self._pad is not None

    @property
    def held(self) -> list[str]:
        with self._lock:
            return list(self._held)

    def probe(self) -> GamepadStatus:
        version = ""
        if self._vg is not None:
            try:
                from importlib.metadata import version as pkg_version

                version = pkg_version("vgamepad")
            except Exception:  # noqa: BLE001
                version = ""
        driver = DRIVER_PATH.exists() if sys.platform == "win32" else False
        return GamepadStatus(
            installed=self.available,
            version=version,
            driver=driver,
            error=self._error,
        )

    # ---------------------------------------------------------------- 连接

    def connect(self) -> None:
        if self._vg is None:
            raise RuntimeError(self._error or "vgamepad 没有安装")
        with self._lock:
            if self._pad is not None:
                return
            self._pad = self._vg.VX360Gamepad()
            self._held.clear()

    def disconnect(self) -> None:
        with self._lock:
            pad = self._pad
            if pad is None:
                return
            self._held.clear()
            self._pad = None
            try:
                pad.reset()
                pad.update()
            except Exception:  # noqa: BLE001
                pass
            del pad

    # ---------------------------------------------------------------- 输入

    def press(self, name: str) -> None:
        with self._lock:
            if self._pad is None:
                raise RuntimeError("虚拟手柄还没连接")
            count = self._held.get(name, 0)
            self._held[name] = count + 1
            if count == 0:
                self._pad.press_button(button=self._button(name))

    def release(self, name: str) -> None:
        with self._lock:
            if self._pad is None:
                return
            count = self._held.get(name, 0)
            if count <= 0:
                return
            if count == 1:
                self._held.pop(name, None)
                self._pad.release_button(button=self._button(name))
            else:
                self._held[name] = count - 1

    def release_all(self) -> None:
        with self._lock:
            if self._pad is None:
                return
            held = list(self._held)
            self._held.clear()
            for name in held:
                try:
                    self._pad.release_button(button=self._button(name))
                except Exception:  # noqa: BLE001
                    pass
            # 扫弦是右摇杆「推一下」：停止/结束时必须回中，别让游戏一直收到下扫/上扫
            try:
                self._pad.right_joystick(x_value=0, y_value=0)
            except Exception:  # noqa: BLE001
                pass
            self._pad.update()

    def update(self) -> None:
        with self._lock:
            if self._pad is not None:
                self._pad.update()

    def reset(self) -> None:
        with self._lock:
            self._held.clear()
            if self._pad is not None:
                self._pad.reset()
                self._pad.update()

    def set_trigger(self, side: str, value: int) -> None:
        with self._lock:
            if self._pad is None:
                raise RuntimeError("虚拟手柄还没连接")
            value = max(0, min(255, int(value)))
            if side == "LT":
                self._pad.left_trigger(value=value)
            else:
                self._pad.right_trigger(value=value)

    def set_stick(self, x: int, y: int) -> None:
        with self._lock:
            if self._pad is None:
                raise RuntimeError("虚拟手柄还没连接")
            self._pad.left_joystick(
                x_value=max(-STICK_MAX, min(STICK_MAX, int(x))),
                y_value=max(-STICK_MAX, min(STICK_MAX, int(y))),
            )

    def set_right_stick(self, x: int, y: int) -> None:
        """右摇杆：gct 扫弦用——向下推 = 下扫，向上推 = 上扫。"""
        with self._lock:
            if self._pad is None:
                raise RuntimeError("虚拟手柄还没连接")
            self._pad.right_joystick(
                x_value=max(-STICK_MAX, min(STICK_MAX, int(x))),
                y_value=max(-STICK_MAX, min(STICK_MAX, int(y))),
            )

    def _button(self, name: str):
        attr = BUTTON_ATTRS.get(name)
        if attr is None:
            raise ValueError(f"未知按键 {name!r}")
        return getattr(self._vg.XUSB_BUTTON, attr)


# ------------------------------------------------------------------ 排期


# 同一根弦要重拨时，提前一点松开：给游戏一个「先抬起、再按下」的按键边沿。
# 10ms 实测不稳定——可能短于游戏的一帧（60fps≈16.7ms），游戏采样不到松开；
# 默认给 40ms，手柄页可以现场调大。
REPLUCK_GAP = 0.04
# 摇杆换方向（或翻页）后，先让游戏吃到新的方向/页号，再按弦。
# 同一时刻发「摇杆 + 按键」时，游戏可能在同一帧里先处理按键，弹成上一个品。
# 2026-10-18 真机实测：50ms 仍有概率弹成上一个品，65ms 稳定（用户反馈）。
# 手柄页可以现场调（0–300ms）。
AIM_GAP = 0.065
# 一次按住的最短时间，避免释放时间早于按下时间
MIN_PRESS = 0.005


def _mark_sustained(blocks: list[tuple[int, int, int, int]]) -> list[bool]:
    """标出与其他音（任意弦）在时间上真的重叠的音。

    ``blocks`` 必须按开始 tick 排序。只有严格重叠（后音开始 < 前音结束）才算；
    首尾相接的两个音是两次独立拨弦，不算重叠。
    """
    sustained = [False] * len(blocks)
    for index, (_start, end, _string, _fret) in enumerate(blocks):
        for later in range(index + 1, len(blocks)):
            if blocks[later][0] >= end:
                break
            sustained[index] = True
            sustained[later] = True
    return sustained


def _next_same_string_start(blocks: list[tuple[int, int, int, int]]) -> dict[int, int]:
    """每个音之后、同一根弦上下一个音的开始 tick（没有就不在返回表里）。"""
    result: dict[int, int] = {}
    latest: dict[int, int] = {}
    for index in range(len(blocks) - 1, -1, -1):
        start_tick, _end_tick, string, _fret = blocks[index]
        if string in latest:
            result[index] = latest[string]
        latest[string] = start_tick
    return result


class PositionPlanner:
    """「翻页 + 摇杆提前到位 + 按弦时刻」的共用规划器。

    gtab 的单音（:func:`build_schedule`）和 gct 的和弦（``ui.gct_backend``）
    用同一套规则，避免时序细节写两份：

    1. 换页提前 ``PAGE_LEAD``，而且**不能早于别的页上一个音按下**——否则那个音
       会被按在错误的页上；来不及就把冲突写进 ``conflicts``；
    2. 摇杆比按键早 ``aim_gap`` 秒到位；方向没变不重复发、也不多等；
    3. 前一个音太近时按键可以往后推迟，但最多到下一个音开始前；
       补不足就把实际间隔写进 ``hints``。
    """

    def __init__(
        self,
        actions: list[ScheduleAction],
        conflicts: list[str],
        hints: list[str],
        aim_gap: float,
    ) -> None:
        self.actions = actions
        self.conflicts = conflicts
        self.hints = hints
        self.gap_aim = aim_gap
        self.current_page = 1
        self.current_stick: tuple[int, int] | None = None
        # 最近一次「按下」的时刻：摇杆不能早于它，否则会把上一个音瞄准错
        self.last_press_time = 0.0
        # 每一页最近一次「按下」发生在什么时刻：翻页不能早于它
        self.last_press_at: dict[int, float] = {}

    # ---------------------------------------------------------------- 翻页

    def plan_page(self, target_page: int, start: float, tick: int, where: str) -> float:
        """按需要翻页，返回「最后一次翻页推到位」的时刻（没翻页返回 0.0）。

        翻页是**右摇杆**向左（下一页）/ 向右（上一页）推 ``PAGE_TAP_HOLD`` 再回中，
        一次推 = 一页；要跨几页就推几次（每次之间留 ``PAGE_TAP_GAP`` 让摇杆回中）。
        """
        if target_page == self.current_page:
            return 0.0

        forward = target_page > self.current_page
        direction = PAGE_STICK_NEXT if forward else PAGE_STICK_PREV
        arrow = "左" if forward else "右"
        steps = abs(target_page - self.current_page)
        span = steps * (PAGE_TAP_HOLD + PAGE_TAP_GAP)
        switch_at = max(0.0, start - PAGE_LEAD - span)
        # 上一批「别的页」的音已经按下之前不能翻页，否则它会按在错误的页上
        busy_until = max(
            (time for page, time in self.last_press_at.items() if page != target_page),
            default=0.0,
        )
        switch_at = max(switch_at, busy_until)
        if switch_at + span > start:
            self.conflicts.append(
                f"{where} 前翻页时间不足（需要向右摇杆{arrow}推 {steps} 次）"
            )
        for step in range(steps):
            press_at = switch_at + step * (PAGE_TAP_HOLD + PAGE_TAP_GAP)
            self.actions.append(
                ScheduleAction(
                    time=press_at,
                    kind="page",
                    stick=direction,
                    tick=tick,
                    page=target_page,
                    label=f"翻页 → 第 {target_page} 页（右摇杆{arrow}）",
                )
            )
            # 回中：下一次推（或随后的扫弦）要有一个新的边沿
            self.actions.append(
                ScheduleAction(
                    time=press_at + PAGE_TAP_HOLD,
                    kind="rstick",
                    stick=(0, 0),
                    tick=tick,
                    page=target_page,
                )
            )
        # 页号在最后一次推到位时就已切换，所以左摇杆只需等最后一次推之后就能动。
        page_done_at = switch_at + (steps - 1) * (PAGE_TAP_HOLD + PAGE_TAP_GAP)
        self.current_page = target_page
        return page_done_at

    # ---------------------------------------------------------------- 摇杆

    def aim_and_press(
        self,
        *,
        start: float,
        tick: int,
        stick_target: tuple[int, int],
        page_done_at: float,
        next_start: float | None,
        where: str,
        string: int = 0,
        fret: int = 0,
        chord: str = "",
    ) -> float:
        """必要时先发摇杆（提前 ``gap_aim``），返回按弦时刻。"""
        press_at = max(start, self.last_press_time)
        if stick_target == self.current_stick:
            return press_at

        # 不能早于：翻页完成、以及上一个音按下（否则上一个音会被瞄准错）。
        # 加 1ms 是为了避免「摇杆」与「上一个音的按下」落在同一时刻时被排序到前面。
        after_press = self.last_press_time + 0.001 if self.last_press_time > 0 else 0.0
        aim_at = max(
            0.0,
            start - self.gap_aim,
            min(page_done_at, start),
            min(after_press, start),
        )
        press_at = max(press_at, aim_at + self.gap_aim)
        if next_start is not None and press_at > next_start:
            press_at = next_start

        actual_gap = max(0.0, press_at - aim_at)
        if actual_gap + 1e-9 < self.gap_aim:
            self.hints.append(
                f"{where}：摇杆到位到按键只有 {actual_gap * 1000:.0f}ms"
                f"（建议 {self.gap_aim * 1000:.0f}ms，前一个音太近）"
            )
        self.actions.append(
            ScheduleAction(
                time=aim_at,
                kind="stick",
                stick=stick_target,
                tick=tick,
                string=string,
                fret=fret,
                page=self.current_page,
                chord=chord,
            )
        )
        self.current_stick = stick_target
        return press_at

    # ---------------------------------------------------------------- 只动摇杆

    def move_stick_only(
        self,
        *,
        at: float,
        tick: int,
        stick_target: tuple[int, int],
        fret: int = 0,
        chord: str = "",
    ) -> None:
        """只把摇杆摆到目标方向（gct 的 ChordSet：换指位、不出声）。

        没有按键，所以不需要 aim_gap；但不能早于上一个音按下（否则会瞄错）。
        """
        if stick_target == self.current_stick:
            return
        when = max(at, self.last_press_time + 0.001 if self.last_press_time > 0 else 0.0)
        self.actions.append(
            ScheduleAction(
                time=when,
                kind="stick",
                stick=stick_target,
                tick=tick,
                fret=fret,
                page=self.current_page,
                chord=chord,
            )
        )
        self.current_stick = stick_target

    # ---------------------------------------------------------------- 记账

    def mark_pressed(self, press_at: float, page: int) -> None:
        self.last_press_at[page] = max(self.last_press_at.get(page, 0.0), press_at)
        self.last_press_time = max(self.last_press_time, press_at)


def build_schedule(
    doc: GtabDocument,
    *,
    tap_notes: bool = True,
    tap_seconds: float = 0.08,
    repluck_gap: float = REPLUCK_GAP,
    aim_gap: float = AIM_GAP,
) -> Schedule:
    """把谱面翻译成按时间排序的动作序列。

    每个音符：必要的话先翻页（右摇杆向左/向右推一次 = 一页），把摇杆转到对应品，再按弦。
    摇杆（以及翻页）到位后，会留 ``aim_gap`` 秒再按弦——同一时刻切摇杆 + 按弦时，
    游戏可能在同一帧先处理按键，弹成上一个品。摇杆方向没变时不重复移动、也不等待。

    松键时间按下面的规则算：

    1. 不与其他音重叠的单个音：``tap_notes=True`` 时点按 ``tap_seconds``，
       否则按 NoteOff 时长；
    2. **与其他音（任意弦）时间重叠的音：保持按住到自己的 NoteOff**。
       摇杆在下一个音开始时照常切到新音需要的品，原来的弦不松手；
       这条不受点按开关影响，否则长音会在重叠处被提前掐断；
    3. 同一根弦上如果还没松手就要拨下一个音，则在新音开始前 ``repluck_gap`` 秒
       先松开，再重新按下（同一根弦不能一直按着当重拨）。默认
       ``REPLUCK_GAP=40ms``：太短（例如 10ms）可能短于游戏一帧，采样不到松开边沿。
    4. **摇杆/翻页与按弦之间至少隔 ``aim_gap`` 秒**（默认 65ms）。摇杆在音符开始前
       ``aim_gap`` 秒就转到位；如果前一个音按得太近、来不及提前，就按实际间隙缩短并
       记一条 ``hints``。摇杆方向没变时不重复发动作，也不额外等待。

    游戏在按下那一刻就锁定这个音，按住期间切摇杆/翻页不会改变它。所以
    「保持按住 + 摇杆照常切」是正常演奏方式，只记进 ``hints``，不算 ``conflicts``。
    """
    blocks = sorted(doc.note_blocks(), key=lambda block: block[0])
    sustained = _mark_sustained(blocks)
    next_same = _next_same_string_start(blocks)
    gap = max(0.005, min(0.2, float(repluck_gap)))
    gap_aim = max(0.0, min(0.3, float(aim_gap)))
    actions: list[ScheduleAction] = []
    conflicts: list[str] = []
    hints: list[str] = []
    planner = PositionPlanner(actions, conflicts, hints, gap_aim)

    # 重叠检测：按下即锁音，所以「按住长的、摇杆去切新的」是正常演奏，只记提示
    for index, (start_tick, end_tick, string, fret) in enumerate(blocks):
        for other_start, _other_end, other_string, other_fret in blocks[index + 1 :]:
            if other_start >= end_tick:
                break
            if other_fret == fret:
                continue
            if other_string == string:
                hints.append(
                    f"{string} 弦在第 {doc.beats_at(other_start):g} 拍从 {fret} 品"
                    f"换到 {other_fret} 品：先松开再重拨"
                )
            else:
                hints.append(
                    f"{string} 弦 {fret} 品与 {other_string} 弦 {other_fret} 品"
                    f"在第 {doc.beats_at(other_start):g} 拍重叠："
                    f"{string} 弦保持按住，摇杆切到 {other_fret} 品"
                )

    for index, (start_tick, end_tick, string, fret) in enumerate(blocks):
        start = LEAD_IN + tick_to_seconds(doc, start_tick)
        end = LEAD_IN + tick_to_seconds(doc, end_tick)
        target_page = page_for(fret)

        # 1) 翻页（不能早于别的页上一个音按下，否则那个音会按在错的页上）
        page_done_at = planner.plan_page(
            target_page, start, start_tick, f"{string}弦{fret}品"
        )

        # 2) 摇杆方向（品）：方向变了才移动，并且尽量比按键早 aim_gap 秒到位
        #    下一个音的起点：按弦最多推迟到这里，避免抢到后一个音前面
        next_start = (
            LEAD_IN + tick_to_seconds(doc, blocks[index + 1][0])
            if index + 1 < len(blocks)
            else None
        )
        press_at = planner.aim_and_press(
            start=start,
            tick=start_tick,
            stick_target=fret_direction(fret),
            page_done_at=page_done_at,
            next_start=next_start,
            where=f"{string} 弦 {fret} 品",
            string=string,
            fret=fret,
        )

        # 3) 按弦
        button = STRING_BUTTONS.get(string)
        if button is None:
            conflicts.append(f"{string}弦没有映射到按键，已跳过")
            continue
        actions.append(
            ScheduleAction(
                time=press_at,
                kind="press",
                button=button,
                tick=start_tick,
                string=string,
                fret=fret,
                page=planner.current_page,
                label=f"{string}弦 {fret}品 → {button} + {direction_label(fret)}",
                unit=True,
            )
        )
        planner.mark_pressed(press_at, target_page)
        # 与其他音重叠的长音保持到自己的 NoteOff；不重叠的才按点按开关走
        if sustained[index] or not tap_notes:
            release_at = max(end, press_at + MIN_PRESS)
        else:
            release_at = press_at + min(tap_seconds, max(0.02, end - start))

        # 同一根弦要拨下一个音时，先松开再重拨
        next_tick = next_same.get(index)
        if next_tick is not None:
            next_start = LEAD_IN + tick_to_seconds(doc, next_tick)
            if next_start > start:
                release_at = min(release_at, next_start - gap)
        release_at = max(release_at, start + MIN_PRESS)

        actions.append(
            ScheduleAction(
                time=release_at,
                kind="release",
                button=button,
                tick=end_tick,
                string=string,
                fret=fret,
                page=planner.current_page,
            )
        )

    # 同一时刻：先松、再动摇杆/翻页、最后按
    priority = {"release": 0, "stick": 1, "page": 1, "rstick": 2, "press": 2}
    actions.sort(key=lambda action: (round(action.time, 6), priority.get(action.kind, 3)))
    return Schedule(actions=actions, conflicts=conflicts, hints=hints)
