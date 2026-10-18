"""GCT 编辑页：读 / 改 / 写 ``.gct`` 和弦谱。

页面结构（和 GTAB 页一致：左侧参数 + 右侧事件列表）：

- **头部参数**：弦数、BPM（写进 ``default_tempo_us``）、事件 CRC 开关；
- **和弦表**：``chord.json`` 里的 24 个名字按「页 × 方向」排开，后面跟
  6 弦指法（``-1`` 显示成 ``x``）。指法只用于显示/核对，回放位置仍由名字顺序决定；
- **和弦事件**：一条 = 一个 ``Strum``（扫弦，可下扫/上扫）/ ``Pluck``（单弦拨奏）/
  ``Set``（只切指位）；位置统一用拍，保存时按 tick 排序；
- **其他事件**：``Tempo``（拍 + BPM）；``End`` 只读，保存时自动补在最后。
- **试听**：按 BPM / Tempo 把整段和弦谱合成成 WAV 播放，音高取 ``chord.json`` 的
  ``CHORDS_MAPPINGS``（扫弦六根弦依次响、上扫反着来、单弦拨奏只发一根）；节拍轴上跟着走
  一条红色游标。合成在后台线程，界面刷新必须走页面事件循环的 async ticker
  （后台线程里 ``page.update()`` 不重绘）。

``chord.json`` 是运行时读取的：工具栏「重载和弦表」或切回本页都会重读，
所以改完 json 不用重启程序（缓存见 ``ui.chords.reload_chord_table``）。
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from pathlib import Path

import flet as ft

from gct import (
    DIR_DOWN,
    DIR_UP,
    EV_CHORD_PLUCK,
    EV_CHORD_SET,
    EV_CHORD_STRUM,
    EV_END,
    EV_TEMPO,
    GctError,
)
from ui import audio_preview, chord_preview
from ui.chords import (
    ChordError,
    ChordTable,
    load_chord_table,
    position_of,
    reload_chord_table,
)
from ui.components import BEATS_PER_BAR, LANE_HEIGHT, MUTED, PX_PER_BEAT, RULER_HEIGHT, card
from ui.gct_doc import ChordRow, GctDocument, demo_document, seconds_to_tick

__all__ = ["GctPage"]

_BEAT_FILTER = r"^\d*\.?\d{0,3}$"

# 事件类型在界面上的中文名（EVENT_LABELS 里是文件里的英文名，tooltip 里给出）
_TYPE_LABELS: dict[int, str] = {
    EV_CHORD_STRUM: "扫弦",
    EV_CHORD_PLUCK: "拨弦",
    EV_CHORD_SET: "只换指位",
}
_TYPE_BY_LABEL = {label: kind for kind, label in _TYPE_LABELS.items()}

_DIRECTION_LABELS: dict[int, str] = {DIR_DOWN: "下扫", DIR_UP: "上扫"}
_DIRECTION_BY_LABEL = {label: value for value, label in _DIRECTION_LABELS.items()}

# 页内六个方向（0 = 正上方，顺时针），和手柄摇杆方向一致。
# 用中文而不是 ↗↙ 这类箭头：Flet 的默认字体缺 ↙ 字形，会被替换成 ✓ 之类的怪字符。
_ARROWS = ("上", "右上", "右下", "下", "左下", "左上")

# 指法文本宽度不够时截断，完整内容放 tooltip
_FINGER_TIP = "按 1 弦 → 6 弦排列；x = 这根弦不发声（json 里写 -1）"

# 节拍轴：三条泳道按事件类型分，同一拍上的扫弦 / 拨弦 / 指位就不会互相压住。
# 刻度常数和 GTAB 页共用（ui/components.py），两页看起来才对得上。
_LANES: tuple[tuple[int, str], ...] = (
    (EV_CHORD_STRUM, "扫弦"),
    (EV_CHORD_PLUCK, "拨弦"),
    (EV_CHORD_SET, "指位"),
)
_LANE_BY_TYPE = {event_type: index for index, (event_type, _) in enumerate(_LANES)}
_LANES_HEIGHT = len(_LANES) * LANE_HEIGHT
_TIMELINE_HEIGHT = RULER_HEIGHT + _LANES_HEIGHT


def _parse_beats(text: str | None) -> float | None:
    """``1`` / ``2.5`` → 拍数；解析不了返回 None（和 gtab 页规则一致）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_beats(beats: float) -> str:
    return f"{max(0.0, float(beats)):.2f}"


def _block_width(label: str) -> float:
    """节拍轴上事件块的宽度：装得下和弦名，但不盖住下一拍。"""
    return max(38.0, min(PX_PER_BEAT - 6, 16 + 7.5 * len(label)))


class GctPage:
    """GCT 和弦谱编辑器页面。"""

    TITLE = "GCT 编辑"
    SUBTITLE = "读取 / 编辑 / 写回 .gct 和弦谱（和弦名决定手柄位置）"
    DEMO_PATH = Path(__file__).resolve().parents[2] / "MusicList" / "gctfile" / "demo.gct"

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.doc = GctDocument()
        self.path: Path | None = None
        self.dirty = False
        self.chord_error: str = ""
        self.chord_table = ChordTable((), {})
        self._timeline_width = 0
        # 试听状态（和 GTAB 页同一套：合成在线程里，界面由 ticker 在事件循环里刷）
        self._playing = False
        self._preview_token = 0
        self._preview_stop: threading.Event | None = None
        self._audio_started_at: float | None = None
        self._audio_duration = 0.0
        self._audio_error: str | None = None
        self._follow_offset = 0.0
        self._timeline_scroll: ft.Row | None = None
        self._read_chord_table()
        # FilePicker 在页面上下文里创建，Flet 会自动注册到当前 page
        self._picker: ft.FilePicker | None = ft.FilePicker() if page is not None else None
        self._root = self._build()
        self._render_all()

    # ------------------------------------------------------------------ 组合

    def build(self) -> ft.Control:
        return self._root

    def _build(self) -> ft.Control:
        self._file_label = ft.Text("未命名", size=12, color=MUTED)
        self._stats_label = ft.Text("", size=12, color=MUTED)
        self._warning = ft.Text("", size=11, color=ft.Colors.ORANGE_300, visible=False)
        self._preview_label = ft.Text("", size=11, color=MUTED)

        # 试听游标（在 _render_timeline 里塞进 Stack）
        self._playhead = ft.Container(
            left=0,
            top=RULER_HEIGHT - 2,
            width=3,
            height=_LANES_HEIGHT,
            bgcolor=ft.Colors.RED_400,
            visible=False,
            tooltip="试听播放位置",
        )

        self._f_strings = ft.TextField(
            width=84,
            dense=True,
            label="弦数",
            hint_text="6",
            tooltip="gct 头里的弦数；和弦表固定按 1–6 弦显示。",
            input_filter=ft.NumbersOnlyInputFilter(),
            on_change=self._on_strings_change,
        )
        self._f_bpm = ft.TextField(
            width=96,
            dense=True,
            label="BPM",
            hint_text="120",
            tooltip="默认速度（写进 default_tempo_us）；后面的 Tempo 事件可以再变速。",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=self._on_bpm_change,
        )
        self._sw_crc = ft.Switch(
            label="事件 CRC",
            value=self.doc.flags & 0x01 == 0x01,
            tooltip="打开后每个事件后面多 4 字节 CRC32；读取方必须支持这个 flag。",
            on_change=self._on_crc_change,
        )

        self._chord_table_column = ft.Column(spacing=2)
        self._chord_table_warning = ft.Text("", size=11, color=ft.Colors.ORANGE_300, visible=False)

        self._events_list = ft.ListView(expand=True, spacing=6, padding=4)
        # Tempo 通常只有一两条，给固定高度，End 和提示才不会被挤出去
        self._other_list = ft.ListView(height=112, spacing=6, padding=4)

        body = ft.Row(
            [self._left_panel(), self._events_panel()],
            expand=True,
            spacing=16,
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
                                    ft.Text(self.SUBTITLE, size=12, color=MUTED),
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
                ft.TextButton(
                    "重载和弦表",
                    icon=ft.Icons.REFRESH,
                    tooltip="改完 src/chord.json 点这里重读（切回本页也会自动重读）",
                    on_click=self._on_reload_chords,
                ),
                ft.Container(expand=True),
                self._stats_label,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _left_panel(self) -> ft.Control:
        header_card = card(
            "头部参数",
            ft.Column(
                [
                    ft.Row(
                        [self._f_strings, self._f_bpm, self._sw_crc],
                        wrap=True,
                        spacing=8,
                        run_spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "事件位置统一用拍（默认 480 tick/拍，界面不暴露）；"
                        "BPM 只影响回放速度，不改事件位置。",
                        size=11,
                        color=MUTED,
                    ),
                ],
                spacing=8,
            ),
        )

        chord_card = card(
            "和弦表（chord.json）",
            ft.Column(
                [
                    ft.Text(
                        "名字顺序 = 手柄位置（第 1 个 = 第 1 页正上方，顺时针；"
                        f"4 页 × 6 方向）。指法按 1 弦 → 6 弦，x = 不发声。",
                        size=11,
                        color=MUTED,
                    ),
                    self._chord_table_column,
                    self._chord_table_warning,
                ],
                spacing=6,
            ),
        )

        return ft.Column(
            [header_card, chord_card],
            width=372,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _events_panel(self) -> ft.Control:
        self._timeline_host = ft.Container(
            height=_TIMELINE_HEIGHT + 4,
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.ON_SURFACE),
            content=ft.Row(),
        )

        chord_header = ft.Row(
            [
                ft.Text("和弦事件", size=14, weight=ft.FontWeight.BOLD),
                ft.Text("一条 = 一个扫弦 / 拨弦 / 换指位；保存时按拍排序", size=11, color=MUTED),
                ft.Container(expand=True),
                ft.Button(
                    "试听",
                    icon=ft.Icons.PLAY_ARROW,
                    tooltip=(
                        "按 BPM / Tempo 合成后播放：扫弦的六根弦依次响"
                        "（下扫 6→1、上扫 1→6），音高取自 chord.json 的指法；"
                        "没有指法的和弦会跳过"
                    ),
                    on_click=self._on_preview,
                ),
                ft.TextButton(
                    "停止",
                    icon=ft.Icons.STOP,
                    on_click=self._on_stop_preview,
                ),
                ft.TextButton(
                    "添加和弦",
                    icon=ft.Icons.ADD,
                    tooltip="在最后一个事件之后加 1 拍的扫弦（默认和弦表第一个）",
                    on_click=self._on_add_chord,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        chord_heading = ft.Row(
            [
                ft.Text("#", width=34, size=11, color=MUTED),
                ft.Text("拍", width=78, size=11, color=MUTED),
                ft.Text("类型", width=112, size=11, color=MUTED),
                ft.Text("和弦", width=150, size=11, color=MUTED),
                ft.Text("方向 / 弦", width=96, size=11, color=MUTED),
                ft.Text("指法", width=104, size=11, color=MUTED, tooltip=_FINGER_TIP),
                ft.Text("", width=36, size=11, color=MUTED),
            ],
            spacing=6,
        )

        other_header = ft.Row(
            [
                ft.Text("Tempo / End", size=14, weight=ft.FontWeight.BOLD),
                ft.Text("Tempo 只改速度，不改事件位置", size=11, color=MUTED),
                ft.Container(expand=True),
                ft.TextButton(
                    "添加 Tempo",
                    icon=ft.Icons.ADD,
                    tooltip="在最后一个事件之后加一条 120 BPM 的变速事件",
                    on_click=self._on_add_tempo,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        other_heading = ft.Row(
            [
                ft.Text("#", width=34, size=11, color=MUTED),
                ft.Text("拍", width=90, size=11, color=MUTED),
                ft.Text("类型", width=112, size=11, color=MUTED),
                ft.Text("BPM", width=72, size=11, color=MUTED),
                ft.Text("说明", size=11, color=MUTED),
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
                    chord_header,
                    ft.Row(
                        [
                            ft.Text("节拍轴", size=14, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "同拍不同类型各占一条泳道；Tempo 橙色竖线只改速度",
                                size=11,
                                color=MUTED,
                            ),
                            ft.Container(expand=True),
                            self._preview_label,
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._timeline_host,
                    chord_heading,
                    ft.Divider(height=1),
                    self._events_list,
                    self._warning,
                    other_header,
                    other_heading,
                    ft.Divider(height=1),
                    self._other_list,
                ],
                spacing=8,
                expand=True,
            ),
        )

    # ------------------------------------------------------------------ 渲染

    def _render_all(self) -> None:
        if self._playing:
            self.stop_preview()
        self._render_header_fields()
        self._render_chord_table()
        self._render_events()
        self._refresh_warnings()
        self._refresh_labels()

    def _render_header_fields(self) -> None:
        self._f_strings.value = str(self.doc.string_count)
        self._f_bpm.value = f"{self.doc.bpm:.1f}".rstrip("0").rstrip(".")
        self._sw_crc.value = bool(self.doc.flags & 0x01)

    def _render_chord_table(self) -> None:
        names = self.chord_table.names
        controls: list[ft.Control] = []
        for page in range(1, 5):
            controls.append(
                ft.Text(f"第 {page} 页", size=11, weight=ft.FontWeight.BOLD, color=MUTED)
            )
            # 一页 6 个位：2 列 × 3 行，24 个和弦才不用滚动就能看全
            for line in range(3):
                controls.append(
                    ft.Row(
                        [
                            self._chord_cell(page, line, column, names)
                            for column in range(2)
                        ],
                        spacing=8,
                    )
                )
        self._chord_table_column.controls = controls

    def _chord_cell(
        self, page: int, line: int, column: int, names: tuple[str, ...]
    ) -> ft.Control:
        index = line + column * 3
        slot = (page - 1) * 6 + index
        name = names[slot] if slot < len(names) else ""
        if not name:
            return ft.Row(
                [
                    ft.Text(_ARROWS[index], width=26, size=10, color=MUTED),
                    ft.Text("—", width=52, size=12, color=MUTED),
                    ft.Text("空位", size=11, color=MUTED),
                ],
                width=156,
                spacing=4,
            )
        fingering = self.chord_table.fingering(name)
        text = (
            "未定义"
            if fingering is None
            else " ".join("x" if fret == -1 else str(fret) for fret in fingering)
        )
        return ft.Row(
            [
                ft.Text(_ARROWS[index], width=26, size=10, color=MUTED),
                ft.Text(name, width=52, size=12, tooltip=f"第 {page} 页 · 第 {index + 1} 个方向"),
                ft.Text(
                    text,
                    size=11,
                    color=ft.Colors.ORANGE_300 if fingering is None else MUTED,
                    tooltip=_FINGER_TIP,
                ),
            ],
            width=156,
            spacing=4,
        )

    def _render_events(self) -> None:
        rows = sorted(
            (ev for ev in self.doc.events if ev.event_type != EV_END),
            key=lambda ev: ev.tick,
        )
        chord_rows = [ev for ev in rows if ev.is_chord_event]
        other_rows = [ev for ev in rows if not ev.is_chord_event]

        if chord_rows:
            self._events_list.controls = [
                self._chord_row(index, ev) for index, ev in enumerate(chord_rows)
            ]
        else:
            self._events_list.controls = [
                ft.Container(
                    padding=16,
                    content=ft.Text(
                        "还没有和弦事件。点「添加和弦」加一个（默认 1 拍的扫弦），"
                        "类型可以在行内切换。",
                        size=12,
                        color=MUTED,
                    ),
                )
            ]

        self._other_list.controls = [
            *(self._tempo_row(index, ev) for index, ev in enumerate(other_rows)),
            self._end_row(),
        ]
        self._render_timeline()

    # ------------------------------------------------------------------ 节拍轴

    def _render_timeline(self) -> None:
        """把和弦事件按拍画成时间轴。

        只重建 ``_timeline_host`` 里的 Stack，**不碰下面的事件行**，所以改拍、
        换方向时不会抢输入框的焦点（和 GTAB 页同一套做法）。
        """
        span = max(4.0, self.doc.duration_beats + 1.0)
        width = int(span * PX_PER_BEAT)
        self._timeline_width = width

        children: list[ft.Control] = []

        # 泳道底色（隔行）
        for lane in range(len(_LANES)):
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

        # 节拍刻度 + 网格（每小节首拍加重，和 GTAB 页一致）
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
                    height=_LANES_HEIGHT,
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
                        color=ft.Colors.ON_SURFACE if is_bar else MUTED,
                    )
                )
            mark += step

        # Tempo：一条橙色竖线贯穿三条泳道（只标位置，位置不受它影响）
        for beat, bpm in self._tempo_marks():
            children.append(
                ft.Container(
                    left=beat * PX_PER_BEAT,
                    top=RULER_HEIGHT - 2,
                    width=2,
                    height=_LANES_HEIGHT + 2,
                    bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.DEEP_ORANGE),
                    tooltip=f"Tempo {bpm:.1f} BPM · 第 {beat:.2f} 拍",
                )
            )

        # 事件块
        for beat, lane, label, bgcolor, color, tip in self._event_blocks():
            children.append(
                ft.Container(
                    left=beat * PX_PER_BEAT,
                    top=RULER_HEIGHT + lane * LANE_HEIGHT + 3,
                    width=_block_width(label),
                    height=LANE_HEIGHT - 6,
                    bgcolor=bgcolor,
                    border_radius=4,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text(
                        label,
                        size=9,
                        color=color,
                        weight=ft.FontWeight.BOLD,
                    ),
                    tooltip=tip,
                )
            )

        if not self.doc.chord_events():
            children.append(
                ft.Text(
                    left=8,
                    top=RULER_HEIGHT + 6,
                    value="还没有和弦事件；点「添加和弦」就会出现在这里。",
                    size=11,
                    color=MUTED,
                )
            )

        stack = ft.Stack(controls=children, width=width, height=_TIMELINE_HEIGHT)

        # 试听游标：夹在当前宽度内再塞进 Stack（否则重画后可能停在宽度外）
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = _LANES_HEIGHT + 2
        self._playhead.left = max(
            0.0, min(float(self._playhead.left or 0.0), max(0.0, width - 3))
        )
        stack.controls.append(self._playhead)

        lane_labels = ft.Column(
            [ft.Container(height=RULER_HEIGHT)]
            + [
                ft.Container(
                    height=LANE_HEIGHT,
                    width=30,
                    alignment=ft.Alignment(1, 0),
                    content=ft.Text(name, size=9, color=MUTED),
                )
                for _, name in _LANES
            ],
            spacing=0,
        )
        self._timeline_scroll = ft.Row([stack], scroll=ft.ScrollMode.ALWAYS, expand=True)
        self._timeline_host.content = ft.Row(
            [
                lane_labels,
                self._timeline_scroll,
            ],
            spacing=8,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self._timeline_host.height = _TIMELINE_HEIGHT + 4

    def _tempo_marks(self) -> list[tuple[float, float]]:
        """节拍轴上的 Tempo 竖线：``(拍, BPM)``（0 或坏数据跳过）。"""
        marks: list[tuple[float, float]] = []
        for ev in self.doc.events:
            if ev.event_type != EV_TEMPO or ev.tempo_us <= 0:
                continue
            marks.append((self.doc.beats_at(ev.tick), 60_000_000 / ev.tempo_us))
        return marks

    def _event_blocks(self) -> list[tuple[float, int, str, object, object, str]]:
        """节拍轴上的块：``(拍, 泳道, 文字, 底色, 文字色, tooltip)``。

        单独抽出来是为了能测：不建控件也能断言「哪个事件画在哪条泳道的第几拍」。
        颜色保持 ``ft.Colors`` 的原值，**不要 str()**：``ft.Colors`` 是枚举，
        ``str()`` 会得到 ``"Colors.INDIGO_400"`` 这种非法颜色串，块会画成透明。
        """
        blocks: list[tuple[float, int, str, object, object, str]] = []
        for ev in self.doc.chord_events():
            beat = self.doc.beats_at(ev.tick)
            lane = _LANE_BY_TYPE.get(ev.event_type, 0)
            name = self.doc.chord_name(ev.chord_id) or f"#{ev.chord_id}"
            if ev.event_type == EV_CHORD_STRUM:
                up = ev.direction == DIR_UP
                label = name
                bgcolor = ft.Colors.TEAL_400 if up else ft.Colors.INDIGO_400
                tip = f"{name} · {'上扫' if up else '下扫'} · 第 {beat:.2f} 拍"
            elif ev.event_type == EV_CHORD_PLUCK:
                label = f"{name}·{ev.string or 1}"
                bgcolor = ft.Colors.AMBER_400
                tip = f"{name} · 拨 {ev.string or 1} 弦 · 第 {beat:.2f} 拍"
            else:
                label = name
                bgcolor = ft.Colors.with_opacity(0.35, ft.Colors.ON_SURFACE)
                tip = f"{name} · 只换指位 · 第 {beat:.2f} 拍"
            color = (
                ft.Colors.ON_SURFACE
                if ev.event_type == EV_CHORD_SET
                else ft.Colors.WHITE
            )
            blocks.append((beat, lane, label, bgcolor, color, tip))
        return blocks

    def _chord_row(self, index: int, ev: ChordRow) -> ft.Control:
        name = self.doc.chord_name(ev.chord_id)
        beat_field = ft.TextField(
            value=_format_beats(self.doc.beats_at(ev.tick)),
            width=78,
            dense=True,
            text_size=12,
            hint_text="拍",
            tooltip="这个事件在第几拍（保存时会按拍排序）",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, ev=ev: self._on_beat_change(ev, e.control.value),
        )
        kind_field = ft.Dropdown(
            value=_TYPE_LABELS.get(ev.event_type, "扫弦"),
            width=112,
            text_size=12,
            options=[ft.DropdownOption(key=label, text=label) for label in _TYPE_LABELS.values()],
            on_select=lambda e, ev=ev: self._on_kind_change(ev, e.control.value),
        )
        chord_field = ft.Dropdown(
            value=name or None,
            width=150,
            text_size=12,
            hint_text="和弦",
            options=[
                ft.DropdownOption(key=option, text=self._option_text(option))
                for option in self._chord_options(name)
            ],
            on_select=lambda e, ev=ev: self._on_chord_change(ev, e.control.value),
        )

        if ev.event_type == EV_CHORD_STRUM:
            extra: ft.Control = ft.Dropdown(
                value=_DIRECTION_LABELS.get(ev.direction, "下扫"),
                width=96,
                text_size=12,
                tooltip="扫弦方向：下扫 6→1 弦，上扫 1→6 弦",
                options=[
                    ft.DropdownOption(key=label, text=label)
                    for label in _DIRECTION_LABELS.values()
                ],
                on_select=lambda e, ev=ev: self._on_direction_change(ev, e.control.value),
            )
        elif ev.event_type == EV_CHORD_PLUCK:
            extra = ft.Dropdown(
                value=str(ev.string or 1),
                width=96,
                text_size=12,
                tooltip="单弦拨奏只弹这一根弦",
                options=[
                    ft.DropdownOption(key=str(number), text=f"{number} 弦")
                    for number in range(1, max(1, min(6, self.doc.string_count)) + 1)
                ],
                on_select=lambda e, ev=ev: self._on_pluck_string_change(ev, e.control.value),
            )
        else:
            extra = ft.Text("—", width=96, size=12, color=MUTED)

        fingering = self.chord_table.fingering(name) if name else None
        finger_text = (
            "—"
            if not name
            else "未定义"
            if fingering is None
            else " ".join("x" if fret == -1 else str(fret) for fret in fingering)
        )

        return ft.Row(
            [
                ft.Text(f"C{index + 1:02d}", width=34, size=11, color=MUTED),
                beat_field,
                kind_field,
                chord_field,
                extra,
                ft.Text(
                    finger_text,
                    width=104,
                    size=11,
                    color=ft.Colors.ORANGE_300 if name and fingering is None else MUTED,
                    tooltip=_FINGER_TIP,
                ),
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

    def _tempo_row(self, index: int, ev: ChordRow) -> ft.Control:
        beat_field = ft.TextField(
            value=_format_beats(self.doc.beats_at(ev.tick)),
            width=90,
            dense=True,
            text_size=12,
            hint_text="拍",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, ev=ev: self._on_tempo_beat_change(ev, e.control.value),
        )
        bpm_field = ft.TextField(
            value=f"{60_000_000 / ev.tempo_us:.1f}".rstrip("0").rstrip(".")
            if ev.tempo_us
            else "",
            width=72,
            dense=True,
            text_size=12,
            hint_text="BPM",
            input_filter=ft.InputFilter(allow=True, regex_string=_BEAT_FILTER),
            on_change=lambda e, ev=ev: self._on_tempo_bpm_change(ev, e.control.value),
        )
        return ft.Row(
            [
                ft.Text(f"E{index + 1:02d}", width=34, size=11, color=MUTED),
                beat_field,
                ft.Text("Tempo", width=112, size=12),
                bpm_field,
                ft.Text("从这个位置开始变速", size=11, color=MUTED, expand=True),
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=16,
                    tooltip="删除这个 Tempo 事件",
                    on_click=lambda e, ev=ev: self._on_delete_event(ev),
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _end_row(self) -> ft.Control:
        ends = [ev for ev in self.doc.events if ev.event_type == EV_END]
        tick = ends[0].tick if ends else int(self.doc.duration_beats * self.doc.time_base_value)
        return ft.Row(
            [
                ft.Text("END", width=34, size=11, color=MUTED),
                ft.Text(f"{_format_beats(self.doc.beats_at(tick))}", width=90, size=12, color=MUTED),
                ft.Text("End", width=112, size=12, color=MUTED),
                ft.Text("", width=72),
                ft.Text("结束标记（保存时自动保证在最后）", size=11, color=MUTED),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # ------------------------------------------------------------------ 标签 / 提示

    def _refresh_labels(self) -> None:
        name = self.path.name if self.path else "未命名"
        prefix = "● " if self.dirty else ""
        self._file_label.value = f"{prefix}{name}"
        chords = len(self.doc.chord_events())
        tempos = sum(1 for ev in self.doc.events if ev.event_type == EV_TEMPO)
        self._stats_label.value = (
            f"{chords} 个和弦 · {tempos} 个 Tempo · "
            f"{self.doc.duration_beats:.2f} 拍 · {self.doc.bpm:.0f} BPM"
        )

    def _refresh_warnings(self) -> None:
        lines: list[str] = []
        if self.chord_error:
            lines.append(f"⚠ chord.json 读不出来：{self.chord_error}")
        missing = self.chord_table.missing_mappings()
        if missing:
            lines.append(
                f"ℹ {len(missing)} 个和弦还没填指法：{'、'.join(missing[:6])}"
                f"{'…' if len(missing) > 6 else ''}"
                "（只影响指法显示和试听，不影响手柄回放位置）"
            )
        unknown = self.chord_table.unknown_mappings()
        if unknown:
            lines.append(
                f"⚠ CHORDS_MAPPINGS 里有不在 CHORD_NAMES 里的名字：{'、'.join(unknown[:6])}"
            )
        used = {self.doc.chord_name(ev.chord_id) for ev in self.doc.chord_events()}
        missing_in_table = sorted(
            name for name in used if name and name not in self.chord_table.names
        )
        if missing_in_table:
            lines.append(
                f"⚠ 谱面用到 chord.json 里没有的和弦：{'、'.join(missing_in_table[:6])}"
                "（回放会被跳过；先把它加进 CHORD_NAMES）"
            )
        self._warning.value = "\n".join(lines)
        self._warning.visible = bool(lines)
        self._warning.color = (
            ft.Colors.RED_400 if (self.chord_error or missing_in_table) else ft.Colors.ORANGE_300
        )

        table_lines: list[str] = []
        if self.chord_error:
            table_lines.append("⚠ chord.json 读不出来，指法无法显示")
        elif missing:
            table_lines.append(f"ℹ {len(missing)} 个和弦没填指法")
        if unknown:
            table_lines.append(f"⚠ 有 {len(unknown)} 个多余/拼错的映射名字")
        self._chord_table_warning.value = "；".join(table_lines)
        self._chord_table_warning.visible = bool(table_lines)

    # ------------------------------------------------------------------ 工具

    def _read_chord_table(self) -> None:
        try:
            self.chord_table = load_chord_table()
            self.chord_error = ""
        except ChordError as exc:
            self.chord_table = ChordTable((), {})
            self.chord_error = str(exc)

    def _chord_options(self, current: str) -> list[str]:
        options = list(self.chord_table.names)
        # 谱面里已经存在、但 json 里没有的名字也要能看见（否则下拉一碰就改没了）
        for name in self.doc.chord_names():
            if name and name not in options:
                options.append(name)
        if current and current not in options:
            options.append(current)
        return options

    def _option_text(self, name: str) -> str:
        position = position_of(name, self.chord_table.names)
        if position is None:
            return f"{name}（不在表里）"
        return f"{name}（P{position[0]}）"

    def _default_chord_name(self) -> str:
        if self.chord_table.names:
            return self.chord_table.names[0]
        names = self.doc.chord_names()
        return names[0] if names else "G"

    def _ensure_chord(self, name: str) -> int | None:
        chord_id = self.doc.chord_id(name)
        if chord_id is not None:
            return chord_id
        try:
            return self.doc.add_chord(name)
        except GctError as exc:
            self._snack(f"和弦表更新失败：{exc}", error=True)
            return None

    def _next_tick(self) -> int:
        last = max(
            (ev.tick for ev in self.doc.events if ev.event_type != EV_END),
            default=-self.doc.time_base_value,
        )
        return max(0, last + self.doc.time_base_value)

    def _ensure_picker(self) -> ft.FilePicker:
        if self._picker is None:
            self._picker = ft.FilePicker()
        return self._picker

    def _snack(self, message: str, error: bool = False) -> None:
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

    def _touch(self) -> None:
        self.dirty = True
        self._refresh_labels()
        self._update()

    def _set_dirty(self, value: bool) -> None:
        self.dirty = value
        self._refresh_labels()
        self._update()

    def _update(self) -> None:
        if self.page is not None:
            self.page.update()

    # ------------------------------------------------------------------ 试听

    def _on_preview(self, _event=None) -> None:
        """试听当前和弦谱：按 BPM / Tempo 合成，音高取 chord.json 的指法。"""
        if self._playing:
            self.stop_preview()
            return
        if not self.doc.chord_events():
            self._snack("还没有和弦事件，先添加和弦再试听")
            return

        # 快照：播的时候改谱面不影响已经在响的这一段
        snapshot = copy.deepcopy(self.doc)
        notes, skipped = chord_preview.chord_notes(snapshot, self.chord_table)
        if not notes:
            self._snack(
                "和弦表里没有可用的指法，试听没有声音；"
                "先在 src/chord.json 的 CHORDS_MAPPINGS 里补上"
            )
            return
        if skipped:
            self._snack(
                f"{len(skipped)} 个和弦没有指法，试听跳过：{'、'.join(skipped[:4])}"
            )

        self._playing = True
        self._preview_token += 1
        token = self._preview_token
        stop_flag = threading.Event()
        self._preview_stop = stop_flag
        # 合成线程只写这几个状态；界面由 _preview_ticker 在页面事件循环里刷新
        # （后台线程里直接 page.update() 不会重绘，和 GTAB 页踩过的是同一个坑）。
        self._audio_started_at = None
        self._audio_duration = 0.0
        self._audio_error = None
        self._follow_offset = 0.0
        self._preview_label.value = "正在合成…"
        self._preview_label.color = MUTED
        self._update()

        if self.page is None:
            # 离线构建/测试：只合成，不播放
            try:
                blob = chord_preview.render_wav(snapshot, self.chord_table)
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
            self.page.run_thread(
                self._preview_worker, snapshot, self.chord_table, token, stop_flag
            )
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
        if self._preview_label.value.startswith(("正在", "试听", "已合成")):
            self._preview_label.value = ""
        self._preview_label.color = MUTED
        self._update()

    def _preview_worker(
        self,
        snapshot: GctDocument,
        table: ChordTable,
        token: int,
        stop_flag: threading.Event,
    ) -> None:
        """后台线程：只负责合成 + 启动播放，不碰界面。"""
        try:
            blob = chord_preview.render_wav(snapshot, table)
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
        snapshot: GctDocument,
        token: int,
        stop_flag: threading.Event,
    ) -> None:
        """在页面事件循环里推进游标（后台线程的 page.update() 不会重绘）。"""
        try:
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
                self._preview_label.color = MUTED
                self._update()

    async def _scroll_timeline(self, offset: float, duration: int) -> None:
        """横向滚动节拍轴；失败/超时只忽略，不影响播放。"""
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

    def _set_playhead_seconds(self, score_seconds: float, doc: GctDocument) -> None:
        """把试听游标放到 ``score_seconds`` 对应的拍位置（gct 用 gct_doc 的换算）。"""
        tick = seconds_to_tick(doc, score_seconds)
        left = doc.beats_at(tick) * PX_PER_BEAT
        width = self._timeline_width or 0.0
        self._playhead.left = max(0.0, min(left, max(0.0, width - 3)))
        self._playhead.visible = True
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = _LANES_HEIGHT + 2

    # ------------------------------------------------------------------ 事件编辑

    def _on_beat_change(self, ev: ChordRow, text: str) -> None:
        beats = _parse_beats(text)
        if beats is None:
            return
        ev.tick = self.doc.tick_at(beats)
        self._render_timeline()  # 只重画节拍轴，事件行不动，输入框不失焦
        self._touch()

    def _on_kind_change(self, ev: ChordRow, label: str) -> None:
        new_type = _TYPE_BY_LABEL.get(label)
        if new_type is None or new_type == ev.event_type:
            return
        chord_id = ev.chord_id
        if chord_id <= 0:
            chord_id = self._ensure_chord(self._default_chord_name()) or 0
        if new_type == EV_CHORD_STRUM:
            direction = ev.direction if ev.event_type == EV_CHORD_STRUM else DIR_DOWN
            ev.data = chord_id.to_bytes(2, "little") + bytes([direction])
        elif new_type == EV_CHORD_PLUCK:
            string = ev.string if ev.event_type == EV_CHORD_PLUCK and ev.string else 6
            string = max(1, min(self.doc.string_count, string))
            ev.data = chord_id.to_bytes(2, "little") + bytes([string])
        else:
            ev.data = chord_id.to_bytes(2, "little")
        ev.event_type = new_type
        self._render_events()
        self._touch()

    def _on_chord_change(self, ev: ChordRow, name: str) -> None:
        chord_id = self._ensure_chord(name)
        if chord_id is None:
            return
        ev.set_chord_id(chord_id)
        self._render_events()
        self._touch()

    def _on_direction_change(self, ev: ChordRow, label: str) -> None:
        direction = _DIRECTION_BY_LABEL.get(label)
        if direction is None:
            return
        ev.data = ev.chord_id.to_bytes(2, "little") + bytes([direction])
        self._render_timeline()  # 上扫/下扫换颜色
        self._touch()

    def _on_pluck_string_change(self, ev: ChordRow, value: str) -> None:
        try:
            string = int(str(value).strip())
        except (TypeError, ValueError):
            return
        string = max(1, min(self.doc.string_count, string))
        ev.data = ev.chord_id.to_bytes(2, "little") + bytes([string])
        self._render_timeline()  # 块上的「·3」跟着变
        self._touch()

    def _on_delete_event(self, ev: ChordRow) -> None:
        self.doc.events = [row for row in self.doc.events if row is not ev]
        self._render_events()
        self._touch()

    def _on_add_chord(self, _event=None) -> None:
        chord_id = self._ensure_chord(self._default_chord_name())
        if chord_id is None:
            return
        self.doc.events.append(
            ChordRow(
                tick=self._next_tick(),
                event_type=EV_CHORD_STRUM,
                data=chord_id.to_bytes(2, "little") + bytes([DIR_DOWN]),
            )
        )
        self._render_events()
        self._touch()

    def _on_add_tempo(self, _event=None) -> None:
        self.doc.events.append(
            ChordRow(
                tick=self._next_tick(),
                event_type=EV_TEMPO,
                data=(500_000).to_bytes(4, "little"),
            )
        )
        self._render_events()
        self._touch()

    def _on_tempo_beat_change(self, ev: ChordRow, text: str) -> None:
        beats = _parse_beats(text)
        if beats is None:
            return
        ev.tick = self.doc.tick_at(beats)
        self._render_timeline()  # 橙色竖线跟着挪
        self._touch()

    def _on_tempo_bpm_change(self, ev: ChordRow, text: str) -> None:
        bpm = _parse_beats(text)
        if bpm is None or bpm <= 0:
            return
        ev.data = max(1, round(60_000_000 / bpm)).to_bytes(4, "little")
        self._render_timeline()  # 竖线的 tooltip 里有 BPM
        self._touch()

    # ------------------------------------------------------------------ 头部参数

    def _on_bpm_change(self, _event=None) -> None:
        bpm = _parse_beats(self._f_bpm.value)
        if bpm is None or bpm <= 0:
            return
        try:
            self.doc.bpm = bpm
        except GctError:
            return
        self._touch()

    def _on_strings_change(self, _event=None) -> None:
        try:
            count = int(str(self._f_strings.value or "").strip())
        except ValueError:
            return
        if not (1 <= count <= 255) or count == self.doc.string_count:
            return
        self.doc.string_count = count
        # 拨弦的弦号不能超过弦数（否则保存会被 writer 拒），顺手夹进来。
        # 这里不重建行：重建会让正在输入的「弦数」输入框失焦。
        for ev in self.doc.events:
            if ev.event_type == EV_CHORD_PLUCK and not (1 <= ev.string <= count):
                string = max(1, min(count, ev.string or 1))
                ev.data = ev.chord_id.to_bytes(2, "little") + bytes([string])
        self._touch()

    def _on_crc_change(self, _event=None) -> None:
        if bool(self._sw_crc.value):
            self.doc.flags |= 0x01
        else:
            self.doc.flags &= ~0x01
        self._touch()

    def _on_reload_chords(self, _event=None) -> None:
        self.reload_chords()
        if self.chord_error:
            self._snack(f"chord.json 读不出来：{self.chord_error}", error=True)
        else:
            self._snack(f"已重载和弦表：{len(self.chord_table.names)} 个和弦")

    def reload_chords(self) -> None:
        """重读 chord.json（切回本页时由 app 调用，改了指法不用重启）。"""
        reload_chord_table()
        self._read_chord_table()
        self._render_chord_table()
        self._render_events()
        self._refresh_warnings()
        self._update()

    # ------------------------------------------------------------------ 文件操作

    async def _on_open(self, _event=None) -> None:
        picker = self._ensure_picker()
        try:
            files = await picker.pick_files(
                dialog_title="打开 gct 文件",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gct"],
                allow_multiple=False,
            )
        except Exception as exc:  # noqa: BLE001 - 平台差异兜底
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
                raise GctError("拿不到所选文件的内容")
            doc = GctDocument.from_bytes(blob)
        except (OSError, GctError) as exc:
            self._snack(f"打开失败：{exc}", error=True)
            return

        # 打开前重读一次 chord.json：外面改了指法/名字，这里立刻生效
        self.reload_chords()
        self.doc = doc
        self.path = target
        self._render_all()
        self._set_dirty(False)
        self._snack(f"已载入 {chosen.name}：{len(doc.chord_events())} 个和弦事件")

    async def _on_save(self, _event=None) -> None:
        if self.path is None:
            await self._on_save_as()
            return
        try:
            blob = self.doc.to_bytes()
            self._render_events()
            self.path.write_bytes(blob)
        except (OSError, GctError) as exc:
            self._snack(f"保存失败：{exc}", error=True)
            return
        self._set_dirty(False)
        self._snack(f"已保存 {self.path.name}（{len(blob)} 字节）")

    async def _on_save_as(self, _event=None) -> None:
        try:
            blob = self.doc.to_bytes()
            self._render_events()
        except GctError as exc:
            self._snack(f"保存失败：{exc}", error=True)
            return

        picker = self._ensure_picker()
        try:
            target = await picker.save_file(
                dialog_title="另存为 gct",
                file_name=self.path.name if self.path else "untitled.gct",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gct"],
                src_bytes=blob,
            )
        except Exception as exc:  # noqa: BLE001
            self._snack(f"无法打开保存对话框：{exc}", error=True)
            return
        if not target:
            return

        self.path = Path(target)
        self._set_dirty(False)
        self._snack(f"已保存到 {self.path.name}（{len(blob)} 字节）")

    async def _on_new(self, _event=None) -> None:
        self.doc = GctDocument()
        self.path = None
        self._render_all()
        self._set_dirty(False)
        self._snack("已新建空白和弦谱")

    async def _on_load_demo(self, _event=None) -> None:
        try:
            if self.DEMO_PATH.exists():
                doc = GctDocument.from_path(self.DEMO_PATH)
                path: Path | None = self.DEMO_PATH
            else:
                doc = demo_document()
                path = None
        except (OSError, GctError) as exc:
            self._snack(f"示例载入失败：{exc}", error=True)
            return
        self.doc = doc
        self.path = path
        self._render_all()
        self._set_dirty(False)
        self._snack(f"已载入示例：{len(doc.chord_events())} 个和弦事件")
