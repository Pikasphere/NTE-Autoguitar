"""全局热键模块的测试：键名映射 + 线程注册/注销 + WM_HOTKEY 派发。

不测试真人按键（``RegisterHotKey`` 不响应 ``keybd_event`` 合成的输入），
但会真的注册一个冷门键 ``F24``，跑完立刻注销。

运行：``.venv\\Scripts\\python.exe tests\\test_hotkeys.py``
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ui.hotkeys import (  # noqa: E402
    HOTKEY_PLAY,
    HOTKEY_STOP,
    GlobalHotkeys,
    hotkey_label,
    process_integrity,
)

WM_HOTKEY = 0x0312
TEST_VK = 0x87  # F24，日常软件几乎不用


def test_hotkey_labels() -> None:
    assert hotkey_label(HOTKEY_PLAY) == "F9"
    assert hotkey_label(HOTKEY_STOP) == "F10"
    assert hotkey_label(0x70) == "F1"
    assert hotkey_label(TEST_VK) == "F24"


def test_process_integrity() -> None:
    assert process_integrity() in {"low", "medium", "high", "system", "unknown"}


def test_register_and_close() -> None:
    if sys.platform != "win32":
        return
    hotkeys = GlobalHotkeys({TEST_VK: lambda: None})
    hotkeys.start()
    try:
        assert hotkeys.running
        assert hotkeys.registered or hotkeys.failed, "注册要么成功要么明确失败"
        if hotkeys.failed:
            assert TEST_VK in hotkeys.failed
        else:
            assert TEST_VK in hotkeys.registered
    finally:
        hotkeys.close()
    assert not hotkeys.running


def test_wm_hotkey_dispatch() -> None:
    if sys.platform != "win32":
        return
    hits: list[int] = []
    hotkeys = GlobalHotkeys({TEST_VK: lambda: hits.append(1)})
    hotkeys.start()
    try:
        if TEST_VK not in hotkeys.registered:
            return  # F24 被占用：注册路径已由上一个测试覆盖
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        assert user32.PostThreadMessageW(
            hotkeys._thread_id, WM_HOTKEY, TEST_VK, 0
        ), "PostThreadMessage 失败"
        deadline = time.monotonic() + 1.0
        while not hits and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hits == [1]
    finally:
        hotkeys.close()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
