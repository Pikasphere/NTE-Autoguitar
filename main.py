"""程序入口：只负责启动 GUI。

界面代码分布在：
- ``src/app.py``            主壳（导航 + 页面容器）
- ``src/ui/pages/``         各个页面（gtab_page.py 就是本次的 GTAB 页面）
- ``src/ui/gtab_doc.py``    gtab 读写模型（与界面无关）

启动：``.venv\\Scripts\\python.exe main.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import flet as ft  # noqa: E402  （必须先把 src 放进 sys.path）

from app import main  # noqa: E402


if __name__ == "__main__":
    ft.run(main)
