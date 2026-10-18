"""应用品牌资源：窗口图标。

Flet 的 ``page.window.icon`` 在 Windows 上读一个 ``.ico`` 文件路径；
图标文件由用户放在 ``docs/AkaneRin.ico``（256×256，PNG 压缩的 ICO）。
主壳和开发预览脚本都用这里的路径，避免各写一份。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ICON_PATH"]

#: 窗口 / 任务栏图标。相对项目根定位，不依赖启动时的工作目录。
ICON_PATH = Path(__file__).resolve().parents[1] / "docs" / "AkaneRin.ico"
