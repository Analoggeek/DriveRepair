"""Human-readable report generation (Markdown + JSON + self-contained HTML).

Reports are built only from values the engine actually measured; any field the
hardware did not provide is printed as "not reported" rather than a filler value.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Optional

from usbdoctor.core.util import human_size, iso_now
from usbdoctor.version import APP_NAME, __version__


def _fmt(value: Any, *, suffix: str = "") -> str:
    if value is None or value == "":
        return "not reported"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return f"{value}{suffix}"


@dataclass
class ReportBundle:
    """Everything a report renders, kept explicit so nothing is invented."""

    device: dict[str, Any]
    health: Optional[dict[str, Any]] = None
    filesystem: Optional[dict[str, Any]] = None
    partition: Optional[dict[str, Any]] = None
    smart: Optional[dict[str, Any]] = None
    surface: Optional[dict[str, Any]] = None
    performance: Optional[dict[str, Any]] = None
    diagnosis: Optional[dict[str, Any]] = None
    repairs: Optional[list[dict[str, Any]]] = None
    recovery: Optional[dict[str, Any]] = None
    limitations: Optional[list[str]] = None
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": __version__,
            "generated_at": self.generated_at or iso_now(),
            "device": self.device,
            "health": self.health,
            "filesystem": self.filesystem,
            "partition": self.partition,
            "smart": self.smart,
            "surface": self.surface,
            "performance": self.performance,
            "diagnosis": self.diagnosis,
            "repairs": self.repairs,
            "recovery": self.recovery,
            "limitations": self.limitations or [],
        }


def to_markdown(bundle: ReportBundle) -> str:
    data = bundle.to_dict()
    device = data["device"]
    health = data.get("health") or {}
    lines: list[str] = []
    lines.append(f"# {APP_NAME} — Device Health Report")
    lines.append("")
    lines.append(f"Generated: {data['generated_at']}  ")
    lines.append(f"Version: {data['version']}")
    lines.append("")
    lines.append("## Device")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    for label, key in (
        ("Name", "display_name"),
        ("Path", "path"),
        ("Capacity", "size_bytes"),
        ("Interface", "bus"),
        ("USB speed", "usb_speed"),
        ("Vendor ID", "vid"),
        ("Product ID", "pid"),
        ("Serial", "serial"),
        ("Firmware", "firmware"),
        ("File system", "filesystem"),
        ("Removable", "removable"),
        ("Read-only", "read_only"),
    ):
        value = device.get(key)
        if key == "size_bytes":
            value = human_size(value) if value else "not reported"
        lines.append(f"| {label} | {_fmt(value)} |")
    lines.append("")
    if data["health"]:
        lines.append("## Health verdict")
        lines.append("")
        lines.append(f"**{health.get('verdict')} — {health.get('verdict_summary', '')}**")
        lines.append("")
        lines.append(f"Confidence: {health.get('health_confidence', 'low')}")
        lines.append("")
        counts = health.get("counts") or {}
        lines.append(
            f"Findings: {counts.get('critical', 0)} critical, {counts.get('error', 0)} error, "
            f"{counts.get('warning', 0)} warning"
        )
        lines.append("")
        if health.get("findings"):
            lines.append("| Severity | Finding |")
            lines.append("| --- | --- |")
            for finding in health["findings"]:
                lines.append(f"| {finding.get('severity')} | {finding.get('message')} |")
            lines.append("")
        if health.get("explanation"):
            for note in health["explanation"]:
                lines.append(f"- {note}")
            lines.append("")
    if data.get("smart"):
        smart = data["smart"]
        lines.append("## SMART")
        lines.append("")
        if smart.get("available"):
            lines.append(f"Source: {smart.get('source')}")
            lines.append("")
            lines.append(f"- Health: {_fmt(smart.get('health_text'))}")
            lines.append(f"- Temperature: {_fmt(smart.get('temperature_c'), suffix=' °C')}")
            lines.append(f"- Power-on hours: {_fmt(smart.get('power_on_hours'))}")
            if smart.get("attributes"):
                lines.append("")
                lines.append("| ID | Attribute | Value | Raw |")
                lines.append("| --- | --- | --- | --- |")
                for attr in smart["attributes"][:24]:
                    lines.append(
                        f"| {attr['id']} | {attr['name']} | {attr['value']} | {attr.get('raw_text') or attr['raw']} |"
                    )
        else:
            lines.append("SMART data is **not available** for this device.")
            lines.append("")
            lines.append(f"Reason: {smart.get('reason') or 'not reported by the device'}")
        lines.append("")
    if data.get("surface"):
        surface = data["surface"]
        lines.append("## Surface scan")
        lines.append("")
        lines.append(f"- Scanned: {human_size(surface.get('scanned_bytes'))}")
        lines.append(f"- Unreadable regions: {len(surface.get('read_errors') or [])}")
        lines.append(f"- Duration: {surface.get('duration_s', 0):.1f}s")
        if surface.get("read_speed_bps"):
            lines.append(f"- Average read speed: {surface['read_speed_bps'] / 1e6:.1f} MB/s")
        lines.append("")
    if data.get("filesystem"):
        fs = data["filesystem"]
        lines.append("## File system")
        lines.append("")
        lines.append(f"- Detected: {fs.get('filesystem', 'unknown')}")
        issues = fs.get("issues") or []
        if issues:
            lines.append("")
            lines.append("| Severity | Code | Message | Repairable |")
            lines.append("| --- | --- | --- | --- |")
            for issue in issues:
                lines.append(
                    f"| {issue.get('severity')} | {issue.get('code')} | {issue.get('message')} | "
                    f"{'yes' if issue.get('repairable') else 'no'} |"
                )
        else:
            lines.append("- No structural issues detected.")
        lines.append("")
    if data.get("repairs"):
        lines.append("## Repairs performed")
        lines.append("")
        lines.append("| Operation | Risk | Status | Verified | Message |")
        lines.append("| --- | --- | --- | --- | --- |")
        for step in data["repairs"]:
            plan = step.get("plan") or {}
            result = step.get("result") or {}
            lines.append(
                f"| {plan.get('title')} | {plan.get('risk')} | {result.get('status')} | "
                f"{_fmt(result.get('verified'))} | {result.get('message')} |"
            )
        lines.append("")
    if data.get("diagnosis"):
        diagnosis = data["diagnosis"]
        lines.append("## Diagnosis")
        lines.append("")
        lines.append(f"**{diagnosis.get('headline')}**")
        lines.append("")
        lines.append(diagnosis.get("summary", ""))
        lines.append("")
        for section in diagnosis.get("sections") or []:
            lines.append(f"### {section.get('heading')}")
            for bullet in section.get("bullets") or []:
                lines.append(f"- {bullet}")
            lines.append("")
        if diagnosis.get("friendly_message"):
            lines.append(f"> {diagnosis['friendly_message']}")
            lines.append("")
    if data.get("limitations"):
        lines.append("## Limitations of this scan")
        lines.append("")
        for limitation in data["limitations"]:
            lines.append(f"- {limitation}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "USB Doctor AI reports only what the hardware and operating system actually provided. "
        "Fields shown as *not reported* were unavailable on this device."
    )
    return "\n".join(lines)


def to_html(bundle: ReportBundle) -> str:
    """Self-contained HTML (no external assets, safe to email or archive)."""
    data = bundle.to_dict()
    device = data["device"]
    health = data.get("health") or {}
    verdict = health.get("verdict", "UNKNOWN")
    color = health.get("verdict_color", "#8b95a7")
    rows = []
    for label, key in (
        ("Capacity", "size_bytes"),
        ("Interface", "bus"),
        ("USB speed", "usb_speed"),
        ("Vendor ID", "vid"),
        ("Product ID", "pid"),
        ("Serial", "serial"),
        ("Firmware", "firmware"),
        ("File system", "filesystem"),
    ):
        value = device.get(key)
        if key == "size_bytes":
            value = human_size(value) if value else "not reported"
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(_fmt(value)))}</td></tr>")
    findings = "".join(
        f"<tr class='sev-{html.escape(f.get('severity','info'))}'><td>{html.escape(f.get('severity',''))}"
        f"</td><td>{html.escape(f.get('message',''))}</td></tr>"
        for f in (health.get("findings") or [])
    )
    repair_rows = ""
    if data.get("repairs"):
        repair_rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(str((step.get("plan") or {}).get("title", ""))),
                html.escape(str((step.get("plan") or {}).get("risk", ""))),
                html.escape(str((step.get("result") or {}).get("status", ""))),
                html.escape(str((step.get("result") or {}).get("message", ""))),
            )
            for step in data["repairs"]
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(APP_NAME)} report — {html.escape(str(device.get('display_name')))}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background:#0b0f16; color:#e7ecf3;
        margin:0; padding:40px; line-height:1.55; }}
.wrap {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 26px; margin:0 0 4px; letter-spacing:-0.02em; }}
.sub {{ color:#8b95a7; margin-bottom: 28px; font-size:14px; }}
.verdict {{ display:inline-block; padding:6px 14px; border-radius:999px; font-weight:600;
            background:{color}22; color:{color}; border:1px solid {color}55; margin-bottom:18px; }}
.card {{ background:#121824; border:1px solid #1e2734; border-radius:14px; padding:20px 22px; margin-bottom:18px; }}
.card h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.08em; color:#8b95a7; margin:0 0 14px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th {{ text-align:left; color:#8b95a7; font-weight:500; padding:6px 10px 6px 0; width:190px; }}
td {{ padding:6px 0; border-bottom:1px solid #1a2130; }}
tr:last-child td {{ border-bottom:none; }}
.sev-critical td:first-child, .sev-error td:first-child {{ color:#ff6b7a; }}
.sev-warning td:first-child {{ color:#ffc857; }}
footer {{ color:#5c6879; font-size:12px; margin-top:30px; }}
</style></head>
<body><div class="wrap">
<h1>{html.escape(APP_NAME)}</h1>
<div class="sub">{html.escape(str(device.get('display_name')))} · {html.escape(str(device.get('path')))} ·
generated {html.escape(data['generated_at'])} · v{html.escape(data['version'])}</div>
<div class="verdict">{html.escape(verdict)} — {html.escape(health.get('verdict_summary','no verdict'))}</div>
<div class="card"><h2>Device</h2><table>{''.join(rows)}</table></div>
<div class="card"><h2>Findings</h2>
<table><tr><th>Severity</th><th>Detail</th></tr>{findings or '<tr><td colspan=2>No findings</td></tr>'}</table></div>
{'' if not repair_rows else f'<div class="card"><h2>Repairs performed</h2><table><tr><th>Operation</th><th>Risk</th><th>Status</th><th>Result</th></tr>{repair_rows}</table></div>'}
<footer>USB Doctor AI reports only data actually provided by the device and operating system.
Fields marked "not reported" were unavailable on this hardware.</footer>
</div></body></html>"""


def save(bundle: ReportBundle, base_path: str) -> dict[str, str]:
    """Write .md, .json and .html versions; returns the created paths."""
    paths = {}
    md = base_path + ".md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(bundle))
    paths["markdown"] = md
    js = base_path + ".json"
    with open(js, "w", encoding="utf-8") as fh:
        json.dump(bundle.to_dict(), fh, indent=2, default=str)
    paths["json"] = js
    ht = base_path + ".html"
    with open(ht, "w", encoding="utf-8") as fh:
        fh.write(to_html(bundle))
    paths["html"] = ht
    return paths
