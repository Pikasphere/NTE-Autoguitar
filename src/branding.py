"""应用品牌资源：窗口图标。

Flet 的 ``page.window.icon`` 在 Windows 上读一个 ``.ico`` 文件路径；
图标文件由用户放在 ``docs/AkaneRin.ico``（256×256，PNG 压缩的 ICO）。
主壳和开发预览脚本都用这里的路径，避免各写一份。
"""

from __future__ import annotations

from paths import project_root

__all__ = ["ICON_PATH"]

#: 窗口 / 任务栏图标。源码运行在仓库根的 ``docs/``；打包后 ``--add-data`` 放进
#: 资源根的 ``docs/``（``project_root()`` 两种模式都指向正确的那一层）。
ICON_PATH = project_root() / "docs" / "AkaneRin.ico"
