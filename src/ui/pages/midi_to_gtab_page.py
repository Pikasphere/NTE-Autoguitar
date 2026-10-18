"""MIDI → GTAB 页：载入 MIDI，按「同时音只留最高、把位沿用上一个音」排成 .gtab。

页面只做三件事：选文件 / 看转换结果 / 保存或送到 GTAB 编辑页。
真正的排位算法在 :mod:`ui.midi_to_gtab`（纯逻辑，可单测），这里不重复实现。
"""

from __future__ import annotations

from pathlib import Path

import flet as ft

import midi as midi_format
from ui.components import MUTED, card
from ui.gtab_doc import GtabError
from ui.midi_doc import MidiDocument, demo_document
from ui.midi_to_gtab import (
    GTAB_DIVISION,
    ConversionResult,
    ConvertOptions,
    convert,
)
from ui.settings import force_event_crc

__all__ = ["MidiToGtabPage"]

_CELLS = (64, 64, 64, 40, 40, 64)  # 拍 / 音长 / 音名 / 弦 / 品 / 页


class MidiToGtabPage:
    TITLE = "MIDI→GTAB"
    SUBTITLE = "把 MIDI 排成可玩的 .gtab：同时音默认只留最高音，把位优先沿用上一个音"
    PREVIEW_LIMIT = 300

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.doc: MidiDocument | None = None
        self.path: Path | None = None
        self.result: ConversionResult | None = None
        self.keep_top_only = True
        self.skip_percussion = True
        # 由 app.py 接到 GtabPage：转换结果直接进编辑页（不落盘）
        self.on_send = None
        self._picker: ft.FilePicker | None = ft.FilePicker() if page is not None else None

        self._root = self._build()
        self._render_all()

    # ------------------------------------------------------------------ 组合

    def build(self) -> ft.Control:
        return self._root

    def _build(self) -> ft.Control:
        self._file_label = ft.Text("未载入 MIDI", size=12, color=MUTED)
        self._status_text = ft.Text("", size=12)
        self._info_text = ft.Text("", size=12, selectable=True)
        self._stats_text = ft.Text("", size=12, selectable=True)
        self._warning = ft.Text("", size=11, color=ft.Colors.ORANGE_300, visible=False)
        self._preview_hint = ft.Text("", size=11, color=MUTED)

        self._sw_top = ft.Switch(
            label="同时起音只保留最高音",
            value=True,
            on_change=self._on_option_change,
            tooltip="开：一个 tick 上同时按下的多个音只弹最高的那个；"
            "关：全部保留（最多 6 个，一条弦一个）",
        )
        self._sw_percussion = ft.Switch(
            label="跳过打击乐（通道 10）",
            value=True,
            on_change=self._on_option_change,
            tooltip="GM 的 MIDI 通道 10 是鼓；鼓点转成吉他音没有意义。"
            "如果你的文件把旋律写在通道 10，可以关掉",
        )

        self._btn_save = ft.Button(
            "保存 .gtab",
            icon=ft.Icons.SAVE,
            disabled=True,
            on_click=self._on_save,
        )
        self._btn_send = ft.Button(
            "送到 GTAB 编辑页",
            icon=ft.Icons.OPEN_IN_NEW,
            disabled=True,
            on_click=self._on_send,
        )

        self._preview_list = ft.ListView(expand=True, spacing=0, padding=0)

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
                                    ft.Text(
                                        self.TITLE, size=22, weight=ft.FontWeight.BOLD
                                    ),
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
                    ft.Text("转换", size=13, weight=ft.FontWeight.BOLD),
                    self._status_text,
                    ft.Container(width=8),
                    ft.Button(
                        "打开 MIDI",
                        icon=ft.Icons.FOLDER_OPEN,
                        on_click=self._on_open,
                    ),
                    ft.TextButton(
                        "示例",
                        icon=ft.Icons.AUTO_AWESOME,
                        on_click=self._on_demo,
                    ),
                    ft.VerticalDivider(width=1),
                    self._btn_send,
                    self._btn_save,
                ],
                spacing=8,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _left_panel(self) -> ft.Control:
        return ft.Column(
            [
                card("MIDI 信息", self._info_text),
                card(
                    "转换设置",
                    ft.Column(
                        [
                            self._sw_top,
                            ft.Text(
                                "关掉后同时起音会全部保留；一条弦最多一个音。",
                                size=11,
                                color=MUTED,
                            ),
                            self._sw_percussion,
                            ft.Text(
                                "默认跳过 MIDI 通道 10（鼓），其它通道照常转换。",
                                size=11,
                                color=MUTED,
                            ),
                            ft.Text(
                                "把位：优先沿用上一个音的页面（游戏开局在第 1 页，"
                                "第 5 页是空弦页）。",
                                size=11,
                                color=MUTED,
                            ),
                        ],
                        spacing=6,
                    ),
                ),
                card("转换统计", self._stats_text),
                self._warning,
            ],
            width=360,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _right_panel(self) -> ft.Control:
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
                            ft.Text("转换结果", size=14, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "一行一个音；「页」= 游戏里的把位页",
                                size=11,
                                color=MUTED,
                            ),
                            ft.Container(expand=True),
                            self._preview_hint,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._header_row(),
                    ft.Divider(height=1),
                    self._preview_list,
                ],
                spacing=8,
                expand=True,
            ),
        )

    def _cell(self, text: object, width: int, bold: bool = False) -> ft.Control:
        return ft.Container(
            width=width,
            content=ft.Text(
                str(text),
                size=11,
                weight=ft.FontWeight.BOLD if bold else None,
                color=MUTED if bold else None,
            ),
        )

    def _header_row(self) -> ft.Control:
        labels = ("拍", "音长", "音名", "弦", "品", "页")
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=2, horizontal=6),
            content=ft.Row(
                [self._cell(label, width, bold=True) for label, width in zip(labels, _CELLS)],
                spacing=4,
            ),
        )

    # ------------------------------------------------------------------ 载入 / 转换

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
        try:
            doc = MidiDocument.from_path(chosen.path)
        except (OSError, midi_format.MidiError) as exc:
            self._snack(f"打开失败：{exc}", error=True)
            return
        self.load_document(doc, Path(chosen.path))
        self._snack(f"已载入 {Path(chosen.path).name}：{len(doc.notes)} 个音")

    def _on_demo(self, _event=None) -> None:
        self.load_document(demo_document())
        self._snack("已载入内存示例（音阶 + 和弦）")

    def load_document(self, doc: MidiDocument, path: Path | str | None = None) -> None:
        """换一份 MIDI 并立刻转换（path=None 表示内存示例）。"""
        self.doc = doc
        self.path = Path(path) if path is not None else None
        self._convert()

    def _convert(self) -> None:
        if self.doc is None:
            self.result = None
            self._render_all()
            return
        try:
            self.result = convert(
                self.doc,
                ConvertOptions(
                    keep_top_only=self.keep_top_only,
                    skip_percussion=self.skip_percussion,
                ),
            )
        except (ValueError, TypeError, GtabError) as exc:
            self.result = None
            self._snack(f"转换失败：{exc}", error=True)
        self._render_all()

    def _on_option_change(self, _event=None) -> None:
        self.keep_top_only = bool(self._sw_top.value)
        self.skip_percussion = bool(self._sw_percussion.value)
        self._convert()

    # ------------------------------------------------------------------ 保存 / 送出

    async def _on_save(self, _event=None) -> None:
        if self.result is None or not self.result.notes:
            self._snack("还没有可保存的转换结果", error=True)
            return
        try:
            # 与 GTAB 编辑页一致：保存时按设置页的全局开关强制事件 CRC
            force_event_crc(self.result.doc)
            blob = self.result.doc.to_bytes()
        except (GtabError, ValueError) as exc:
            self._snack(f"生成 gtab 失败：{exc}", error=True)
            return

        picker = self._ensure_picker()
        try:
            target = await picker.save_file(
                dialog_title="保存为 gtab",
                file_name=f"{self._suggested_stem()}.gtab",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gtab"],
                src_bytes=blob,
            )
        except Exception as exc:  # noqa: BLE001
            self._snack(f"无法打开保存对话框：{exc}", error=True)
            return
        if not target:
            return
        self._snack(f"已保存 {Path(target).name}（{len(blob)} 字节）")

    def _on_send(self, _event=None) -> None:
        if self.result is None or not self.result.notes:
            self._snack("还没有可送出的转换结果", error=True)
            return
        if self.on_send is None:
            self._snack("没有接上 GTAB 编辑页", error=True)
            return
        self.on_send(self.result.doc)
        self._snack(f"已送到 GTAB 编辑页：{len(self.result.notes)} 个音（还没保存）")

    def _suggested_stem(self) -> str:
        if self.path is not None:
            raw = self.path.stem
        elif self.doc is not None and self.doc.name.strip():
            raw = self.doc.name.strip()
        else:
            raw = "midi"
        safe = "".join(ch for ch in raw if ch not in '\\/:*?"<>|').strip()
        return safe or "midi"

    # ------------------------------------------------------------------ 渲染

    def _render_all(self) -> None:
        self._render_info()
        self._render_stats()
        self._render_preview()

        has_notes = bool(self.result and self.result.notes)
        self._btn_save.disabled = not has_notes
        self._btn_send.disabled = not has_notes

        if self.doc is None:
            self._status_text.value = "未载入 MIDI"
        elif self.result is not None:
            self._status_text.value = (
                f"源 {self.result.stats.source_notes} 个音 → "
                f"输出 {self.result.stats.output_notes} 个音"
            )
        else:
            self._status_text.value = "转换失败"
        self._update()

    def _render_info(self) -> None:
        if self.doc is None:
            self._file_label.value = "未载入 MIDI"
            self._info_text.value = (
                "还没有载入 MIDI 文件。\n点上方「打开 MIDI」，或先点「示例」看效果。"
            )
            return

        name = self.path.name if self.path is not None else (self.doc.name or "内存示例")
        lines = [
            f"文件：{name}",
            f"音符：{len(self.doc.notes)} 个 · {self.doc.tracks} 轨 · "
            f"{self.doc.division} tick/拍",
            f"时长：{self.doc.duration_beats:.1f} 拍",
            f"Tempo：{len(self.doc.tempos)} 处",
        ]
        if self.doc.unclosed:
            lines.append(f"⚠ {self.doc.unclosed} 个音没有 NoteOff，已在曲尾收掉")
        self._file_label.value = name
        self._info_text.value = "\n".join(lines)

    def _render_stats(self) -> None:
        if self.result is None:
            self._stats_text.value = "载入 MIDI 后自动转换。"
            self._warning.visible = False
            return

        stats = self.result.stats
        lines = [
            f"源音符：{stats.source_notes} 个",
            f"输出：{stats.output_notes} 个",
        ]
        if stats.dropped_simultaneous:
            lines.append(f"省略同时音：{stats.dropped_simultaneous} 个（只留最高）")
        if stats.skipped_out_of_range:
            lines.append(f"超范围跳过：{stats.skipped_out_of_range} 个")
        if stats.skipped_percussion:
            lines.append(f"打击乐跳过：{stats.skipped_percussion} 个")
        if stats.dropped_no_position:
            lines.append(f"无可用位置：{stats.dropped_no_position} 个")
        if stats.trimmed_overlaps:
            lines.append(f"同键截断：{stats.trimmed_overlaps} 个")
        if stats.pages_used:
            pages = " / ".join(f"第 {page} 页" for page in stats.pages_used)
            lines.append(
                f"翻页：{stats.page_changes} 次 · 摇杆共推 {stats.page_steps} 次"
            )
            lines.append(f"用到的页面：{pages}")
        else:
            lines.append("翻页：0 次")
        self._stats_text.value = "\n".join(lines)

        warnings = list(self.result.warnings)
        self._warning.value = "\n".join(warnings)
        self._warning.visible = bool(warnings)

    def _render_preview(self) -> None:
        notes = self.result.notes if self.result is not None else []
        rows: list[ft.Control] = []
        for index, note in enumerate(notes[: self.PREVIEW_LIMIT]):
            fret = "空" if note.fret <= 0 else str(note.fret)
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=4, horizontal=6),
                    bgcolor=(
                        ft.Colors.with_opacity(0.03, ft.Colors.ON_SURFACE)
                        if index % 2
                        else None
                    ),
                    content=ft.Row(
                        [
                            self._cell(f"{note.start / GTAB_DIVISION:.2f}", _CELLS[0]),
                            self._cell(f"{note.duration / GTAB_DIVISION:.2f}", _CELLS[1]),
                            self._cell(note.name, _CELLS[2]),
                            self._cell(note.string, _CELLS[3]),
                            self._cell(fret, _CELLS[4]),
                            self._cell(f"第 {note.page} 页", _CELLS[5]),
                        ],
                        spacing=4,
                    ),
                )
            )

        if not rows:
            rows.append(
                ft.Container(
                    padding=16,
                    content=ft.Text(
                        "还没有转换结果。" if self.doc is not None else "先载入一个 MIDI 文件。",
                        size=12,
                        color=MUTED,
                    ),
                )
            )
        self._preview_list.controls = rows

        if len(notes) > self.PREVIEW_LIMIT:
            self._preview_hint.value = (
                f"只显示前 {self.PREVIEW_LIMIT} / 共 {len(notes)} 个音"
            )
        else:
            self._preview_hint.value = f"共 {len(notes)} 个音" if notes else ""

    # ------------------------------------------------------------------ 辅助

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
