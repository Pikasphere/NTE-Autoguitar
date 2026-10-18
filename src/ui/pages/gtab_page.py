"""GTAB 编辑页 —— 主 GUI 里的一个页面（不是最终成品）。

本文件只产出一个可嵌入的 ``ft.Control``，不依赖应用外壳：
主界面（src/app.py）把它塞进页面容器即可，以后换壳不用动这里。

能力：
- 打开 .gtab：解析头部 / 元数据 / 事件并展示；
- 节拍轴：事件按「第几拍」排列，音符按起止拍画成方块、按弦分轨；
- 音符视图：一行 = 一个音（NoteOn + 音长），**NoteOff 不再单独显示**，
  由模型层按音长自动维护；保存 / 写入时缺的 NoteOff 会自动补上；
- 试听：按当前 BPM / Tempo 把谱面合成成 WAV 直接播放（调试用），
  节拍轴上有一条红色播放游标；
- 编辑：弦数、最大品、默认 BPM、事件 CRC、meta TLV、
  其他事件（Tempo / Marker / Snapshot / End）；
- 保存：覆盖原文件或另存为，保存前做完整校验（回读严格解析）。

说明：gtab 头部仍然有 time_base 字段（格式需要），但界面不再暴露它——
新文档固定用 480（即 1 拍 = 480 tick），打开旧文件时沿用文件里的值。
界面上的位置一律用「拍」，不涉及真实时间（秒）；秒只用于试听进度。
"""

from __future__ import annotations

import asyncio
import copy
import math
import threading
import time
from pathlib import Path

import flet as ft

from gtab import EV_TEMPO
from ui import audio_preview
from ui.components import BEATS_PER_BAR, LANE_HEIGHT, PX_PER_BEAT, RULER_HEIGHT
from ui.gtab_doc import (
    DEFAULT_NOTE_DURATION_BEATS,
    EVENT_LABELS,
    NOTE_EVENTS,
    TYPE_BY_LABEL,
    EventRow,
    GtabDocument,
    GtabError,
    MetaEntry,
    NoteEntry,
    default_event,
    seconds_to_tick,
)
from ui.settings import force_event_crc

__all__ = ["GtabPage"]

_HEX_FILTER = r"[0-9a-fA-F ]*"
_META_TYPE_FILTER = r"[0-9a-fA-F]{0,4}"
_BEAT_FILTER = r"[0-9.]*"

_MUTED = ft.Colors.ON_SURFACE_VARIANT

# 节拍轴外观（PX_PER_BEAT / LANE_HEIGHT / RULER_HEIGHT / BEATS_PER_BAR）
# 从 ui.components 导入：GTAB 和 GCT 两个编辑页共用同一套刻度。

STRING_COLORS = [
    ft.Colors.INDIGO_400,
    ft.Colors.BLUE_400,
    ft.Colors.TEAL_400,
    ft.Colors.GREEN_400,
    ft.Colors.AMBER_400,
    ft.Colors.ORANGE_400,
    ft.Colors.RED_400,
    ft.Colors.PURPLE_400,
]


def _parse_beats(text: str | None) -> float | None:
    """把 ``1`` / ``2.5`` 解析成拍数；解析不了返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class GtabPage:
    """GTAB 编辑器页面。"""

    TITLE = "GTAB 编辑"
    SUBTITLE = "读取 / 编辑 / 写回 .gtab 文件"
    DEMO_PATH = Path(__file__).resolve().parents[2] / "MusicList" / "gtabfile" / "demo.gtab"

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.doc = GtabDocument()
        self.path: Path | None = None
        self.dirty = False
        # 试听状态（线程 + 播放游标）
        self._playing = False
        self._preview_stop: threading.Event | None = None
        self._preview_token = 0
        self._audio_started_at: float | None = None
        self._audio_duration = 0.0
        self._audio_error: str | None = None
        # 在页面上下文里创建，FilePicker 会自动注册到当前 page
        self._picker: ft.FilePicker | None = ft.FilePicker() if page is not None else None
        self._root = self._build()
        self._render_all()

    # ------------------------------------------------------------------ 组合

    def build(self) -> ft.Control:
        """返回页面根控件，供主界面嵌入。"""
        return self._root

    def _build(self) -> ft.Control:
        self._file_label = ft.Text("未命名", size=12, color=_MUTED)
        self._stats_label = ft.Text("", size=12, color=_MUTED)
        self._preview_label = ft.Text("", size=11, color=_MUTED)

        # 头部参数
        self._f_strings = self._num_field(84, "弦数", self._on_header_change)
        self._f_maxfret = self._num_field(84, "最大品", self._on_header_change)
        self._f_bpm = ft.TextField(
            width=96,
            dense=True,
            label="BPM",
            hint_text="120",
            on_change=self._on_bpm_change,
        )

        # 元数据
        self._meta_column = ft.Column(spacing=6)

        # 音符（NoteOn + 音长）与其他事件分成两个列表
        self._notes_list = ft.ListView(expand=True, spacing=6, padding=4)
        # 其他事件通常很少（Tempo / End），给固定高度，保证上下两块都看得见
        self._events_list = ft.ListView(height=132, spacing=6, padding=4)
        self._events_warning = ft.Text(
            "",
            size=11,
            color=ft.Colors.ORANGE_300,
            visible=False,
        )

        # 试听游标（在 _render_timeline 里塞进 Stack）
        self._playhead = ft.Container(
            left=0,
            top=RULER_HEIGHT - 2,
            width=3,
            height=LANE_HEIGHT,
            bgcolor=ft.Colors.RED_400,
            visible=False,
            tooltip="试听播放位置",
        )
        self._timeline_width = 0.0
        # 试听时自动跟随：记录横向滚动到的位置，避免每秒都发滚动指令
        self._follow_offset = 0.0
        self._timeline_scroll: ft.Row | None = None

        body = ft.Row(
            [
                self._left_panel(),
                self._events_panel(),
            ],
            expand=True,
            spacing=16,
            # 必须纵向拉伸：否则右面板高度无界，音符列表会把「其他事件」顶出窗口
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        return ft.Container(
            expand=True,
            padding=ft.Padding.only(left=20, top=16, right=20, bottom=12),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text(self.TITLE, size=22, weight=ft.FontWeight.BOLD),
                                    ft.Text(self.SUBTITLE, size=12, color=_MUTED),
                                ],
                                spacing=2,
                            ),
                            ft.Container(expand=True),
                            self._file_label,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._toolbar(),
                    ft.Divider(height=1),
                    body,
                ],
                spacing=12,
                expand=True,
                # 横向拉伸：标题栏的文件名、工具栏右侧的统计才能贴到右边
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

    def _toolbar(self) -> ft.Control:
        return ft.Row(
            [
                ft.Button("新建", icon=ft.Icons.NOTE_ADD, on_click=self._on_new),
                ft.Button("打开", icon=ft.Icons.FOLDER_OPEN, on_click=self._on_open),
                ft.Button("保存", icon=ft.Icons.SAVE, on_click=self._on_save),
                ft.Button("另存为", icon=ft.Icons.SAVE_AS, on_click=self._on_save_as),
                ft.TextButton("载入示例", icon=ft.Icons.AUTO_AWESOME, on_click=self._on_load_demo),
                ft.VerticalDivider(width=1),
                ft.Button("试听", icon=ft.Icons.PLAY_ARROW, on_click=self._on_preview),
                ft.TextButton("停止", icon=ft.Icons.STOP, on_click=self._on_stop_preview),
                self._preview_label,
                ft.Container(expand=True),
                self._stats_label,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _left_panel(self) -> ft.Control:
        header_card = self._card(
            "头部参数",
            ft.Column(
                [
                    ft.Row(
                        [self._f_strings, self._f_maxfret],
                        wrap=True,
                        spacing=8,
                        run_spacing=8,
                    ),
                    ft.Row(
                        [self._f_bpm],
                        wrap=True,
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "事件位置统一用节拍（拍）表示；BPM 只影响播放速度，不改变节拍位置。",
                        size=11,
                        color=_MUTED,
                    ),
                ],
                spacing=8,
            ),
        )

        meta_card = self._card(
            "元数据（meta TLV）",
            ft.Column(
                [
                    ft.Text(
                        "类型填十六进制，如 0001 = 定弦名；取值写文本，"
                        "或以 hex: 开头写十六进制字节。",
                        size=11,
                        color=_MUTED,
                    ),
                    self._meta_column,
                    ft.TextButton(
                        "添加元数据",
                        icon=ft.Icons.ADD,
                        on_click=self._on_add_meta,
                    ),
                ],
                spacing=8,
            ),
        )

        return ft.Column(
            [header_card, meta_card],
            width=352,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _events_panel(self) -> ft.Control:
        self._timeline_host = ft.Container(
            height=RULER_HEIGHT + 6 * LANE_HEIGHT + 4,
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.ON_SURFACE),
            content=ft.Row(),
        )

        notes_header = ft.Row(
            [
                ft.Text("音符", size=14, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "一行一个音；只改音长，NoteOff 保存时自动补",
                    size=11,
                    color=_MUTED,
                ),
                ft.Container(expand=True),
                ft.TextButton(
                    "添加音符",
                    icon=ft.Icons.ADD,
                    tooltip="在最后一个音之后加一个 1 拍的音（6 弦空弦）",
                    on_click=self._on_add_note,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        notes_heading = ft.Row(
            [
                ft.Text("#", width=34, size=11, color=_MUTED),
                ft.Text("拍", width=84, size=11, color=_MUTED),
                ft.Text("音长(拍)", width=90, size=11, color=_MUTED),
                ft.Text("弦", width=56, size=11, color=_MUTED),
                ft.Text("品", width=56, size=11, color=_MUTED),
                ft.Text("voice", width=56, size=11, color=_MUTED),
            ],
            spacing=6,
        )

        events_header = ft.Row(
            [
                ft.Text("其他事件", size=14, weight=ft.FontWeight.BOLD),
                ft.Text("Tempo / Marker / Snapshot / End", size=11, color=_MUTED),
                ft.Container(expand=True),
                ft.TextButton(
                    "添加事件",
                    icon=ft.Icons.ADD,
                    tooltip="添加一个 Tempo 事件（类型可在行内切换）",
                    on_click=self._on_add_event,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        events_heading = ft.Row(
            [
                ft.Text("#", width=34, size=11, color=_MUTED),
                ft.Text("拍", width=90, size=11, color=_MUTED),
                ft.Text("类型", width=124, size=11, color=_MUTED),
                ft.Text("voice", width=56, size=11, color=_MUTED),
                ft.Text("数据", size=11, color=_MUTED),
            ],
            spacing=6,
        )

        return ft.Container(
            expand=True,
            padding=16,
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("节拍轴", size=14, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "试听按当前 BPM / Tempo 合成，只用于调试",
                                size=11,
                                color=_MUTED,
                            ),
                        ]
                    ),
                    self._timeline_host,
                    notes_header,
                    notes_heading,
                    ft.Divider(height=1),
                    self._notes_list,
                    self._events_warning,
                    events_header,
                    events_heading,
                    ft.Divider(height=1),
                    self._events_list,
                ],
                spacing=8,
                expand=True,
            ),
        )

    @staticmethod
    def _card(title: str, body: ft.Control) -> ft.Control:
        return ft.Container(
            padding=16,
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column(
                [ft.Text(title, size=14, weight=ft.FontWeight.BOLD), body],
                spacing=10,
            ),
        )

    @staticmethod
    def _num_field(width: int, label: str, on_change) -> ft.TextField:
        return ft.TextField(
            width=width,
            dense=True,
            label=label,
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=on_change,
        )

    # ------------------------------------------------------------------ 渲染

    def _render_all(self) -> None:
        if self._playing:
            self.stop_preview()
        self._f_strings.value = str(self.doc.string_count)
        self._f_maxfret.value = str(self.doc.max_fret)
        self._f_bpm.value = f"{self.doc.bpm:g}"
        self._render_meta()
        self._render_events()
        self._render_timeline()
        self._refresh_labels()

    def _render_meta(self) -> None:
        self._meta_column.controls = [self._meta_row(entry) for entry in self.doc.meta]

    def _meta_row(self, entry: MetaEntry) -> ft.Control:
        type_field = ft.TextField(
            value=f"{entry.meta_type:04X}",
            width=78,
            dense=True,
            text_size=12,
            hint_text="类型",
            input_filter=ft.InputFilter(allow=True, regex_string=_META_TYPE_FILTER),
            on_change=lambda e, entry=entry: self._on_meta_type_change(entry, e.control.value),
        )
        value_field = ft.TextField(
            value=entry.display_value(),
            expand=True,
            dense=True,
            text_size=12,
            hint_text="文本，或 hex:00FF",
            on_change=lambda e, entry=entry: self._on_meta_value_change(entry, e.control.value),
        )
        return ft.Row(
            [
                type_field,
                value_field,
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=16,
                    tooltip="删除这条元数据",
                    on_click=lambda e, entry=entry: self._on_delete_meta(entry),
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _render_events(self) -> None:
        self._render_notes()
        self._render_other_events()
        self._refresh_warnings()

    def _render_notes(self) -> None:
        entries = self.doc.note_pairs()
        if not entries:
            self._notes_list.controls = [
                ft.Container(
                    padding=16,
                    content=ft.Text(
                        "还没有音符。点上面的「添加音符」加一个（默认 1 拍，"
                        "6 弦空弦），音长可以直接在这一行改。",
                        size=12,
                        color=_MUTED,
                    ),
                )
            ]
            return
        self._notes_list.controls = [
            self._note_row(index, entry) for index, entry in enumerate(entries)
        ]

    def _render_other_events(self) -> None:
        orphans = self.doc.orphan_note_offs()
        others = [ev for ev in self.doc.events if ev.event_type not in NOTE_EVENTS]
        rows = sorted([*others, *orphans], key=lambda ev: ev.tick)
        if not rows:
            self._events_list.controls = [
                ft.Container(
                    padding=12,
                    content=ft.Text("没有其他事件。", size=12, color=_MUTED),
                )
            ]
            return
        self._events_list.controls = [
            self._event_row(index, ev) for index, ev in enumerate(rows)
        ]

    def _refresh_warnings(self) -> None:
        missing = sum(1 for entry in self.doc.note_pairs() if entry.missing_off)
        orphans = len(self.doc.orphan_note_offs())
        lines: list[str] = []
        if missing:
            lines.append(
                f"ℹ {missing} 个音还没有 NoteOff：保存时会按 1/4 拍自动补，"
                "要更长就在「音长」里改。"
            )
        if orphans:
            lines.append(
                f"⚠ {orphans} 个孤立的 NoteOff（找不到 NoteOn）：严格格式不能保存，"
                "请删掉这一行，或把对应的音补成完整音符。"
            )
        self._events_warning.value = "\n".join(lines)
        self._events_warning.visible = bool(lines)
        self._events_warning.color = (
            ft.Colors.RED_400 if orphans else ft.Colors.ORANGE_300
        )

    def _note_row(self, index: int, entry: NoteEntry) -> ft.Control:
        start_field = ft.TextField(
            value=self._format_beats(self.doc.beats_at(entry.start_tick)),
            width=84,
            dense=True,
            text_size=12,
            hint_text="拍",
            tooltip="这个音从第几拍开始",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, entry=entry: self._on_note_start_change(entry, e.control.value),
        )
        duration_field = ft.TextField(
            value=self._format_beats(self.doc.beats_at(entry.duration_ticks)),
            width=90,
            dense=True,
            text_size=12,
            hint_text="音长",
            tooltip=(
                "音长（拍）。保存时按这个长度自动生成 NoteOff。\n"
                "同一个 (voice, 弦, 品) 上后面的音会限制它的最大长度。"
            ),
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, entry=entry: self._on_note_duration_change(
                entry, e.control.value, e.control
            ),
        )
        string_field = ft.TextField(
            value=str(entry.string),
            width=56,
            dense=True,
            text_size=12,
            hint_text="弦",
            tooltip="吉他弦号（1–6）；改这里会同时改 NoteOn 和 NoteOff。",
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=lambda e, entry=entry: self._on_note_field_change(
                entry, "string", e.control.value
            ),
        )
        fret_field = ft.TextField(
            value=str(entry.fret),
            width=56,
            dense=True,
            text_size=12,
            hint_text="品",
            tooltip="品数：0 = 空弦（第 5 页）；1–24 品。",
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=lambda e, entry=entry: self._on_note_field_change(
                entry, "fret", e.control.value
            ),
        )
        voice_field = ft.TextField(
            value=str(entry.voice),
            width=56,
            dense=True,
            text_size=12,
            hint_text="voice",
            tooltip=(
                "事件头里的 1 字节「声部号」，默认 0。\n"
                "只作为 (voice, 弦, 品) 的配对键；改它会同时改 On/Off。"
            ),
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=lambda e, entry=entry: self._on_note_field_change(
                entry, "voice", e.control.value
            ),
        )

        if entry.missing_off:
            marker: ft.Control = ft.Icon(
                ft.Icons.WARNING_AMBER,
                size=14,
                color=ft.Colors.ORANGE_300,
                tooltip="这个音没有 NoteOff；保存时会自动补 1/4 拍",
            )
        else:
            marker = ft.Container(width=14)

        return ft.Row(
            [
                ft.Text(f"N{index + 1:02d}", width=34, size=11, color=_MUTED),
                start_field,
                duration_field,
                string_field,
                fret_field,
                voice_field,
                marker,
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=16,
                    tooltip="删除这个音（NoteOn 和 NoteOff 一起删）",
                    on_click=lambda e, entry=entry: self._on_delete_note(entry),
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _event_row(self, index: int, ev: EventRow) -> ft.Control:
        beat_field = ft.TextField(
            value=self._format_beats(self.doc.beats_at(ev.tick)),
            width=90,
            dense=True,
            text_size=12,
            hint_text="拍",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, ev=ev: self._on_beat_change(ev, e.control.value),
        )
        voice = ft.TextField(
            value=str(ev.voice),
            width=56,
            dense=True,
            text_size=12,
            hint_text="voice",
            tooltip=(
                "事件头里的 1 字节「声部号」，默认 0。\n"
                "格式没有规定它代表什么；本项目只用它给 NoteOn/NoteOff 配对：\n"
                "（voice, 弦, 品）三者都相同才算一对。"
            ),
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=lambda e, ev=ev: self._on_voice_change(ev, e.control.value),
        )
        kind = ft.Dropdown(
            value=ev.label,
            width=124,
            text_size=12,
            options=[ft.DropdownOption(key=label, text=label) for label in EVENT_LABELS.values()],
            on_select=lambda e, ev=ev: self._on_kind_change(ev, e.control.value),
        )

        if ev.event_type not in NOTE_EVENTS:
            number = f"E{index + 1:02d}"
        else:
            # 只可能是孤立的 NoteOff：它不在音符列表里，单独显示并警告
            number = f"!{index + 1:02d}"

        return ft.Row(
            [
                ft.Text(number, width=34, size=11, color=_MUTED),
                beat_field,
                kind,
                voice,
                *self._detail_fields(ev),
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=16,
                    tooltip="删除这个事件",
                    on_click=lambda e, ev=ev: self._on_delete_event(ev),
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _detail_fields(self, ev: EventRow) -> list[ft.Control]:
        if ev.is_note:
            return [
                ft.TextField(
                    value=str(ev.string),
                    width=64,
                    dense=True,
                    text_size=12,
                    hint_text="弦",
                    tooltip=(
                        "吉他弦号（1–6）。\n"
                        "孤立 NoteOff 没有配对的 NoteOn，保存会被拒绝；"
                        "请删掉它或在音符区补一个音。"
                    ),
                    input_filter=ft.NumbersOnlyInputFilter(),
                    on_change=lambda e, ev=ev: self._on_note_change(ev, "string", e.control.value),
                ),
                ft.TextField(
                    value=str(ev.fret),
                    width=64,
                    dense=True,
                    text_size=12,
                    hint_text="品",
                    tooltip=(
                        "品数：0 = 空弦（第 5 页）；1–24 品。\n"
                        "孤立 NoteOff 没有配对的 NoteOn，保存会被拒绝。"
                    ),
                    input_filter=ft.NumbersOnlyInputFilter(),
                    on_change=lambda e, ev=ev: self._on_note_change(ev, "fret", e.control.value),
                ),
                ft.Text("", expand=True),
            ]
        if ev.event_type == EV_TEMPO:
            return [
                ft.TextField(
                    value=str(ev.tempo_us),
                    width=130,
                    dense=True,
                    text_size=12,
                    hint_text="微秒/拍",
                    input_filter=ft.NumbersOnlyInputFilter(),
                    on_change=lambda e, ev=ev: self._on_tempo_change(ev, e.control.value),
                ),
                ft.Text(
                    f"≈ {60_000_000 / max(ev.tempo_us, 1):.1f} BPM",
                    size=11,
                    color=_MUTED,
                    expand=True,
                ),
            ]
        if ev.event_type == 0x06:  # End
            return [ft.Text("结束标记（保存时自动保证在最后）", size=11, color=_MUTED, expand=True)]
        return [
            ft.TextField(
                value=ev.hex_data,
                width=210,
                dense=True,
                text_size=12,
                hint_text="十六进制字节",
                input_filter=ft.InputFilter(allow=True, regex_string=_HEX_FILTER),
                on_change=lambda e, ev=ev: self._on_hex_change(ev, e.control.value),
            ),
            ft.Text("", expand=True),
        ]

    # ------------------------------------------------------------------ 节拍轴

    def _render_timeline(self) -> None:
        lanes = max(1, self.doc.string_count)
        span = max(4.0, self.doc.duration_beats + 1.0)
        width = int(span * PX_PER_BEAT)
        height = RULER_HEIGHT + lanes * LANE_HEIGHT
        self._timeline_width = width

        children: list[ft.Control] = []

        # 泳道底色（隔行）
        for lane in range(lanes):
            children.append(
                ft.Container(
                    left=0,
                    top=RULER_HEIGHT + lane * LANE_HEIGHT,
                    width=width,
                    height=LANE_HEIGHT,
                    bgcolor=(
                        ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE)
                        if lane % 2 == 0
                        else None
                    ),
                )
            )

        # 节拍刻度 + 网格（每小节首拍加重）
        step = 1.0 if span <= 32 else (2.0 if span <= 64 else 4.0)
        mark = 0.0
        while mark <= span + step:
            x = mark * PX_PER_BEAT
            is_bar = abs(mark % BEATS_PER_BAR) < 1e-9
            children.append(
                ft.Container(
                    left=x,
                    top=RULER_HEIGHT,
                    width=1.5 if is_bar else 1,
                    height=lanes * LANE_HEIGHT,
                    bgcolor=ft.Colors.with_opacity(
                        0.18 if is_bar else 0.08, ft.Colors.ON_SURFACE
                    ),
                )
            )
            if mark <= span:
                children.append(
                    ft.Text(
                        left=x + 4,
                        top=5,
                        value=f"{mark:g}",
                        size=9,
                        color=ft.Colors.ON_SURFACE if is_bar else _MUTED,
                    )
                )
            mark += step

        # Tempo 事件：一条竖线标记（只影响速度，不影响位置）
        for ev in self.doc.events:
            if ev.event_type == EV_TEMPO:
                x = self.doc.beats_at(ev.tick) * PX_PER_BEAT
                children.append(
                    ft.Container(
                        left=x,
                        top=RULER_HEIGHT - 2,
                        width=2,
                        height=lanes * LANE_HEIGHT + 2,
                        bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.DEEP_ORANGE),
                    )
                )

        # 音符块：按起止拍画长度，按弦分轨
        blocks = self._note_blocks()
        for start_tick, end_tick, string, fret in blocks:
            start_beat = self.doc.beats_at(start_tick)
            block_width = max(
                8.0, (self.doc.beats_at(end_tick) - start_beat) * PX_PER_BEAT
            )
            lane = max(1, min(string, lanes)) - 1
            children.append(
                ft.Container(
                    left=start_beat * PX_PER_BEAT,
                    top=RULER_HEIGHT + lane * LANE_HEIGHT + 3,
                    width=block_width,
                    height=LANE_HEIGHT - 6,
                    bgcolor=STRING_COLORS[lane % len(STRING_COLORS)],
                    border_radius=4,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text(
                        str(fret),
                        size=9,
                        color=ft.Colors.WHITE,
                        weight=ft.FontWeight.BOLD,
                    ),
                    tooltip=(
                        f"第 {string} 弦 {fret} 品 · 第 {start_beat:.2f} 拍 · "
                        f"长 {self.doc.beats_at(end_tick - start_tick):.2f} 拍"
                    ),
                )
            )

        if not blocks:
            children.append(
                ft.Text(
                    left=8,
                    top=RULER_HEIGHT + 6,
                    value="还没有音符；点「添加音符」就会出现在这里。",
                    size=11,
                    color=_MUTED,
                )
            )

        # 试听游标：放在最上层
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = lanes * LANE_HEIGHT + 2
        self._playhead.left = max(0.0, min(self._playhead.left or 0.0, max(0.0, width - 3)))
        children.append(self._playhead)

        stack = ft.Stack(controls=children, width=width, height=height)

        lanes_labels = ft.Column(
            [ft.Container(height=RULER_HEIGHT)]
            + [
                ft.Container(
                    height=LANE_HEIGHT,
                    width=22,
                    alignment=ft.Alignment(1, 0),
                    content=ft.Text(str(number), size=9, color=_MUTED),
                )
                for number in range(1, lanes + 1)
            ],
            spacing=0,
        )

        self._timeline_scroll = ft.Row([stack], scroll=ft.ScrollMode.ALWAYS, expand=True)
        self._timeline_host.content = ft.Row(
            [
                lanes_labels,
                self._timeline_scroll,
            ],
            spacing=8,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self._timeline_host.height = height + 4

    def _note_blocks(self) -> list[tuple[int, int, int, int]]:
        """音符块：来自模型层的 NoteOn / NoteOff 配对结果。"""
        return self.doc.note_blocks()

    @staticmethod
    def _format_beats(beats: float) -> str:
        return f"{max(0.0, float(beats)):.2f}"

    def _refresh_labels(self) -> None:
        name = self.path.name if self.path else "未命名"
        prefix = "● " if self.dirty else ""
        self._file_label.value = f"{prefix}{name}"
        notes = len(self.doc.note_pairs())
        others = len([ev for ev in self.doc.events if ev.event_type not in NOTE_EVENTS])
        self._stats_label.value = (
            f"{notes} 音符 · {others} 事件 · {self.doc.duration_beats:.2f} 拍 · "
            f"{self.doc.bpm:.0f} BPM"
        )

    # ------------------------------------------------------------------ 试听

    def _on_preview(self, _event=None) -> None:
        """试听当前文档（按 BPM / Tempo 合成后播放）。"""
        if self._playing:
            self.stop_preview()
            return
        if not self.doc.note_pairs():
            self._snack("还没有音符，先添加音符再试听")
            return

        snapshot = copy.deepcopy(self.doc)
        self._playing = True
        self._preview_token += 1
        token = self._preview_token
        stop_flag = threading.Event()
        self._preview_stop = stop_flag
        # 合成线程只写这几个状态；界面由 _preview_ticker 在页面事件循环里刷新。
        # 后台线程里直接 page.update() 不会重绘（实测），所以 UI 不能放在线程里。
        self._audio_started_at = None
        self._audio_duration = 0.0
        self._audio_error = None
        self._follow_offset = 0.0
        self._preview_label.value = "正在合成…"
        self._preview_label.color = _MUTED
        self._update()

        if self.page is None:
            # 离线构建/测试：只合成，不播放
            try:
                blob = audio_preview.render_wav(snapshot)
                self._preview_label.value = (
                    f"已合成 {audio_preview.wav_duration(blob):.2f}s（离线，不播放）"
                )
            except Exception as exc:  # noqa: BLE001 - 合成失败不该让页面崩
                self._preview_label.value = f"合成失败：{exc}"
            finally:
                self._playing = False
                self._update()
            return

        try:
            self.page.run_thread(self._preview_worker, snapshot, token, stop_flag)
            self.page.run_task(self._preview_ticker, snapshot, token, stop_flag)
        except Exception as exc:  # noqa: BLE001 - 线程/任务起不来时也要恢复状态
            self.stop_preview()
            self._snack(f"试听失败：{exc}", error=True)

    def _on_stop_preview(self, _event=None) -> None:
        self.stop_preview()

    def stop_preview(self) -> None:
        """停止试听、清空播放队列、隐藏游标（切页/载入新谱时也要调）。"""
        self._preview_token += 1
        stop_flag, self._preview_stop = self._preview_stop, None
        if stop_flag is not None:
            stop_flag.set()
        self._playing = False
        self._audio_started_at = None
        self._audio_duration = 0.0
        self._audio_error = None
        audio_preview.stop_playback()
        self._playhead.visible = False
        if getattr(self, "_preview_label", None) is not None:
            if self._preview_label.value.startswith(("正在", "试听", "已合成")):
                self._preview_label.value = ""
        self._update()

    def _preview_worker(
        self,
        snapshot: GtabDocument,
        token: int,
        stop_flag: threading.Event,
    ) -> None:
        """后台线程：只负责合成 + 启动播放，不碰界面。"""
        try:
            blob = audio_preview.render_wav(snapshot)
            if stop_flag.is_set() or token != self._preview_token:
                return
            duration = audio_preview.wav_duration(blob)
            audio_preview.play_wav(blob)
            if stop_flag.is_set() or token != self._preview_token:
                # 刚好在开始播放前被停止：把已经响起来的这段收掉
                audio_preview.stop_playback()
                return
            self._audio_duration = duration
            self._audio_started_at = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - 失败状态交给 ticker 显示
            if token == self._preview_token:
                self._audio_error = str(exc)

    async def _preview_ticker(
        self,
        snapshot: GtabDocument,
        token: int,
        stop_flag: threading.Event,
    ) -> None:
        """在页面事件循环里推进游标。后台线程的 page.update() 不会重绘。"""
        try:
            # 等合成线程把 WAV 播起来（或报错）
            while token == self._preview_token and not stop_flag.is_set():
                if self._audio_error is not None:
                    self._preview_label.value = f"播放失败：{self._audio_error}"
                    self._preview_label.color = ft.Colors.RED_400
                    self._update()
                    return
                if self._audio_started_at is not None:
                    break
                await asyncio.sleep(0.03)

            if token != self._preview_token or stop_flag.is_set():
                return

            # 从头开始播时把时间轴滚回左边
            await self._scroll_timeline(0.0, 0)
            self._follow_offset = 0.0

            started = self._audio_started_at or time.monotonic()
            duration = self._audio_duration
            while token == self._preview_token and not stop_flag.is_set():
                elapsed = time.monotonic() - started
                if elapsed >= duration:
                    break
                self._set_playhead_seconds(
                    max(0.0, elapsed - audio_preview.LEAD_IN_SECONDS), snapshot
                )
                await self._follow_playhead()
                self._preview_label.value = f"试听中 {elapsed:.1f}s / {duration:.1f}s"
                self._preview_label.color = ft.Colors.PRIMARY
                self._update()
                await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001 - 播放失败只提示，不崩页面
            if token == self._preview_token:
                self._preview_label.value = f"播放失败：{exc}"
                self._preview_label.color = ft.Colors.RED_400
                self._update()
        finally:
            if token == self._preview_token:
                audio_preview.stop_playback()
                self._playing = False
                self._playhead.visible = False
                self._audio_started_at = None
                self._audio_duration = 0.0
                self._audio_error = None
                if self._preview_label.value.startswith(("正在", "试听")):
                    self._preview_label.value = ""
                self._preview_label.color = _MUTED
                self._update()

    async def _scroll_timeline(self, offset: float, duration: int) -> None:
        """横向滚动时间轴；失败/超时只忽略，不影响播放。"""
        if self._timeline_scroll is None:
            return
        try:
            await asyncio.wait_for(
                self._timeline_scroll.scroll_to(offset=offset, duration=duration),
                timeout=1.0,
            )
        except Exception:  # noqa: BLE001 - 滚动失败不该中断试听
            pass

    async def _follow_playhead(self) -> None:
        """长谱播放时把横向滚动带到游标附近，避免红线走出可视区。"""
        left = float(self._playhead.left or 0.0)
        if left <= self._follow_offset + 420:
            return
        target = max(0.0, left - 240)
        await self._scroll_timeline(target, 120)
        self._follow_offset = target

    def _set_playhead_seconds(self, score_seconds: float, doc: GtabDocument) -> None:
        """把试听游标放到 ``score_seconds`` 对应的拍位置。"""
        tick = seconds_to_tick(doc, score_seconds)
        beat = doc.beats_at(tick)
        left = beat * PX_PER_BEAT
        width = self._timeline_width or 0.0
        self._playhead.left = max(0.0, min(left, max(0.0, width - 3)))
        self._playhead.visible = True
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = max(1, self.doc.string_count) * LANE_HEIGHT + 2

    # ------------------------------------------------------------------ 数据同步

    def _on_header_change(self, _event=None) -> None:
        self.doc.string_count = self._read_int(self._f_strings, self.doc.string_count)
        self.doc.max_fret = self._read_int(self._f_maxfret, self.doc.max_fret)
        self._render_timeline()
        self._touch()

    def _on_bpm_change(self, _event=None) -> None:
        try:
            bpm = float(self._f_bpm.value)
        except (TypeError, ValueError):
            return
        if bpm <= 0:
            return
        self.doc.bpm = bpm
        # 节拍位置不受 BPM 影响，只更新统计信息（试听速度由 BPM 决定）
        self._touch()

    def _on_beat_change(self, ev: EventRow, text: str) -> None:
        beats = _parse_beats(text)
        if beats is None:
            return
        ev.tick = self.doc.tick_at(beats)
        self._render_timeline()
        self._touch()

    def _on_note_start_change(self, entry: NoteEntry, text: str) -> None:
        beats = _parse_beats(text)
        if beats is None:
            return
        self.doc.move_note(entry, self.doc.tick_at(beats))
        self._render_timeline()
        self._touch(refresh_stats=True)

    def _on_note_duration_change(self, entry: NoteEntry, text: str, field=None) -> None:
        beats = _parse_beats(text)
        if beats is None or beats <= 0:
            return
        requested = self.doc.tick_at(beats)
        applied = self.doc.set_note_duration(entry, requested)
        if field is not None and applied != requested:
            # 只在被同键下一个音夹短时才回写，避免正常输入 "2.5" 时被改成 "2.00" 后没法继续输入
            field.value = self._format_beats(self.doc.beats_at(applied))
            field.tooltip = (
                "音长被夹到同一个 (voice, 弦, 品) 的下一个音之前：\n"
                "同一个键不能同时按住两次。"
            )
        self._render_timeline()
        self._touch(refresh_stats=True)

    def _on_note_field_change(self, entry: NoteEntry, field: str, text: str) -> None:
        if field == "string":
            value = self._to_int(text, entry.string)
            self.doc.set_note_fields(entry, string=value)
        elif field == "fret":
            value = self._to_int(text, entry.fret)
            self.doc.set_note_fields(entry, fret=value)
        else:
            value = self._to_int(text, entry.voice)
            self.doc.set_note_fields(entry, voice=value)
        self._render_timeline()
        self._touch(refresh_stats=True)

    def _on_delete_note(self, entry: NoteEntry) -> None:
        self.doc.delete_note(entry)
        self._rerender_events()
        self._touch(refresh_stats=True)

    def _on_voice_change(self, ev: EventRow, text: str) -> None:
        ev.voice = min(255, self._to_int(text, ev.voice))
        self._touch(refresh_stats=True)

    def _on_note_change(self, ev: EventRow, field: str, text: str) -> None:
        """孤立 NoteOn/Off 行里的弦/品编辑（正常音符走 _on_note_field_change）。"""
        current = ev.string if field == "string" else ev.fret
        value = self._to_int(text, current)
        if field == "string":
            ev.set_note(value, ev.fret)
        else:
            ev.set_note(ev.string, value)
        self._touch(refresh_stats=True)

    def _on_tempo_change(self, ev: EventRow, text: str) -> None:
        us = self._to_int(text, ev.tempo_us)
        if us <= 0:
            return
        ev.data = us.to_bytes(4, "little")
        self._touch(refresh_stats=True)

    def _on_hex_change(self, ev: EventRow, text: str) -> None:
        try:
            ev.data = bytes.fromhex(text.replace(" ", ""))
        except ValueError:
            return
        self._touch(refresh_stats=True)

    def _on_kind_change(self, ev: EventRow, label: str) -> None:
        new_type = TYPE_BY_LABEL.get(label)
        if new_type is None or new_type == ev.event_type:
            return
        fresh = default_event(new_type, ev.tick)
        ev.event_type = fresh.event_type
        ev.data = fresh.data
        self._rerender_events()

    def _on_meta_type_change(self, entry: MetaEntry, text: str) -> None:
        try:
            entry.meta_type = int(text or "0", 16)
        except ValueError:
            return
        self._touch()

    def _on_meta_value_change(self, entry: MetaEntry, text: str) -> None:
        try:
            entry.data = MetaEntry.parse_value(text)
        except GtabError:
            return
        self._touch()

    def _on_add_meta(self, _event=None) -> None:
        self.doc.meta.append(MetaEntry(0, b""))
        self._render_meta()
        self._touch(refresh_stats=True)

    def _on_delete_meta(self, entry: MetaEntry) -> None:
        for index, item in enumerate(self.doc.meta):
            if item is entry:
                del self.doc.meta[index]
                break
        self._render_meta()
        self._touch(refresh_stats=True)

    def _next_note_tick(self) -> int:
        """新音符的落点：最后一个音结束后的下一个整拍。"""
        entries = self.doc.note_pairs()
        if not entries:
            return 0
        last_end = max(entry.end_tick for entry in entries)
        return self.doc.tick_at(math.ceil(self.doc.beats_at(last_end)))

    def _on_add_note(self, _event=None) -> None:
        tick = self._next_note_tick()
        duration = self.doc.tick_at(DEFAULT_NOTE_DURATION_BEATS)
        self.doc.add_note(
            tick,
            self.doc.string_count,
            0,
            duration_ticks=duration,
        )
        self._rerender_events()
        self._touch(refresh_stats=True)

    def _on_add_event(self, _event=None) -> None:
        gap = max(1, self.doc.time_base_value)
        last_tick = max(
            (ev.tick for ev in self.doc.events if ev.event_type != 0x06),
            default=-gap,
        )
        self.doc.events.append(default_event(EV_TEMPO, last_tick + gap))
        self._rerender_events()
        self._touch(refresh_stats=True)

    def _on_delete_event(self, ev: EventRow) -> None:
        for index, item in enumerate(self.doc.events):
            if item is ev:
                del self.doc.events[index]
                break
        self._rerender_events()

    def _rerender_events(self) -> None:
        self._render_events()
        self._render_timeline()
        self._touch()

    # ------------------------------------------------------------------ 文件操作

    def load_document(
        self, doc: GtabDocument, path: Path | str | None = None
    ) -> None:
        """外部把一份文档送进本页（MIDI→GTAB 页的「送到 GTAB 编辑页」）。

        只换内存文档，不写盘；``path=None`` 表示还没有磁盘文件，保存时走「另存为」。
        """
        self.doc = doc
        self.path = Path(path) if path is not None else None
        self._render_all()
        self._set_dirty(self.path is None)

    async def _on_open(self, _event=None) -> None:
        picker = self._ensure_picker()
        try:
            files = await picker.pick_files(
                dialog_title="打开 gtab 文件",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gtab"],
                allow_multiple=False,
            )
        except Exception as exc:  # 用户取消通常不会抛，这里兜底平台差异
            self._snack(f"无法打开文件选择器：{exc}", error=True)
            return
        if not files:
            return

        chosen = files[0]
        try:
            if chosen.path:
                blob = Path(chosen.path).read_bytes()
                target: Path | None = Path(chosen.path)
            elif chosen.bytes is not None:
                raw = chosen.bytes
                blob = raw.getvalue() if hasattr(raw, "getvalue") else bytes(raw)
                target = None
            else:
                raise GtabError("拿不到所选文件的内容")
            doc = GtabDocument.from_bytes(blob)
        except (OSError, GtabError) as exc:
            self._snack(f"打开失败：{exc}", error=True)
            return

        self.doc = doc
        self.path = target
        self._render_all()
        self._set_dirty(False)
        self._snack(f"已载入 {chosen.name}：{len(doc.events)} 个事件")

    async def _on_save(self, _event=None) -> None:
        if self.path is None:
            await self._on_save_as()
            return
        try:
            # 本页已没有「事件 CRC」开关：按设置页的全局开关在写盘前强制一次
            force_event_crc(self.doc)
            blob = self.doc.to_bytes()
            # to_bytes 会自动补 NoteOff，立刻刷新音符区（警告图标/音长）
            self._render_events()
            self.path.write_bytes(blob)
        except (OSError, GtabError) as exc:
            self._snack(f"保存失败：{exc}", error=True)
            return
        self._set_dirty(False)
        self._snack(f"已保存 {self.path.name}（{len(blob)} 字节）")

    async def _on_save_as(self, _event=None) -> None:
        try:
            force_event_crc(self.doc)
            blob = self.doc.to_bytes()
            # 即使随后取消另存为，模型里也已经补好了 NoteOff，界面要跟着更新
            self._render_events()
        except GtabError as exc:
            self._snack(f"保存失败：{exc}", error=True)
            return

        picker = self._ensure_picker()
        try:
            target = await picker.save_file(
                dialog_title="另存为 gtab",
                file_name=self.path.name if self.path else "untitled.gtab",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gtab"],
                src_bytes=blob,
            )
        except Exception as exc:
            self._snack(f"无法打开保存对话框：{exc}", error=True)
            return
        if not target:
            return

        self.path = Path(target)
        self._set_dirty(False)
        self._snack(f"已保存到 {self.path.name}（{len(blob)} 字节）")

    async def _on_new(self, _event=None) -> None:
        self.doc = GtabDocument()
        self.path = None
        self._render_all()
        self._set_dirty(False)
        self._snack("已新建空白文档")

    async def _on_load_demo(self, _event=None) -> None:
        if not self.DEMO_PATH.exists():
            self._snack("找不到示例文件 MusicList/gtabfile/demo.gtab", error=True)
            return
        try:
            doc = GtabDocument.from_path(self.DEMO_PATH)
        except (OSError, GtabError) as exc:
            self._snack(f"示例载入失败：{exc}", error=True)
            return
        self.doc = doc
        self.path = self.DEMO_PATH
        self._render_all()
        self._set_dirty(False)
        self._snack(f"已载入示例：{len(doc.events)} 个事件")

    # ------------------------------------------------------------------ 辅助

    def _ensure_picker(self) -> ft.FilePicker:
        if self._picker is None:
            self._picker = ft.FilePicker()
        return self._picker

    def _snack(self, message: str, error: bool = False) -> None:
        # page 为 None 只出现在离线构建/测试时（和 GctPage / MidiPage 一致）
        if self.page is None:
            return
        self.page.show_dialog(
            ft.SnackBar(
                content=ft.Text(message),
                bgcolor=ft.Colors.RED_400 if error else None,
                show_close_icon=True,
                behavior=ft.SnackBarBehavior.FLOATING,
            )
        )

    def _touch(self, refresh_stats: bool = False) -> None:
        self.dirty = True
        self._refresh_labels()
        self._update()

    def _set_dirty(self, value: bool) -> None:
        self.dirty = value
        self._refresh_labels()
        self._update()

    def _update(self) -> None:
        # page 为 None 只出现在离线构建/测试时
        if self.page is not None:
            self.page.update()

    @staticmethod
    def _read_int(field: ft.TextField, fallback: int) -> int:
        return GtabPage._to_int(field.value, fallback)

    @staticmethod
    def _to_int(text: str | None, fallback: int) -> int:
        try:
            return int(str(text).strip())
        except (TypeError, ValueError):
            return fallback
