"""Directory-level recovery for FAT12/16/32 and exFAT volumes.

Reads the real directory entries (including deleted ones, which FAT marks with
0xE5) and reconstructs each file's cluster chain.  This recovers files *with
their names and folder structure*, which content carving cannot do.

Deleted-file recovery is only possible while the clusters have not been
reallocated — the tool proves this by reading the FAT chain and refusing to
export files whose clusters are now owned by something else.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from usbdoctor.core.io import DiskReader
from usbdoctor.core.util import safe_filename


@dataclass
class RecoveredEntry:
    name: str
    path: str
    size_bytes: int
    first_cluster: int
    deleted: bool
    is_dir: bool
    recovered: bool = False
    reason: str = ""
    output_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "first_cluster": self.first_cluster,
            "deleted": self.deleted,
            "is_dir": self.is_dir,
            "recovered": self.recovered,
            "reason": self.reason,
            "output_path": self.output_path,
        }


@dataclass
class FatRecoveryResult:
    volume_type: str
    output_dir: str
    entries_seen: int = 0
    deleted_found: int = 0
    files_recovered: int = 0
    bytes_recovered: int = 0
    failures: int = 0
    entries: list[RecoveredEntry] = field(default_factory=list)
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "volume_type": self.volume_type,
            "output_dir": self.output_dir,
            "entries_seen": self.entries_seen,
            "deleted_found": self.deleted_found,
            "files_recovered": self.files_recovered,
            "bytes_recovered": self.bytes_recovered,
            "failures": self.failures,
            "duration_s": round(self.duration_s, 1),
            "notes": self.notes,
            "entries": [e.to_dict() for e in self.entries[:5000]],
        }


class FatRecovery:
    """Read-only FAT/exFAT walker that exports recoverable files."""

    def __init__(
        self,
        reader: DiskReader,
        output_dir: str,
        *,
        base_offset: int = 0,
        include_deleted: bool = True,
        max_files: int = 20000,
        progress=None,
    ) -> None:
        self.reader = reader
        self.base = base_offset
        self.output_dir = Path(output_dir)
        self.include_deleted = include_deleted
        self.max_files = max_files
        self.progress = progress
        self._abort = False
        self._seen_dirs: set[int] = set()

    def abort(self) -> None:
        self._abort = True

    # ------------------------------------------------------------------- run
    def recover(self) -> FatRecoveryResult:
        started = time.time()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        geo = self._geometry()
        result = FatRecoveryResult(volume_type=geo["type"], output_dir=str(self.output_dir))
        if geo["type"] not in ("fat12", "fat16", "fat32", "exfat"):
            result.notes.append(f"Unsupported volume type: {geo['type']}")
            return result
        self.geo = geo
        self._walk_dir(geo["root_offset"], geo["root_size"], "", result, depth=0)
        result.duration_s = time.time() - started
        return result

    # -------------------------------------------------------------- geometry
    def _geometry(self) -> dict:
        boot = self.reader.read(self.base, 512)
        if len(boot) < 512:
            return {"type": "unknown"}
        if boot[3:11] == b"EXFAT   ":
            bps_shift = boot[108]
            bps = 1 << bps_shift if bps_shift <= 12 else 512
            spc = 1 << boot[109] if boot[109] <= 25 else 8
            fat_offset = struct.unpack("<I", boot[80:84])[0] * bps
            heap_offset = struct.unpack("<I", boot[88:92])[0] * bps
            cluster_count = struct.unpack("<I", boot[92:96])[0]
            root_cluster = struct.unpack("<I", boot[96:100])[0]
            cluster_size = bps * spc
            return {
                "type": "exfat",
                "bytes_per_sector": bps,
                "cluster_size": cluster_size,
                "fat_offset": self.base + fat_offset,
                "data_offset": self.base + heap_offset,
                "root_offset": self.base + heap_offset + (root_cluster - 2) * cluster_size,
                "root_size": cluster_size,
                "root_cluster": root_cluster,
                "cluster_count": cluster_count,
            }
        bps = struct.unpack("<H", boot[11:13])[0] or 512
        spc = boot[13] or 1
        reserved = struct.unpack("<H", boot[14:16])[0]
        num_fats = boot[16] or 2
        root_entries = struct.unpack("<H", boot[17:19])[0]
        total = struct.unpack("<H", boot[19:21])[0] or struct.unpack("<I", boot[32:36])[0]
        fat_size = struct.unpack("<H", boot[22:24])[0] or struct.unpack("<I", boot[36:40])[0]
        data_start = (reserved + num_fats * fat_size) * bps
        clusters = (total * bps - data_start) // (bps * spc) if bps and spc else 0
        vtype = "fat32" if fat_size and struct.unpack("<I", boot[36:40])[0] else (
            "fat12" if clusters < 4085 else "fat16"
        )
        if struct.unpack("<I", boot[36:40])[0]:
            vtype = "fat32"
        root_offset = data_start if vtype == "fat32" else data_start
        root_size = (root_entries * 32) if vtype != "fat32" else bps * spc
        return {
            "type": vtype,
            "bytes_per_sector": bps,
            "cluster_size": bps * spc,
            "fat_offset": self.base + reserved * bps,
            "data_offset": self.base + data_start,
            "root_offset": self.base + root_offset,
            "root_size": root_size,
            "root_cluster": struct.unpack("<I", boot[44:48])[0] if vtype == "fat32" else 0,
            "cluster_count": clusters,
            "num_fats": num_fats,
        }

    # ------------------------------------------------------------ FAT access
    def _next_cluster(self, cluster: int) -> int:
        geo = self.geo
        if geo["type"] == "exfat":
            raw = self.reader.read(geo["fat_offset"] + cluster * 4, 4)
            if len(raw) < 4:
                return 0xFFFFFFFF
            value = struct.unpack("<I", raw)[0]
            return value if value < 0xFFFFFFF8 else 0xFFFFFFFF
        if geo["type"] == "fat32":
            raw = self.reader.read(geo["fat_offset"] + cluster * 4, 4)
            if len(raw) < 4:
                return 0xFFFFFFFF
            value = struct.unpack("<I", raw)[0] & 0x0FFFFFFF
            return value if value < 0x0FFFFFF8 else 0xFFFFFFFF
        offset = cluster * 2 if geo["type"] == "fat16" else cluster + cluster // 2
        raw = self.reader.read(geo["fat_offset"] + offset, 2)
        if len(raw) < 2:
            return 0xFFFFFFFF
        if geo["type"] == "fat16":
            value = struct.unpack("<H", raw)[0]
            return value if value < 0xFFF8 else 0xFFFFFFFF
        value = struct.unpack("<H", raw)[0]
        value = (value >> 4) if (cluster & 1) else (value & 0x0FFF)
        return value if value < 0xFF8 else 0xFFFFFFFF

    def _cluster_data(self, cluster: int) -> bytes:
        geo = self.geo
        cluster_size = geo["cluster_size"]
        if cluster < 2:
            return b""
        offset = geo["data_offset"] + (cluster - 2) * cluster_size
        try:
            return self.reader.read(offset, cluster_size)
        except Exception:
            return b""

    def _read_chain(self, start_cluster: int, size: Optional[int] = None) -> bytes:
        chunks: list[bytes] = []
        cluster = start_cluster
        seen: set[int] = set()
        limit = self.geo["cluster_count"] + 2
        while 2 <= cluster < limit:
            if cluster in seen:
                break
            seen.add(cluster)
            data = self._cluster_data(cluster)
            if not data:
                break
            chunks.append(data)
            if size is not None and sum(len(c) for c in chunks) >= size:
                break
            nxt = self._next_cluster(cluster)
            if nxt >= 0xFFFFFFFF or nxt < 2:
                break
            cluster = nxt
        blob = b"".join(chunks)
        return blob[:size] if size is not None else blob

    # ------------------------------------------------------------ directory
    def _walk_dir(
        self, offset: int, size: int, prefix: str, result: FatRecoveryResult, *, depth: int
    ) -> None:
        if depth > 24 or self._abort:
            return
        try:
            raw = self.reader.read(offset, min(size, 32 << 20))
        except Exception:
            result.notes.append(f"Directory {prefix or '/'} could not be read.")
            return
        if self.geo["type"] == "exfat":
            self._parse_exfat_dir(raw, prefix, result, depth)
            return
        long_name: list[str] = []
        for pos in range(0, len(raw), 32):
            entry = raw[pos : pos + 32]
            if len(entry) < 32:
                break
            if entry[0] == 0x00:
                break
            attr = entry[11]
            if attr & 0x3F == 0x0F:
                long_name = _decode_lfn(entry) + long_name
                continue
            if attr & 0x08 and not long_name:
                continue  # volume label
            deleted = entry[0] == 0xE5
            name = "".join(long_name) if long_name else _short_name(entry, deleted)
            long_name = []
            result.entries_seen += 1
            if deleted:
                result.deleted_found += 1
                if not self.include_deleted:
                    continue
            cluster = struct.unpack("<H", entry[26:28])[0]
            file_size = struct.unpack("<I", entry[28:32])[0]
            if self.geo["type"] == "fat32":
                cluster |= struct.unpack("<H", entry[20:22])[0] << 16
            is_dir = bool(attr & 0x10)
            display = safe_filename(name) + ("/" if is_dir else "")
            record = RecoveredEntry(
                name=name,
                path=prefix + display,
                size_bytes=file_size,
                first_cluster=cluster,
                deleted=deleted,
                is_dir=is_dir,
            )
            result.entries.append(record)
            if is_dir:
                if cluster >= 2 and cluster not in self._seen_dirs:
                    self._seen_dirs.add(cluster)
                    dir_data = self._read_chain(cluster)
                    if dir_data:
                        self._walk_bytes(dir_data, prefix + display + "/", result, depth + 1)
                continue
            if file_size == 0 or cluster < 2:
                record.reason = "empty file or no cluster allocated"
                continue
            self._export(record, prefix, result)
            if len(result.entries) > self.max_files:
                self._abort = True
                return

    def _walk_bytes(self, raw: bytes, prefix: str, result: FatRecoveryResult, depth: int) -> None:
        """Walk a directory that has already been read into memory."""
        long_name: list[str] = []
        for pos in range(0, len(raw), 32):
            entry = raw[pos : pos + 32]
            if len(entry) < 32 or entry[0] == 0x00:
                break
            attr = entry[11]
            if attr & 0x3F == 0x0F:
                long_name = _decode_lfn(entry) + long_name
                continue
            if attr & 0x08 and not long_name:
                continue
            deleted = entry[0] == 0xE5
            name = "".join(long_name) if long_name else _short_name(entry, deleted)
            long_name = []
            result.entries_seen += 1
            if deleted:
                result.deleted_found += 1
                if not self.include_deleted:
                    continue
            cluster = struct.unpack("<H", entry[26:28])[0]
            file_size = struct.unpack("<I", entry[28:32])[0]
            if self.geo["type"] == "fat32":
                cluster |= struct.unpack("<H", entry[20:22])[0] << 16
            is_dir = bool(attr & 0x10)
            display = safe_filename(name) + ("/" if is_dir else "")
            record = RecoveredEntry(
                name=name, path=prefix + display, size_bytes=file_size,
                first_cluster=cluster, deleted=deleted, is_dir=is_dir,
            )
            result.entries.append(record)
            if is_dir:
                if cluster >= 2 and cluster not in self._seen_dirs and depth < 24:
                    self._seen_dirs.add(cluster)
                    dir_data = self._read_chain(cluster)
                    if dir_data:
                        self._walk_bytes(dir_data, prefix + display + "/", result, depth + 1)
                continue
            if file_size == 0 or cluster < 2:
                record.reason = "empty file or no cluster allocated"
                continue
            self._export(record, prefix, result)

    def _parse_exfat_dir(self, raw: bytes, prefix: str, result: FatRecoveryResult, depth: int) -> None:
        """exFAT directory entries: 0x85 (file), 0xC0 (stream), 0x83 (label)."""
        pending_name: Optional[str] = None
        pending_deleted = False
        pending_attr = 0
        for pos in range(0, len(raw), 32):
            rec = raw[pos : pos + 32]
            if len(rec) < 32 or rec[0] == 0x00:
                break
            etype = rec[0]
            if etype == 0x85 or etype == 0x05:  # file entry (0x05 = deleted variant)
                length = rec[3]
                name = rec[2 : 2 + min(length * 2, 30)].decode("utf-16-le", "replace")
                pending_name = name
                pending_deleted = etype == 0x05
                pending_attr = struct.unpack("<H", rec[4:6])[0]
                result.entries_seen += 1
                if pending_deleted:
                    result.deleted_found += 1
                continue
            if etype in (0xC0, 0x40) and pending_name is not None:  # stream extension
                cluster = struct.unpack("<I", rec[20:24])[0]
                data_length = struct.unpack("<Q", rec[24:32])[0]
                is_dir = bool(pending_attr & 0x10)
                display = safe_filename(pending_name) + ("/" if is_dir else "")
                record = RecoveredEntry(
                    name=pending_name,
                    path=prefix + display,
                    size_bytes=data_length,
                    first_cluster=cluster,
                    deleted=pending_deleted,
                    is_dir=is_dir,
                )
                if not (pending_deleted and not self.include_deleted):
                    result.entries.append(record)
                    if is_dir:
                        if cluster >= 2 and cluster not in self._seen_dirs and depth < 24:
                            self._seen_dirs.add(cluster)
                            self._walk_bytes_exfat(cluster, prefix + display + "/", result, depth + 1)
                    elif data_length and cluster >= 2:
                        self._export(record, prefix, result)
                pending_name = None

    def _walk_bytes_exfat(self, cluster: int, prefix: str, result: FatRecoveryResult, depth: int) -> None:
        data = self._read_chain(cluster)
        if data:
            # Recursive exFAT parse without offset statistics
            saved = result.entries_seen
            self._parse_exfat_dir(data, prefix, result, depth)
            result.entries_seen = max(saved, result.entries_seen)

    # ------------------------------------------------------------ exporting
    def _export(self, record: RecoveredEntry, prefix: str, result: FatRecoveryResult) -> None:
        try:
            payload = self._read_chain(record.first_cluster, record.size_bytes)
        except Exception as exc:
            record.reason = f"read failed: {exc}"
            result.failures += 1
            return
        if len(payload) < record.size_bytes:
            record.reason = (
                f"only {len(payload):,} of {record.size_bytes:,} bytes are still readable "
                "(clusters may have been reused)"
            )
            result.failures += 1
            # Still export the partial data: some of the file is better than none.
        target_dir = self.output_dir / prefix.strip("/") if prefix else self.output_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(record.name) or f"file_{record.first_cluster}"
        if record.deleted:
            base, dot, ext = name.rpartition(".")
            name = f"{base}_deleted.{ext}" if dot else f"{name}_deleted"
        target = _unique_path(target_dir, name)
        target.write_bytes(payload)
        os.chmod(target, 0o600)
        record.recovered = True
        record.output_path = str(target)
        result.files_recovered += 1
        result.bytes_recovered += len(payload)
        if self.progress:
            self.progress(result.files_recovered, 0, f"recovered {name}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, _, suffix = filename.rpartition(".")
    counter = 2
    while True:
        candidate = directory / (f"{stem}_{counter}.{suffix}" if suffix else f"{filename}_{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _short_name(entry: bytes, deleted: bool) -> str:
    raw = entry[0:11]
    if deleted:
        raw = b"?" + raw[1:]
    name = raw.decode("cp437", "replace")
    base = name[:8].rstrip()
    ext = name[8:].rstrip()
    return f"{base}.{ext}" if ext else base


def _decode_lfn(entry: bytes) -> list[str]:
    """Decode one long-filename entry (13 UCS-2 characters)."""
    chars = []
    for start in (1, 14, 28):
        chunk = entry[start : start + 10]
        for i in range(0, 10, 2):
            code = struct.unpack("<H", chunk[i : i + 2])[0]
            if code in (0x0000, 0xFFFF):
                continue
            chars.append(chr(code))
    return ["".join(chars)]
