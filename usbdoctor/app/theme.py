"""Dark / light design system for the desktop app.

One palette, one stylesheet, no external assets. Widgets reference object names
(``#Card``, ``#Primary``, ``#Badge--good``) so the whole look can be retuned in a
single place without touching page code.

The 1.1 look adds depth without adding images: panels are gradients rather than
flat fills, the primary action carries a lit top edge, the sidebar darkens towards
the top, and focus is shown with a ring instead of a colour change on the border —
so a keyboard user can see where they are on a light theme as clearly as on a dark
one. Every colour here is still a pair in the palette, and both palettes define the
same keys, which a test enforces.
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
    panel_hi: str        # top edge of a panel: the 1px light that suggests depth
    line: str
    line_strong: str
    text: str
    text_mid: str
    text_dim: str
    accent: str
    accent_hover: str
    accent_press: str
    accent_soft: str     # accent at low opacity, for halos and selection
    good: str
    warn: str
    bad: str
    critical: str
    shadow: str          # near-black, used for separators that read as depth


DARK = Palette(
    name="dark",
    bg="#070a0f",
    bg_soft="#0b1017",
    panel="#111823",
    panel_alt="#161f2c",
    panel_hi="#1b2634",
    line="#1d2733",
    line_strong="#2a3746",
    text="#e9eff8",
    text_mid="#a3b1c4",
    text_dim="#68788e",
    accent="#3d8bff",
    accent_hover="#5b9cff",
    accent_press="#2f74e0",
    accent_soft="rgba(61, 139, 255, 0.16)",
    good="#2ecc9a",
    warn="#ffbe3d",
    bad="#ff5d6e",
    critical="#ff3b5c",
    shadow="#04070b",
)

LIGHT = Palette(
    name="light",
    bg="#f2f5fa",
    bg_soft="#e9eefa",
    panel="#ffffff",
    panel_alt="#f6f8fd",
    panel_hi="#ffffff",
    line="#e3e9f3",
    line_strong="#cfd9e8",
    text="#0d1626",
    text_mid="#42506a",
    text_dim="#77869c",
    accent="#1f6ff0",
    accent_hover="#3d85ff",
    accent_press="#1558c8",
    accent_soft="rgba(31, 111, 240, 0.11)",
    good="#0f9d72",
    warn="#b97b00",
    bad="#d63c4f",
    critical="#c4152f",
    shadow="#b9c4d6",
)

PALETTES = {"dark": DARK, "light": LIGHT}


def palette_for(theme: str) -> Palette:
    return PALETTES.get(theme, DARK)


def stylesheet(theme: str = "dark") -> str:
    p = palette_for(theme)
    dark = p.name == "dark"
    # Depth is a gradient on dark and a hairline on light: the same recipe looks
    # muddy on white and flat on black.
    panel_top = p.panel_hi if dark else "#ffffff"
    card_fill = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {panel_top}, stop:1 {p.panel})"
                 if dark else p.panel)
    sidebar_fill = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.panel_alt}, "
                    f"stop:1 {p.bg_soft})" if dark else p.panel)
    primary_fill = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.accent_hover}, "
                    f"stop:1 {p.accent})")
    primary_press = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.accent}, "
                     f"stop:1 {p.accent_press})")
    return f"""
* {{
    font-family: -apple-system, "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                 Inter, Roboto, Ubuntu, sans-serif;
    font-size: 13px;
    color: {p.text};
    outline: none;
}}

QMainWindow, QWidget#Root {{
    background: {p.bg};
}}

/* ───────────── sidebar ───────────── */
QWidget#Sidebar {{
    background: {sidebar_fill};
    border-right: 1px solid {p.line};
}}
QLabel#BrandTitle {{
    font-size: 15px;
    font-weight: 650;
    letter-spacing: -0.2px;
}}
QLabel#BrandVersion {{
    color: {p.text_dim};
    font-size: 10px;
    letter-spacing: 0.3px;
    text-transform: uppercase;
}}
QLabel#NavSection {{
    color: {p.text_dim};
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 1.1px;
    padding: 12px 12px 4px 12px;
}}

QPushButton#NavButton {{
    text-align: left;
    padding: 8px 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: {p.text_mid};
    font-weight: 500;
    font-size: 13px;
}}
QPushButton#NavButton:hover {{
    background: {p.panel_alt};
    color: {p.text};
    border-color: {p.line};
}}
QPushButton#NavButton:checked {{
    background: {p.accent_soft};
    color: {p.text};
    border-color: {p.accent};
    font-weight: 600;
}}

/* ───────────── buttons ───────────── */
QPushButton {{
    background: {p.panel_alt};
    border: 1px solid {p.line_strong};
    border-radius: 9px;
    padding: 8px 16px;
    font-weight: 550;
    color: {p.text};
}}
QPushButton:hover:!disabled {{
    background: {p.panel_hi};
    border-color: {p.accent};
}}
QPushButton:pressed:!disabled {{
    background: {p.bg_soft};
}}
QPushButton:focus {{
    border-color: {p.accent};
}}
QPushButton:disabled {{
    color: {p.text_dim};
    border-color: {p.line};
    background: transparent;
}}
QPushButton#Primary {{
    background: {primary_fill};
    border: 1px solid {p.accent_press};
    color: #ffffff;
    font-weight: 600;
    padding: 9px 20px;
}}
QPushButton#Primary:hover:!disabled {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QPushButton#Primary:pressed:!disabled {{
    background: {primary_press};
}}
QPushButton#Primary:disabled {{
    background: {p.panel_alt};
    color: {p.text_dim};
    border-color: {p.line};
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
    color: {p.text_mid};
}}
QPushButton#Ghost:hover:!disabled {{
    background: {p.panel_alt};
    color: {p.text};
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
QPushButton#Pill {{
    background: {p.accent_soft};
    border: 1px solid {p.accent};
    border-radius: 12px;
    color: {p.text};
    padding: 4px 11px;
    font-size: 11.5px;
    font-weight: 600;
}}

/* ───────────── cards ───────────── */
QFrame#Card {{
    background: {card_fill};
    border: 1px solid {p.line};
    border-radius: 14px;
}}
QFrame#Card:hover {{
    border-color: {p.line_strong};
}}
QFrame#Hero {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {p.panel_alt}, stop:0.55 {p.panel}, stop:1 {p.panel_hi});
    border: 1px solid {p.line_strong};
    border-radius: 16px;
}}
QLabel#CardTitle {{
    font-size: 13.5px;
    font-weight: 650;
    letter-spacing: -0.1px;
}}
QLabel#PageTitle {{
    font-size: 24px;
    font-weight: 680;
    letter-spacing: -0.5px;
}}
QLabel#PageSub {{
    color: {p.text_mid};
    font-size: 12.5px;
}}
QLabel#HeroTitle {{
    font-size: 21px;
    font-weight: 680;
    letter-spacing: -0.4px;
}}
QLabel#Muted {{
    color: {p.text_mid};
}}
QLabel#Dim {{
    color: {p.text_dim};
    font-size: 11.5px;
}}
QLabel#Mono {{
    font-family: "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
    font-size: 11.5px;
    color: {p.text_mid};
}}
QLabel#StatValue {{
    font-size: 28px;
    font-weight: 680;
    letter-spacing: -0.7px;
}}
QLabel#StatLabel {{
    color: {p.text_dim};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.9px;
}}

/* ───────────── badges ───────────── */
QLabel#Badge {{
    background: {p.panel_alt};
    border: 1px solid {p.line_strong};
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 10.5px;
    font-weight: 650;
    color: {p.text_mid};
}}
QLabel#Badge--good {{
    background: rgba(46, 204, 154, 0.14);
    border: 1px solid {p.good};
    color: {p.good};
}}
QLabel#Badge--warn {{
    background: rgba(255, 190, 61, 0.14);
    border: 1px solid {p.warn};
    color: {p.warn};
}}
QLabel#Badge--bad, QLabel#Badge--critical {{
    background: rgba(255, 93, 110, 0.14);
    border: 1px solid {p.bad};
    color: {p.bad};
}}

/* ───────────── inputs ───────────── */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {p.bg_soft};
    border: 1px solid {p.line_strong};
    border-radius: 9px;
    padding: 7px 11px;
    selection-background-color: {p.accent};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextBrowser:focus {{
    border: 2px solid {p.accent};
    padding: 6px 10px;
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {p.panel};
    border: 1px solid {p.line_strong};
    border-radius: 10px;
    selection-background-color: {p.accent};
    selection-color: #ffffff;
    padding: 4px;
}}
QCheckBox {{ spacing: 9px; color: {p.text_mid}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.line_strong};
    border-radius: 5px;
    background: {p.bg_soft};
}}
QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: none;
}}
QCheckBox::indicator:focus {{ border: 2px solid {p.accent}; }}

/* ───────────── tables / lists ───────────── */
QTableWidget, QTreeWidget, QListWidget {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 12px;
    gridline-color: {p.line};
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
    alternate-background-color: {p.panel_alt};
}}
QHeaderView::section {{
    background: {p.panel};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.line};
    padding: 9px 11px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.7px;
}}
QTableWidget::item {{
    padding: 8px 11px;
    border-bottom: 1px solid {p.line};
}}
QTableWidget::item:selected, QListWidget::item:selected {{
    background: {p.accent_soft};
    color: {p.text};
}}
QListWidget::item {{ padding: 7px 9px; border-radius: 7px; }}

/* ───────────── progress ───────────── */
QProgressBar {{
    background: {p.bg_soft};
    border: 1px solid {p.line};
    border-radius: 6px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {p.accent}, stop:1 {p.accent_hover});
    border-radius: 6px;
}}

/* ───────────── misc ───────────── */
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 3px 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.line_strong}; border-radius: 4px; min-height: 34px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{ background: {p.line_strong}; border-radius: 4px; }}

QGroupBox {{
    border: 1px solid {p.line};
    border-radius: 11px;
    margin-top: 11px;
    padding-top: 11px;
    font-weight: 600;
    color: {p.text_mid};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 13px; padding: 0 5px; }}

QSplitter::handle {{ background: {p.line}; }}

QToolTip {{
    background: {p.panel_hi};
    color: {p.text};
    border: 1px solid {p.line_strong};
    border-radius: 7px;
    padding: 6px 9px;
    font-size: 11.5px;
}}

QStatusBar {{
    background: {p.bg_soft};
    border-top: 1px solid {p.line};
    color: {p.text_dim};
    font-size: 11.5px;
}}
QStatusBar::item {{ border: none; }}

QMenuBar {{
    background: {p.bg_soft};
    color: {p.text_mid};
    border-bottom: 1px solid {p.line};
    padding: 2px 4px;
}}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {p.panel_alt}; color: {p.text}; }}
QMenu {{
    background: {p.panel};
    border: 1px solid {p.line_strong};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}
QMenu::separator {{ height: 1px; background: {p.line}; margin: 5px 8px; }}

QMessageBox {{ background: {p.panel}; }}
QDialog {{ background: {p.bg}; }}

QTextBrowser#Diagnosis {{
    background: {p.panel_alt};
    border: 1px solid {p.line};
    border-radius: 11px;
    padding: 15px;
    font-size: 12.5px;
}}
/* Default link blue is unreadable on a dark panel. */
QTextBrowser#Diagnosis a, QTextBrowser a {{
    color: {p.accent_hover};
    text-decoration: none;
}}

/* Release notes in the update dialog scroll like a document, not like a box. */
QTextBrowser#ReleaseNotes {{
    background: {p.bg_soft};
    border: 1px solid {p.line};
    border-radius: 11px;
    padding: 14px;
    font-size: 12.5px;
    line-height: 1.5;
}}

QLabel#UpdateStatus {{
    color: {p.text_mid};
    font-size: 12.5px;
}}
QLabel#UpdateStatus--good {{ color: {p.good}; font-size: 12.5px; font-weight: 600; }}
QLabel#UpdateStatus--bad {{ color: {p.bad}; font-size: 12.5px; }}
QLabel#UpdateStatus--busy {{ color: {p.accent}; font-size: 12.5px; }}
"""
