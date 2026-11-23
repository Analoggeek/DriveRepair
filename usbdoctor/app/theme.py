"""Dark / light design system for the desktop app.

One palette, one stylesheet, no external assets.  Widgets reference object names
(``#Card``, ``#Primary``, ``#Badge--good``) so the whole look can be retuned in a
single place without touching page code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    bg_soft: str
    panel: str
    panel_alt: str
    line: str
    line_strong: str
    text: str
    text_mid: str
    text_dim: str
    accent: str
    accent_hover: str
    accent_press: str
    good: str
    warn: str
    bad: str
    critical: str


DARK = Palette(
    name="dark",
    bg="#080b11",
    bg_soft="#0d1219",
    panel="#111823",
    panel_alt="#151d2a",
    line="#1e2836",
    line_strong="#2a3648",
    text="#e8eef7",
    text_mid="#a5b2c4",
    text_dim="#6d7c91",
    accent="#3d8bff",
    accent_hover="#5b9cff",
    accent_press="#2a6fe0",
    good="#2ecc9a",
    warn="#ffbe3d",
    bad="#ff5d6e",
    critical="#ff3b5c",
)

LIGHT = Palette(
    name="light",
    bg="#f4f6fb",
    bg_soft="#eaeef6",
    panel="#ffffff",
    panel_alt="#f7f9fd",
    line="#e2e8f2",
    line_strong="#ccd6e5",
    text="#0f1723",
    text_mid="#45536a",
    text_dim="#7a889e",
    accent="#1f6ff0",
    accent_hover="#3d85ff",
    accent_press="#1558c8",
    good="#0f9d72",
    warn="#b97b00",
    bad="#d63c4f",
    critical="#c4152f",
)

PALETTES = {"dark": DARK, "light": LIGHT}


def palette_for(theme: str) -> Palette:
    return PALETTES.get(theme, DARK)


def stylesheet(theme: str = "dark") -> str:
    p = palette_for(theme)
    return f"""
* {{
    font-family: -apple-system, "Segoe UI", "SF Pro Text", Roboto, Ubuntu, sans-serif;
    font-size: 13px;
    color: {p.text};
    outline: none;
}}

QMainWindow, QWidget#Root {{
    background: {p.bg};
}}

/* ───────────── sidebar ───────────── */
QWidget#Sidebar {{
    background: {p.bg_soft};
    border-right: 1px solid {p.line};
}}
QLabel#BrandMark {{
    background: {p.accent};
    border-radius: 11px;
    color: white;
    font-size: 17px;
    font-weight: 700;
}}
QLabel#BrandTitle {{
    font-size: 15px;
    font-weight: 650;
    letter-spacing: -0.2px;
}}
QLabel#BrandVersion {{
    color: {p.text_dim};
    font-size: 10px;
}}

QPushButton#NavButton {{
    text-align: left;
    padding: 9px 13px;
    border: none;
    border-radius: 9px;
    background: transparent;
    color: {p.text_mid};
    font-weight: 500;
}}
QPushButton#NavButton:hover {{
    background: {p.panel_alt};
    color: {p.text};
}}
QPushButton#NavButton:checked {{
    background: {p.panel_alt};
    color: {p.text};
    border-left: 2px solid {p.accent};
    border-top-left-radius: 2px;
    border-bottom-left-radius: 2px;
}}

/* ───────────── buttons ───────────── */
QPushButton {{
    background: {p.panel_alt};
    border: 1px solid {p.line_strong};
    border-radius: 9px;
    padding: 8px 15px;
    font-weight: 550;
    color: {p.text};
}}
QPushButton:hover:!disabled {{
    border-color: {p.accent};
}}
QPushButton:pressed:!disabled {{
    background: {p.bg_soft};
}}
QPushButton:disabled {{
    color: {p.text_dim};
    border-color: {p.line};
}}
QPushButton#Primary {{
    background: {p.accent};
    border: 1px solid {p.accent};
    color: #ffffff;
}}
QPushButton#Primary:hover:!disabled {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QPushButton#Primary:pressed:!disabled {{
    background: {p.accent_press};
}}
QPushButton#Danger {{
    background: {p.critical};
    border: 1px solid {p.critical};
    color: #ffffff;
}}
QPushButton#Danger:hover:!disabled {{
    background: {p.bad};
}}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {p.line_strong};
}}
QPushButton#Ghost:hover:!disabled {{
    background: {p.panel_alt};
}}
QPushButton#LinkButton {{
    background: transparent;
    border: none;
    color: {p.accent};
    padding: 2px 4px;
    text-align: left;
}}
QPushButton#LinkButton:hover {{
    color: {p.accent_hover};
    text-decoration: underline;
}}

/* ───────────── cards ───────────── */
QFrame#Card {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 14px;
}}
QLabel#CardTitle {{
    font-size: 13.5px;
    font-weight: 600;
    letter-spacing: -0.1px;
}}
QLabel#PageTitle {{
    font-size: 23px;
    font-weight: 650;
    letter-spacing: -0.4px;
}}
QLabel#PageSub {{
    color: {p.text_mid};
    font-size: 12.5px;
}}
QLabel#Muted {{
    color: {p.text_mid};
}}
QLabel#Dim {{
    color: {p.text_dim};
    font-size: 11.5px;
}}
QLabel#Mono {{
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 11.5px;
    color: {p.text_mid};
}}
QLabel#StatValue {{
    font-size: 27px;
    font-weight: 650;
    letter-spacing: -0.6px;
}}
QLabel#StatLabel {{
    color: {p.text_dim};
    font-size: 10.5px;
    letter-spacing: 0.7px;
}}

/* ───────────── badges ───────────── */
QLabel#Badge {{
    background: {p.panel_alt};
    border: 1px solid {p.line_strong};
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 10.5px;
    font-weight: 600;
    color: {p.text_mid};
}}
QLabel#Badge--good {{
    background: rgba(46, 204, 154, 0.13);
    border: 1px solid {p.good};
    color: {p.good};
}}
QLabel#Badge--warn {{
    background: rgba(255, 190, 61, 0.13);
    border: 1px solid {p.warn};
    color: {p.warn};
}}
QLabel#Badge--bad, QLabel#Badge--critical {{
    background: rgba(255, 93, 110, 0.13);
    border: 1px solid {p.bad};
    color: {p.bad};
}}

/* ───────────── inputs ───────────── */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {p.bg_soft};
    border: 1px solid {p.line_strong};
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: {p.accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextBrowser:focus {{
    border-color: {p.accent};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {p.panel};
    border: 1px solid {p.line_strong};
    selection-background-color: {p.accent};
    selection-color: white;
    padding: 4px;
}}
QCheckBox {{ spacing: 8px; color: {p.text_mid}; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {p.line_strong};
    border-radius: 4px;
    background: {p.bg_soft};
}}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: none;
}}

/* ───────────── tables / lists ───────────── */
QTableWidget, QTreeWidget {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 10px;
    gridline-color: {p.line};
    selection-background-color: {p.panel_alt};
    selection-color: {p.text};
    alternate-background-color: {p.panel_alt};
}}
QHeaderView::section {{
    background: {p.panel};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.line};
    padding: 8px 10px;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QTableWidget::item {{
    padding: 7px 10px;
    border-bottom: 1px solid {p.line};
}}
QTableWidget::item:selected {{
    background: {p.panel_alt};
}}

/* ───────────── progress ───────────── */
QProgressBar {{
    background: {p.bg_soft};
    border: none;
    border-radius: 4px;
    height: 7px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 4px;
}}

/* ───────────── misc ───────────── */
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.line_strong}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {p.line_strong}; border-radius: 4px; }}

QGroupBox {{
    border: 1px solid {p.line};
    border-radius: 10px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: 600;
    color: {p.text_mid};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}

QSplitter::handle {{ background: {p.line}; }}

QToolTip {{
    background: {p.panel_alt};
    color: {p.text};
    border: 1px solid {p.line_strong};
    border-radius: 6px;
    padding: 6px 8px;
}}

QStatusBar {{
    background: {p.bg_soft};
    border-top: 1px solid {p.line};
    color: {p.text_dim};
    font-size: 11.5px;
}}

QMessageBox {{ background: {p.panel}; }}
QDialog {{ background: {p.bg}; }}

QTextBrowser#Diagnosis {{
    background: {p.panel_alt};
    border: 1px solid {p.line};
    border-radius: 10px;
    padding: 14px;
    font-size: 12.5px;
}}
/* Default link blue is unreadable on a dark panel. */
QTextBrowser#Diagnosis a, QTextBrowser a {{
    color: {p.accent_hover};
    text-decoration: none;
}}
"""
