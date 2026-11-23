"""HTTP API behaviour: auth, validation, consent enforcement, real responses.

The app is invoked as a plain WSGI callable, so no socket is opened and the tests
are deterministic. A separate test starts a real server to exercise the
production code path.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

import pytest

from tests.fixtures import images as F

from usbdoctor.service import DoctorService
from usbdoctor.webapi.server import ApiApp, create_app


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self) -> Any:
        return json.loads(self.body)


class Client:
    """Tiny WSGI test client."""

    def __init__(self, app: ApiApp) -> None:
        self.app = app
        self.token = app.tokens.token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        token: Optional[str] = "auto",
    ) -> Response:
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        raw = json.dumps(body).encode() if body is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(raw),
            "wsgi.errors": io.StringIO(),
            "wsgi.version": (1, 0),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "wsgi.url_scheme": "http",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
        }
        if token == "auto":
            token = self.token
        if token:
            environ["HTTP_X_USBDOCTOR_TOKEN"] = token
        captured: dict[str, Any] = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(headers)
            return lambda data: None

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks) if isinstance(chunks, list) else b"".join(chunks)
        return Response(captured["status"], captured["headers"], payload)


@pytest.fixture()
def damaged_image(tmp_path):
    path = tmp_path / "damaged.img"
    geo = F.build_fat32(path, 8 * 1024 * 1024)
    F.add_fat32_file(path, "DATA.TXT", b"recover me please", geo)
    data = bytearray(path.read_bytes())
    data[0:512] = b"\x00" * 512
    path.write_bytes(data)
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    service = DoctorService(state_dir=tmp_path / "state")
    # Analyse the image file as if it were an attached drive.
    from usbdoctor.core.model import DeviceRef

    def device(path: str):
        if path.endswith(".img"):
            return DeviceRef(
                path=path, kind="image", platform="linux", size_bytes=8 * 1024 * 1024,
                bus="usb", filesystem="fat32",
            )
        return None

    monkeypatch.setattr(service, "device", device)
    monkeypatch.setattr(
        service, "devices",
        lambda refresh_policy=True: {
            "devices": [], "warnings": [], "source": "test",
            "is_admin": False, "platform": "linux",
            "policy": {"allow_internal_disks": False, "allow_mounted_write": False},
        },
    )
    return Client(ApiApp(service))


class TestAuth:
    def test_mutations_require_the_token(self, client, damaged_image):
        response = client.request("POST", "/api/scan", body={"device_path": str(damaged_image)}, token=None)
        assert response.status == 403
        assert response.json["code"] == "unauthorized"
        assert "hint" in response.json

    def test_wrong_token_is_rejected(self, client):
        response = client.request("POST", "/api/scan", body={"device_path": "/x"}, token="not-the-token")
        assert response.status == 403

    def test_reads_do_not_require_the_token(self, client):
        assert client.request("GET", "/api/status", token=None).status == 200
        assert client.request("GET", "/api/devices", token=None).status == 200

    def test_html_carries_a_same_origin_token(self, client):
        response = client.request("GET", "/", token=None)
        assert response.status == 200
        assert b"usbdoctor-token" in response.body
        assert b"USB Doctor AI" in response.body

    def test_path_traversal_is_blocked(self, client):
        response = client.request("GET", "/../../etc/passwd", token=None)
        assert response.status in (403, 404)
        assert b"root:" not in response.body


class TestValidation:
    def test_unknown_endpoint_is_404(self, client):
        assert client.request("GET", "/api/nope", token=None).status == 404

    def test_wrong_method_is_405(self, client):
        assert client.request("GET", "/api/scan", token=None).status == 405

    def test_missing_device_is_400_with_a_message(self, client):
        response = client.request("POST", "/api/scan", body={})
        assert response.status == 400
        assert "device_path" in response.json["error"]

    def test_invalid_json_is_400(self, client):
        app = client.app
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/scan", "QUERY_STRING": "",
            "CONTENT_LENGTH": "7", "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(b"{oops!}"), "wsgi.errors": io.StringIO(),
            "wsgi.version": (1, 0), "wsgi.multithread": False, "wsgi.multiprocess": False,
            "wsgi.run_once": False, "wsgi.url_scheme": "http",
            "SERVER_NAME": "localhost", "SERVER_PORT": "80",
            "HTTP_X_USBDOCTOR_TOKEN": app.tokens.token,
        }
        captured = {}
        app(environ, lambda s, h, e=None: captured.update(status=int(s.split()[0])))
        assert captured["status"] == 400

    def test_bad_mode_is_rejected(self, client, damaged_image):
        response = client.request("POST", "/api/scan", body={"device_path": str(damaged_image), "mode": "turbo"})
        assert response.status == 400
        assert "mode" in response.json["error"]

    def test_unknown_setting_is_rejected(self, client):
        response = client.request("POST", "/api/settings", body={"hack_the_planet": True})
        assert response.status == 400
        assert "Unknown setting" in response.json["error"]

    def test_oversized_body_is_refused(self, client):
        huge = {"device_path": "x" * (300 * 1024)}
        response = client.request("POST", "/api/scan", body=huge)
        assert response.status == 400


class TestScanFlow:
    def test_scan_async_then_fetch_the_job(self, client, damaged_image):
        import time

        start = client.request("POST", "/api/scan", body={"device_path": str(damaged_image), "mode": "smart",
                                                         "surface_time_budget_s": 3})
        assert start.status == 202
        job_id = start.json["job_id"]
        for _ in range(300):
            job = client.request("GET", f"/api/jobs/detail?id={job_id}")
            if job.json["status"] != "running":
                break
            time.sleep(0.02)
        assert job.json["status"] == "completed", job.json.get("error")
        result = job.json["result"]
        assert "FAT_BOOT_SECTOR_DESTROYED" in result["evidence_codes"]
        assert result["plans"][0]["operation_id"] == "fat-boot"

    def test_last_scan_and_report(self, client, damaged_image):
        import time

        job_id = client.request("POST", "/api/scan", body={"device_path": str(damaged_image), "mode": "smart",
                                                          "surface_time_budget_s": 2}).json["job_id"]
        for _ in range(300):
            if client.request("GET", f"/api/jobs/detail?id={job_id}").json["status"] != "running":
                break
            time.sleep(0.02)
        last = client.request("GET", f"/api/scan/last?path={urllib.parse.quote(str(damaged_image))}")
        assert last.status == 200
        report = client.request("GET", f"/api/report?path={urllib.parse.quote(str(damaged_image))}&format=markdown")
        assert report.status == 200
        assert b"USB Doctor AI" in report.body
        html = client.request("GET", f"/api/report?path={urllib.parse.quote(str(damaged_image))}&format=html")
        assert b"<!DOCTYPE html>" in html.body


class TestRepairSafety:
    def test_execute_without_consent_is_refused(self, client, damaged_image):
        response = client.request("POST", "/api/repair/execute",
                                  body={"device_path": str(damaged_image), "mode": "smart"})
        assert response.status == 400
        assert response.json["code"] == "UnsafeOperationError"

    def test_approve_then_execute(self, client, damaged_image):
        approval = client.request("POST", "/api/repair/approve",
                                  body={"device_path": str(damaged_image), "mode": "smart"})
        assert approval.status == 200
        assert approval.json["operations"] == ["fat-boot"]
        result = client.request("POST", "/api/repair/execute", body={
            "device_path": str(damaged_image),
            "consent_token": approval.json["consent_token"],
            "mode": "smart",
        })
        assert result.status == 200
        assert result.json["succeeded"] == 1
        assert result.json["steps"][0]["result"]["verified"] is True

    def test_emergency_requires_confirmation(self, client, damaged_image):
        response = client.request("POST", "/api/repair/emergency",
                                  body={"device_path": str(damaged_image), "mode": "safe"})
        assert response.status == 400
        assert response.json["code"] == "not_confirmed"

    def test_emergency_runs_only_low_risk_operations(self, client, damaged_image):
        response = client.request("POST", "/api/repair/emergency", body={
            "device_path": str(damaged_image), "mode": "safe", "confirm": True,
        })
        assert response.status == 200
        payload = response.json
        if payload["performed"]:
            for operation in payload["approved_operations"]:
                assert operation == "fat-boot"  # the only low-risk plan here


class TestSettingsAndHistory:
    def test_settings_roundtrip(self, client):
        saved = client.request("POST", "/api/settings", body={"offline_mode": True, "theme": "light"})
        assert saved.status == 200
        fetched = client.request("GET", "/api/settings")
        assert fetched.json["settings"]["offline_mode"] is True
        assert fetched.json["settings"]["theme"] == "light"

    def test_history_is_structured(self, client):
        payload = client.request("GET", "/api/history").json
        assert set(payload) == {"statistics", "recent_scans", "recent_repairs", "recent_recoveries", "audit"}
        assert "scans" in payload["statistics"]

    def test_status_reports_real_facts(self, client):
        payload = client.request("GET", "/api/status").json
        assert payload["app"] == "USB Doctor AI"
        assert payload["platform"] in ("linux", "windows", "macos")
        assert payload["token_required"] is True
        assert all(isinstance(v, bool) for v in payload["tools"].values())

    def test_capacity_without_consent_is_refused(self, client, damaged_image):
        response = client.request("POST", "/api/capacity/test", body={"device_path": str(damaged_image)})
        assert response.status == 400


class TestRealServer:
    """Start the actual stdlib server and speak HTTP to it."""

    def test_server_serves_api_and_static(self, tmp_path):
        import socket
        import time

        from usbdoctor.webapi.server import serve

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        service = DoctorService(state_dir=tmp_path / "state")
        thread = threading.Thread(
            target=serve, kwargs={"host": "127.0.0.1", "port": port, "service": service,
                                  "require_token": True},
            daemon=True,
        )
        thread.start()
        base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"{base}/api/status", timeout=1) as response:
                    status = json.loads(response.read())
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.05)
        else:
            pytest.fail("the server did not start")

        assert status["app"] == "USB Doctor AI"
        # Unauthenticated mutation must be refused over real HTTP too.
        request = urllib.request.Request(f"{base}/api/devices/refresh", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 403
        # And the HTML must carry the token for same-origin bootstrapping.
        with urllib.request.urlopen(f"{base}/") as response:
            html = response.read()
        assert b"usbdoctor-token" in html
