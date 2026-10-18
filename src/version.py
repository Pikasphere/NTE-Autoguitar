"""程序版本号（唯一来源）。

设置页把它显示在右上角；以后打包 / 「关于」框也读这里。
版本号只在**用户可见的功能或行为变化**时改，改这一处就够。
"""

from __future__ import annotations

__all__ = ["VERSION"]

#: ``1.0.0-rc1`` = 1.0.0 的发布候选（release candidate）。正式发布时去掉 ``-rc1``。
VERSION = "1.0.0-rc2"
