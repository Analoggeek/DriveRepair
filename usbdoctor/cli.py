"""USB Doctor AI command-line interface.

Every command maps 1:1 onto the same service calls the GUI uses, so behaviour
(and the safety rules) cannot drift between frontends.

    usbdoctor devices
    usbdoctor status
    usbdoctor scan /dev/sdb --mode smart --report ./report
    usbdoctor plan /dev/sdb --mode advanced
    usbdoctor repair /dev/sdb --mode smart --approve     # asks before writing
    usbdoctor image /dev/sdb backup.img
    usbdoctor recover /dev/sdb --out ./recovered
    usbdoctor capacity /dev/sdb --approve
    usbdoctor report /dev/sdb --format html --out report.html
    usbdoctor serve --port 8765 --open
"""

from __future__ import annotations

import argparse
from argparse import _SubParsersAction
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import MODES, DoctorService, ScanOptions
from usbdoctor.version import APP_NAME, __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSAFE = 2
EXIT_USER_ABORT = 3

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
RESET = "\033[0m"


def _color(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usbdoctor",
        description=f"{APP_NAME} {__version__} — intelligent storage diagnosis, repair and recovery",
        epilog="No command writes to a device without --approve and an explicit confirmation.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--state-dir", help="override the application data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list storage devices")
    sub.add_parser("status", help="environment, tools and policy")

    scan = sub.add_parser("scan", help="read-only health scan")
    scan.add_argument("device")
    scan.add_argument("--mode", choices=MODES, default="smart")
    scan.add_argument("--time-budget", type=float, default=300.0,
                      help="surface scan seconds (0 = full device)")
    scan.add_argument("--no-surface", action="store_true", help="skip the surface scan")
    scan.add_argument("--no-smart", action="store_true", help="skip SMART (useful without smartctl)")
    scan.add_argument("--report", help="write report files with this base name")
    scan.add_argument("--watch", action="store_true", help="stream live progress events")

    plan = sub.add_parser("plan", help="show applicable repairs and their risk")
    plan.add_argument("device")
    plan.add_argument("--mode", choices=MODES, default="smart")

    repair = sub.add_parser("repair", help="run repairs (requires --approve)")
    repair.add_argument("device")
    repair.add_argument("--mode", choices=MODES, default="smart")
    repair.add_argument("--approve", action="store_true", help="approve the operations shown")
    repair.add_argument("--only", help="comma-separated operation ids")
    repair.add_argument("--dry-run", action="store_true", help="simulate without writing")
    repair.add_argument("--yes", action="store_true", help="skip the interactive confirmation")

    image = sub.add_parser("image", help="create a resumable sector image")
    image.add_argument("device")
    image.add_argument("output")
    image.add_argument("--no-ddrescue", action="store_true", help="use the built-in imager only")

    recover = sub.add_parser("recover", help="recover files (never writes to the source)")
    recover.add_argument("device")
    recover.add_argument("--out", help="output folder")
    recover.add_argument("--method", choices=("auto", "filesystem", "carve"), default="auto")
    recover.add_argument("--no-deleted", action="store_true", help="skip deleted directory entries")

    capacity = sub.add_parser("capacity", help="verify the real capacity (writes a temp file)")
    capacity.add_argument("device")
    capacity.add_argument("--approve", action="store_true")
    capacity.add_argument("--yes", action="store_true")

    report = sub.add_parser("report", help="render the last scan as a report")
    report.add_argument("device")
    report.add_argument("--format", choices=("markdown", "html"), default="markdown")
    report.add_argument("--out", help="write to this file instead of stdout")

    research = sub.add_parser("research", help="official sources for this exact model")
    research.add_argument("device")

    history = sub.add_parser("history", help="recent scans and repairs")
    history.add_argument("--limit", type=int, default=20)

    serve = sub.add_parser("serve", help="start the local web interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="open a browser")
    serve.add_argument("--no-token", action="store_true", help="disable the session token (local only)")

    sub.add_parser(
        "selfcheck",
        help="verify this installation end-to-end (used by the installers)",
    ).add_argument("--json", action="store_true", dest="json")
    return parser


def _selfcheck(*, json_out: bool = False) -> int:
    """Prove an installation works: data files, engine, and the desktop pages.

    Run by the Windows/macOS build scripts and by the installers after
    installation. Everything is offline: no network, no device access, no writes
    outside a temporary directory.
    """
    import importlib
    import tempfile
    from pathlib import Path

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            checks.append((name, True, str(detail or "")))
        except Exception as exc:  # report, never crash the self-check itself
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    # 1. Bundled data files must be present inside the frozen bundle.
    def packaged_data() -> str:
        root = Path(__file__).resolve().parent
        missing = [
            name
            for name in ("frontend/web/index.html", "frontend/web/app.css", "frontend/web/app.js")
            if not (root / name).exists()
        ]
        if missing:
            raise FileNotFoundError(f"missing packaged data: {', '.join(missing)}")
        return "frontend assets present"

    check("packaged data", packaged_data)

    # 2. Every lazily-imported GUI page must resolve.
    def gui_modules() -> str:
        pages = [
            "usbdoctor.app.main", "usbdoctor.app.main_window", "usbdoctor.app.theme",
            "usbdoctor.app.widgets.common", "usbdoctor.app.pages.dashboard",
            "usbdoctor.app.pages.devices", "usbdoctor.app.pages.scan",
            "usbdoctor.app.pages.repair", "usbdoctor.app.pages.recovery",
            "usbdoctor.app.pages.performance", "usbdoctor.app.pages.assistant",
            "usbdoctor.app.pages.history", "usbdoctor.app.pages.reports",
            "usbdoctor.app.pages.settings",
        ]
        for name in pages:
            importlib.import_module(name)
        return f"{len(pages)} GUI modules importable"

    check("gui modules", gui_modules)

    # 3. The desktop window must actually build (offscreen: no display needed).
    def gui_window() -> str:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from usbdoctor.app.main_window import MainWindow
        from usbdoctor.service import DoctorService

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(DoctorService(state_dir=Path(tmp) / "state"), theme="dark")
            count = len(window.pages)
            window.close()
            app.processEvents()
        if count < 10:
            raise RuntimeError(f"only {count} pages were constructed")
        return f"{count} pages constructed"

    check("desktop window", gui_window)

    # 4. Engine + database must work in a scratch state directory.
    def engine() -> str:
        from usbdoctor.service import DoctorService

        # Always a private scratch directory: the self-check must never create
        # or touch the real state directory (it runs during installation).
        saved = os.environ.pop("USBDOCTOR_STATE_DIR", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                status = DoctorService(state_dir=Path(tmp) / "state").status()
        finally:
            if saved is not None:
                os.environ["USBDOCTOR_STATE_DIR"] = saved
        if status.get("app") != "USB Doctor AI":
            raise RuntimeError(f"unexpected status payload: {status.get('app')!r}")
        return f"engine ok on {status['platform']}"

    check("engine", engine)

    # 5. Every CLI verb the launcher dispatches must exist.
    def verbs() -> str:
        known = {
            "devices", "status", "scan", "plan", "repair", "image", "recover",
            "capacity", "report", "research", "history", "serve", "selfcheck",
        }
        parser = build_parser()
        actions = [
            action for action in parser._actions if isinstance(action, _SubParsersAction)
        ]
        registered = set(actions[0].choices) if actions else set()
        missing = known - registered
        if missing:
            raise RuntimeError(f"missing verbs: {', '.join(sorted(missing))}")
        return f"{len(registered)} verbs registered"

    check("cli verbs", verbs)

    ok = all(passed for _, passed, _ in checks)
    if json_out:
        print(json.dumps({"ok": ok, "checks": [
            {"name": n, "passed": p, "detail": d} for n, p, d in checks
        ]}, indent=2))
    else:
        for name, passed, detail in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {name:22} {detail}")
        print("self-check:", "OK" if ok else "FAILED")
    return EXIT_OK if ok else 1


def _service(args: argparse.Namespace) -> DoctorService:
    return DoctorService(state_dir=Path(args.state_dir) if args.state_dir else None)


def _print_devices(service: DoctorService, *, json_out: bool, color: bool) -> int:
    listing = service.devices()
    if json_out:
        print(json.dumps(listing, indent=2, default=str))
        return EXIT_OK
    devices = listing["devices"]
    print(_color(f"{APP_NAME} {__version__}", BOLD, color))
    print(f"platform: {listing['platform']}   rights: {'administrator' if listing['is_admin'] else 'standard user'}")
    print(f"enumerated via: {listing['source']}\n")
    if not devices:
        print("No storage devices were discovered.")
        for warning in listing["warnings"]:
            print(f"  ! {warning}")
        return EXIT_OK
    header = f"{'PATH':<22}{'NAME':<34}{'SIZE':>10}  {'BUS':<6}{'FS':<8}{'RW':<4}HEALTH"
    print(_color(header, BOLD, color))
    print(_color("-" * len(header), DIM, color))
    for device in devices:
        writable = "yes" if (device.get("can_write") or [False])[0] else "no"
        health = device.get("health_hint") or "not reported"
        line = (
            f"{device['path']:<22}{(device['display_name'] or 'unknown')[:33]:<34}"
            f"{human_size(device['size_bytes']):>10}  {(device['bus'] or '?')[:5]:<6}"
            f"{(device['filesystem'] or '-')[:7]:<8}{writable:<4}{health}"
        )
        print(line)
    warnings = listing.get("warnings") or []
    if warnings:
        print()
        for warning in warnings:
            print(_color(f"  ! {warning}", YELLOW, color))
    return EXIT_OK


def _print_scan(result, *, json_out: bool, color: bool) -> int:
    if json_out:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return EXIT_OK
    health = result.health
    palette = {
        "EXCELLENT": GREEN, "GOOD": GREEN, "WARNING": YELLOW,
        "CRITICAL": RED, "FAILING": RED,
    }
    code = palette.get(health.verdict.name, DIM)
    print(_color(f"[{health.verdict.name}] {health.verdict_summary}", code + BOLD, color))
    print(f"device   : {result.device.display_name} ({result.device.path})")
    print(f"capacity : {human_size(result.device.size_bytes)}")
    print(f"file sys : {result.evidence.filesystem}")
    print(f"took     : {result.duration_s:.1f}s   confidence: {health.health_confidence}")
    if result.smart:
        if result.smart.get("available"):
            print(f"SMART    : {result.smart.get('health_text') or 'reported'}")
        else:
            print(f"SMART    : unavailable — {result.smart.get('reason')}")
    if result.surface:
        print(
            f"surface  : {human_size(result.surface['scanned_bytes'])} scanned, "
            f"{len(result.surface.get('read_errors') or [])} unreadable region(s)"
        )
    if result.performance and result.performance.get("rating"):
        print(
            f"speed    : {result.performance['median_mbps']:.1f} MB/s median "
            f"({result.performance['rating']} for {result.performance.get('interface') or 'this interface'})"
        )
    print()
    print(_color(result.diagnosis.headline, BOLD, color))
    print(result.diagnosis.summary)
    if health.findings:
        print()
        for finding in health.findings:
            tag = finding.severity.upper()
            color_code = {"critical": RED, "error": RED, "warning": YELLOW}.get(finding.severity, DIM)
            print(f"  {_color(tag.ljust(9), color_code, color)} {finding.message}")
    if result.plans:
        print()
        print(_color("applicable repairs", BOLD, color))
        for plan in result.plans:
            print(f"  [{plan.risk.value:<8}] {plan.operation_id:<16} {plan.title}")
    else:
        print()
        print("No automatic repair applies. Use 'usbdoctor recover' if files are missing.")
    for limitation in result.limitations:
        print(_color(f"  note: {limitation}", DIM, color))
    return EXIT_OK


def _confirm(prompt: str, *, assume_yes: bool, color: bool) -> bool:
    if assume_yes:
        return True
    print(_color(prompt, YELLOW, color))
    try:
        answer = input("Type 'yes' to continue: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("yes", "y")


def _run_scan_with_progress(service: DoctorService, device: str, options: ScanOptions, color: bool):
    """Scan in a worker while printing real events from the bus."""
    import queue as _queue
    import threading
    import time

    bus = service.bus
    listener = bus.subscribe()
    holder: dict[str, Any] = {}

    def worker() -> None:
        try:
            holder["result"] = service.scan(device, options)
        except Exception as exc:  # reported below
            holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    last_stage = ""
    while thread.is_alive() or not listener.empty():
        try:
            event = listener.get(timeout=0.15)
        except _queue.Empty:
            if not thread.is_alive():
                break
            continue
        if event.type == "stage":
            message = event.payload.get("message")
            if message and message != last_stage:
                last_stage = message
                print(_color(f"  · {message}", DIM, color))
        elif event.type == "progress":
            percent = event.payload.get("percent")
            stage = event.payload.get("stage", "")
            if percent is not None:
                sys.stdout.write(f"\r  {stage}: {percent:5.1f}%   ")
                sys.stdout.flush()
        if event.type == "job_finished":
            break
    sys.stdout.write("\r" + " " * 60 + "\r")
    bus.unsubscribe(listener)
    thread.join(timeout=5)
    if "error" in holder:
        raise holder["error"]
    return holder["result"]


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    color = (not args.no_color) and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    service: Optional[DoctorService] = None
    try:
        command = args.command

        # selfcheck must not construct the service: doing so would create the
        # user's state directory (and a history database) merely to prove the
        # installation works. It uses its own scratch directory instead.
        if command == "selfcheck":
            return _selfcheck(json_out=getattr(args, "json", False))

        service = _service(args)

        if command == "devices":
            return _print_devices(service, json_out=args.json, color=color)

        if command == "status":
            status = service.status()
            if args.json:
                print(json.dumps(status, indent=2, default=str))
                return EXIT_OK
            print(_color(f"{status['app']} {status['version']}", BOLD, color))
            print(f"platform  : {status['platform']}")
            print(f"rights    : {'administrator' if status['is_admin'] else 'standard user (limited raw access)'}")
            print(f"state dir : {status['state_dir']}")
            policy = status["policy"]
            print(f"policy    : internal disks {'allowed' if policy['allow_internal_disks'] else 'blocked'}, "
                  f"mounted writes {'allowed' if policy['allow_mounted_write'] else 'blocked'}")
            print("tools     :" + "".join(
                f"\n  {'' if present else '-'} {name}" for name, present in status["tools"].items()
            ))
            stats = status["stats"]
            print(f"history   : {stats['scans']} scans, {stats['repairs_succeeded']} repairs, "
                  f"{stats['files_recovered']} files recovered")
            return EXIT_OK

        if command == "scan":
            options = ScanOptions(
                mode=args.mode,
                read_smart=not args.no_smart,
                surface_scan=not args.no_surface,
                surface_time_budget_s=(args.time_budget or None),
            )
            if args.watch and not args.json:
                result = _run_scan_with_progress(service, args.device, options, color)
            else:
                result = service.scan(args.device, options)
            code = _print_scan(result, json_out=args.json, color=color)
            if args.report:
                from usbdoctor.report import ReportBundle, save

                data = result.to_dict()
                paths = save(
                    ReportBundle(
                        device=data["device"], health=data["health"], filesystem=data.get("filesystem"),
                        partition=data.get("partition"), smart=data.get("smart"), surface=data.get("surface"),
                        performance=data.get("performance"), diagnosis=data.get("diagnosis"),
                        limitations=data.get("limitations"),
                    ),
                    args.report,
                )
                print()
                for kind, path in paths.items():
                    print(f"{kind:9s} report: {path}")
            return code

        if command == "plan":
            payload = service.plan(args.device, mode=args.mode)
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
                return EXIT_OK
            print(f"mode: {payload['mode']}")
            if payload["evidence_codes"]:
                print("evidence: " + ", ".join(payload["evidence_codes"]))
            if not payload["plans"]:
                print("No repair applies to this device.")
                return EXIT_OK
            for plan in payload["plans"]:
                print(f"\n[{plan['risk'].upper()}] {plan['operation_id']} — {plan['title']}")
                print(f"  {plan['summary']}")
                print(f"  needs consent: {'yes' if plan['requires_consent'] else 'no'}")
            return EXIT_OK

        if command == "repair":
            if not args.approve and not args.dry_run:
                print(_color("Refusing to write without --approve.", RED, color))
                print("Run 'usbdoctor plan " + args.device + "' first, then re-run with --approve.")
                return EXIT_UNSAFE
            plan_payload = service.plan(args.device, mode=args.mode)
            plans = plan_payload["plans"]
            if args.only:
                wanted = {item.strip() for item in args.only.split(",")}
                plans = [plan for plan in plans if plan["operation_id"] in wanted]
            if not plans:
                print("No applicable operations.")
                return EXIT_OK
            if not args.json:
                print(_color("These operations will run:", BOLD, color))
                for plan in plans:
                    print(f"  [{plan['risk'].upper()}] {plan['operation_id']} — {plan['title']}")
                print(f"\nDevice: {args.device}")
            if not args.dry_run:
                if not _confirm("Continue and modify the device?", assume_yes=args.yes, color=color):
                    print("Cancelled — nothing was written.")
                    return EXIT_USER_ABORT
            approval = service.approve(
                args.device, operation_ids=[plan["operation_id"] for plan in plans], mode=args.mode
            )
            report = service.execute(
                args.device,
                consent_token=approval["consent_token"],
                mode=args.mode,
                operation_ids=[plan["operation_id"] for plan in plans],
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(report, indent=2, default=str))
            else:
                for step in report["steps"]:
                    result = step["result"]
                    mark = {"succeeded": "✓", "failed": "✗", "skipped": "–"}.get(result["status"], "?")
                    print(f"{mark} {step['plan']['operation_id']:<18} {result['status']:<10} "
                          f"verified={result['verified']} {result['message']}")
                print(f"\n{report['succeeded']} succeeded, {report['failed']} failed")
            return EXIT_OK if not report["failed"] else EXIT_ERROR

        if command == "image":
            payload = service.image_device(
                args.device, args.output, prefer_ddrescue=not args.no_ddrescue
            )
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print(f"image     : {payload.get('image_path')}")
                print(f"good bytes: {human_size(payload.get('good_bytes'))}")
                print(f"bad areas : {len(payload.get('bad_regions') or [])}")
                print(f"engine    : {payload.get('engine')}")
            return EXIT_OK

        if command == "recover":
            payload = service.recover(
                args.device,
                method=args.method,
                output_dir=args.out,
                include_deleted=not args.no_deleted,
            )
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print(f"method     : {payload.get('method')}")
                print(f"files      : {payload.get('files_recovered')}")
                print(f"bytes      : {human_size(payload.get('bytes_recovered'))}")
                print(f"output     : {payload.get('output_dir')}")
                carved = payload.get("carved") or {}
                by_category = carved.get("by_category") or {}
                if by_category:
                    print("categories : " + ", ".join(f"{k}={v}" for k, v in by_category.items()))
            return EXIT_OK

        if command == "capacity":
            if not args.approve:
                print(_color("Capacity testing writes temporary data and needs --approve.", YELLOW, color))
                return EXIT_UNSAFE
            if not _confirm(
                "This writes and deletes a temporary pattern file on the drive. Continue?",
                assume_yes=args.yes,
                color=color,
            ):
                print("Cancelled.")
                return EXIT_USER_ABORT
            device = service.device(args.device)
            if device is None:
                raise UsbDoctorError(f"{args.device} is not connected.")
            approval = service.approve(args.device, mode="advanced", operation_ids=[])
            payload = service.capacity_test(
                args.device, consent_token=approval["consent_token"]
            )
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print(f"verdict  : {payload['verdict']}")
                print(f"tested   : {human_size(payload.get('tested_bytes'))}")
                if payload.get("real_capacity_estimate"):
                    print(f"real size: {human_size(payload['real_capacity_estimate'])}")
                print(payload["message"])
            return EXIT_OK

        if command == "report":
            from usbdoctor.report import ReportBundle, to_html, to_markdown

            last = service._last_scan.get(args.device)  # noqa: SLF001 - CLI convenience
            if last is None:
                service.scan(args.device, ScanOptions(surface_time_budget_s=60.0))
                last = service._last_scan[args.device]  # noqa: SLF001
            data = last.to_dict()
            bundle = ReportBundle(
                device=data["device"], health=data["health"], filesystem=data.get("filesystem"),
                partition=data.get("partition"), smart=data.get("smart"), surface=data.get("surface"),
                performance=data.get("performance"), diagnosis=data.get("diagnosis"),
                limitations=data.get("limitations"),
            )
            body = to_html(bundle) if args.format == "html" else to_markdown(bundle)
            if args.out:
                Path(args.out).write_text(body, encoding="utf-8")
                print(f"written: {args.out}")
            else:
                print(body)
            return EXIT_OK

        if command == "research":
            payload = service.research(args.device)
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
                return EXIT_OK
            print(_color("queries used", BOLD, color))
            for query in payload["queries"]:
                print(f"  {query}")
            print(_color("\nofficial pages", BOLD, color))
            for page in payload["official_pages"]:
                print(f"  [{page['status']}] {page['url']}")
                if page.get("error"):
                    print(f"        {page['error']}")
            print(_color("\nvendor portals", BOLD, color))
            for link in payload["vendor_links"]:
                print(f"  {link['vendor']:<14} {link['url']}")
            print()
            print(payload["disclaimer"])
            return EXIT_OK

        if command == "history":
            overview = service.history_overview()
            if args.json:
                print(json.dumps(overview, indent=2, default=str))
                return EXIT_OK
            print(_color("recent scans", BOLD, color))
            for scan in overview["recent_scans"][: args.limit]:
                print(f"  {scan['started_at'][:19]}  {scan['verdict'] or '-':<10} "
                      f"{scan['device_name'] or scan['device_path']}")
            print(_color("\nrecent repairs", BOLD, color))
            for repair in overview["recent_repairs"][: args.limit]:
                print(f"  {repair['started_at'][:19]}  {repair['status']:<10} {repair['operation_id']}")
            return EXIT_OK

        if command == "serve":
            from usbdoctor.webapi.server import serve as serve_api

            serve_api(
                host=args.host,
                port=args.port,
                service=service,
                require_token=not args.no_token,
                open_browser=args.open,
            )
            return EXIT_OK

        parser.print_help()
        return EXIT_ERROR
    except UsbDoctorError as exc:
        if args.json:
            print(json.dumps({"error": exc.user_message, "hint": getattr(exc, "hint", "")}))
        else:
            print(_color(f"error: {exc.user_message}", RED, color), file=sys.stderr)
            hint = getattr(exc, "hint", "")
            if hint:
                print(f"hint : {hint}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_USER_ABORT
    finally:
        # AuditLog writes synchronously per entry, so there is nothing to flush;
        # the hook stays for symmetry with future buffered writers.
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
