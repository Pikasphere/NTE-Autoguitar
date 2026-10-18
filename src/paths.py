"""程序运行路径：**源码运行** 与 **打包运行**（PyInstaller / ``flet pack``）。

两种模式下的资源位置不同，所有读写资源的模块都应该从这里取路径，别再自己数
``Path(__file__).parents[n]``：

- 源码运行：资源根是 ``src/``（本文件所在目录），仓库根是它的上一级；
- 打包运行：``flet pack`` / PyInstaller 会把 Python 模块解到 ``sys._MEIPASS``
  （单文件 exe 是临时目录、``--onedir`` 时是解包目录），资源用 ``--add-data``
  按下面的约定放：
  - ``chord.json`` / ``midi_keys.json`` → 资源根（与 ``ui/`` 同级）；
  - ``MusicList/...`` → 资源根下的 ``MusicList``；
  - ``docs/AkaneRin.ico`` → 资源根下的 ``docs``。

``settings.json`` 是**用户偏好**，打包后不能写在资源根里（单文件 exe 退出就删），
改写到 ``%APPDATA%\\NTE-Autoguitar\\settings.json``；源码运行仍写 ``src/settings.json``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "APP_DIR_NAME",
    "bundle_root",
    "is_frozen",
    "project_root",
    "settings_path",
    "user_data_dir",
]

#: 用户数据目录名（%APPDATA% 下）。
APP_DIR_NAME = "NTE-Autoguitar"


def is_frozen() -> bool:
    """当前是否是打包后的程序（PyInstaller / flet pack 都会设 ``sys.frozen``）。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """**资源根**：源码运行 = ``src/``；打包运行 = ``sys._MEIPASS``。

    PyInstaller 单文件 exe 把资源解到临时目录并设 ``sys._MEIPASS``；``--onedir``
    时它是解包目录（``_internal``）。没设时退回本文件所在目录（源码运行）。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """**仓库根**：源码运行 = 项目的上一级；打包运行 = 资源根（``_MEIPASS``）。

    只有 ``docs/`` 这种在源码里位于 ``src/`` 外层、打包时又被 ``--add-data``
    放进资源根的东西需要它。
    """
    return bundle_root() if is_frozen() else bundle_root().parent


def user_data_dir(base: Path | None = None) -> Path:
    """用户数据目录：``%APPDATA%\\NTE-Autoguitar``（可用 ``base`` 覆盖，测试用）。"""
    root = base if base is not None else Path(os.environ.get("APPDATA") or Path.home())
    return root / APP_DIR_NAME


def settings_path(base: Path | None = None) -> Path:
    """``settings.json`` 的落点：打包后写用户目录，源码运行写 ``src/settings.json``。"""
    if is_frozen():
        return user_data_dir(base) / "settings.json"
    return bundle_root() / "settings.json"
