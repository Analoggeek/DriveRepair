"""AI Assistant: plain-language diagnosis plus official-source research."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import DoctorService


class AssistantPage(QWidget):
    SUBTITLE = "Plain-language explanation of what your hardware actually reported."

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(13)

        self.banner = InfoBanner()
        layout.addWidget(self.banner)

        top = Card("Diagnosis")
        self.confidence = Badge("no scan")
        top.add_header_widget(self.confidence)
        row = QHBoxLayout()
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(300)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 1)
        top.add_layout(row)
        top.add(label(
            "This assistant explains measured evidence. It cannot invent a repair, cannot run one, "
            "and says 'unavailable' when the hardware reports nothing.", name="Muted", wrap=True,
        ))
        self.diagnosis_view = QTextBrowser()
        self.diagnosis_view.setObjectName("Diagnosis")
        self.diagnosis_view.setOpenExternalLinks(False)
        self.diagnosis_view.setMinimumHeight(320)
        self.diagnosis_view.setHtml("<p style='color:#6d7c91'>Run a health scan to get a diagnosis.</p>")
        top.add(self.diagnosis_view)
        layout.addWidget(top)

        research = Card("Official source research")
        self.research_button = QPushButton("Research this model")
        self.research_button.setObjectName("Primary")
        self.research_button.clicked.connect(self.research)
        self.research_button.setEnabled(False)
        research.add_header_widget(self.research_button)
        research.add(label(
            "Only allow-listed HTTPS vendor and standards-body pages are queried, and every result "
            "shows its URL so you can check the original guidance yourself.",
            name="Muted", wrap=True,
        ))
        self.research_view = QTextBrowser()
        self.research_view.setObjectName("Diagnosis")
        self.research_view.setOpenExternalLinks(True)
        self.research_view.setMinimumHeight(220)
        self.research_view.setHtml(
            "<p style='color:#6d7c91'>Select a drive, then press 'Research this model'.</p>"
        )
        research.add(self.research_view)
        layout.addWidget(research)
        layout.addStretch(1)

    # ------------------------------------------------------------------ data
    def set_device(self, path: str) -> None:
        index = self.device_box.findData(path)
        if index >= 0:
            self.device_box.blockSignals(True)
            self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self.device_path = path
            self.research_button.setEnabled(True)
            self.apply_last_scan()

    def refresh_devices(self) -> None:
        current = self.device_path or self.device_box.currentData()
        self.device_box.blockSignals(True)
        self.device_box.clear()
        for device in self.service.devices()["devices"]:
            self.device_box.addItem(
                f"{device['display_name']} — {device['path']} ({human_size(device.get('size_bytes'))})",
                device["path"],
            )
        if current:
            index = self.device_box.findData(current)
            if index >= 0:
                self.device_box.setCurrentIndex(index)
        self.device_box.blockSignals(False)
        self.device_path = self.device_box.currentData()
        self.research_button.setEnabled(bool(self.device_path))

    def _device_changed(self) -> None:
        self.device_path = self.device_box.currentData()
        self.apply_last_scan()

    def apply_last_scan(self) -> None:
        if not self.device_path:
            return
        scan = self.service._last_scan.get(self.device_path)  # noqa: SLF001
        if scan is not None:
            self.apply_scan(scan.to_dict())

    def apply_scan(self, result: dict[str, Any]) -> None:
        diagnosis = result.get("diagnosis") or {}
        confidence = diagnosis.get("confidence", "low")
        self.confidence.set_status(
            f"confidence: {confidence}",
            {"high": "good", "medium": "warn"}.get(confidence, "neutral"),
        )
        parts = [
            "<div style='font-size:15px; font-weight:600; margin-bottom:4px'>"
            f"{_esc(diagnosis.get('headline', ''))}</div>",
            f"<p style='color:#a5b2c4'>{_esc(diagnosis.get('summary', ''))}</p>",
        ]
        for section in diagnosis.get("sections") or []:
            color = {"critical": "#ff3b5c", "error": "#ff5d6e", "warning": "#ffbe3d"}.get(
                section.get("severity"), "#6d7c91"
            )
            parts.append(
                f"<h4 style='color:{color}; margin:12px 0 4px'>{_esc(section.get('heading', ''))}</h4>"
            )
            parts.append("<ul style='margin:0 0 0 16px; padding:0'>")
            for bullet in section.get("bullets") or []:
                parts.append(f"<li style='margin:3px 0'>{_esc(bullet)}</li>")
            parts.append("</ul>")
        steps = diagnosis.get("recommended_steps") or []
        if steps:
            parts.append("<h4 style='margin:12px 0 4px'>Recommended order</h4><ol style='margin:0 0 0 16px'>")
            for step in steps:
                parts.append(f"<li style='margin:3px 0'>{_esc(step)}</li>")
            parts.append("</ol>")
        knowledge = diagnosis.get("knowledge") or []
        if knowledge:
            parts.append("<h4 style='margin:12px 0 4px'>Related known issues</h4>")
            for entry in knowledge:
                tone = "#6d7c91" if entry.get("software_fixable") else "#ffbe3d"
                fixable = "software-repairable" if entry.get("software_fixable") else "hardware — not software-repairable"
                parts.append(
                    f"<p style='margin:8px 0 2px'><b>{_esc(entry.get('title', ''))}</b> "
                    f"<span style='color:{tone}'>({fixable})</span></p>"
                )
                parts.append("<ul style='margin:0 0 0 16px'>")
                for step in (entry.get("steps") or [])[:4]:
                    parts.append(f"<li style='margin:2px 0'>{_esc(step)}</li>")
                parts.append("</ul>")
                for source in entry.get("sources") or []:
                    parts.append(
                        f"<div style='color:#6d7c91; font-size:11px'>source: "
                        f"<a href='{_esc(source)}'>{_esc(source)}</a></div>"
                    )
                for warning in entry.get("when_not_to") or []:
                    parts.append(f"<p style='color:#ffbe3d; margin:4px 0'>{_esc(warning)}</p>")
        friendly = diagnosis.get("friendly_message")
        if friendly:
            border = "#ffbe3d" if diagnosis.get("replacement_advised") else "#2ecc9a"
            parts.append(
                f"<div style='margin-top:14px; padding:12px; border:1px solid {border};"
                f" border-radius:9px; background: rgba(255,255,255,0.02)'>{_esc(friendly)}</div>"
            )
        self.diagnosis_view.setHtml("".join(parts))

    # --------------------------------------------------------------- research
    def research(self) -> None:
        if not self.device_path:
            self.banner.show_message("Select a drive first.", "warn")
            return
        self.research_button.setEnabled(False)
        self.research_view.setHtml("<p style='color:#6d7c91'>contacting official sources…</p>")
        try:
            payload = self.service.research(self.device_path)
        except Exception as exc:
            self.research_view.setHtml(f"<p style='color:#ff5d6e'>{_esc(str(exc))}</p>")
            self.research_button.setEnabled(True)
            return
        parts = ["<h4 style='margin:0 0 4px'>Search terms built from this exact device</h4><ul>"]
        for query in payload.get("queries") or []:
            parts.append(f"<li style='font-family:monospace; font-size:11px'>{_esc(query)}</li>")
        parts.append("</ul><h4 style='margin:12px 0 4px'>Official pages</h4>")
        for page in payload.get("official_pages") or []:
            color = "#6d7c91" if page.get("status") == "ok" else "#ffbe3d"
            parts.append(
                f"<p style='margin:6px 0'><span style='color:{color}'>[{_esc(page.get('status'))}]</span> "
                f"<a href='{_esc(page.get('url'))}'>{_esc(page.get('title') or page.get('url'))}</a>"
                + (f"<br><span style='color:#ffbe3d'>{_esc(page.get('error'))}</span>" if page.get("error") else "")
                + "</p>"
            )
        parts.append("<h4 style='margin:12px 0 4px'>Vendor support portals</h4><ul>")
        for link in payload.get("vendor_links") or []:
            parts.append(
                f"<li><a href='{_esc(link.get('url'))}'>{_esc(link.get('title'))}</a> — "
                f"<span style='color:#6d7c91'>{_esc(link.get('notes'))}</span></li>"
            )
        parts.append("</ul>")
        parts.append(f"<p style='color:#6d7c91; font-size:11px'>{_esc(payload.get('disclaimer', ''))}</p>")
        self.research_view.setHtml("".join(parts))
        self.research_button.setEnabled(True)

    def on_shown(self) -> None:
        self.refresh_devices()
        self.apply_last_scan()


def _esc(text: Any) -> str:
    import html

    return html.escape(str(text or ""))
