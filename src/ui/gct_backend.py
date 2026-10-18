"""gct 和弦谱 → 手柄动作排期。

玩法与 gtab 相同（左摇杆选位置、按键拨弦），区别有三点：

1. **位置不是品，而是和弦位**：``chord.json`` 的 24 个和弦 = 4 页 × 6 个摇杆
   方向，名字 → 位置的换算在 :mod:`ui.chords` 里；
2. **扫弦用右摇杆**（用户 2026-10-18 指定）：**向下推 = 下扫、向上推 = 上扫**，
   不再依次按 6 个弦键。推到位后 ``CHORD_STRUM_FLICK`` 秒回中，形成一次「扫」；
   轮盘上的六根弦仍然**按顺序点亮**（纯显示，速度比原来逐键按快），
   亮完 ``CHORD_LIGHT_HOLD`` 秒后一起熄灭；
3. **单弦拨奏（Pluck）** 仍按对应的弦键并按住 ``CHORD_HOLD``；**只切指位
   （Set）** 只摆左摇杆、不出声。

翻页 / 左摇杆提前量 / 时序推迟这些规则与 gtab 完全共用
:class:`ui.gamepad_backend.PositionPlanner`，改一处两边都变。
"""

from __future__ import annotations

from gct import DIR_DOWN, EV_CHORD_PLUCK, EV_CHORD_STRUM
from ui.chords import ChordError, load_chord_names, position_of
from ui.gamepad_backend import (
    AIM_GAP,
    LEAD_IN,
    STRING_BUTTONS,
    STRUM_DOWN,
    STRUM_UP,
    PositionPlanner,
    Schedule,
    ScheduleAction,
    direction_label,
    fret_direction,
)
from ui.gct_doc import GctDocument, tick_to_seconds

__all__ = [
    "CHORD_HOLD",
    "CHORD_LIGHT_HOLD",
    "CHORD_STRUM_FLICK",
    "CHORD_STRUM_SWEEP",
    "build_chord_schedule",
]

# 右摇杆推下去保持多久再回中：太短游戏可能采样不到，太长像「按住」不像「扫」
CHORD_STRUM_FLICK = 0.05
# 轮盘上六根弦依次点亮的速度（纯显示；原来逐键按是 30ms 一根，这里更快一点但还能看清）
CHORD_STRUM_SWEEP = 0.02
# 六根弦亮完之后保持多久再一起熄灭
CHORD_LIGHT_HOLD = 0.18
# 单弦拨奏按住多久
CHORD_HOLD = 0.15

_STRUM_DOWN = (6, 5, 4, 3, 2, 1)
_STRUM_UP = (1, 2, 3, 4, 5, 6)


def build_chord_schedule(
    doc: GctDocument,
    *,
    sweep: float = CHORD_STRUM_SWEEP,
    flick: float = CHORD_STRUM_FLICK,
    light_hold: float = CHORD_LIGHT_HOLD,
    hold: float = CHORD_HOLD,
    aim_gap: float = AIM_GAP,
) -> Schedule:
    """把 gct 和弦谱翻译成按时间排序的手柄动作。

    - 每个和弦事件先翻页（右摇杆向左/向右推一次 = 一页）、把左摇杆转到和弦位，
      隔 ``aim_gap`` 再扫弦/拨弦；
    - 扫弦（Strum）：右摇杆向下（下扫）或向上（上扫）推 ``flick`` 秒再回中；
      轮盘六根弦按 ``sweep`` 秒一根依次点亮，``light_hold`` 后一起熄灭；
    - 单弦拨奏（Pluck）：只按那根弦的键，按住 ``hold`` 秒；
    - 只切指位（Set）：只动左摇杆，不按键、不扫弦；
    - 和弦名不在 ``chord.json`` 里时记一条冲突并跳过那个事件。
    """
    actions: list[ScheduleAction] = []
    conflicts: list[str] = []
    hints: list[str] = []
    gap_aim = max(0.0, min(0.3, float(aim_gap)))
    gap_sweep = max(0.004, min(0.2, float(sweep)))
    gap_flick = max(0.01, min(0.3, float(flick)))
    gap_light = max(gap_flick, min(2.0, float(light_hold)))
    gap_hold = max(0.02, min(1.0, float(hold)))
    planner = PositionPlanner(actions, conflicts, hints, gap_aim)

    try:
        names = load_chord_names()
    except ChordError as exc:
        raise ChordError(f"读取和弦表失败：{exc}") from exc

    events = doc.chord_events()
    for index, event in enumerate(events):
        name = doc.chord_name(event.chord_id)
        position = position_of(name, names)
        if position is None:
            conflicts.append(
                f"和弦 {name or '（空）'} 不在 chord.json 的和弦表里，已跳过"
            )
            continue
        page, dir_index = position
        stick_target = fret_direction(dir_index + 1)
        start = LEAD_IN + tick_to_seconds(doc, event.tick)

        # 1) 翻页（与 gtab 同一套规则：不能早于别的页上一个音按下）
        page_done_at = planner.plan_page(page, start, event.tick, f"和弦 {name}")

        # 2) 只切指位：摇杆摆过去就好，不按键
        if event.event_type != EV_CHORD_STRUM and event.event_type != EV_CHORD_PLUCK:
            planner.move_stick_only(
                at=start,
                tick=event.tick,
                stick_target=stick_target,
                fret=dir_index + 1,
                chord=name,
            )
            continue

        # 3) 左摇杆提前到位，得到「开始扫/拨」的时刻
        at = planner.aim_and_press(
            start=start,
            tick=event.tick,
            stick_target=stick_target,
            page_done_at=page_done_at,
            next_start=None,
            where=f"和弦 {name}",
            fret=dir_index + 1,
            chord=name,
        )

        if event.event_type == EV_CHORD_PLUCK:
            # 单弦拨奏：还是按一根弦键，按住 CHORD_HOLD
            button = STRING_BUTTONS.get(event.string)
            if button is None:
                conflicts.append(f"{event.string} 弦没有映射到按键，已跳过")
                continue
            actions.append(
                ScheduleAction(
                    time=at,
                    kind="press",
                    button=button,
                    tick=event.tick,
                    string=event.string,
                    fret=dir_index + 1,
                    page=page,
                    label=(
                        f"{name} 拨 {event.string} 弦"
                        f"（第 {page} 页 {direction_label(dir_index + 1)}）"
                    ),
                    unit=True,
                    chord=name,
                )
            )
            actions.append(
                ScheduleAction(
                    time=at + gap_hold,
                    kind="release",
                    button=button,
                    tick=event.tick,
                    string=event.string,
                    fret=dir_index + 1,
                    page=page,
                    chord=name,
                )
            )
            planner.mark_pressed(at, page)
            continue

        # 4) 扫弦：右摇杆推一下（下扫/上扫），轮盘上的弦按顺序亮
        up = event.direction != DIR_DOWN
        strings = _STRUM_UP if up else _STRUM_DOWN
        push = STRUM_UP if up else STRUM_DOWN
        verb = "上扫" if up else "下扫"
        actions.append(
            ScheduleAction(
                time=at,
                kind="rstick",
                stick=push,
                tick=event.tick,
                fret=dir_index + 1,
                page=page,
                chord=name,
                label=f"{name} {verb}（右摇杆）",
            )
        )
        actions.append(
            ScheduleAction(
                time=at + gap_flick,
                kind="rstick",
                stick=(0, 0),
                tick=event.tick,
                fret=dir_index + 1,
                page=page,
                chord=name,
            )
        )

        last_light = at + (len(strings) - 1) * gap_sweep
        for order, string in enumerate(strings):
            actions.append(
                ScheduleAction(
                    time=at + order * gap_sweep,
                    kind="light",
                    tick=event.tick,
                    string=string,
                    fret=dir_index + 1,
                    page=page,
                    chord=name,
                    # 一个和弦事件只算一个单位（第 2–6 根弦不重复计）
                    unit=order == 0,
                    label=(
                        f"{name} {verb} 第 {string} 弦"
                        f"（第 {page} 页 {direction_label(dir_index + 1)}）"
                    )
                    if order == 0
                    else "",
                )
            )

        # 熄灭：不早于最后一根亮起，也别拖到下一个和弦的开头
        dark_at = at + gap_light
        if index + 1 < len(events):
            next_start = LEAD_IN + tick_to_seconds(doc, events[index + 1].tick)
            dark_at = min(dark_at, next_start - 0.005)
        dark_at = max(dark_at, last_light + 0.005)
        for string in strings:
            actions.append(
                ScheduleAction(
                    time=dark_at,
                    kind="dark",
                    tick=event.tick,
                    string=string,
                    fret=dir_index + 1,
                    page=page,
                    chord=name,
                )
            )

        planner.mark_pressed(at, page)

    # 同一时刻：先松/灭、再摇杆/翻页、最后点亮/按弦
    priority = {
        "release": 0,
        "dark": 0,
        "stick": 1,
        "page": 1,
        "rstick": 2,
        "light": 3,
        "press": 4,
    }
    actions.sort(key=lambda action: (round(action.time, 6), priority.get(action.kind, 5)))
    return Schedule(
        actions=actions,
        conflicts=conflicts,
        hints=hints,
        unit_label="和弦",
    )
