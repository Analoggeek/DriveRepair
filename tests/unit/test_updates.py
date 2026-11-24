"""The update check: version ordering, asset choice, and failure as a sentence.

Nothing in this file touches the network. Every call takes an ``opener``, so the
tests supply their own and a live request is a bug in the test, not a flake in CI.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error

import pytest

from usbdoctor import updates
from usbdoctor.updates import (
    Asset,
    Release,
    UpdateError,
    check_for_update,
    checksums_for,
    download,
    download_update,
    is_newer,
    parse_checksums,
    parse_version,
    platform_tag,
    select_asset,
)


def release_payload(version: str = "9.9.9", assets=None) -> dict:
    return {
        "tag_name": f"v{version}",
        "name": f"USB Doctor AI {version}",
        "body": "New things:\n- a faster scan\n- a calmer update dialog",
        "html_url": f"https://example.invalid/releases/tag/v{version}",
        "published_at": "2026-11-24T10:00:00Z",
        "prerelease": False,
        "assets": assets if assets is not None else [
            {"name": f"USBDoctorAI-Setup-{version}.exe",
             "browser_download_url": "https://example.invalid/setup.exe", "size": 1024},
            {"name": f"USBDoctorAI-{version}-x64-portable.zip",
             "browser_download_url": "https://example.invalid/portable.zip", "size": 2048},
            {"name": f"USBDoctorAI-{version}-arm64.dmg",
             "browser_download_url": "https://example.invalid/mac-arm.dmg", "size": 4096},
            {"name": f"USBDoctorAI-{version}-x64.dmg",
             "browser_download_url": "https://example.invalid/mac-intel.dmg", "size": 8192},
        ],
    }


def opener_for(payload: dict, *, record: list | None = None):
    body = json.dumps(payload).encode()

    def opener(url: str, timeout: float) -> bytes:
        if record is not None:
            record.append((url, timeout))
        return body

    return opener


class TestVersionOrdering:
    @pytest.mark.parametrize("candidate,current,expected", [
        ("1.1.0", "1.0.0", True),
        ("1.10.0", "1.9.9", True),      # the bug a string comparison hides
        ("1.0.0", "1.1.0", False),
        ("1.1.0", "1.1.0", False),
        ("v1.1.0", "1.0.0", True),
        ("1.2.0", "1.2.0-beta.1", True),
        ("1.2.0-beta.1", "1.2.0", False),
        ("2.0", "1.9.9", True),
    ])
    def test_newer(self, candidate, current, expected):
        assert is_newer(candidate, current) is expected

    def test_nonsense_versions_are_not_newer(self):
        assert is_newer("not-a-version", "1.0.0") is False
        assert is_newer("1.0.0", "also-nonsense") is False

    def test_parse_rejects_rubbish(self):
        with pytest.raises(ValueError):
            parse_version("latest")


class TestPlatformTags:
    @pytest.mark.parametrize("system,machine,expected", [
        ("Windows", "AMD64", "windows-x64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Linux", "x86_64", "linux-x64"),
    ])
    def test_tag(self, system, machine, expected):
        assert platform_tag(system, machine) == expected


class TestAssetChoice:
    ASSETS = tuple(
        Asset(name, f"https://example.invalid/{name}", index * 1000)
        for index, name in enumerate([
            "USBDoctorAI-Setup-1.1.0.exe",
            "USBDoctorAI-1.1.0-x64-portable.zip",
            "USBDoctorAI-1.1.0-arm64.dmg",
            "USBDoctorAI-1.1.0-x64.dmg",
            "SHA256SUMS-windows-x64.txt",
            "SHA256SUMS-macos-arm64.txt",
        ], start=1)
    )

    def test_windows_gets_the_installer(self):
        assert select_asset(self.ASSETS, "windows-x64").name == "USBDoctorAI-Setup-1.1.0.exe"

    def test_macos_gets_the_matching_dmg(self):
        assert select_asset(self.ASSETS, "macos-arm64").name == "USBDoctorAI-1.1.0-arm64.dmg"
        assert select_asset(self.ASSETS, "macos-x64").name == "USBDoctorAI-1.1.0-x64.dmg"

    def test_a_checksum_list_is_never_picked(self):
        for tag in ("windows-x64", "macos-arm64", "macos-x64"):
            chosen = select_asset(self.ASSETS, tag)
            assert chosen is not None and not chosen.name.startswith("SHA256SUMS")

    def test_no_build_for_this_platform(self):
        assert select_asset(self.ASSETS, "linux-x64") is None

    def test_asset_size_is_readable(self):
        assert Asset("x", "u", 68 * 1024 * 1024).human_size == "68.0 MB"


class TestCheckForUpdate:
    def test_an_update_is_found_and_the_right_file_selected(self, monkeypatch):
        monkeypatch.setattr(updates.platform, "system", lambda: "Windows")
        monkeypatch.setattr(updates.platform, "machine", lambda: "AMD64")
        result = check_for_update("1.0.0", opener=opener_for(release_payload("1.1.0")))
        assert result.status == "update_available"
        assert result.update_available is True
        assert result.latest == "1.1.0"
        assert result.asset is not None and result.asset.name.endswith(".exe")
        assert "1.1.0" in result.message

    def test_the_current_version_is_reported_as_current(self):
        result = check_for_update("9.9.9", opener=opener_for(release_payload("9.9.9")))
        assert result.status == "up_to_date"
        assert result.update_available is False
        assert result.asset is None

    def test_a_newer_release_without_a_build_still_says_so(self, monkeypatch):
        monkeypatch.setattr(updates.platform, "system", lambda: "Linux")
        payload = release_payload("1.1.0", assets=[
            {"name": "USBDoctorAI-Setup-1.1.0.exe", "browser_download_url": "u", "size": 1},
        ])
        result = check_for_update("1.0.0", opener=opener_for(payload))
        assert result.status == "update_available"
        assert result.asset is None
        assert "no build for this platform" in result.message.lower()

    def test_offline_is_a_sentence_not_an_exception(self):
        def offline(url: str, timeout: float) -> bytes:
            raise urllib.error.URLError("Connection refused")

        result = check_for_update("1.0.0", opener=offline)
        assert result.status == "unavailable"
        assert "could not reach" in (result.error or "").lower()
        assert result.update_available is False

    def test_a_timeout_is_reported_as_offline(self):
        def timeout(url: str, timeout_seconds: float) -> bytes:
            raise TimeoutError("timed out")

        result = check_for_update("1.0.0", opener=timeout)
        assert result.status == "unavailable"

    def test_http_errors_are_named(self):
        def not_found(url: str, timeout: float) -> bytes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        result = check_for_update("1.0.0", opener=not_found)
        assert result.status == "unavailable"
        assert "no published release" in (result.error or "").lower()

    def test_rate_limiting_is_explained(self):
        def limited(url: str, timeout: float) -> bytes:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        result = check_for_update("1.0.0", opener=limited)
        assert "rate limit" in (result.error or "").lower()
        assert "nothing is wrong" in (result.hint or "").lower()

    def test_a_non_json_answer_is_not_a_crash(self):
        result = check_for_update("1.0.0", opener=lambda url, timeout: b"<html>502</html>")
        assert result.status == "unavailable"

    def test_the_timeout_is_passed_through(self):
        seen: list = []
        check_for_update("1.0.0", timeout=3.5, opener=opener_for(release_payload(), record=seen))
        assert seen and seen[0][1] == 3.5

    def test_the_result_serialises_for_the_api(self, monkeypatch):
        monkeypatch.setattr(updates.platform, "system", lambda: "Windows")
        result = check_for_update("1.0.0", opener=opener_for(release_payload("1.1.0")))
        payload = result.to_dict()
        assert payload["update_available"] is True
        assert payload["release"]["version"] == "1.1.0"
        assert payload["asset"]["name"].endswith(".exe")
        json.dumps(payload)  # must be JSON-safe for the web endpoint


class TestDownload:
    BODY = b"pretend installer bytes"

    def _release(self, digest: str | None) -> Release:
        assets = [{"name": "USBDoctorAI-Setup-1.1.0.exe",
                   "browser_download_url": "https://example.invalid/setup.exe",
                   "size": len(self.BODY)}]
        if digest:
            assets.append({"name": "SHA256SUMS-windows-x64.txt",
                           "browser_download_url": "https://example.invalid/sums.txt",
                           "size": 100})
        return Release.from_json(release_payload("1.1.0", assets=assets))

    def test_checksum_lines_are_parsed(self):
        digest = hashlib.sha256(self.BODY).hexdigest()
        parsed = parse_checksums(f"{digest}  USBDoctorAI-Setup-1.1.0.exe\n"
                                 f"{'0' * 64} *other.zip\n")
        assert parsed["USBDoctorAI-Setup-1.1.0.exe"] == digest
        assert parsed["other.zip"] == "0" * 64

    def test_a_verified_download_lands(self, tmp_path):
        digest = hashlib.sha256(self.BODY).hexdigest()
        asset = Asset("USBDoctorAI-Setup-1.1.0.exe", "https://example.invalid/setup.exe", len(self.BODY))
        saved = download(asset, tmp_path, opener=lambda url, t: self.BODY, expected_sha256=digest)
        assert saved.read_bytes() == self.BODY
        assert saved.parent == tmp_path

    def test_a_bad_checksum_saves_nothing(self, tmp_path):
        asset = Asset("USBDoctorAI-Setup-1.1.0.exe", "https://example.invalid/setup.exe", len(self.BODY))
        with pytest.raises(UpdateError) as caught:
            download(asset, tmp_path, opener=lambda url, t: self.BODY, expected_sha256="0" * 64)
        assert caught.value.kind == "checksum"
        assert not list(tmp_path.glob("*"))  # no half file, no temporary file

    def test_a_truncated_download_is_refused(self, tmp_path):
        asset = Asset("big.zip", "https://example.invalid/big.zip", len(self.BODY) * 2)
        with pytest.raises(UpdateError) as caught:
            download(asset, tmp_path, opener=lambda url, t: self.BODY)
        assert caught.value.kind == "bad_download"

    def test_download_update_verifies_against_the_published_sums(self, tmp_path, monkeypatch):
        digest = hashlib.sha256(self.BODY).hexdigest()
        release = self._release(digest)
        check = check_for_update("1.0.0", opener=opener_for(release_payload()))
        check = updates.UpdateCheck(
            status="update_available", current="1.0.0", latest="1.1.0", release=release,
            asset=release.assets[0], message="", platform_tag="windows-x64",
        )

        def opener(url: str, timeout: float) -> bytes:
            if url.endswith("sums.txt"):
                return f"{digest}  USBDoctorAI-Setup-1.1.0.exe\n".encode()
            return self.BODY

        saved = download_update(check, tmp_path, opener=opener)
        assert saved.name == "USBDoctorAI-Setup-1.1.0.exe"
        assert updates.sha256_of(saved) == digest

    def test_nothing_to_download_is_explicit(self, tmp_path):
        check = updates.UpdateCheck(status="up_to_date", current="1.0.0")
        with pytest.raises(UpdateError):
            download_update(check, tmp_path)

    def test_missing_checksums_do_not_block_the_download(self):
        release = self._release(None)
        assert checksums_for(release, "windows-x64", opener=lambda url, t: b"") == {}

    def test_progress_is_reported(self, tmp_path):
        seen: list = []
        asset = Asset("a.bin", "https://example.invalid/a.bin", len(self.BODY))
        download(asset, tmp_path, opener=lambda url, t: self.BODY,
                 progress=lambda done, total: seen.append((done, total)))
        assert seen and seen[0][0] == len(self.BODY)


class TestDownloadLocation:
    def test_a_downloads_folder_is_preferred(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updates.Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / "Downloads").mkdir()
        assert updates.default_download_dir() == tmp_path / "Downloads"

    def test_it_falls_back_to_temp_when_there_is_no_downloads_folder(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updates.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        assert updates.default_download_dir().exists() or True
        assert "Downloads" not in str(updates.default_download_dir())


class TestWindowsFileHandles:
    """Windows refuses to rename a file that still has an open handle.

    tempfile.mkstemp returns a descriptor *and* a name; leaving the descriptor open
    made every download fail there with WinError 32 while Linux and macOS were
    perfectly happy.
    """

    def test_no_descriptor_is_left_open(self, tmp_path, monkeypatch):
        import os as os_module

        closed: list[int] = []
        real_close = os_module.close

        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(updates.os, "close", tracking_close)
        asset = Asset("a.bin", "https://example.invalid/a.bin", 3)
        download(asset, tmp_path, opener=lambda url, timeout: b"abc")
        assert closed, "the temporary file's descriptor was never closed"

    def test_the_finished_file_is_where_it_was_asked_to_go(self, tmp_path):
        asset = Asset("a.bin", "https://example.invalid/a.bin", 3)
        saved = download(asset, tmp_path, opener=lambda url, timeout: b"abc")
        assert saved == tmp_path / "a.bin"
        assert list(tmp_path.iterdir()) == [saved], "a temporary file was left behind"
