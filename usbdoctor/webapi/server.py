"""Local HTTP + WebSocket API.

Only ever bound to loopback in normal desktop use; the preview/proxy mode binds
0.0.0.0 but still requires a per-session token on every mutating endpoint.

Security model:
* a random session token is generated at start-up and required for every
  POST/PUT/DELETE request (header ``X-USBDoctor-Token`` or ``?token=``);
* the token is also required for the event stream;
* no endpoint can execute a repair without a consent token issued by
  ``POST /api/repair/approve`` (which itself records the approval in the audit log);
* request bodies are size-limited and validated, and errors are returned as
  structured JSON with a human-readable message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import secrets
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.events import EventBus
from usbdoctor.core.platform import is_admin
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import MODES, DoctorService, ScanOptions
from usbdoctor.version import APP_NAME, __version__

LOG = logging.getLogger(__name__)
MAX_BODY = 256 * 1024
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "web"


class TokenStore:
    """Constant-time token checks plus simple rate limiting."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self._failures: dict[str, int] = {}

    def check(self, supplied: Optional[str], remote: str = "") -> bool:
        if supplied and secrets.compare_digest(supplied, self.token):
            self._failures.pop(remote, None)
            return True
        self._failures[remote] = self._failures.get(remote, 0) + 1
        return False

    def throttled(self, remote: str) -> bool:
        return self._failures.get(remote, 0) > 25


class ApiRequest:
    def __init__(self, method: str, path: str, query: dict[str, list[str]], headers: dict[str, str],
                 body: bytes, remote: str) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.remote = remote

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        if len(self.body) > MAX_BODY:
            raise ValueError("Request body is too large.")
        try:
            payload = json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def parameter(self, name: str) -> Optional[str]:
        values = self.query.get(name) or []
        return values[0] if values else None

    def flag(self, name: str, default: bool = False) -> bool:
        raw = self.parameter(name)
        if raw is None:
            return default
        return raw.lower() in ("1", "true", "yes", "on")

    def integer(self, name: str) -> Optional[int]:
        raw = self.parameter(name)
        if raw is None or raw == "":
            return None
        return int(raw)


class ApiError(Exception):
    def __init__(self, status: int, message: str, hint: str = "", code: str = "error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint
        self.code = code


class ApiApp:
    """Framework-free WSGI callable: no extra dependencies to install."""

    def __init__(self, service: Optional[DoctorService] = None, *, require_token: bool = True) -> None:
        self.service = service or DoctorService()
        self.bus: EventBus = self.service.bus
        self.tokens = TokenStore()
        self.require_token = require_token
        self.routes = {
            ("GET", "/api/status"): self.get_status,
            ("GET", "/api/devices"): self.get_devices,
            ("POST", "/api/devices/refresh"): self.refresh_devices,
            ("GET", "/api/devices/detail"): self.get_device_detail,
            ("POST", "/api/scan"): self.post_scan,
            ("GET", "/api/jobs"): self.get_jobs,
            ("GET", "/api/jobs/detail"): self.get_job,
            ("POST", "/api/jobs/cancel"): self.cancel_job,
            ("GET", "/api/scan/last"): self.get_last_scan,
            ("POST", "/api/plan"): self.post_plan,
            ("POST", "/api/repair/approve"): self.post_approve,
            ("POST", "/api/repair/execute"): self.post_execute,
            ("POST", "/api/repair/dry-run"): self.post_dry_run,
            ("POST", "/api/repair/emergency"): self.post_emergency,
            ("POST", "/api/backup/image"): self.post_image,
            ("POST", "/api/recovery/run"): self.post_recovery,
            ("POST", "/api/capacity/test"): self.post_capacity,
            ("POST", "/api/research"): self.post_research,
            ("GET", "/api/history"): self.get_history,
            ("GET", "/api/history/scan"): self.get_history_scan,
            ("POST", "/api/history/purge"): self.post_purge,
            ("GET", "/api/settings"): self.get_settings,
            ("POST", "/api/settings"): self.post_settings,
            ("GET", "/api/events"): self.get_events,
            ("GET", "/api/events/poll"): self.poll_events,
            ("GET", "/api/report"): self.get_report,
            ("GET", "/api/token"): self.get_token,
        }

    # ------------------------------------------------------------- WSGI glue
    def __call__(self, environ: dict[str, Any], start_response) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        raw_path = environ.get("PATH_INFO", "/")
        query: dict[str, list[str]] = {}
        for pair in (environ.get("QUERY_STRING") or "").split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            from urllib.parse import unquote_plus

            query.setdefault(unquote_plus(key), []).append(unquote_plus(value))
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(min(length, MAX_BODY + 1)) if length else b""
        headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        remote = environ.get("REMOTE_ADDR", "")
        request = ApiRequest(method, raw_path, query, headers, body, remote)

        try:
            if raw_path.startswith("/api/"):
                return self._handle_api(request, start_response)
            return self._serve_static(raw_path, start_response)
        except ApiError as exc:
            return self._json(start_response, exc.status,
                              {"error": exc.message, "hint": exc.hint, "code": exc.code})
        except UsbDoctorError as exc:
            return self._json(start_response, 400, {
                "error": exc.user_message,
                "hint": getattr(exc, "hint", ""),
                "code": type(exc).__name__,
            })
        except ValueError as exc:
            return self._json(start_response, 400, {"error": str(exc), "code": "bad_request"})
        except Exception as exc:  # never leak a stack trace to the browser
            LOG.exception("unhandled API error on %s %s", method, raw_path)
            return self._json(start_response, 500, {"error": f"Internal error: {exc}", "code": "internal"})

    def _handle_api(self, request: ApiRequest, start_response) -> list[bytes]:
        if request.path == "/api/events":
            return self.get_events(request, start_response)
        route = self.routes.get((request.method, request.path))
        if route is None:
            known = [path for (m, path) in self.routes if path == request.path]
            if known:
                raise ApiError(405, f"{request.path} does not accept {request.method}.", code="method_not_allowed")
            raise ApiError(404, f"No such endpoint: {request.path}", code="not_found")

        if request.method != "GET" and self.require_token:
            if self.tokens.throttled(request.remote):
                raise ApiError(429, "Too many attempts. Restart the local server.", code="throttled")
            supplied = request.headers.get("x-usbdoctor-token") or request.parameter("token")
            if not self.tokens.check(supplied, request.remote):
                raise ApiError(
                    403,
                    "Missing or invalid session token.",
                    hint="Open the app through the URL that includes the token, or send X-USBDoctor-Token.",
                    code="unauthorized",
                )
        if request.path == "/api/token":
            return route(request, start_response)
        return route(request, start_response)

    @staticmethod
    def _json(start_response, status: int, payload: Any) -> list[bytes]:
        from http.client import responses as http_reasons

        body = json.dumps(payload, default=str).encode()
        # A valid WSGI status line MUST include a reason phrase.
        start_response(
            f"{status} {http_reasons.get(status, 'Error')}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]

    def _serve_static(self, path: str, start_response) -> list[bytes]:
        if not STATIC_DIR.exists():
            raise ApiError(404, "The web interface has not been installed.", code="no_static")
        relative = path.lstrip("/") or "index.html"
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in target.parents and target != STATIC_DIR:
            raise ApiError(403, "Path traversal blocked.", code="forbidden")
        if not target.is_file():
            target = STATIC_DIR / "index.html"
            if not target.is_file():
                raise ApiError(404, "Not found.", code="not_found")
        content = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".html" and self.require_token:
            marker = b"</head>"
            injection = (
                f'<meta name="usbdoctor-token" content="{self.tokens.token}">'
                "</head>"
            ).encode()
            if marker in content:
                content = content.replace(marker, injection, 1)
        start_response(
            "200 OK",
            [
                ("Content-Type", (mime or "application/octet-stream") + ("; charset=utf-8"
                                                                        if (mime or "").startswith("text/")
                                                                        or target.suffix == ".js" else "")),
                ("Content-Length", str(len(content))),
                ("Cache-Control", "no-cache"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ],
        )
        return [content]

    # ------------------------------------------------------------- endpoints
    def get_token(self, req: ApiRequest, respond):
        # Only returns the token to a caller that already has it (bootstrap from
        # the printed URL); this keeps the endpoint useful without weakening it.
        supplied = req.headers.get("x-usbdoctor-token") or req.parameter("token")
        if self.require_token and not self.tokens.check(supplied, req.remote):
            raise ApiError(403, "Token required.", code="unauthorized")
        return self._json(respond, 200, {"token": self.tokens.token})

    def get_status(self, req: ApiRequest, respond):
        payload = self.service.status()
        payload["token_required"] = self.require_token
        payload["human"] = {
            "state_dir": payload["state_dir"],
        }
        return self._json(respond, 200, payload)

    def get_devices(self, req: ApiRequest, respond):
        return self._json(respond, 200, self.service.devices())

    def refresh_devices(self, req: ApiRequest, respond):
        return self._json(respond, 200, self.service.devices(refresh_policy=True))

    def get_device_detail(self, req: ApiRequest, respond):
        path = req.parameter("path") or ""
        device = self.service.device(path)
        if device is None:
            raise ApiError(404, f"{path} is not connected.", code="device_missing")
        payload = device.to_dict()
        payload["signature"] = self.service._device_payload(device)["signature"]  # noqa: SLF001
        payload["last_scan"] = self.service.history.last_scan_for(path)
        payload["history"] = self.service.history.recent_repairs(10, device_path=path)
        payload["limitations"] = device.limitations
        return self._json(respond, 200, payload)

    def post_scan(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path") or req.parameter("path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        options = ScanOptions(
            mode=payload.get("mode", "smart"),
            read_smart=bool(payload.get("read_smart", True)),
            surface_scan=bool(payload.get("surface_scan", True)),
            surface_limit_bytes=payload.get("surface_limit_bytes"),
            surface_time_budget_s=payload.get("surface_time_budget_s", 300.0),
            benchmark=bool(payload.get("benchmark", True)),
            verify_capacity=bool(payload.get("verify_capacity", True)),
            analyze_filesystem=bool(payload.get("analyze_filesystem", True)),
        )
        if options.mode not in MODES:
            raise ApiError(400, f"mode must be one of {', '.join(MODES)}.", code="bad_mode")
        job_id = self.service.scan_async(path, options)
        return self._json(respond, 202, {"job_id": job_id, "device_path": path, "mode": options.mode})

    def get_jobs(self, req: ApiRequest, respond):
        jobs = [
            {key: job[key] for key in ("id", "kind", "device_path", "status", "progress", "stage", "started_at", "error")}
            for job in self.service.jobs()
        ]
        return self._json(respond, 200, {"jobs": jobs})

    def get_job(self, req: ApiRequest, respond):
        job_id = req.parameter("id") or ""
        job = self.service.job(job_id)
        if job is None:
            raise ApiError(404, "No such job.", code="job_missing")
        return self._json(respond, 200, job)

    def cancel_job(self, req: ApiRequest, respond):
        job_id = req.json().get("job_id") or req.parameter("id") or ""
        if not self.service.cancel(job_id):
            raise ApiError(404, "That job is not running.", code="job_missing")
        return self._json(respond, 200, {"cancelled": job_id})

    def get_last_scan(self, req: ApiRequest, respond):
        path = req.parameter("path") or ""
        scan = self.service._last_scan.get(path)  # noqa: SLF001
        if scan is None:
            raise ApiError(404, "No scan has been run on this device in this session.", code="no_scan")
        return self._json(respond, 200, scan.to_dict())

    def post_plan(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path") or req.parameter("path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        return self._json(respond, 200, self.service.plan(path, mode=payload.get("mode", "smart")))

    def post_approve(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        result = self.service.approve(
            path, operation_ids=payload.get("operation_ids"), mode=payload.get("mode", "smart")
        )
        return self._json(respond, 200, result)

    def post_execute(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        result = self.service.execute(
            path,
            consent_token=payload.get("consent_token"),
            mode=payload.get("mode", "smart"),
            operation_ids=payload.get("operation_ids"),
            dry_run=bool(payload.get("dry_run", False)),
            stop_on_failure=bool(payload.get("stop_on_failure", False)),
        )
        return self._json(respond, 200, result)

    def post_dry_run(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        result = self.service.execute(
            path,
            consent_token=None,
            mode=payload.get("mode", "smart"),
            operation_ids=payload.get("operation_ids"),
            dry_run=True,
        )
        return self._json(respond, 200, result)

    def post_emergency(self, req: ApiRequest, respond):
        """One-click 'SCAN & REPAIR AUTOMATICALLY' in SAFE mode.

        Safety-critical semantics: it *diagnoses*, then repairs only when the
        device is writable AND every applicable plan is low-risk or lower AND
        the user has passed ``confirm`` (a free-text acknowledgement is not
        enough — the UI must show the list before this is called).
        """
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        mode = payload.get("mode", "safe")
        if mode not in MODES:
            raise ApiError(400, f"mode must be one of {', '.join(MODES)}.", code="bad_mode")
        if not payload.get("confirm"):
            raise ApiError(
                400,
                "Emergency repair requires explicit confirmation.",
                hint="Show the user the operations that will run, then send confirm=true.",
                code="not_confirmed",
            )
        device = self.service.device(path)
        if device is None:
            raise ApiError(404, f"{path} is not connected.", code="device_missing")
        writable, reason = self.service._can_write(device)  # noqa: SLF001
        plan_payload = self.service.plan(path, mode=mode)
        auto_safe = [
            plan for plan in plan_payload["plans"]
            if plan["risk"] in ("none", "low") and plan.get("requires_consent")
        ]
        if not writable:
            return self._json(respond, 200, {
                "performed": False,
                "reason": reason or "The device is not writable.",
                "plan": plan_payload,
                "hint": "Run the read-only scan and recover files to another drive.",
            })
        if not auto_safe:
            return self._json(respond, 200, {
                "performed": False,
                "reason": "No automatically safe repairs apply to this device.",
                "plan": plan_payload,
            })
        approval = self.service.approve(path, operation_ids=[p["operation_id"] for p in auto_safe], mode=mode)
        result = self.service.execute(
            path,
            consent_token=approval["consent_token"],
            mode=mode,
            operation_ids=[p["operation_id"] for p in auto_safe],
        )
        return self._json(respond, 200, {
            "performed": True,
            "approved_operations": approval["operations"],
            "repair": result,
        })

    def post_image(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        output = payload.get("output_path")
        if not path or not output:
            raise ApiError(400, "device_path and output_path are required.", code="missing_parameter")
        return self._json(respond, 200, self.service.image_device(
            path, output, prefer_ddrescue=bool(payload.get("prefer_ddrescue", True))
        ))

    def post_recovery(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        return self._json(respond, 200, self.service.recover(
            path,
            method=payload.get("method", "auto"),
            output_dir=payload.get("output_dir"),
            image_path=payload.get("image_path"),
            categories=payload.get("categories"),
            include_deleted=bool(payload.get("include_deleted", True)),
            length=payload.get("length"),
        ))

    def post_capacity(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        return self._json(respond, 200, self.service.capacity_test(
            path, size_bytes=payload.get("size_bytes"), consent_token=payload.get("consent_token")
        ))

    def post_research(self, req: ApiRequest, respond):
        payload = req.json()
        path = payload.get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        return self._json(respond, 200, self.service.research(path))

    def get_history(self, req: ApiRequest, respond):
        return self._json(respond, 200, self.service.history_overview())

    def get_history_scan(self, req: ApiRequest, respond):
        scan_id = req.integer("id")
        if scan_id is None:
            raise ApiError(400, "id is required.", code="missing_parameter")
        row = self.service.history.get_scan(scan_id)
        if row is None:
            raise ApiError(404, "No such scan.", code="scan_missing")
        return self._json(respond, 200, row)

    def post_purge(self, req: ApiRequest, respond):
        path = req.json().get("device_path")
        if not path:
            raise ApiError(400, "device_path is required.", code="missing_parameter")
        self.service.history.purge_device(path)
        self.service.audit.record("history:purged", device_path=path, outcome="ok")
        return self._json(respond, 200, {"purged": path})

    def get_settings(self, req: ApiRequest, respond):
        return self._json(respond, 200, {
            "settings": self.service.history.all_settings(),
            "tools": self.service.status()["tools"],
            "is_admin": is_admin(),
        })

    def post_settings(self, req: ApiRequest, respond):
        payload = req.json()
        allowed = {
            "offline_mode", "allow_internal_disks", "allow_mounted_write", "theme",
            "auto_backup_before_repair", "surface_time_budget_s", "ai_local_model",
            "telemetry", "download_dir", "recovery_dir",
        }
        applied = {}
        for key, value in payload.items():
            if key not in allowed:
                raise ApiError(400, f"Unknown setting: {key}", code="unknown_setting")
            self.service.set_setting(key, value)
            applied[key] = value
        return self._json(respond, 200, {"applied": applied, "settings": self.service.history.all_settings()})

    def poll_events(self, req: ApiRequest, respond):
        """Polling fallback for clients that cannot use SSE."""
        since = req.integer("since") or 0
        limit = min(req.integer("limit") or 100, 500)
        events = self.bus.events_since(since, limit=limit)
        return self._json(respond, 200, {"events": events, "cursor": self.bus.cursor()})

    def get_events(self, req: ApiRequest, start_response):
        """Server-Sent Events stream (works through the preview proxy)."""
        supplied = req.headers.get("x-usbdoctor-token") or req.parameter("token")
        if self.require_token and not self.tokens.check(supplied, req.remote):
            raise ApiError(403, "Token required for the event stream.", code="unauthorized")
        since = req.integer("since") or self.bus.cursor()
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("X-Accel-Buffering", "no"),
                ("Connection", "keep-alive"),
            ],
        )
        return _EventIterator(self.bus, since)

    def get_report(self, req: ApiRequest, respond):
        """Render the last scan of a device as Markdown or HTML."""
        from usbdoctor.report import ReportBundle, to_html, to_markdown

        path = req.parameter("path") or ""
        fmt = (req.parameter("format") or "markdown").lower()
        scan = self.service._last_scan.get(path)  # noqa: SLF001
        if scan is None:
            raise ApiError(404, "Run a scan first.", code="no_scan")
        data = scan.to_dict()
        bundle = ReportBundle(
            device=data["device"],
            health=data["health"],
            filesystem=data.get("filesystem"),
            partition=data.get("partition"),
            smart=data.get("smart"),
            surface=data.get("surface"),
            performance=data.get("performance"),
            diagnosis=data.get("diagnosis"),
            repairs=None,
            limitations=data.get("limitations"),
        )
        body = to_html(bundle) if fmt == "html" else to_markdown(bundle)
        mime = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
        raw = body.encode()
        respond("200 OK", [("Content-Type", mime), ("Content-Length", str(len(raw)))])
        return [raw]


class _EventIterator:
    """Blocking WSGI iterable that yields SSE frames until the client leaves."""

    def __init__(self, bus: EventBus, since: int, *, heartbeat: float = 15.0) -> None:
        self.bus = bus
        self.cursor = since
        self.heartbeat = heartbeat
        self.lock = threading.Lock()

    def __iter__(self):
        import time as _time

        yield b": connected\n\n"
        idle_since = _time.monotonic()
        while True:
            events = self.bus.events_since(self.cursor, limit=200)
            if events:
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n" for event in events
                ).encode()
                self.cursor = events[-1]["seq"]
                idle_since = _time.monotonic()
                yield payload
                continue
            if _time.monotonic() - idle_since >= self.heartbeat:
                idle_since = _time.monotonic()
                yield b": heartbeat\n\n"
            _time.sleep(0.4)

    def close(self) -> None:  # pragma: no cover - WSGI server cleanup hook
        pass


def create_app(service: Optional[DoctorService] = None, *, require_token: bool = True) -> ApiApp:
    return ApiApp(service, require_token=require_token)


def serve(host: str = "127.0.0.1", port: int = 8765, *, service: Optional[DoctorService] = None,
          require_token: bool = True, open_browser: bool = False) -> None:
    """Run the WSGI app with the stdlib server (no external dependencies)."""
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    app = create_app(service, require_token=require_token)
    local = host in ("127.0.0.1", "localhost")

    class Handler(WSGIRequestHandler):
        def log_message(self, fmt, *args):  # quieter, but still real logging
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        def address_string(self):
            return self.client_address[0]

    # ThreadingWSGIServer so the SSE stream does not block the API.
    from socketserver import ThreadingMixIn

    class ThreadingServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = make_server(host, port, app, server_class=ThreadingServer, handler_class=Handler)
    url = f"http://{'127.0.0.1' if local else host}:{port}/"
    if require_token:
        url += f"?token={app.tokens.token}"
    print(f"{APP_NAME} {__version__} — interface at {url}")
    if not is_admin():
        print("Note: not running with administrator/root rights; raw device access is limited.")
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
