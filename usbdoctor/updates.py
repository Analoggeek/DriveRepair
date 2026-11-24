"""Finding, verifying and fetching a newer release.

The application is deliberately offline: it never contacts a network while it
diagnoses a drive, and it never phones home. An update check is the one exception,
so it is explicit — the user presses the button, or turns on a single opt-in
setting — and everything here degrades into a sentence rather than an exception.

What this module does not do is download and run something on its own. It resolves
the release, compares versions, picks the asset that matches this machine, streams
it to a temporary file, and — when the release publishes ``SHA256SUMS-*.txt`` —
refuses to hand back a file whose digest does not match. Installing it stays the
user's decision, taken with their operating system's own dialogs.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

from usbdoctor.version import APP_NAME, __version__

DEFAULT_REPO = "Analoggeek/DriveRepair"
RELEASES_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
RELEASES_TAG = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
#: A desktop application is not a package manager; a short timeout is the
#: difference between "checking" and a window that looks frozen.
DEFAULT_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 30.0
CHUNK = 256 * 1024

Opener = Callable[[str, float], bytes]


class UpdateError(Exception):
    """A failure the user can be told about in one sentence."""

    def __init__(self, message: str, *, kind: str = "network", hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": str(self), "hint": self.hint}


# ------------------------------------------------------------------ versions
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]([0-9A-Za-z.\-]+))?$")


def parse_version(text: str) -> Tuple[int, int, int, Tuple[int, Tuple[str, ...]]]:
    """Order versions the way people expect, including pre-releases.

    ``1.2.0`` sorts above ``1.2.0-rc.1``, and ``1.2.10`` above ``1.2.9`` — the
    second is the bug that a string comparison quietly introduces.
    """
    match = _VERSION_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"not a version: {text!r}")
    major, minor, patch = (int(part) if part else 0 for part in match.group(1, 2, 3))
    suffix = match.group(4)
    if not suffix:
        return (major, minor, patch, (1, ()))
    # A pre-release sorts below the release it precedes.
    return (major, minor, patch, (0, tuple(suffix.split("."))))


def is_newer(candidate: str, current: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


def version_of_tag(tag: str) -> str:
    return str(tag).strip().lstrip("vV")


# -------------------------------------------------------------------- assets
@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0

    @property
    def human_size(self) -> str:
        value = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"  # pragma: no cover - unreachable


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    notes: str
    url: str
    published_at: str
    assets: Tuple[Asset, ...] = ()
    prerelease: bool = False
    version: str = ""

    @classmethod
    def from_json(cls, payload: dict) -> "Release":
        assets = tuple(
            Asset(
                name=str(item.get("name", "")),
                url=str(item.get("browser_download_url", "")),
                size=int(item.get("size") or 0),
            )
            for item in payload.get("assets") or []
            if item.get("browser_download_url")
        )
        tag = str(payload.get("tag_name") or "").strip()
        return cls(
            tag=tag,
            name=str(payload.get("name") or tag),
            notes=str(payload.get("body") or ""),
            url=str(payload.get("html_url") or ""),
            published_at=str(payload.get("published_at") or ""),
            assets=assets,
            prerelease=bool(payload.get("prerelease")),
            version=version_of_tag(tag),
        )

    def asset(self, name: str) -> Optional[Asset]:
        return next((item for item in self.assets if item.name == name), None)

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "version": self.version,
            "name": self.name,
            "notes": self.notes,
            "url": self.url,
            "published_at": self.published_at,
            "prerelease": self.prerelease,
            "assets": [{"name": a.name, "size": a.size, "url": a.url} for a in self.assets],
        }


def platform_tag(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """Which build belongs on this machine: ``windows-x64``, ``macos-arm64`` …"""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system.startswith("win"):
        return "windows-x64" if machine in ("amd64", "x86_64", "x64") else f"windows-{machine}"
    if system == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    if system == "linux":
        return "linux-arm64" if machine in ("arm64", "aarch64") else "linux-x64"
    return f"{system}-{machine}"


def select_asset(assets: Sequence[Asset], tag: str) -> Optional[Asset]:
    """The file a person on this machine should actually open.

    An installer is preferred over a portable archive, and a checksum list is never
    picked. Ordering is explicit rather than "first asset that matches", so a
    release that gains a new file cannot silently change what is downloaded.
    """
    names = {asset.name: asset for asset in assets}
    version = _guess_version(names)
    if tag.startswith("windows"):
        return (names.get(f"USBDoctorAI-Setup-{version}.exe")
                or names.get(f"USBDoctorAI-{tag}-portable.zip")
                or _first(names.values(), tag, (".exe",)))
    if tag.startswith("macos"):
        arch = "arm64" if tag.endswith("arm64") else "x64"
        return (names.get(f"USBDoctorAI-{version}-{arch}.dmg")
                or names.get(f"USBDoctorAI-{version}-{arch}-portable.zip")
                or _first(names.values(), f"{tag}-portable", (".zip",))
                or _first(names.values(), arch, (".dmg",)))
    return _first(names.values(), tag, (".zip", ".tar.gz"))


def _guess_version(names: Iterable[str]) -> str:
    for name in names:
        match = re.search(r"(\d+\.\d+\.\d+)", name)
        if match:
            return match.group(1)
    return ""


def _suffix(names: Iterable[str], marker: str) -> str:
    """Version segment from an asset name like USBDoctorAI-Setup-1.1.0.exe."""
    for name in names:
        match = re.search(re.escape(marker) + r"(\d+\.\d+\.\d+)", name)
        if match:
            return match.group(1)
    return _guess_version(names)


def _first(assets: Iterable[Asset], tag: str, suffixes: Tuple[str, ...]) -> Optional[Asset]:
    candidates = [a for a in assets if tag in a.name and a.name.endswith(suffixes)]
    return sorted(candidates, key=lambda a: len(a.name))[0] if candidates else None


def checksum_name(tag: str) -> str:
    return f"SHA256SUMS-{tag}.txt"


# ----------------------------------------------------------------- networking
def _default_opener(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{APP_NAME}/{__version__}",
            "Accept": "application/vnd.github+json, application/octet-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _read(url: str, timeout: float, opener: Optional[Opener]) -> bytes:
    """One place where every transport failure becomes a sentence."""
    opener = opener or _default_opener
    try:
        return opener(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                "No published release was found for this build.",
                kind="not_found",
                hint="The project may not have published a release yet.",
            ) from None
        if exc.code in (403, 429):
            raise UpdateError(
                "GitHub refused the request (rate limit).",
                kind="rate_limited",
                hint="Try again in a few minutes; nothing is wrong with the application.",
            ) from None
        raise UpdateError(f"GitHub answered with HTTP {exc.code}.", kind="network") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(
            f"Could not reach the update server: {reason}.",
            kind="offline",
            hint="This application works fully offline; updates are the only thing that needs a connection.",
        ) from None
    except OSError as exc:  # pragma: no cover - rare transport failures
        raise UpdateError(f"Could not reach the update server: {exc}.", kind="network") from None


def fetch_release(repo: str = DEFAULT_REPO, *, timeout: float = DEFAULT_TIMEOUT,
                  opener: Optional[Opener] = None, tag: Optional[str] = None) -> Release:
    url = (RELEASES_TAG.format(repo=repo, tag=tag) if tag
           else RELEASES_LATEST.format(repo=repo))
    body = _read(url, timeout, opener)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpdateError("The update server sent something that is not a release.",
                          kind="bad_response") from None
    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateError("The update server sent something that is not a release.",
                          kind="bad_response")
    return Release.from_json(payload)


# ---------------------------------------------------------------------- check
@dataclass(frozen=True)
class UpdateCheck:
    """The result of looking, in a shape both the GUI and the API can render."""

    status: str                      # update_available | up_to_date | unavailable
    current: str
    latest: Optional[str] = None
    release: Optional[Release] = None
    asset: Optional[Asset] = None
    message: str = ""
    error: Optional[str] = None
    hint: str = ""
    platform_tag: str = ""

    @property
    def update_available(self) -> bool:
        return self.status == "update_available"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "message": self.message,
            "error": self.error,
            "hint": self.hint,
            "platform": self.platform_tag,
            "asset": ({"name": self.asset.name, "size": self.asset.size,
                       "url": self.asset.url} if self.asset else None),
            "release": (self.release.to_dict() if self.release else None),
        }


def check_for_update(current: Optional[str] = None, *, repo: str = DEFAULT_REPO,
                     timeout: float = DEFAULT_TIMEOUT, tag: Optional[str] = None,
                     opener: Optional[Opener] = None) -> UpdateCheck:
    """Look for a newer release. Never raises: a failure is a status."""
    current = current or __version__
    wanted = platform_tag()
    try:
        release = fetch_release(repo, timeout=timeout, opener=opener, tag=tag)
    except UpdateError as exc:
        return UpdateCheck(status="unavailable", current=current, error=str(exc),
                           message=str(exc), hint=exc.hint, platform_tag=wanted)
    latest = release.version or release.tag
    if not is_newer(latest, current):
        return UpdateCheck(
            status="up_to_date", current=current, latest=latest, release=release,
            message=f"You are on the newest version ({current}).",
            platform_tag=wanted,
        )
    asset = select_asset(release.assets, wanted)
    if asset is None:
        return UpdateCheck(
            status="update_available", current=current, latest=latest, release=release,
            message=(f"Version {latest} is available, but it has no build for this "
                     f"platform ({wanted})."),
            hint="Open the release page to see what is published.",
            platform_tag=wanted,
        )
    return UpdateCheck(
        status="update_available", current=current, latest=latest, release=release,
        asset=asset,
        message=f"Version {latest} is available ({asset.name}, {asset.human_size}).",
        platform_tag=wanted,
    )


# ------------------------------------------------------------------- download
def parse_checksums(text: str) -> Dict[str, str]:
    """``sha256  filename`` lines, as published alongside the builds."""
    result: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            result[parts[-1].lstrip("*")] = parts[0].lower()
    return result


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(asset: Asset, target_dir: Path, *, progress: Optional[Callable[[int, int], None]] = None,
             timeout: float = DOWNLOAD_TIMEOUT, opener: Optional[Opener] = None,
             expected_sha256: Optional[str] = None) -> Path:
    """Fetch one asset, verifying it if a digest is known.

    The file is written to a temporary name in the destination and only renamed
    once the digest matches, so an interrupted or tampered download can never look
    like a finished one.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    body = _read(asset.url, timeout if not progress else max(timeout, 60.0), opener)
    if progress:
        progress(len(body), len(body) or asset.size)
    if asset.size and len(body) != asset.size:
        raise UpdateError(
            f"{asset.name} arrived truncated ({len(body)} of {asset.size} bytes).",
            kind="bad_download", hint="Try again; the connection dropped.",
        )
    # mkstemp opens the file and hands back both a descriptor and a name. The
    # descriptor must be closed before we write to it again: on Windows an open
    # handle makes os.replace and unlink fail with WinError 32, so the download
    # lands but can never be renamed into place.
    descriptor, name = tempfile.mkstemp(prefix=".usbdoctor-", dir=str(target_dir))
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_bytes(body)
        if expected_sha256:
            actual = sha256_of(temporary)
            if actual != expected_sha256.lower():
                raise UpdateError(
                    f"{asset.name} failed its checksum: the download is not the published file.",
                    kind="checksum",
                    hint="Nothing was saved. Try again, or download from the release page.",
                )
        final = target_dir / asset.name
        os.replace(temporary, final)
        return final
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a scanner holding the file on Windows
            pass


def checksums_for(release: Release, tag: str, *, timeout: float = DEFAULT_TIMEOUT,
                  opener: Optional[Opener] = None) -> Dict[str, str]:
    """The published digests for this platform, or an empty map if there are none."""
    asset = release.asset(checksum_name(tag))
    if asset is None:
        return {}
    try:
        return parse_checksums(_read(asset.url, timeout, opener).decode("utf-8", "replace"))
    except UpdateError:
        return {}


def download_update(check: UpdateCheck, target_dir: Path, *,
                    progress: Optional[Callable[[int, int], None]] = None,
                    timeout: float = DOWNLOAD_TIMEOUT,
                    opener: Optional[Opener] = None) -> Path:
    """Download the asset a check selected, verifying the published digest."""
    if not check.asset or not check.release:
        raise UpdateError("There is nothing to download.", kind="nothing_to_do")
    digests = (checksums_for(check.release, check.platform_tag, opener=opener)
               if check.platform_tag else {})
    return download(check.asset, target_dir, progress=progress, timeout=timeout, opener=opener,
                    expected_sha256=digests.get(check.asset.name))


def default_download_dir() -> Path:
    """Where a person expects to find a file they just downloaded."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return Path(tempfile.gettempdir())


def open_downloaded(path: Path) -> bool:
    """Hand the file to the desktop, so the OS shows its own install prompts."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])  # noqa: S603, S607
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607
        return True
    except Exception:  # noqa: BLE001 - a missing xdg-open is not an error worth raising
        return False


# ------------------------------------------------------------- release notes
def escape_html(text: str) -> str:
    """Escape text before it is placed inside HTML: a release body is untrusted."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape(text: str) -> str:
    return escape_html(text)


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET = re.compile(r"^[-*+]\s+")


def format_notes_html(notes: str) -> str:
    """Render the little markdown a release body actually uses.

    GitHub release notes are markdown; showing the raw text puts ``-`` and ``**``
    in front of a person who was told there were notes worth reading. Only the
    inline marks that appear in practice are handled, and everything is escaped
    before it is wrapped, so a release body cannot inject markup.
    """
    out: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            out.append("<ul style='margin:6px 0 10px 18px'>" + "".join(bullets) + "</ul>")
            bullets.clear()

    for raw in notes.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("#"):
            flush()
            text = _escape(line.lstrip("# ").strip())
            out.append(f"<h4 style='margin:14px 0 6px'>{text}</h4>")
            continue
        if _BULLET.match(line):
            bullets.append(f"<li>{_inline_markdown(_BULLET.sub('', line))}</li>")
            continue
        flush()
        out.append(f"<p style='margin:6px 0'>{_inline_markdown(line)}</p>")
    flush()
    return "".join(out)


def _inline_markdown(text: str) -> str:
    escaped = _escape(text)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _LINK.sub(r'<a href="\2">\1</a>', escaped)
    return escaped


