"""General-purpose helpers: sizes, hashing, atomic files, subprocess, time."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from usbdoctor.core.errors import ToolExecutionError

# ---------------------------------------------------------------------------
# Sizes & formatting
# ---------------------------------------------------------------------------

_SECTOR = 512


def human_size(num_bytes: Optional[int], *, precision: int = 1) -> str:
    """Format a byte count using binary units (KiB, MiB ...)."""
    if num_bytes is None:
        return "unknown"
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.{precision}f} PiB"  # pragma: no cover


def parse_size(text: str) -> int:
    """Parse human input such as ``"4G"``, ``"512MiB"``, ``"1048576"``.

    Units are binary (G == GiB) to match how storage vendors' tools speak.
    """
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?i?b?)?\s*", text, re.I)
    if not match:
        raise ValueError(f"cannot parse size: {text!r}")
    value = float(match.group(1))
    unit = (match.group(2) or "").lower().rstrip("b").rstrip("i")
    factors = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
    if unit not in factors:
        raise ValueError(f"unknown unit in size: {text!r}")
    return int(value * factors[unit])


def sectors_to_bytes(sectors: int, sector_size: int = _SECTOR) -> int:
    return sectors * sector_size


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """RFC 3339 UTC timestamp with second precision (sortable, timezone-explicit)."""
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

_HASH_CHUNK = 1 << 20


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_reader(
    read_fn: Callable[[int, int], bytes],
    *,
    offset: int = 0,
    length: Optional[int] = None,
    total_size: Optional[int] = None,
    chunk: int = _HASH_CHUNK,
    budget: int = 64 << 20,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Hash a byte stream through an abstract ``read_fn(offset, size)`` reader.

    To stay fast for huge devices, only up to ``budget`` bytes are hashed,
    spread over the whole range (head 50 %, then evenly-spaced samples).
    """
    end = total_size if length is None and total_size is not None else (offset + (length or 0))
    if length is None and total_size is None:
        raise ValueError("either length or total_size is required")
    digest = hashlib.sha256()
    span = max(0, end - offset)
    if span <= budget:
        pos = offset
        while pos < end:
            n = min(chunk, end - pos)
            data = read_fn(pos, n)
            if not data:
                break
            digest.update(data)
            pos += len(data)
            if on_progress:
                on_progress(pos - offset, span)
        return digest.hexdigest()
    # Sample: first 50 % of budget contiguously, remainder in 16 stripes.
    first = budget // 2
    pos = offset
    while pos < offset + first:
        n = min(chunk, offset + first - pos)
        digest.update(read_fn(pos, n))
        pos += n
    stripe_len = max(4096, (budget // 2) // 16)
    stripe_span = max(1, (span - first) // 16)
    for i in range(16):
        start = offset + first + i * stripe_span
        start = min(start, end - stripe_len)
        digest.update(read_fn(start, min(stripe_len, end - start)))
    if on_progress:
        on_progress(span, span)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Atomic files & JSON
# ---------------------------------------------------------------------------


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically (same-directory temp + rename)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], payload: Any, *, indent: int = 2) -> None:
    atomic_write_bytes(path, json.dumps(payload, indent=indent, sort_keys=True).encode())


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    argv: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout[-8000:],
            "stderr": self.stderr[-8000:],
            "duration_ms": self.duration_ms,
        }


def run_process(
    argv: Sequence[str],
    *,
    timeout: float = 60.0,
    check: bool = False,
    input_text: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    binary_stdout: bool = False,
) -> ProcessResult:
    """Run a subprocess without a shell, capturing text output.

    ``argv`` is never passed through a shell, so device paths and model strings
    cannot be interpreted as shell syntax.  This is a security invariant.
    """
    started = monotonic_ms()
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=not binary_stdout,
            timeout=timeout,
            input=input_text,
            env={**os.environ, **(env or {})} if env else None,
            cwd=cwd,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(
            f"Required tool not found: {argv[0]}",
            hint="Install the tool or point USB Doctor AI at it in Settings → Tools.",
            command=list(argv),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(
            f"{argv[0]} did not finish within {timeout:.0f}s",
            hint="The device may be hanging. Try a USB 2.0 port or a different cable.",
            command=list(argv),
            returncode=None,
        ) from exc
    out = proc.stdout or ""
    err = proc.stderr or ""
    if binary_stdout:
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
    result = ProcessResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=out,
        stderr=err,
        duration_ms=monotonic_ms() - started,
    )
    if check and not result.ok:
        raise ToolExecutionError(
            f"{Path(str(argv[0])).name} exited with status {proc.returncode}",
            command=list(argv),
            returncode=proc.returncode,
            stderr=err,
        )
    return result


def which(tool: str) -> Optional[str]:
    """Locate a tool, honouring USB Doctor's bundled-tools directory first."""
    bundled = Path(__file__).resolve().parents[2] / "vendor" / "bin"
    for candidate in (bundled / tool, bundled / f"{tool}.exe"):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(tool)


def tool_available(tool: str) -> bool:
    return which(tool) is not None


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def safe_filename(name: str, *, max_len: int = 120) -> str:
    """Make an untrusted string safe to embed in a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_len] or "unnamed")


def hexdump(data: bytes, *, width: int = 16, limit: int = 256) -> str:
    lines = []
    for off in range(0, min(len(data), limit), width):
        chunk = data[off : off + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {hexpart:<{width * 3}}  {text}")
    return "\n".join(lines)


def chunked(iterable: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")
