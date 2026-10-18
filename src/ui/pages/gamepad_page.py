"""手柄映射页 —— 把 gtab / gct 谱面按规则映射成虚拟手柄（vgamepad）输入并回放。

映射规则（已按需求实现，见 ``ui/gamepad_backend.py``）：

- 弦：``X``=6 弦、``A``=5 弦、十字键上/右/左/下 = 4/3/2/1 弦；
- 品：左摇杆以「上」为 1 品、顺时针每 60° 一个品，一页 6 品；
- 翻页：``右摇杆向左`` 下一页（7-12 品…），向右 上一页；
- 第 5 页：六个方向都是空弦。

**连接是手动的**：必须先点「连接手柄」，程序不会在播放时偷偷连上——
免得用户在别的窗口里忽然多出一个 Xbox 360 手柄。

页面左侧是「回放轮盘」：六个扇区显示当前页的品、六根弦显示正在按哪根；
右侧是谱面回放，回放结束会给平均/最大时序误差，用来判断这条路是否可用于实际演奏。
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import flet as ft

from gtab import EV_STRING_NOTE_OFF, EV_STRING_NOTE_ON
from ui.chords import ChordError, load_chord_names
from ui.components import MUTED, card
from ui.gamepad_backend import (
    AIM_GAP,
    OPEN_PAGE,
    PAGE_SIZE,
    REPLUCK_GAP,
    GamepadBackend,
    ScheduleAction,
    build_schedule,
)
from ui.gct_backend import build_chord_schedule
from ui.gct_doc import GctDocument, GctError, demo_document
from ui.gtab_doc import EventRow, GtabDocument, GtabError
from ui.hotkeys import (
    HOTKEY_PLAY,
    HOTKEY_STOP,
    GlobalHotkeys,
    hotkey_label,
    process_integrity,
)
from ui.replay_view import ReplayWheel

__all__ = ["GamepadPage"]


class GamepadPage:
    TITLE = "手柄映射"
    SUBTITLE = "gtab / gct 谱面 → 虚拟手柄（vgamepad）的映射与时序"
    DEMO_PATH = Path(__file__).resolve().parents[2] / "MusicList" / "gtabfile" / "demo.gtab"
    GCT_DEMO_PATH = (
        Path(__file__).resolve().parents[2] / "MusicList" / "gctfile" / "demo.gct"
    )

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.backend = GamepadBackend()
        # kind = "gtab"（单音谱，doc）或 "gct"（和弦谱，gct_doc）
        self.kind = "gtab"
        self.doc: GtabDocument | None = None
        self.gct_doc: GctDocument | None = None
        self.path: Path | None = None
        self._picker: ft.FilePicker | None = ft.FilePicker() if page is not None else None

        self._playing = False
        self._pressed = 0
        self._total_notes = 0
        self._unit_label = "音符"
        self._drifts: list[float] = []
        self._log: deque[str] = deque(maxlen=5)
        self._wheel = ReplayWheel()

        # 全局热键只在「手柄映射」页激活时注册，离开就注销
        self._active = False
        self._hotkeys = GlobalHotkeys(
            {
                HOTKEY_PLAY: self._on_hotkey_play,
                HOTKEY_STOP: self._on_hotkey_stop,
            }
        )

        self._root = self._build()
        self.refresh_status()

    # ------------------------------------------------------------------ 组合

    def build(self) -> ft.Control:
        return self._root

    def _build(self) -> ft.Control:
        self._status_text = ft.Text("", size=12)
        self._file_text = ft.Text("未载入谱面", size=12, color=MUTED)
        self._progress_text = ft.Text("", size=12, color=MUTED)
        self._conflict_text = ft.Text("", size=11, color=ft.Colors.ORANGE_700)
        self._hotkey_text = ft.Text(
            "全局快捷键：进入本页后生效", size=11, color=MUTED
        )
        self._log_column = ft.Column(spacing=2)
        self._tap_switch = ft.Switch(label="音符点按 80ms", value=True, on_change=lambda e: None)
        # 不要用 TextField.label 写「重拨ms」这种长标签：宽度只有 66/72，
        # 浮动标签会被挤成两行并压住边框（docs/gamepad_page.png 逐像素复核时发现）。
        # 单位用行内的 Text("ms") 表示，字段左边已有「同弦重拨」「摇杆/翻页→按键」说明。
        self._gap_field = ft.TextField(
            value=str(round(REPLUCK_GAP * 1000)),
            width=66,
            dense=True,
            text_size=12,
            text_align=ft.TextAlign.CENTER,
            input_filter=ft.NumbersOnlyInputFilter(),
            tooltip="同一根弦松开多久再按；第二次没声就调大（最大 200ms）",
        )
        self._aim_field = ft.TextField(
            value=str(round(AIM_GAP * 1000)),
            width=72,
            dense=True,
            text_size=12,
            text_align=ft.TextAlign.CENTER,
            input_filter=ft.NumbersOnlyInputFilter(),
            tooltip=(
                "摇杆/翻页到位后隔多久再按弦。\n"
                "同一时刻切摇杆 + 按弦时，游戏可能先吃按键、弹成上一个品；\n"
                "默认 65ms（2026-10-18 真机实测稳定），弹错品就调大（最大 300ms）。"
            ),
        )

        body = ft.Row(
            [self._wheel_card(), self._play_card()],
            expand=True,
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.START,
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
                            self._file_text,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._status_card(),
                    body,
                ],
                spacing=12,
                expand=True,
            ),
        )

    def _status_card(self) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=12, horizontal=16),
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("环境", size=13, weight=ft.FontWeight.BOLD),
                            self._status_text,
                            ft.Container(expand=True),
                            ft.Button("刷新", icon=ft.Icons.REFRESH, on_click=lambda e: self.refresh_status()),
                            ft.Button(
                                "连接手柄",
                                icon=ft.Icons.USB,
                                tooltip="手动连接（Xbox 360）。播放前必须先连上；程序不会自动连",
                                on_click=self._on_connect,
                            ),
                            ft.Button("断开", icon=ft.Icons.LINK_OFF, on_click=self._on_disconnect),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "连接后可在「设置 → 蓝牙和其他设备 → 设备和打印机」里看到 "
                        "Xbox 360 Controller，或用 joy.cpl 打开「游戏控制器」测试。",
                        size=11,
                        color=MUTED,
                    ),
                ],
                spacing=6,
            ),
        )

    def _wheel_card(self) -> ft.Control:
        """左侧：回放轮盘（本来这里是手动按键测试面板）。

        用户要求用轮盘显示回放正在发的摇杆方向和弦，所以手动按钮不再占用左半边；
        需要手动按键时仍可从「播放」跑示例谱验证。
        """
        return ft.Container(
            width=400,
            content=card("回放轮盘", self._wheel.build()),
        )

    def _play_card(self) -> ft.Control:
        rules = (
            f"规则：X/A/上/右/左/下 = 6/5/4/3/2/1 弦；"
            f"摇杆从「上」顺时针每 60° 一个品（每页 {PAGE_SIZE} 品）；"
            f"右摇杆向左 下一页（7-12 品…）、向右 上一页；第 {OPEN_PAGE} 页 = 空弦。"
            "按下即锁定这个音，按住期间切摇杆/翻页不会改变它；"
            "时间重叠的长音会保持按住到 NoteOff（点按开关对它们不生效），"
            "同一根弦要重拨时会先松开再按；"
            "摇杆/翻页会提前到位，隔一点时间再按弦/扫弦（两个间隔都可在下方调）。"
            "和弦谱（.gct）：和弦按 chord.json 的顺序排在 4 页 × 6 个方向上，"
            "扫弦 = 右摇杆向下（下扫）/ 向上（上扫），单弦拨奏只按一根弦。"
        )
        return ft.Container(
            expand=True,
            content=card(
                "谱面回放",
                ft.Column(
                    [
                        # 主工具栏只放动作按钮，并且 wrap=True：窗口带左侧导航栏后内容只有
                        # ~1140 逻辑像素（最小 1020 时更窄），不能靠 expand 占位把「停止」推到
                        # 右边——那样窄窗口下「停止」会被裁掉（实测 1430 截图右侧就缺一块）。
                        ft.Row(
                            [
                                ft.Button("打开谱面", icon=ft.Icons.FOLDER_OPEN, on_click=self._on_open),
                                ft.TextButton("示例 gtab", icon=ft.Icons.AUTO_AWESOME, on_click=self._on_demo),
                                ft.TextButton("示例 gct", icon=ft.Icons.QUEUE_MUSIC, on_click=self._on_demo_gct),
                                ft.TextButton("同弦重拨", icon=ft.Icons.REPEAT, on_click=self._on_same_string_test),
                                ft.Button("播放", icon=ft.Icons.PLAY_ARROW, on_click=self._on_play),
                                ft.Button("停止", icon=ft.Icons.STOP, on_click=lambda e: self.stop()),
                            ],
                            spacing=8,
                            run_spacing=8,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Text(rules, size=11, color=MUTED),
                        ft.Row(
                            [
                                ft.Text("同弦重拨", size=11, color=MUTED),
                                self._gap_field,
                                ft.Text("ms", size=11, color=MUTED),
                                ft.Text("摇杆/翻页→按键", size=11, color=MUTED),
                                self._aim_field,
                                ft.Text(
                                    "ms（弹错品就把摇杆间隔调大）",
                                    size=11,
                                    color=MUTED,
                                ),
                                self._tap_switch,
                            ],
                            spacing=6,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        self._hotkey_text,
                        self._progress_text,
                        self._conflict_text,
                        ft.Divider(height=1),
                        ft.Text("触发记录", size=12, weight=ft.FontWeight.BOLD),
                        self._log_column,
                    ],
                    spacing=10,
                ),
            ),
        )

    # ------------------------------------------------------------------ 状态

    def refresh_status(self) -> None:
        status = self.backend.probe()
        if not status.installed:
            self._status_text.value = f"vgamepad：未安装（{status.error}）"
        else:
            driver = "已安装" if status.driver else "未检测到"
            pad = "已连接" if self.backend.connected else "未连接"
            self._status_text.value = (
                f"vgamepad {status.version or '?'} · ViGEmBus {driver} · 虚拟手柄 {pad}"
            )
        self._update_ui()

    def _on_connect(self, _event=None) -> None:
        try:
            self.backend.connect()
            self._snack("虚拟手柄已连接（Xbox 360）")
        except Exception as exc:  # noqa: BLE001
            self._snack(f"连接失败：{exc}", error=True)
        self.refresh_status()

    def _on_disconnect(self, _event=None) -> None:
        self.backend.disconnect()
        self._snack("已断开虚拟手柄")
        self.refresh_status()

    # ------------------------------------------------------------------ 载入

    async def _on_open(self, _event=None) -> None:
        picker = self._ensure_picker()
        try:
            files = await picker.pick_files(
                dialog_title="打开 gtab / gct 谱面",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["gtab", "gct"],
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
        self.path = Path(chosen.path)
        self.load_path(self.path, chosen.name)

    def load_path(self, path: Path | str, name: str | None = None) -> bool:
        """按扩展名载入 .gtab 或 .gct；失败弹提示并返回 False。"""
        file = Path(path)
        label = name or file.name
        suffix = file.suffix.lower()
        try:
            if suffix == ".gct":
                self._set_gct_doc(GctDocument.from_path(file), label)
            else:
                self._set_doc(GtabDocument.from_path(file), label)
        except (OSError, GtabError, GctError) as exc:
            self._snack(f"打开失败：{exc}", error=True)
            return False
        return True

    async def _on_demo(self, _event=None) -> None:
        if not self.DEMO_PATH.exists():
            self._snack("找不到示例文件 demo.gtab", error=True)
            return
        try:
            doc = GtabDocument.from_path(self.DEMO_PATH)
        except (OSError, GtabError) as exc:
            self._snack(f"示例载入失败：{exc}", error=True)
            return
        self.path = self.DEMO_PATH
        self._set_doc(doc, self.DEMO_PATH.name)

    async def _on_demo_gct(self, _event=None) -> None:
        """和弦示例：优先读 src/MusicList/gctfile/demo.gct，没有就用内存里生成的那张。"""
        if self.GCT_DEMO_PATH.exists():
            try:
                doc = GctDocument.from_path(self.GCT_DEMO_PATH)
            except (OSError, GctError) as exc:
                self._snack(f"和弦示例载入失败：{exc}", error=True)
                return
            self.path = self.GCT_DEMO_PATH
            self._set_gct_doc(doc, self.GCT_DEMO_PATH.name)
            return
        self.path = None
        self._set_gct_doc(demo_document(), "和弦示例（内存）")

    def _on_same_string_test(self, _event=None) -> None:
        """内存里生成「同一根弦重叠、必须松开重拨」的测试谱。

        6 弦 1 品 0–2 拍、6 弦 3 品 1–3 拍：排期会在 1.0s 前按 `REPLUCK_GAP`
        （默认 40ms）松开 X，再按 X 到 3 品。用来确认游戏能识别这个重拨边沿。
        """
        doc = GtabDocument(default_tempo_us=500_000)  # 120 BPM，1 拍 = 0.5s
        doc.events = [
            EventRow(tick=0, event_type=EV_STRING_NOTE_ON, data=bytes([6, 1])),
            EventRow(tick=480, event_type=EV_STRING_NOTE_ON, data=bytes([6, 3])),
            EventRow(tick=960, event_type=EV_STRING_NOTE_OFF, data=bytes([6, 1])),
            EventRow(tick=1440, event_type=EV_STRING_NOTE_OFF, data=bytes([6, 3])),
        ]
        self.path = None
        self._set_doc(doc, "同弦重拨测试（1 品 → 3 品）")

    def _set_doc(self, doc: GtabDocument, name: str) -> None:
        self.kind = "gtab"
        self.doc = doc
        self.gct_doc = None
        self._file_text.value = (
            f"{name} · {len(doc.events)} 事件 · {doc.duration_beats:.2f} 拍 · "
            f"{len(doc.note_blocks())} 个音符"
        )
        self._progress_text.value = "就绪，点「播放」开始回放"
        self._conflict_text.value = ""
        self._wheel.set_chord_mode(False)
        self._wheel.reset()
        self._update_ui()

    def _set_gct_doc(self, doc: GctDocument, name: str) -> None:
        self.kind = "gct"
        self.gct_doc = doc
        self.doc = None
        names: tuple[str, ...] = ()
        try:
            names = load_chord_names()
        except ChordError:
            names = ()
        self._file_text.value = (
            f"{name} · {len(doc.events)} 事件 · {doc.duration_beats:.2f} 拍 · "
            f"{len(doc.chord_events())} 个和弦事件"
        )
        self._progress_text.value = "就绪，点「播放」开始回放和弦"
        self._conflict_text.value = ""
        self._wheel.set_chord_mode(True, names)
        self._wheel.reset()
        self._update_ui()

    # ------------------------------------------------------------------ 回放

    async def _on_play(self, _event=None) -> None:
        if self._playing:
            return
        if self.kind == "gct":
            if self.gct_doc is None:
                self._snack("先打开或载入一个 gct 和弦谱", error=True)
                return
        elif self.doc is None:
            self._snack("先打开或载入一个 gtab 文件", error=True)
            return
        if not self.backend.connected:
            # 连接是手动的：不在播放时偷偷连手柄（免得别的窗口忽然多出一个 Xbox 手柄）
            self._snack("虚拟手柄还没连接：先点上方「连接手柄」", error=True)
            return

        try:
            gap_ms = int(self._gap_field.value or round(REPLUCK_GAP * 1000))
        except (TypeError, ValueError):
            gap_ms = round(REPLUCK_GAP * 1000)
        gap_ms = max(5, min(200, gap_ms))
        try:
            aim_ms = int(self._aim_field.value or round(AIM_GAP * 1000))
        except (TypeError, ValueError):
            aim_ms = round(AIM_GAP * 1000)
        aim_ms = max(0, min(300, aim_ms))
        if self.kind == "gct":
            try:
                schedule = build_chord_schedule(
                    self.gct_doc, aim_gap=aim_ms / 1000.0
                )
            except ChordError as exc:
                self._snack(f"和弦表有问题：{exc}", error=True)
                return
        else:
            schedule = build_schedule(
                self.doc,
                tap_notes=bool(self._tap_switch.value),
                repluck_gap=gap_ms / 1000.0,
                aim_gap=aim_ms / 1000.0,
            )
        if not schedule.actions:
            self._snack("这张谱面里没有可映射的内容", error=True)
            return

        self._playing = True
        self._pressed = 0
        self._total_notes = schedule.note_count
        self._unit_label = schedule.unit_label
        self._drifts.clear()
        self._log.clear()
        self._log_column.controls = []
        self._wheel.reset()
        parts: list[str] = []
        if schedule.conflicts:
            parts.append(
                f"⚠ {len(schedule.conflicts)} 处冲突："
                + "；".join(schedule.conflicts[:2])
            )
        if schedule.hints:
            parts.append(
                f"ℹ {len(schedule.hints)} 条提示：" + "；".join(schedule.hints[:2])
            )
        self._conflict_text.value = "　".join(parts)
        self._conflict_text.color = (
            ft.Colors.RED_400 if schedule.conflicts else MUTED
        )
        self._progress_text.value = f"回放中… 0/{self._total_notes}"
        self._update_ui()

        self.page.run_task(self._play_loop, schedule.actions)

    async def _play_loop(self, actions: list[ScheduleAction]) -> None:
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
                if action.kind == "stick":
                    if action.stick is not None:
                        self.backend.set_stick(*action.stick)
                elif action.kind in ("rstick", "page"):
                    # 右摇杆：扫弦（下/上）或翻页（左/右）
                    if action.stick is not None:
                        self.backend.set_right_stick(*action.stick)
                elif action.kind == "press":
                    self.backend.press(action.button)
                elif action.kind == "release":
                    self.backend.release(action.button)
                # light / dark 只驱动轮盘上的六根弦，不发手柄
                self.backend.update()
                # 轮盘跟着回放走：摇杆 → 扇区，按弦/扫弦 → 对应那根弦高亮
                self._wheel.apply(action)
                if action.unit:
                    # 一次扫弦（六根弦依次亮）只算一个和弦（gtab 里每个音都是 unit）
                    self._pressed += 1
                    self._drifts.append(drift_ms)
                    self._append_log(action)
                if action.kind in ("light", "dark"):
                    # 扫弦的弦是一根一根亮的（20ms 一根），不能等 60ms 的常规 UI 节流
                    if action.unit:
                        self._refresh_progress()
                    last_ui = loop.time()
                    self._update_ui()

                now = loop.time()
                if now - last_ui >= 0.06:
                    last_ui = now
                    self._refresh_progress()
                    self._update_ui()
        except Exception as exc:  # noqa: BLE001
            self._snack(f"回放中断：{exc}", error=True)
        finally:
            self._playing = False
            try:
                self.backend.release_all()
            except Exception:  # noqa: BLE001
                pass
            self._refresh_progress(finished=True)
            self._wheel.reset()
            self._update_ui()

    def stop(self) -> None:
        """停止回放并松开所有键（离开页面时也会调用）。"""
        if not self._playing:
            return
        self._playing = False
        try:
            self.backend.release_all()
        except Exception:  # noqa: BLE001
            pass
        self._progress_text.value = "已停止"
        self._wheel.reset()
        self._update_ui()

    # ------------------------------------------------------------------ 全局热键

    def set_active(self, active: bool) -> None:
        """进入/离开手柄页时注册或注销全局热键（由 ``app.py`` 调用）。"""
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
            # Agent/沙盒启动的进程是 Low integrity；Windows 不允许它接收更高权限游戏窗口的
            # 全局输入，表现就是「点一下本程序再用热键才行」。
            self._hotkey_text.value = (
                "⚠ 当前以低权限运行（Agent/沙盒启动）：游戏权限更高时，全局热键会失效。"
                "请关闭程序，双击项目根目录的 run.bat 重新启动"
            )
            self._hotkey_text.color = ft.Colors.RED_400
            self._update_ui()
            return
        if not self._active:
            self._hotkey_text.value = (
                f"全局快捷键：进入本页后生效（{play} 播放 / {stop} 停止）"
            )
            self._hotkey_text.color = MUTED
        elif self._hotkeys.failed:
            names = "、".join(hotkey_label(vk) for vk in self._hotkeys.failed)
            self._hotkey_text.value = (
                f"⚠ 全局快捷键 {names} 被其他软件占用；关掉冲突软件后重新进入本页"
            )
            self._hotkey_text.color = ft.Colors.RED_400
        elif self._hotkeys.available:
            self._hotkey_text.value = (
                f"全局快捷键：{play} 播放 · {stop} 停止并松键（游戏保持焦点也能用）"
            )
            self._hotkey_text.color = MUTED
        else:
            self._hotkey_text.value = (
                f"全局快捷键不可用：{self._hotkeys.error or '注册失败'}"
            )
            self._hotkey_text.color = ft.Colors.ORANGE_700
        self._update_ui()

    def _has_playable(self) -> bool:
        """当前载入的谱面有没有可回放的内容（热键路径用）。"""
        if self.kind == "gct":
            return self.gct_doc is not None and bool(self.gct_doc.chord_events())
        return self.doc is not None and bool(self.doc.note_blocks())

    def _on_hotkey_play(self) -> None:
        """热键线程回调：这里只把任务丢回 Flet 事件循环。"""
        if not self._active or self._playing or self.page is None:
            return
        if not self._has_playable():
            return
        self.page.run_task(self._play_from_hotkey)

    async def _play_from_hotkey(self) -> None:
        if self._playing or not self._has_playable():
            return
        if not self.backend.connected:
            # 连接是手动的：热键路径也不偷偷连手柄（和 _on_play 一致）
            self._progress_text.value = (
                "虚拟手柄还没连接：先点「连接手柄」（热键不会自动连接）"
            )
            self.page.update()
            return
        await self._on_play()

    def _on_hotkey_stop(self) -> None:
        if self.page is None:
            return
        self.page.run_task(self._panic_stop)

    async def _panic_stop(self) -> None:
        """停止键：无论是否在回放，都确保所有键已松开。"""
        self._playing = False
        try:
            self.backend.release_all()
        except Exception:  # noqa: BLE001
            pass
        self._progress_text.value = (
            f"已停止（{hotkey_label(HOTKEY_STOP)}，所有键已松开）"
        )
        self._wheel.reset()
        self._update_ui()

    def _beats_at(self, tick: int) -> float:
        """当前谱面的 tick → 拍（gtab / gct 两种文档都有 beats_at）。"""
        active = self.gct_doc if self.kind == "gct" else self.doc
        return active.beats_at(tick) if active is not None else 0.0

    def _append_log(self, action: ScheduleAction) -> None:
        beat = self._beats_at(action.tick)
        self._log.append(f"第 {beat:5.2f} 拍   第 {action.page} 页   {action.label}")
        self._log_column.controls = [
            ft.Text(line, size=11, color=MUTED) for line in reversed(self._log)
        ]

    def _refresh_progress(self, finished: bool = False) -> None:
        average = sum(self._drifts) / len(self._drifts) if self._drifts else 0.0
        unit = self._unit_label
        if finished:
            worst = max(self._drifts, default=0.0)
            self._progress_text.value = (
                f"回放结束：{self._pressed}/{self._total_notes} 个{unit} · "
                f"平均误差 {average:.1f} ms · 最大误差 {worst:.1f} ms"
            )
        else:
            self._progress_text.value = (
                f"回放中… {self._pressed}/{self._total_notes} 个{unit} · "
                f"当前误差 {average:.1f} ms"
            )

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

    def _update_ui(self) -> None:
        if self.page is not None:
            self.page.update()
