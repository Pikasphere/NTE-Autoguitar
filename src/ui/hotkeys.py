"""全局快捷键（Windows ``RegisterHotKey``）。

用途：回放测试时游戏窗口保持焦点，不用切回 Flet 点「播放」。系统级热键由
``RegisterHotKey`` 注册，游戏在前台也能收到；回调在**热键线程**里执行，
所以回调里只应做「把任务丢回 Flet 事件循环」这类轻量操作。

默认绑定：

- ``F9``  播放当前谱面
- ``F10`` 停止回放并松开所有键（panic stop）

要换键就改下面的 ``HOTKEY_PLAY`` / ``HOTKEY_STOP``（Windows 虚拟键码）。
非 Windows 平台上 ``start()`` 会直接标记为不可用，不影响其它功能。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable, Mapping
from ctypes import wintypes

__all__ = [
    "HOTKEY_PLAY",
    "HOTKEY_STOP",
    "GlobalHotkeys",
    "hotkey_label",
    "process_integrity",
]

# Windows 虚拟键码：F9 / F10
HOTKEY_PLAY = 0x78
HOTKEY_STOP = 0x79

_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


def hotkey_label(vk: int) -> str:
    """虚拟键码 → 给人看的名字（只覆盖 F1–F24，其它返回十六进制）。"""
    if 0x70 <= vk <= 0x87:  # VK_F1 .. VK_F24
        return f"F{vk - 0x6F}"
    return f"0x{vk:02X}"


def process_integrity() -> str:
    """当前进程的 Windows 完整性级别。

    返回 ``low`` / ``medium`` / ``high`` / ``system`` / ``unknown``。

    **低权限（low）进程的全局热键和键盘钩子看不到更高权限游戏窗口的输入**，
    表现就是「点一下本程序再用热键才行」。本程序被 Agent 沙盒启动时就是 low，
    需要用户用普通终端或 ``run.bat`` 启动。
    """
    if sys.platform != "win32":
        return "unknown"
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            return "unknown"
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, 25, None, 0, ctypes.byref(size)  # TokenIntegrityLevel
            )
            if not size.value:
                return "unknown"
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, 25, buffer, size.value, ctypes.byref(size)
            ):
                return "unknown"

            class _SidAndAttributes(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

            class _TokenMandatoryLabel(ctypes.Structure):
                _fields_ = [("Label", _SidAndAttributes)]

            label = ctypes.cast(
                buffer, ctypes.POINTER(_TokenMandatoryLabel)
            ).contents
            sid_text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(
                label.Label.Sid, ctypes.byref(sid_text)
            ):
                return "unknown"
            sid = sid_text.value or ""
        finally:
            kernel32.CloseHandle(token)
    except OSError:
        return "unknown"

    for suffix, level in (
        ("-4096", "low"),
        ("-8192", "medium"),
        ("-12288", "high"),
        ("-16384", "system"),
    ):
        if sid.endswith(suffix):
            return level
    return "unknown"


class GlobalHotkeys:
    """在独立线程里注册全局热键并跑一个消息循环。

    用法::

        hotkeys = GlobalHotkeys({HOTKEY_PLAY: on_play, HOTKEY_STOP: on_stop})
        hotkeys.start()
        ...
        hotkeys.close()

    ``start()`` 会等注册结果（最多 1 秒），之后 ``registered`` / ``failed``
    就是实际结果；被别的软件占用时对应键会出现在 ``failed`` 里。
    """

    def __init__(self, bindings: Mapping[int, Callable[[], None]]) -> None:
        self._bindings = dict(bindings)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered: tuple[int, ...] = ()
        self.failed: tuple[int, ...] = ()
        self.error = ""

    # ------------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def available(self) -> bool:
        return bool(self.registered)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """注册热键并启动消息循环；重复调用无副作用。"""
        if sys.platform != "win32":
            self.error = "全局热键只支持 Windows"
            return
        if self._thread is not None:
            return
        self._ready.clear()
        self.registered = ()
        self.failed = ()
        self.error = ""
        self._thread = threading.Thread(
            target=self._run, name="gtab-global-hotkeys", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def close(self) -> None:
        """注销热键并结束消息循环；重复调用无副作用。"""
        thread, thread_id = self._thread, self._thread_id
        self._thread = None
        self._thread_id = 0
        if thread is None:
            return
        if thread_id:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except OSError:
                pass
        thread.join(timeout=1.0)

    # ------------------------------------------------------------------ 线程

    def _run(self) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError as exc:  # 理论上不会发生
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return

        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostThreadMessageW.restype = wintypes.BOOL

        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered: list[int] = []
        failed: list[int] = []
        for vk in self._bindings:
            if user32.RegisterHotKey(None, int(vk), _MOD_NOREPEAT, int(vk)):
                registered.append(int(vk))
            else:
                failed.append(int(vk))
        self.registered = tuple(registered)
        self.failed = tuple(failed)
        self._ready.set()

        try:
            self._message_loop(user32)
        finally:
            for vk in registered:
                user32.UnregisterHotKey(None, vk)

    def _message_loop(self, user32) -> None:
        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:  # 0 = WM_QUIT，-1 = 错误
                return
            if msg.message != _WM_HOTKEY:
                continue
            callback = self._bindings.get(int(msg.wParam))
            if callback is None:
                continue
            try:
                callback()
            except Exception as exc:  # noqa: BLE001  （热键线程不能把异常抛给 UI）
                print(
                    f"全局热键 {hotkey_label(int(msg.wParam))} 回调失败：{exc!r}",
                    file=sys.stderr,
                )
