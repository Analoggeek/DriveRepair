"""Apply verified metadata patches through the guarded writer.

Two properties matter for user safety:

1. A full snapshot of every region we are about to overwrite is written to a
   rollback file *before* the write (so "undo" is a real, tested operation).
2. After writing, the affected structures are re-parsed to prove the change
   actually fixed the problem.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from usbdoctor.core.io import DiskReader
from usbdoctor.core.model import DeviceRef
from usbdoctor.repair.base import RepairResult, RepairStatus, RunContext
from usbdoctor.security.writer_guard import GuardedWriter


@dataclass
class PatchRecord:
    offset: int
    data: bytes
    description: str
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "length": len(self.data),
            "description": self.description,
            "reason_code": self.reason_code,
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


def rollback_path(device: DeviceRef, state_dir: Optional[Path] = None) -> Path:
    base = state_dir or Path(
        os.environ.get("USBDOCTOR_STATE_DIR", Path.home() / ".local" / "state" / "usbdoctor-ai")
    )
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in device.path)
    stamp = os.environ.get("USBDOCTOR_RUN_ID", "session")
    return base / "rollback" / f"{safe}-{stamp}.json"


def save_rollback(ctx: RunContext, patches: list[PatchRecord], path: Optional[Path] = None) -> Path:
    """Persist the original bytes so the operation can be undone later."""
    target = path or rollback_path(ctx.device)
    payload = {
        "device": ctx.device.path,
        "size_bytes": ctx.device.size_bytes,
        "signature": ctx.signature.to_dict() if hasattr(ctx.signature, "to_dict") else None,
        "patches": [
            {**p.to_dict(), "original_hex": _read_original(ctx.device, p.offset, len(p.data)).hex()}
            for p in patches
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return target


def _read_original(device: DeviceRef, offset: int, length: int) -> bytes:
    with DiskReader(device.path, sector_size=device.sector_size) as reader:
        return reader.read(offset, length)


def apply_patches(
    ctx: RunContext,
    patches: list[PatchRecord],
    *,
    operation: str,
    verify: Optional[Any] = None,
) -> RepairResult:
    """Write ``patches`` atomically-per-region, then verify the result."""
    if not patches:
        return RepairResult.skipped("No patches were required for this device.")
    rollback_file: Optional[Path] = None
    written = 0
    try:
        if not ctx.dry_run:
            rollback_file = save_rollback(ctx, patches)
            ctx.log(f"Rollback snapshot saved: {rollback_file}")
        with GuardedWriter(
            ctx.device,
            expected_signature=ctx.signature,
            policy=ctx.policy,
            audit=ctx.audit,
            operation=operation,
            dry_run=ctx.dry_run,
        ) as writer:
            for index, patch in enumerate(patches, start=1):
                ctx.log(f"Writing {len(patch.data):,} bytes at offset {patch.offset:,}: {patch.description}")
                written += writer.write(patch.offset, patch.data, note=patch.reason_code)
                ctx.progress(index, len(patches), patch.description)
            if not ctx.dry_run:
                # Independent read-back proof for every patch.
                for patch in patches:
                    readback = writer.readback(patch.offset, len(patch.data))
                    if readback != patch.data:
                        return RepairResult(
                            status=RepairStatus.FAILED,
                            message=(
                                f"Write verification failed at offset {patch.offset:,}: the device did not "
                                "store the bytes we wrote. This often indicates failing flash."
                            ),
                            verified=False,
                            bytes_written=written,
                        )
    except Exception as exc:  # policy / identity / IO failure
        return RepairResult(
            status=RepairStatus.FAILED,
            message=f"Repair could not be applied: {exc}",
            verified=False,
            bytes_written=written,
            details={"error": type(exc).__name__},
        )

    if ctx.dry_run:
        return RepairResult(
            status=RepairStatus.DRY_RUN,
            message=f"Dry run: {len(patches)} patch(es), {written:,} bytes would be written.",
            verified=None,
            bytes_written=written,
        )

    verification_note = ""
    verified: Optional[bool] = None
    if verify is not None:
        try:
            verified, verification_note = verify()
        except Exception as exc:
            verified = False
            verification_note = f"Verification raised: {exc}"
        if verified is False:
            return RepairResult(
                status=RepairStatus.PARTIAL,
                message=(
                    f"Changes were written ({written:,} bytes) but re-checking the device did not confirm "
                    f"the fix. {verification_note}"
                ),
                verified=False,
                verification_note=verification_note,
                bytes_written=written,
            )
    return RepairResult(
        status=RepairStatus.SUCCEEDED,
        message=f"{len(patches)} metadata repair(s) written and verified ({written:,} bytes).",
        verified=verified,
        verification_note=verification_note,
        bytes_written=written,
        details={"rollback_file": str(rollback_file) if rollback_file else None},
    )


def undo_rollback(device: DeviceRef, rollback_file: Path, *, signature: Any, policy: Any, audit: Any) -> RepairResult:
    """Restore the original bytes recorded by :func:`save_rollback`."""
    try:
        payload = json.loads(rollback_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RepairResult.failure(f"Rollback file could not be read: {exc}")
    if payload.get("device") != device.path:
        return RepairResult.failure(
            f"Rollback file belongs to {payload.get('device')}, not {device.path}."
        )
    written = 0
    with GuardedWriter(
        device,
        expected_signature=signature,
        policy=policy,
        audit=audit,
        operation="rollback",
    ) as writer:
        for patch in payload.get("patches", []):
            data = bytes.fromhex(patch["original_hex"])
            writer.write(int(patch["offset"]), data, note="rollback")
            written += len(data)
    return RepairResult.success(
        f"Restored the original metadata for {len(payload.get('patches', []))} region(s) ({written:,} bytes)."
    )
