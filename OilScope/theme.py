# -*- coding: utf-8 -*-
"""
theme.py

OilScope의 브랜드 색상(한국석유관리원 K-Petro CI 규정)과 Qt 스타일시트, 아이콘/
로고 리소스 경로 유틸리티를 모아둔 모듈.

색상 값은 "전용색상" 규정의 Main/Sub Color RGB 값을 그대로 사용한다:
    Main  - K-Petro LIGHT GREEN R140 G198 B63 / K-Petro GRAY R82 G82 B88
    Sub   - K-Petro BLUE R27 G66 B152 / SILVER R128 G127 B131 /
            LIGHT GRAY R216 G217 B219 / GOLD R180 G152 B90
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# K-Petro 전용색상
# ---------------------------------------------------------------------------
COLOR_GREEN = "#8CC63F"        # Main - K-Petro LIGHT GREEN
COLOR_GRAY = "#525258"         # Main - K-Petro GRAY
COLOR_BLUE = "#1B4298"         # Sub  - K-Petro BLUE
COLOR_SILVER = "#807F83"       # Sub  - K-Petro SILVER
COLOR_LIGHT_GRAY = "#D8D9DB"   # Sub  - K-Petro LIGHT GRAY
COLOR_GOLD = "#B4985A"         # Sub  - K-Petro GOLD

COLOR_GREEN_HOVER = "#7AB032"
COLOR_GREEN_PRESSED = "#6A9A2B"


# ---------------------------------------------------------------------------
# 리소스 경로 (개발 환경 / PyInstaller 패키징 환경 겸용)
# ---------------------------------------------------------------------------
def resource_path(*parts: str) -> str:
    """
    개발 중(스크립트 직접 실행)과 PyInstaller로 패키징된 실행 파일 양쪽에서
    모두 올바르게 동작하는 리소스 경로를 반환한다.

    --onefile로 빌드된 실행 파일은 번들 데이터를 실행 시점에 sys._MEIPASS
    임시 폴더에 풀어놓으므로, 패키징된 상태에서는 거기서 찾아야 한다.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def icon_path() -> str:
    return resource_path("assets", "icon.ico")


def icon_png_path() -> str:
    return resource_path("assets", "icon_1024.png")


def logo_path() -> str:
    return resource_path("assets", "kpetro_logo.png")


# ---------------------------------------------------------------------------
# Qt 스타일시트
# ---------------------------------------------------------------------------
APP_STYLESHEET = f"""
QWidget {{
    background-color: #ffffff;
    color: {COLOR_GRAY};
}}

QGroupBox {{
    border: 1px solid {COLOR_LIGHT_GRAY};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: bold;
    color: {COLOR_GRAY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QTabWidget::pane {{
    border: 1px solid {COLOR_LIGHT_GRAY};
    top: -1px;
}}
QTabBar::tab {{
    background: {COLOR_LIGHT_GRAY};
    color: {COLOR_GRAY};
    padding: 8px 18px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {COLOR_GREEN};
    color: #ffffff;
    font-weight: bold;
}}
QTabBar::tab:hover:!selected {{
    background: #c9dba0;
}}

QPushButton {{
    background-color: {COLOR_GREEN};
    color: #ffffff;
    border: none;
    border-radius: 5px;
    padding: 6px 14px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {COLOR_GREEN_HOVER};
}}
QPushButton:pressed {{
    background-color: {COLOR_GREEN_PRESSED};
}}
QPushButton:disabled {{
    background-color: {COLOR_LIGHT_GRAY};
    color: #ffffff;
}}

QSlider::groove:horizontal {{
    height: 6px;
    background: {COLOR_LIGHT_GRAY};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {COLOR_GREEN};
    width: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {COLOR_GREEN};
    border-radius: 3px;
}}

QTableWidget {{
    gridline-color: {COLOR_LIGHT_GRAY};
    selection-background-color: {COLOR_GREEN};
    selection-color: #ffffff;
    alternate-background-color: #f5f7f2;
}}
QHeaderView::section {{
    background-color: {COLOR_GRAY};
    color: #ffffff;
    padding: 5px;
    border: none;
}}

QLineEdit, QDoubleSpinBox {{
    border: 1px solid {COLOR_LIGHT_GRAY};
    border-radius: 4px;
    padding: 3px 5px;
    background: #ffffff;
    selection-background-color: {COLOR_GREEN};
}}
QLineEdit:focus, QDoubleSpinBox:focus {{
    border: 1px solid {COLOR_GREEN};
}}

QSplitter::handle {{
    background: {COLOR_LIGHT_GRAY};
}}

QDialog {{
    background-color: #ffffff;
}}
"""
