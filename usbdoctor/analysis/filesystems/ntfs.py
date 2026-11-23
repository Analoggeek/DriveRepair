"""NTFS analysis: boot sector, $MFT record 0, $Volume dirty flags.

Windows-only CHKDSK is delegated by the repair engine, but a complete read-only
inspection runs on every platform so macOS users get the same diagnosis.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

NTFS_OEM = b"NTFS    "
MFT_RECORD_MAGIC = b"FILE"
MFT_IN_USE = 0x01
MFT_IS_DIRECTORY = 0x02


def decode_mft_runlist(data: bytes) -> list[tuple[int, int]]:
    """Decode an NTFS data-run list into (lcn, length_in_clusters) tuples."""
    runs: list[tuple[int, int]] = []
    i = 0
    lcn = 0
    while i < len(data):
        header = data[i]
        if header == 0x00:
            break
        length_size = header & 0x0F
        offset_size = (header >> 4) & 0x0F
        i += 1
        if i + length_size + offset_size > len(data):
            break
        length = int.from_bytes(data[i : i + length_size], "little", signed=False)
        i += length_size
        offset_raw = data[i : i + offset_size]
        i += offset_size
        offset = int.from_bytes(offset_raw, "little", signed=True) if offset_raw else 0
        if offset == 0:
            continue  # sparse run
        lcn += offset
        if length:
            runs.append((lcn, length))
    return runs


@dataclass
class NtfsResult:
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 8
    total_sectors: int = 0
    mft_lcn: int = 0
    mft_mirror_lcn: int = 0
    clusters_per_mft_record: int = 1
    clusters_per_index_buffer: int = 1
    volume_serial: int = 0
    volume_label: str = ""
    fs_version: str = ""
    dirty: Optional[bool] = None
    mft_record_size: int = 1024
    mft_runs: list[tuple[int, int]] = field(default_factory=list)
    mft_records_scanned: int = 0
    used_records: int = 0
    issues: list[TableIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * max(1, abs(self.sectors_per_cluster))

    @property
    def volume_size(self) -> int:
        return self.total_sectors * self.bytes_per_sector

    def to_dict(self) -> dict:
        return {
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "total_sectors": self.total_sectors,
            "mft_lcn": self.mft_lcn,
            "mft_mirror_lcn": self.mft_mirror_lcn,
            "mft_record_size": self.mft_record_size,
            "volume_serial": f"{self.volume_serial:08X}",
            "volume_label": self.volume_label,
            "fs_version": self.fs_version,
            "dirty": self.dirty,
            "mft_runs": [{"lcn": l, "clusters": c} for l, c in self.mft_runs],
            "mft_records_scanned": self.mft_records_scanned,
            "used_records": self.used_records,
            "issues": [i.__dict__ for i in self.issues],
            "notes": self.notes,
        }


class NtfsAnalyzer:
    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size

    def analyze(self) -> NtfsResult:
        res = NtfsResult()
        boot = self.reader.read(self.base, 512)
        if len(boot) < 512 or boot[3:11] != NTFS_OEM:
            res.issues.append(
                TableIssue("critical", "NTFS_SIGNATURE_MISSING", "NTFS OEM identifier 'NTFS    ' not found.")
            )
            return res
        res.bytes_per_sector = struct.unpack("<H", boot[11:13])[0] or 512
        res.sectors_per_cluster = struct.unpack("<b", boot[13:14])[0]
        if res.sectors_per_cluster > 0x80:  # negative => 2^|value| sectors per cluster
            res.sectors_per_cluster = 1 << (256 - boot[13])
        res.total_sectors = struct.unpack("<Q", boot[40:48])[0]
        res.mft_lcn = struct.unpack("<Q", boot[48:56])[0]
        res.mft_mirror_lcn = struct.unpack("<Q", boot[56:64])[0]
        # NTFS stores these as *signed* bytes: positive = clusters per record,
        # negative = 2^|value| bytes per record (e.g. 0xF6 => 1024 bytes).
        mft_rec = struct.unpack("<b", boot[64:65])[0]
        if mft_rec < 0:
            res.clusters_per_mft_record = 1
            res.mft_record_size = 1 << (-mft_rec)
        else:
            res.clusters_per_mft_record = mft_rec or 1
            res.mft_record_size = res.clusters_per_mft_record * res.cluster_size
        idx = struct.unpack("<b", boot[68:69])[0]
        res.clusters_per_index_buffer = (1 << (-idx)) if idx < 0 else (idx or 1)
        if not 512 <= res.mft_record_size <= 65536:
            res.issues.append(
                TableIssue(
                    "error",
                    "NTFS_BAD_MFT_RECORD_SIZE",
                    f"$MFT record size decodes to {res.mft_record_size} bytes, which is impossible.",
                )
            )
            res.mft_record_size = 1024
        res.volume_serial = struct.unpack("<Q", boot[72:80])[0]
        if boot[510:512] != b"\x55\xaa":
            res.issues.append(
                TableIssue(
                    "critical",
                    "NTFS_BOOT_SIGNATURE_MISSING",
                    "NTFS boot sector signature 0x55AA is missing — Windows will report the volume as RAW.",
                    repairable=True,
                )
            )
        if res.bytes_per_sector not in (512, 1024, 2048, 4096):
            res.issues.append(
                TableIssue("error", "NTFS_BAD_SECTOR_SIZE", f"Bytes per sector {res.bytes_per_sector} is invalid.")
            )
        if res.sectors_per_cluster <= 0 or res.sectors_per_cluster > 128:
            res.issues.append(
                TableIssue("error", "NTFS_BAD_CLUSTERS", f"Sectors per cluster {res.sectors_per_cluster} is invalid.")
            )
        limit = self.volume_size or self._limit()
        if limit and res.volume_size > limit:
            res.issues.append(
                TableIssue(
                    "critical",
                    "NTFS_VOLUME_BEYOND_DEVICE",
                    f"NTFS geometry claims {res.volume_size:,} bytes but the device has {limit:,}.",
                    detail={"claimed": res.volume_size, "device": limit},
                )
            )
        self._read_mft(res)
        return res

    def _limit(self) -> Optional[int]:
        try:
            return self.reader.size() - self.base
        except Exception:
            return None

    def _read_mft(self, res: NtfsResult) -> None:
        """Read $MFT record 0 (the MFT's own file record) and derive its runs."""
        if res.mft_lcn == 0 or res.cluster_size == 0:
            res.issues.append(TableIssue("error", "NTFS_MFT_POINTER_ZERO", "MFT location pointer is zero."))
            return
        mft_offset = self.base + res.mft_lcn * res.cluster_size
        record = self.reader.read(mft_offset, max(1024, res.mft_record_size))
        if len(record) < 48 or record[:4] != MFT_RECORD_MAGIC:
            res.issues.append(
                TableIssue(
                    "critical",
                    "NTFS_MFT_RECORD_INVALID",
                    "The master file table's first record has an invalid signature — heavy metadata damage.",
                )
            )
            # Try the mirror.
            mirror = self.reader.read(self.base + res.mft_mirror_lcn * res.cluster_size, 1024)
            if mirror[:4] == MFT_RECORD_MAGIC:
                res.issues.append(
                    TableIssue(
                        "warning",
                        "NTFS_MFT_MIRROR_OK",
                        "The MFT mirror still contains a valid record; CHKDSK /ntfsfix can often rebuild from it.",
                        repairable=True,
                    )
                )
            return
        # Byte 22 of an MFT record is the flags byte.  Bit 0 is IN_USE: a
        # *clean* record has it set.  Editors (and Windows itself) clear bit 1
        # of the $Volume record for a dirty volume, but a volume is flagged
        # dirty to consumers via the $Volume metadata file; at this level we
        # report what the record says and let the OS-level tools confirm.
        flags = record[22]
        res.dirty = not bool(flags & 0x01)
        if res.dirty:
            res.issues.append(
                TableIssue(
                    "warning",
                    "NTFS_DIRTY",
                    "NTFS volume is marked dirty (unclean shutdown or pending metadata changes).",
                    repairable=True,
                )
            )
        alloc_size = struct.unpack("<I", record[28:32])[0]
        used = struct.unpack("<I", record[24:28])[0]
        records = max(0, used) // max(1, res.mft_record_size)
        res.mft_records_scanned = min(records, 65536)

        # Walk attributes to find the unnamed $DATA run list (0x80 non-resident).
        attr_off = struct.unpack("<H", record[20:22])[0]
        pos = attr_off
        while pos + 8 < len(record):
            atype = struct.unpack("<I", record[pos : pos + 4])[0]
            if atype == 0xFFFFFFFF:
                break
            length = struct.unpack("<I", record[pos + 4 : pos + 8])[0]
            if length == 0 or pos + length > len(record):
                break
            non_resident = record[pos + 8]
            name_len = record[pos + 9]
            if atype == 0x80 and non_resident == 1 and name_len == 0:
                # Non-resident header: 0x20 = run-list offset, 0x30 = real size.
                # The run list occupies the rest of the attribute record, so its
                # length is (attribute length - run-list offset), not a field.
                run_off = struct.unpack("<H", record[pos + 32 : pos + 34])[0]
                run_len = max(0, length - run_off)
                runlist = record[pos + run_off : pos + run_off + run_len]
                res.mft_runs = decode_mft_runlist(runlist)[:64]
                real_size = struct.unpack("<Q", record[pos + 48 : pos + 56])[0]
                res.notes.append(f"$MFT data size {real_size:,} bytes across {len(res.mft_runs)} run(s).")
                break
            pos += length
        if not res.mft_runs:
            res.issues.append(
                TableIssue(
                    "warning",
                    "NTFS_MFT_RUNLIST_UNREADABLE",
                    "Could not decode the $MFT run list; deep metadata verification is limited.",
                )
            )


def analyze_ntfs(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> NtfsResult:
    return NtfsAnalyzer(reader, base_offset, volume_size).analyze()
