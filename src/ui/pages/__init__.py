"""各个页面。每个页面暴露一个类，提供 ``build() -> ft.Control``。"""

from ui.pages.gtab_page import GtabPage
from ui.pages.placeholder import PlaceholderPage

__all__ = ["GtabPage", "PlaceholderPage"]
