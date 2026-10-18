"""MIDI → GTAB 转换：把 MIDI 音符排到 6 弦 24 品上（纯逻辑，不依赖 Flet）。

规则（与界面上的说明一一对应）：

- **标准定弦** E2 A2 D3 G3 B3 E4（1–6 弦，MIDI 64/59/55/50/45/40），
  0 品 = 空弦，最多 24 品，所以能弹的音域是 MIDI 40–88（E2–E6）；
- **同时起音**（同一个 tick）默认只保留**最高音**，其余计入
  ``dropped_simultaneous``；``ConvertOptions.keep_top_only=False`` 时保留全部，
  但最多 6 个（一条弦一个），并且尽量排在同一页；
- **打击乐**：MIDI 通道 10（0 基 9，GM 鼓）默认跳过（``skip_percussion=True``），
  否则鼓点会被当成低音/噪音排进谱里；
- **把位（页面）优先沿用上一个音**：游戏每页 6 个品（第 1 页 1–6、第 2 页 7–12、
  第 3 页 13–18、第 4 页 19–24，第 5 页是空弦），翻页要推右摇杆、很花时间。
  所以候选位置的打分顺序是：先“离上一个音的页面最近”，再“不碰还按着的弦”，
  再“不撞还按着的同弦同品”，最后才是“品离上一个音近 / 品小 / 弦高”。
  初始页面按游戏默认的第 1 页算，所以第一个音也不会没事先跑到空弦页；
- 超出音域的音直接跳过并计数；MIDI 的 tempo 变化会按 480 tick/拍重新换算后
  写成 GTAB 的 Tempo 事件，速度与 MIDI 保持一致；
- 同一个 ``(弦, 品)`` 不允许重叠：真有重复音压在一起时，把前一个音截到后一个
  音的起点（``trimmed_overlaps``），保证写出的 GTAB 能通过严格校验。

``convert()`` 返回 :class:`ConversionResult`：``doc`` 是可以直接保存的
``GtabDocument``，``notes`` / ``stats`` / ``warnings`` 给界面显示用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import groupby

from gtab import EV_TEMPO
from ui.gtab_doc import EventRow, GtabDocument
from ui.midi_doc import MidiDocument, note_name

__all__ = [
    "GTAB_DIVISION",
    "MAX_FRET",
    "OPEN_PAGE",
    "PAGE_SIZE",
    "START_PAGE",
    "TUNING",
    "ConversionResult",
    "ConversionStats",
    "ConvertOptions",
    "ConvertedNote",
    "Position",
    "convert",
    "page_for",
    "positions_for",
]

#: 1–6 弦的空弦 MIDI 音高（标准定弦）。
TUNING: tuple[int, ...] = (64, 59, 55, 50, 45, 40)

MAX_FRET = 24
PAGE_SIZE = 6
OPEN_PAGE = 5
#: 游戏开局停在第 1 页（见 ``ui.gamepad_backend.GamepadBackend.current_page``）。
START_PAGE = 1

#: 写出的 gtab 固定 480 tick/拍（与 GTAB 编辑页一致）。
GTAB_DIVISION = 480
DEFAULT_TEMPO_US = 500_000


def page_for(fret: int) -> int:
    """品 → 游戏页面：0 品是第 5 页（空弦），1–6 品第 1 页，以此类推。

    必须与 ``ui.gamepad_backend.page_for`` 保持一致；测试里有一条断言把两者
    逐品对比，改常量时两边一起改。
    """
    if fret <= 0:
        return OPEN_PAGE
    return (fret - 1) // PAGE_SIZE + 1


@dataclass(frozen=True)
class Position:
    """一个可弹的位置：弦（1–6）+ 品（0–24）。"""

    string: int
    fret: int

    @property
    def page(self) -> int:
        return page_for(self.fret)

    @property
    def key(self) -> tuple[int, int]:
        """GTAB 的配对键 ``(弦, 品)``（``voice`` 统一为 0）。"""
        return (self.string, self.fret)


def positions_for(pitch: int, max_fret: int = MAX_FRET) -> list[Position]:
    """一个 MIDI 音高在标准定弦上的所有位置（按弦号 1→6 排序）。"""
    result: list[Position] = []
    for string, open_pitch in enumerate(TUNING, start=1):
        fret = int(pitch) - open_pitch
        if 0 <= fret <= max_fret:
            result.append(Position(string, fret))
    return result


@dataclass
class ConvertedNote:
    """转换后的一个音（起止 tick、原 MIDI 音高、落在哪根弦/哪一品）。"""

    start: int
    end: int
    pitch: int
    string: int
    fret: int

    @property
    def duration(self) -> int:
        return max(1, self.end - self.start)

    @property
    def page(self) -> int:
        return page_for(self.fret)

    @property
    def name(self) -> str:
        return note_name(self.pitch)

    @property
    def key(self) -> tuple[int, int]:
        return (self.string, self.fret)


@dataclass
class ConvertOptions:
    """转换选项；界面上的开关直接映射到这里。"""

    #: True：同时起音只保留最高音（默认）；False：全部保留（最多 6 个）
    keep_top_only: bool = True
    #: True：跳过 MIDI 通道 10（0 基通道 9）的打击乐（默认）
    skip_percussion: bool = True
    max_fret: int = MAX_FRET


@dataclass
class ConversionStats:
    """转换统计，界面左栏显示。"""

    source_notes: int = 0
    output_notes: int = 0
    dropped_simultaneous: int = 0
    skipped_out_of_range: int = 0
    skipped_percussion: int = 0
    dropped_no_position: int = 0
    trimmed_overlaps: int = 0
    page_changes: int = 0
    page_steps: int = 0
    pages_used: list[int] = field(default_factory=list)


@dataclass
class ConversionResult:
    """一次转换的全部结果。"""

    doc: GtabDocument
    notes: list[ConvertedNote]
    stats: ConversionStats
    warnings: list[str] = field(default_factory=list)


def _scale_tick(tick: int, scale: float) -> int:
    return max(0, int(round(int(tick) * scale)))


def _score(
    position: Position,
    prev_page: int,
    prev_fret: int,
    active_keys: set[tuple[int, int]],
    active_strings: set[int],
) -> tuple[int, int, int, int, int, int]:
    """候选位置排序键：页面距离优先，其次避开还按着的弦/键，再求手位近。"""
    return (
        abs(position.page - prev_page),
        1 if position.string in active_strings else 0,
        1 if position.key in active_keys else 0,
        abs(position.fret - prev_fret),
        position.fret,
        -position.string,
    )


def _assign_group(
    pitches: list[int],
    prev_page: int,
    prev_fret: int,
    active_keys: set[tuple[int, int]],
    active_strings: set[int],
    max_fret: int,
) -> list[Position]:
    """给同一 tick 的一组音找位置：先试“整组落在同一页”，再退回逐音贪心。"""
    if not pitches:
        return []

    # 页面按“离上一页近”排序；整组都能落在同一页就采用（和弦不跨页）。
    for page in sorted(range(1, 6), key=lambda item: (abs(item - prev_page), item)):
        chosen: list[Position] = []
        used_strings: set[int] = set()
        for pitch in pitches:
            candidates = [
                position
                for position in positions_for(pitch, max_fret)
                if position.page == page and position.string not in used_strings
            ]
            if not candidates:
                break
            chosen.append(
                min(
                    candidates,
                    key=lambda position: _score(
                        position, prev_page, prev_fret, active_keys, active_strings
                    ),
                )
            )
            used_strings.add(chosen[-1].string)
        if len(chosen) == len(pitches):
            return chosen

    # 单页放不下（例如低音必须用空弦页）：逐音选，仍尽量不重复用弦。
    chosen = []
    used_strings = set()
    for pitch in pitches:
        candidates = [
            position
            for position in positions_for(pitch, max_fret)
            if position.string not in used_strings
        ]
        if not candidates:
            candidates = positions_for(pitch, max_fret)
        if not candidates:
            continue
        chosen.append(
            min(
                candidates,
                key=lambda position: _score(
                    position, prev_page, prev_fret, active_keys, active_strings
                ),
            )
        )
        used_strings.add(chosen[-1].string)
    return chosen


def _tempo_plan(doc: MidiDocument, scale: float) -> tuple[int, dict[int, int]]:
    """返回 (头部默认速度, {tick: 微秒/拍})；tick 0 的速度写进头部。"""
    default_tempo = DEFAULT_TEMPO_US
    events: dict[int, int] = {}
    for mark in sorted(doc.tempos, key=lambda item: item.tick):
        tick = _scale_tick(mark.tick, scale)
        tempo_us = max(1, int(mark.tempo_us))
        if tick <= 0:
            default_tempo = tempo_us
        else:
            events[tick] = tempo_us
    return default_tempo, events


def convert(
    doc: MidiDocument, options: ConvertOptions | None = None
) -> ConversionResult:
    """把 MIDI 文档转换成 GTAB 文档（不修改入参）。"""
    options = options or ConvertOptions()
    stats = ConversionStats(source_notes=len(doc.notes))
    scale = GTAB_DIVISION / max(1, doc.division)
    max_fret = max(1, int(options.max_fret))
    highest = TUNING[0] + max_fret

    # 1) 过滤音域、换算时间轴
    raw: list[tuple[int, int, int]] = []
    for note in doc.notes:
        pitch = int(note.note)
        if options.skip_percussion and int(note.channel) == 9:
            # GM 的通道 10（0 基 9）是鼓；鼓点转成吉他音没有意义
            stats.skipped_percussion += 1
            continue
        if pitch < TUNING[-1] or pitch > highest:
            stats.skipped_out_of_range += 1
            continue
        start = _scale_tick(note.start, scale)
        end = max(start + 1, _scale_tick(note.end, scale))
        raw.append((start, end, pitch))

    raw.sort(key=lambda item: (item[0], -item[2]))

    # 2) 按起点分组；每组先按音高去重（同音保留最长的那次）
    planned: list[ConvertedNote] = []
    active: list[ConvertedNote] = []
    prev_page = START_PAGE
    prev_fret = 0

    for start, group in groupby(raw, key=lambda item: item[0]):
        by_pitch: dict[int, int] = {}
        for _start, end, pitch in group:
            by_pitch[pitch] = max(by_pitch.get(pitch, 0), end - start)
        ordered = sorted(by_pitch.items(), key=lambda item: -item[0])

        if options.keep_top_only:
            stats.dropped_simultaneous += max(0, len(ordered) - 1)
            ordered = ordered[:1]
        elif len(ordered) > 6:
            stats.dropped_no_position += len(ordered) - 6
            ordered = ordered[:6]

        pitches = [pitch for pitch, _duration in ordered]

        # 只保留“还在响”的音：它们的弦/键不能随便复用
        active = [note for note in active if note.end > start]
        active_keys = {note.key for note in active}
        active_strings = {note.string for note in active}

        positions = _assign_group(
            pitches, prev_page, prev_fret, active_keys, active_strings, max_fret
        )
        if len(positions) != len(ordered):
            # 正常不会发生（音域内每个音至少有一个位置）；留个保险计数
            stats.dropped_no_position += len(ordered) - len(positions)
            ordered = ordered[: len(positions)]

        for (pitch, duration), position in zip(ordered, positions):
            note = ConvertedNote(
                start=start,
                end=start + max(1, duration),
                pitch=pitch,
                string=position.string,
                fret=position.fret,
            )
            planned.append(note)
            # 同弦同品不允许重叠：把前面那个截到当前音的起点
            for earlier in active:
                if earlier.key == note.key and earlier.end > start:
                    earlier.end = max(earlier.start + 1, start)
                    stats.trimmed_overlaps += 1
            active.append(note)

        if positions:
            group_page = positions[0].page
            stats.page_changes += 1 if group_page != prev_page else 0
            stats.page_steps += abs(group_page - prev_page)
            prev_page = group_page
            prev_fret = positions[0].fret

    # 3) 组装 GTAB：Tempo 事件 + 音符
    default_tempo, tempo_events = _tempo_plan(doc, scale)
    out = GtabDocument(
        max_fret=max_fret,
        time_base_value=GTAB_DIVISION,
        default_tempo_us=default_tempo,
    )
    for tick in sorted(tempo_events):
        out.events.append(
            EventRow(tick, EV_TEMPO, 0, int(tempo_events[tick]).to_bytes(4, "little"))
        )
    for note in planned:
        out.add_note(
            note.start,
            note.string,
            note.fret,
            duration_ticks=note.duration,
        )

    stats.output_notes = len(planned)
    stats.pages_used = sorted({note.page for note in planned})

    warnings: list[str] = []
    if stats.skipped_percussion:
        warnings.append(
            f"{stats.skipped_percussion} 个打击乐（MIDI 通道 10）音符已跳过"
            "（可在左栏关掉）。"
        )
    if stats.skipped_out_of_range:
        warnings.append(
            f"{stats.skipped_out_of_range} 个音超出 6 弦 0–24 品的音域"
            f"（{note_name(TUNING[-1])}–{note_name(highest)}），已跳过。"
        )
    if stats.dropped_simultaneous:
        warnings.append(
            f"同时起音的 {stats.dropped_simultaneous} 个较低音被省略"
            "（默认只弹最高音；可在左栏关掉）。"
        )
    if stats.dropped_no_position:
        warnings.append(f"{stats.dropped_no_position} 个音找不到可用的弦/品，已跳过。")
    if stats.trimmed_overlaps:
        warnings.append(
            f"{stats.trimmed_overlaps} 个同弦同品的音在重叠处被截断"
            "（GTAB 不允许同键重叠）。"
        )
    if not planned:
        warnings.append("没有可转换的音符。")

    return ConversionResult(doc=out, notes=planned, stats=stats, warnings=warnings)
