"""设置页：两个全局开关 + 版本号，改完立即写盘并通知各页面刷新。

- **全局应用事件 CRC**：gtab / gct 保存时一律带事件 CRC（两页已没有单独的
  「事件 CRC」开关，这个开关就是唯一的控制点）；
- **MIDI 音域超出后按八度投影**：MIDI 映射页遇到键位窗口外的音时，按 ±12 半音
  移到窗口里再发，而不是跳过并报错。

设置本体在 ``ui/settings.py``（``AppSettings`` + ``src/settings.json``）；
本页只负责界面，不碰文档模型。主壳（``src/app.py``）用 ``on_change`` 把变更
转发给 MIDI 页。
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from ui.components import MUTED, card
from ui.settings import (
    SETTINGS_FILE,
    AppSettings,
    get_settings,
    update_settings,
)
from version import VERSION

__all__ = ["SettingsPage"]


class SettingsPage:
    TITLE = "设置"
    SUBTITLE = "全局偏好；改完立即生效，并写进 src/settings.json"

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        #: 设置变化后的回调（主壳把它接到 gtab / gct / midi 三个页面）
        self.on_change: Callable[[AppSettings], None] | None = None
        self._status = ft.Text("", size=11)
        self._version_text = ft.Text(f"版本 {VERSION}", size=11, color=MUTED)
        self._path_text = ft.Text(
            f"配置文件：{SETTINGS_FILE}（本地偏好，不进版本库）",
            size=11,
            color=MUTED,
        )
        self._sw_crc = ft.Switch(
            label="全局应用事件 CRC",
            value=False,
            on_change=self._on_crc_change,
            tooltip="开：gtab / gct 保存时一律写入事件 CRC（这是唯一控制点）",
        )
        self._sw_project = ft.Switch(
            label="MIDI 音域超出后按八度投影",
            value=False,
            on_change=self._on_project_change,
            tooltip="开：窗口外的音 ±12 半音移到窗口内再发；关：跳过并报出音名",
        )
        self._root = self._build()
        self.refresh()

    def build(self) -> ft.Control:
        return self._root

    # ------------------------------------------------------------------ 组合

    def _build(self) -> ft.Control:
        crc_card = card(
            "事件 CRC",
            ft.Column(
                [
                    self._sw_crc,
                    ft.Text(
                        "打开后，GTAB / GCT 两个编辑页保存时都会在每个事件后面写入 "
                        "CRC32（文件头的 flags bit0 置 1）。关掉时不强制：打开的文件"
                        "原本带 CRC 就继续带，新建或原本不带的不加。",
                        size=11,
                        color=MUTED,
                    ),
                ],
                spacing=6,
            ),
        )
        project_card = card(
            "MIDI 八度投影",
            ft.Column(
                [
                    self._sw_project,
                    ft.Text(
                        "键位表默认 E3–D#6（三个八度）。打开本项后，超出的音会被整体"
                        "移动若干个八度直到落进窗口（音名会变，节奏不变），适合音域比"
                        "三个八度宽一点的谱；关掉则保持默认行为：跳过该音并在节拍轴上"
                        "画灰块、播放时提示音名，用 MIDI 页的 Capo 把整首移进窗口。",
                        size=11,
                        color=MUTED,
                    ),
                    ft.Text(
                        "注意：MIDI 页的 Capo 是手动设定（0–12 品），不会自动移调；"
                        "载入新的 MIDI 时 Capo 回到 0。",
                        size=11,
                        color=MUTED,
                    ),
                ],
                spacing=6,
            ),
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
                            self._version_text,
                            ft.TextButton(
                                "恢复默认",
                                icon=ft.Icons.RESTART_ALT,
                                tooltip="两个开关都关掉，并立即写盘",
                                on_click=self._on_reset,
                            ),
                            self._status,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Column(
                        [crc_card, project_card, self._path_text],
                        spacing=12,
                        width=760,
                    ),
                ],
                spacing=12,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.START,
                scroll=ft.ScrollMode.AUTO,
            ),
        )

    # ------------------------------------------------------------------ 状态

    def refresh(self) -> None:
        """从 ``get_settings()`` 同步控件（不进回调，不会触发写盘）。"""
        settings = get_settings()
        self._sw_crc.value = bool(settings.global_event_crc)
        self._sw_project.value = bool(settings.midi_project_octave)

    def _saved_text(self) -> str:
        return "已保存"

    # ------------------------------------------------------------------ 回调

    def _on_crc_change(self, _event=None) -> None:
        self._apply(global_event_crc=bool(self._sw_crc.value))

    def _on_project_change(self, _event=None) -> None:
        self._apply(midi_project_octave=bool(self._sw_project.value))

    def _on_reset(self, _event=None) -> None:
        self._apply(global_event_crc=False, midi_project_octave=False)

    def _apply(self, **changes) -> None:
        try:
            settings = update_settings(**changes)
        except OSError as exc:
            # 缓存已经更新（本次运行有效），只是没落盘：如实告诉用户
            settings = get_settings()
            self.refresh()
            self._status.value = f"⚠ 没写进设置文件：{exc}"
            self._status.color = ft.Colors.RED_400
            self._notify(settings)
            self._update()
            return
        self.refresh()
        self._status.value = self._saved_text()
        self._status.color = MUTED
        self._notify(settings)
        self._update()

    def _notify(self, settings: AppSettings) -> None:
        if callable(self.on_change):
            self.on_change(settings)

    def _update(self) -> None:
        if self.page is not None:
            self.page.update()
