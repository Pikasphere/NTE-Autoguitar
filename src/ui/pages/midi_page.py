"""MIDI 映射页：把 MIDI 文件里的音符映射成键盘按键，并按时间发出去。

页面结构（和另外两个编辑页一致）：

- **左侧**：MIDI 信息（格式 / 轨道 / 音符 / 时长 / 音域 / BPM）、
  **转换设置**（Capo、点按时长）、**键位表**（``src/midi_keys.json`` 的 36 个键，
  发出去用到的键会高亮）；
- **右侧**：节拍轴（低 / 中 / 高八度三条泳道 = 键位表的三行，块上写按键字符）、
  播放控制、进度、超出键位窗口的提示、触发记录。

映射规则（见 ``ui/keyboard_backend.py`` 顶部）：``midi_keys.json`` 从 ``BASE_NOTE``
起每个半音一个键，默认 36 个键 = E3–D#6 三个八度；音高不在窗口里的音默认**跳过并报出来**，
用 **Capo**（0–12 品，N 品 = 整首升高 N 个半音）把整首移进去；**不自动移调**，
Capo 由用户自己选，载入新 MIDI 时回到 0。设置页打开「MIDI 音域超出后按八度投影」后，
超窗的音改为**按八度移到窗口里**再发。

发送用 Windows ``SendInput`` + **扫描码**；低权限进程（Agent/沙盒）发不进权限更高的
游戏，和全局热键是同一个 UIPI 限制，页面会红字提示。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import flet as ft

import midi as midi_format
from ui.components import BEATS_PER_BAR, LANE_HEIGHT, MUTED, PX_PER_BEAT, RULER_HEIGHT, card
from ui.hotkeys import (
    HOTKEY_PLAY,
    HOTKEY_STOP,
    GlobalHotkeys,
    hotkey_label,
    process_integrity,
)
from ui.keyboard_backend import (
    LEAD_IN,
    KeyAction,
    KeyTable,
    KeyTableError,
    KeyboardBackend,
    build_key_schedule,
    key_label,
    load_key_table,
    project_note,
    reload_key_table,
)
from ui.midi_doc import MidiDocument, demo_document, note_name
from ui.settings import get_settings

__all__ = ["MidiPage"]

#: 每条泳道 = 键位表的一行（12 个半音）。默认键位表三行 = E3–D#6。
_LANES = ("低八度", "中八度", "高八度")
_KEYS_PER_LANE = 12
_TIMELINE_HEIGHT = RULER_HEIGHT + len(_LANES) * LANE_HEIGHT

#: Capo 上限：12 品 = 一个八度。再高就该直接改键位表了。
CAPO_MAX = 12


class MidiPage:
    TITLE = "MIDI 映射"
    SUBTITLE = "MIDI 文件 → 键盘按键（SendInput 扫描码），按时间发进游戏"
    DEMO_PATH = Path(__file__).resolve().parents[2] / "MusicList" / "midifile" / "demo.mid"

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.doc: MidiDocument | None = None
        self.path: Path | None = None
        self.table: KeyTable = KeyTable()
        self.table_error = ""
        # Capo 品位：N 品 = 整首升高 N 个半音（不自动移调，载入新文档时回到 0）
        self.capo = 0
        # 超窗的音是按八度投进窗口，还是跳过并报出来（设置页的全局开关）
        self.project_octave = bool(get_settings().midi_project_octave)
        self._picker: ft.FilePicker | None = ft.FilePicker() if page is not None else None

        self.backend = KeyboardBackend()
        self._playing = False
        self._pressed = 0
        self._total_notes = 0
        self._drifts: list[float] = []
        self._timeline_width = 0
        self._follow_offset = 0.0
        self._timeline_scroll: ft.Row | None = None

        self._active = False
        self._hotkeys = GlobalHotkeys(
            {
                HOTKEY_PLAY: self._on_hotkey_play,
                HOTKEY_STOP: self._on_hotkey_stop,
            }
        )

        self._read_table()
        self._root = self._build()
        self._refresh_project_hint()
        self._render_all()

    # ------------------------------------------------------------------ 组合

    def build(self) -> ft.Control:
        return self._root

    def _build(self) -> ft.Control:
        self._status_text = ft.Text("", size=12)
        self._file_label = ft.Text("未载入 MIDI", size=12, color=MUTED)
        self._progress = ft.Text("", size=12, color=MUTED)
        self._warning = ft.Text("", size=11, color=ft.Colors.ORANGE_300, visible=False)
        self._hotkey_text = ft.Text("全局快捷键：进入本页后生效", size=11, color=MUTED)
        self._log_column = ft.Column(spacing=2)

        self._dd_capo = ft.Dropdown(
            value="0",
            width=84,
            text_size=12,
            options=[
                ft.DropdownOption(key=str(fret), text=str(fret))
                for fret in range(CAPO_MAX + 1)
            ],
            on_select=self._on_capo_change,
            tooltip="变调夹：N 品 = 整首升高 N 个半音（0 = 不升）。载入新 MIDI 时回到 0",
        )
        self._f_tap = ft.TextField(
            value="100",
            width=64,
            dense=True,
            text_size=12,
            text_align=ft.TextAlign.CENTER,
            hint_text="100",
            input_filter=ft.NumbersOnlyInputFilter(),
            tooltip="点按时长（毫秒）。键盘游戏通常在按下那一刻判定；"
            "要长按的游戏请把「点按」关掉",
        )
        self._sw_tap = ft.Switch(
            label="点按",
            value=True,
            tooltip="开：每个音按一下就松开（默认 100ms）；关：按住到音尾",
        )

        self._playhead = ft.Container(
            left=0,
            top=RULER_HEIGHT - 2,
            width=3,
            height=len(_LANES) * LANE_HEIGHT + 2,
            bgcolor=ft.Colors.RED_400,
            visible=False,
            tooltip="播放位置",
        )

        body = ft.Row(
            [self._left_panel(), self._right_panel()],
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
                    self._status_card(),
                    body,
                ],
                spacing=12,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

    def _status_card(self) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=10, horizontal=16),
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Row(
                [
                    ft.Text("键盘", size=13, weight=ft.FontWeight.BOLD),
                    self._status_text,
                    ft.Container(width=8),
                    ft.TextButton(
                        "重载键位",
                        icon=ft.Icons.REFRESH,
                        tooltip="改完 src/midi_keys.json 点这里重读",
                        on_click=self._on_reload_keys,
                    ),
                    ft.Button("打开 MIDI", icon=ft.Icons.FOLDER_OPEN, on_click=self._on_open),
                    ft.TextButton("示例 MIDI", icon=ft.Icons.AUTO_AWESOME, on_click=self._on_demo),
                    ft.VerticalDivider(width=1),
                    ft.Button("播放", icon=ft.Icons.PLAY_ARROW, on_click=self._on_play),
                    ft.Button("停止", icon=ft.Icons.STOP, on_click=lambda e: self.stop()),
                ],
                spacing=8,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _left_panel(self) -> ft.Control:
        self._info_column = ft.Column(spacing=3)
        self._keys_column = ft.Column(spacing=4)
        self._project_hint = ft.Text("", size=11, color=MUTED)
        settings = card(
            "转换设置",
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Capo", size=11, color=MUTED),
                            self._dd_capo,
                            ft.Text("品", size=11, color=MUTED),
                            ft.Text("整首升高 N 个半音", size=11, color=MUTED),
                        ],
                        spacing=6,
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            ft.Text("点按", size=11, color=MUTED),
                            self._f_tap,
                            ft.Text("ms", size=11, color=MUTED),
                            self._sw_tap,
                        ],
                        spacing=6,
                        wrap=True,
                    ),
                    self._project_hint,
                ],
                spacing=8,
            ),
        )
        return ft.Column(
            [card("MIDI 信息", self._info_column), settings, card("键位表", self._keys_column)],
            width=348,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _right_panel(self) -> ft.Control:
        self._timeline_host = ft.Container(
            height=_TIMELINE_HEIGHT + 4,
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.ON_SURFACE),
            content=ft.Row(),
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
                                "三条泳道 = 键位表的三行（每行 12 个半音）；块上写要按的键",
                                size=11,
                                color=MUTED,
                            ),
                            ft.Container(expand=True),
                            self._progress,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._timeline_host,
                    self._warning,
                    self._hotkey_text,
                    ft.Divider(height=1),
                    ft.Text("触发记录", size=12, weight=ft.FontWeight.BOLD),
                    ft.ListView([self._log_column], expand=True, padding=0),
                ],
                spacing=8,
                expand=True,
            ),
        )

    # ------------------------------------------------------------------ 键位表 / 信息

    def _read_table(self) -> None:
        try:
            self.table = load_key_table()
            self.table_error = ""
        except KeyTableError as exc:
            self.table = KeyTable()
            self.table_error = str(exc)

    def _render_all(self) -> None:
        if self._playing:
            self.stop()
        self._render_info()
        self._render_keys()
        self._render_timeline()
        self._refresh_labels()
        self._refresh_status()

    def _refresh_status(self) -> None:
        """状态卡：能不能发按键 / 是不是低权限（UIPI 会让发送静默失效）。"""
        if not self.backend.available:
            self._status_text.value = f"不能发送按键：{self.backend.error}"
            self._status_text.color = ft.Colors.RED_400
        elif process_integrity() == "low":
            self._status_text.value = (
                "⚠ 低权限运行：游戏权限更高时发送会被拒绝，请用 run.bat 启动"
            )
            self._status_text.color = ft.Colors.RED_400
        else:
            self._status_text.value = "发送就绪：SendInput + 扫描码（发进当前前台窗口）"
            self._status_text.color = MUTED

    def _render_info(self) -> None:
        doc = self.doc
        if doc is None:
            self._info_column.controls = [
                ft.Text("还没有载入 MIDI。点「打开 MIDI」或「示例 MIDI」。", size=12, color=MUTED)
            ]
            return
        low, high = doc.note_range
        rows = [
            ("文件", doc.name or "（无标题）"),
            ("格式", f"format {doc.format} · {doc.tracks} 轨 · {doc.division} tick/拍"),
            ("音符", f"{len(doc.notes)} 个 · 音域 {note_name(low)}–{note_name(high)}"),
            ("时长", f"{doc.duration_beats:.2f} 拍 · {doc.duration_seconds:.2f} 秒"),
            ("BPM", f"{doc.bpm:.1f}" + (f"（{len(doc.tempos)} 处变速）" if len(doc.tempos) > 1 else "")),
        ]
        if doc.unclosed:
            rows.append(("提示", f"{doc.unclosed} 个音没有音尾，已按曲尾收掉"))
        self._info_column.controls = [
            ft.Row(
                [
                    ft.Text(label, width=42, size=11, color=MUTED),
                    ft.Text(value, size=12, expand=True),
                ],
                spacing=6,
            )
            for label, value in rows
        ]

    def _render_keys(self) -> None:
        if self.table_error:
            self._keys_column.controls = [
                ft.Text(f"⚠ 键位表读不出来：{self.table_error}", size=11, color=ft.Colors.RED_400)
            ]
            return
        used = self._used_keys()
        controls: list[ft.Control] = []
        columns = 12
        rows = max(1, (self.table.span + columns - 1) // columns)
        for row in range(rows):
            cells: list[ft.Control] = []
            for column in range(columns):
                index = row * columns + column
                if index >= self.table.span:
                    cells.append(ft.Container(width=26))
                    continue
                key = self.table.keys[index]
                pitch = self.table.base_note + index
                active = key in used
                cells.append(
                    ft.Container(
                        width=26,
                        padding=ft.Padding.symmetric(vertical=2),
                        border_radius=4,
                        bgcolor=(
                            ft.Colors.with_opacity(0.18, ft.Colors.PRIMARY)
                            if active
                            else ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE)
                        ),
                        alignment=ft.Alignment.CENTER,
                        content=ft.Column(
                            [
                                ft.Text(note_name(pitch), size=8, color=MUTED),
                                ft.Text(
                                    key_label(key),
                                    size=12 if len(key) == 1 else 9,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.PRIMARY if active else None,
                                ),
                            ],
                            spacing=0,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        tooltip=f"{note_name(pitch)} → {key_label(key)}",
                    )
                )
            controls.append(ft.Row(cells, spacing=2))
        self._keys_column.controls = controls

    def _refresh_project_hint(self) -> None:
        if self.project_octave:
            self._project_hint.value = (
                "音高超出键位窗口时会按八度投影进窗口（设置页已打开）；"
                "Capo 仍会在此基础上整体再升高一次。"
            )
        else:
            self._project_hint.value = (
                "音高不在键位窗口里的音会被跳过（上方会提示音名），"
                "用 Capo 把整首移进来；也可以在设置页打开「八度投影」。"
            )

    def _effective_pitch(self, note: int) -> int | None:
        """按当前策略这个音实际会按的音高（Capo + 可选八度投影）；发不出去返回 None。"""
        pitch = int(note) + self.capo
        if self.table.key_for(note, self.capo) is not None:
            return pitch
        if self.project_octave:
            return project_note(note, self.table, self.capo)
        return None

    def _key_for(self, note: int) -> str | None:
        pitch = self._effective_pitch(note)
        if pitch is None:
            return None
        index = pitch - self.table.base_note
        if 0 <= index < self.table.span:
            return self.table.keys[index]
        return None

    def _used_keys(self) -> set[str]:
        if self.doc is None:
            return set()
        return {key for key in (self._key_for(note.note) for note in self.doc.notes) if key}

    def _refresh_labels(self) -> None:
        doc = self.doc
        if doc is None:
            self._file_label.value = "未载入 MIDI"
            return
        name = self.path.name if self.path else (doc.name or "示例（内存）")
        self._file_label.value = (
            f"{name} · {len(doc.notes)} 个音 · {doc.duration_seconds:.1f} 秒"
        )

    # ------------------------------------------------------------------ 节拍轴

    def _render_timeline(self) -> None:
        doc = self.doc
        span = max(4.0, (doc.duration_beats if doc else 0.0) + 1.0)
        width = int(span * PX_PER_BEAT)
        self._timeline_width = width

        children: list[ft.Control] = []
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
                    height=len(_LANES) * LANE_HEIGHT,
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

        # tempo 变化：橙色竖线（只标位置）
        for tempo in (doc.tempos if doc else []):
            if tempo.tick <= 0:
                continue
            children.append(
                ft.Container(
                    left=(doc.beats_at(tempo.tick) * PX_PER_BEAT),
                    top=RULER_HEIGHT - 2,
                    width=2,
                    height=len(_LANES) * LANE_HEIGHT + 2,
                    bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.DEEP_ORANGE),
                    tooltip=f"变速 {tempo.bpm:.1f} BPM · 第 {doc.beats_at(tempo.tick):.2f} 拍",
                )
            )

        for beat, lane, label, width_px, tip, used in self._note_blocks():
            children.append(
                ft.Container(
                    left=beat * PX_PER_BEAT,
                    top=RULER_HEIGHT + lane * LANE_HEIGHT + 3,
                    width=width_px,
                    height=LANE_HEIGHT - 6,
                    bgcolor=(
                        ft.Colors.INDIGO_400
                        if used
                        else ft.Colors.with_opacity(0.35, ft.Colors.ON_SURFACE)
                    ),
                    border_radius=3,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text(label, size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    tooltip=tip,
                )
            )

        if doc is None or not doc.notes:
            children.append(
                ft.Text(
                    left=8,
                    top=RULER_HEIGHT + 6,
                    value="还没有 MIDI；点「打开 MIDI」或「示例 MIDI」。",
                    size=11,
                    color=MUTED,
                )
            )

        stack = ft.Stack(controls=children, width=width, height=_TIMELINE_HEIGHT)
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = len(_LANES) * LANE_HEIGHT + 2
        self._playhead.left = max(
            0.0, min(float(self._playhead.left or 0.0), max(0.0, width - 3))
        )
        stack.controls.append(self._playhead)

        lane_labels = ft.Column(
            [ft.Container(height=RULER_HEIGHT)]
            + [
                ft.Container(
                    height=LANE_HEIGHT,
                    width=40,
                    alignment=ft.Alignment(1, 0),
                    content=ft.Text(name, size=9, color=MUTED),
                )
                for name in _LANES
            ],
            spacing=0,
        )
        self._timeline_scroll = ft.Row([stack], scroll=ft.ScrollMode.ALWAYS, expand=True)
        self._timeline_host.content = ft.Row(
            [lane_labels, self._timeline_scroll],
            spacing=8,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self._timeline_host.height = _TIMELINE_HEIGHT + 4

    def _note_blocks(self) -> list[tuple[float, int, str, float, str, bool]]:
        """节拍轴上的块：``(拍, 泳道, 文字, 宽, tooltip, 是否在键位内)``。"""
        if self.doc is None:
            return []
        blocks = []
        for note in self.doc.notes:
            key = self._key_for(note.note)
            pitch = self._effective_pitch(note.note)
            base_pitch = note.note + self.capo
            index = (pitch if pitch is not None else base_pitch) - self.table.base_note
            # 泳道 = 键位表的第几行（每行 12 个半音）；窗口外的音贴到首/末行
            lane = min(len(_LANES) - 1, max(0, index // _KEYS_PER_LANE))
            beat = self.doc.beats_at(note.start)
            length = max(0.05, self.doc.beats_at(note.end) - beat)
            label = key_label(key) if key else note.name
            tip = f"{note.name}"
            if key:
                tip += f" → 键 {key_label(key)}"
                if pitch is not None and pitch != base_pitch:
                    tip += f"（八度投影到 {note_name(pitch)}）"
            else:
                tip += "（超出键位窗口）"
            tip += f" · 第 {beat:.2f} 拍 · 长 {length:.2f} 拍"
            blocks.append(
                (beat, lane, label, max(10.0, min(PX_PER_BEAT * 2, length * PX_PER_BEAT)), tip, key is not None)
            )
        return blocks

    # ------------------------------------------------------------------ 载入

    async def _on_open(self, _event=None) -> None:
        picker = self._ensure_picker()
        try:
            files = await picker.pick_files(
                dialog_title="打开 MIDI 文件",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["mid", "midi"],
                allow_multiple=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._snack(f"无法打开文件选择器：{exc}", error=True)
            return
        if not files:
            return
        chosen = files[0]
        if not chosen.path:
            self._snack("拿不到文件路径", error=True)
            return
        self.reload_keys()
        self.load_path(Path(chosen.path))

    def load_path(self, path: Path | str) -> bool:
        try:
            doc = MidiDocument.from_path(path)
        except (OSError, midi_format.MidiError) as exc:
            self._snack(f"打开失败：{exc}", error=True)
            return False
        self.path = Path(path)
        self._set_doc(doc)
        self._snack(
            f"已载入 {self.path.name}：{len(doc.notes)} 个音 · {doc.duration_seconds:.1f} 秒"
        )
        return True

    async def _on_demo(self, _event=None) -> None:
        self.path = None
        doc = demo_document()
        if self.DEMO_PATH.exists():
            try:
                doc = MidiDocument.from_path(self.DEMO_PATH)
                self.path = self.DEMO_PATH
            except (OSError, midi_format.MidiError):
                doc = demo_document()
                self.path = None
        self._set_doc(doc)

    def _set_doc(self, doc: MidiDocument) -> None:
        self.stop()
        self.doc = doc
        # 不自动移调：Capo 由用户自己选，换谱时回到 0
        self.capo = 0
        self._dd_capo.value = "0"
        self._progress.value = "就绪，点「播放」开始发送按键"
        self._warning.visible = False
        self._render_all()

    # ------------------------------------------------------------------ 播放

    def _tap_seconds(self) -> float:
        try:
            milliseconds = int(self._f_tap.value or 100)
        except (TypeError, ValueError):
            milliseconds = 100
        return max(10, min(2000, milliseconds)) / 1000.0

    async def _on_play(self, _event=None) -> None:
        if self._playing or self.doc is None:
            if self.doc is None:
                self._snack("先打开或载入一个 MIDI 文件", error=True)
            return
        if not self.backend.available:
            self._snack(f"这台机器不能发送键盘按键：{self.backend.error}", error=True)
            return
        try:
            schedule = build_key_schedule(
                self.doc,
                self.table,
                transpose=self.capo,
                tap_notes=bool(self._sw_tap.value),
                tap_seconds=self._tap_seconds(),
                project_octave=self.project_octave,
            )
        except (KeyTableError, ValueError) as exc:
            self._snack(f"键位表有问题：{exc}", error=True)
            return
        if not schedule.actions:
            self._snack(
                "没有可发送的按键：音符都超出了键位窗口，先调 Capo，"
                "或到设置页打开「八度投影」",
                error=True,
            )
            return

        if self.page is None:
            # 离线构建/测试：只排期，不真的发按键
            self._progress.value = f"已排期 {schedule.note_count} 个按键（离线，不发送）"
            self._playing = False
            self._update()
            return

        self._playing = True
        self._pressed = 0
        self._total_notes = schedule.note_count
        self._drifts.clear()
        self._log_column.controls = []
        self._follow_offset = 0.0
        if schedule.projected or schedule.skipped:
            messages: list[str] = []
            if schedule.projected:
                names = "、".join(schedule.projected[:8])
                more = "…" if len(schedule.projected) > 8 else ""
                messages.append(
                    f"♫ {len(schedule.projected)} 个音超出键位窗口，已按八度投影：{names}{more}"
                )
            if schedule.skipped:
                names = "、".join(schedule.skipped[:8])
                more = "…" if len(schedule.skipped) > 8 else ""
                messages.append(
                    f"⚠ {len(schedule.skipped)} 个音超出键位窗口，已跳过：{names}{more}"
                    "（调 Capo，或在设置页打开八度投影）"
                )
            self._warning.value = "；".join(messages)
            self._warning.visible = True
        else:
            self._warning.visible = False
        self._progress.value = f"发送中… 0/{self._total_notes} 个按键"
        self._update()
        self.page.run_task(self._play_loop, schedule.actions)

    async def _play_loop(self, actions: list[KeyAction]) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        last_ui = 0.0
        try:
            for action in actions:
                if not self._playing:
                    break
                due = start + action.time
                while self._playing:
                    remaining = due - loop.time()
                    if remaining <= 0:
                        break
                    if remaining > 0.004:
                        await asyncio.sleep(min(remaining - 0.003, 0.01))
                    else:
                        while loop.time() < due:
                            pass
                        break
                if not self._playing:
                    break

                drift_ms = (loop.time() - due) * 1000.0
                if action.kind == "press":
                    self.backend.press(action.key)
                else:
                    self.backend.release(action.key)

                if action.unit:
                    self._pressed += 1
                    self._drifts.append(drift_ms)
                    self._append_log(action)
                    self._refresh_progress()

                now = loop.time()
                if now - last_ui >= 0.06:
                    last_ui = now
                    self._set_playhead_seconds(max(0.0, now - start - LEAD_IN))
                    await self._follow_playhead()
                    self._refresh_progress()
                    self._update()
        except Exception as exc:  # noqa: BLE001
            self._snack(f"发送中断：{exc}", error=True)
        finally:
            self._playing = False
            try:
                self.backend.release_all()
            except Exception:  # noqa: BLE001
                pass
            self._playhead.visible = False
            self._refresh_progress(finished=True)
            self._update()

    def stop(self) -> None:
        """停止发送并松开所有键（切页时也会调）。"""
        was_playing = self._playing
        self._playing = False
        try:
            self.backend.release_all()
        except Exception:  # noqa: BLE001
            pass
        self._playhead.visible = False
        if was_playing:
            self._progress.value = "已停止"
            self._update()

    def _append_log(self, action: KeyAction) -> None:
        beat = self.doc.beats_at(action.tick) if self.doc is not None else 0.0
        self._log_column.controls = [
            ft.Text(
                f"第 {beat:5.2f} 拍   {action.name} → 键 {key_label(action.key)}",
                size=11,
                color=MUTED,
            )
        ] + list(self._log_column.controls[:4])

    def _refresh_progress(self, finished: bool = False) -> None:
        average = sum(self._drifts) / len(self._drifts) if self._drifts else 0.0
        if finished:
            worst = max(self._drifts, default=0.0)
            self._progress.value = (
                f"结束：{self._pressed}/{self._total_notes} 个按键 · "
                f"平均误差 {average:.1f} ms · 最大 {worst:.1f} ms"
            )
        else:
            self._progress.value = (
                f"发送中… {self._pressed}/{self._total_notes} 个按键 · "
                f"当前误差 {average:.1f} ms"
            )

    def _set_playhead_seconds(self, score_seconds: float) -> None:
        """把红色游标放到 ``score_seconds`` 对应的拍位置。"""
        if self.doc is None:
            return
        tick = self.doc.tick_at_seconds(score_seconds)
        left = self.doc.beats_at(tick) * PX_PER_BEAT
        width = self._timeline_width or 0.0
        self._playhead.left = max(0.0, min(left, max(0.0, width - 3)))
        self._playhead.top = RULER_HEIGHT - 2
        self._playhead.height = len(_LANES) * LANE_HEIGHT + 2
        self._playhead.visible = True

    async def _scroll_timeline(self, offset: float, duration: int) -> None:
        if self._timeline_scroll is None:
            return
        try:
            await asyncio.wait_for(
                self._timeline_scroll.scroll_to(offset=offset, duration=duration),
                timeout=1.0,
            )
        except Exception:  # noqa: BLE001 - 滚动失败不该中断播放
            pass

    async def _follow_playhead(self) -> None:
        """长谱播放时把横向滚动带到游标附近。"""
        left = float(self._playhead.left or 0.0)
        if left <= self._follow_offset + 420:
            return
        target = max(0.0, left - 240)
        await self._scroll_timeline(target, 120)
        self._follow_offset = target

    # ------------------------------------------------------------------ 热键

    def set_active(self, active: bool) -> None:
        """进入/离开本页时注册或注销 F9/F10（由 ``app.py`` 调用）。"""
        if active == self._active:
            return
        self._active = active
        if active:
            self._hotkeys.start()
        else:
            self._hotkeys.close()
        self._refresh_hotkey_text()

    def _refresh_hotkey_text(self) -> None:
        play = hotkey_label(HOTKEY_PLAY)
        stop = hotkey_label(HOTKEY_STOP)
        if process_integrity() == "low":
            self._hotkey_text.value = (
                "⚠ 当前以低权限运行（Agent/沙盒启动）：游戏权限更高时，"
                "全局热键和发送按键都会失效。请用 run.bat 重新启动"
            )
            self._hotkey_text.color = ft.Colors.RED_400
            self._update()
            return
        if not self._active:
            self._hotkey_text.value = f"全局快捷键：进入本页后生效（{play} 播放 / {stop} 停止）"
            self._hotkey_text.color = MUTED
        elif self._hotkeys.failed:
            names = "、".join(hotkey_label(vk) for vk in self._hotkeys.failed)
            self._hotkey_text.value = f"⚠ 全局快捷键 {names} 被占用；关掉冲突软件后重新进入本页"
            self._hotkey_text.color = ft.Colors.RED_400
        elif self._hotkeys.available:
            self._hotkey_text.value = (
                f"全局快捷键：{play} 播放 · {stop} 停止并松键（游戏保持焦点也能用）"
            )
            self._hotkey_text.color = MUTED
        else:
            self._hotkey_text.value = f"全局快捷键不可用：{self._hotkeys.error or '注册失败'}"
            self._hotkey_text.color = ft.Colors.ORANGE_700
        self._update()

    def _on_hotkey_play(self) -> None:
        if not self._active or self._playing or self.page is None or self.doc is None:
            return
        self.page.run_task(self._on_play)

    def _on_hotkey_stop(self) -> None:
        if self.page is None:
            return

        async def _stop() -> None:
            self.stop()

        self.page.run_task(_stop)

    # ------------------------------------------------------------------ 设置 / 辅助

    def apply_settings(self, update: bool = True) -> None:
        """设置页改了之后由主壳调用：切换八度投影并重画。

        只改投影，**不碰 Capo**——Capo 是用户手动选的，切页不该把它冲掉。
        """
        value = bool(get_settings().midi_project_octave)
        changed = value != self.project_octave
        self.project_octave = value
        self._refresh_project_hint()
        if changed and self.doc is not None:
            self._render_all()
        if update:
            self._update()

    def _on_capo_change(self, _event=None) -> None:
        try:
            value = int(str(self._dd_capo.value or "0").strip() or 0)
        except ValueError:
            return
        value = max(0, min(CAPO_MAX, value))
        if str(self._dd_capo.value).strip() != str(value):
            self._dd_capo.value = str(value)
        if value == self.capo:
            return
        self.capo = value
        self._render_keys()
        self._render_timeline()
        self._update()

    def _on_reload_keys(self, _event=None) -> None:
        self.reload_keys()
        if self.table_error:
            self._snack(f"键位表读不出来：{self.table_error}", error=True)
        else:
            self._snack(f"已重载键位表：{self.table.span} 个键（{note_name(self.table.base_note)} 起）")

    def reload_keys(self) -> None:
        reload_key_table()
        self._read_table()
        if self.doc is not None:
            self._render_all()

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

    def _update(self) -> None:
        if self.page is not None:
            self.page.update()
