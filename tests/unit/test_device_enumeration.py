"""Enumeration must survive a machine that actually has partitions.

This is the bug the first honest CI run found: `Partition` is a frozen
dataclass, and the Linux enricher assigned to it. The assignment only executes
when a disk *has* partitions, so it never fired on a partitionless container —
every developer and CI sandbox passed while `usbdoctor devices` crashed with
``cannot assign to field 'offset_bytes'`` on any normal Linux desktop.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError

import pytest

from usbdoctor.core.model import Partition

linux = pytest.importorskip("usbdoctor.devices.linux", reason="Linux provider only")
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux provider only")

GIB = 1024**3

# What lsblk reports for a 32 GiB stick with one FAT32 partition. `offset_bytes`
# is absent from lsblk, which is why the enricher has to fill it in from sysfs.
LSBLK_JSON = {
    "blockdevices": [
        {
            "name": "sda",
            "kname": "sda",
            "type": "disk",
            "size": str(32 * GIB),
            "rm": True,
            "ro": False,
            "pttype": "gpt",
            "vendor": "SanDisk ",
            "model": "Cruzer Blade",
            "serial": "ABC123",
            "mountpoints": [None],
            "children": [
                {
                    "name": "sda1",
                    "kname": "sda1",
                    "type": "part",
                    "size": str(32 * GIB),
                    "fstype": "vfat",
                    "label": "USB",
                    "uuid": "1234-ABCD",
                    "mountpoints": ["/media/user/USB"],
                    "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
                }
            ],
        }
    ]
}

START_SECTOR = 2048


class _Proc:
    ok = True
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


@pytest.fixture()
def provider(monkeypatch):
    """A Linux provider wired to a plausible partitioned disk, no real hardware."""

    def fake_run(args, **kwargs):
        if "--list-columns" in args:
            return _Proc("NAME SIZE RM RO VENDOR MODEL SERIAL MOUNTPOINTS FSTYPE LABEL UUID PARTTYPE PTTYPE\n")
        if "-J" in args and "--noheadings" not in args:
            return _Proc(json.dumps(LSBLK_JSON))
        return _Proc("{}")

    monkeypatch.setattr(linux, "which", lambda name: "/usr/bin/lsblk")
    monkeypatch.setattr(linux, "run_process", fake_run)
    monkeypatch.setattr(linux, "_sysfs_dir", lambda name: f"/sys/devices/fake/{name}")
    monkeypatch.setattr(
        linux,
        "_sysfs_int",
        lambda name, attr: START_SECTOR if attr == "start" else 0,
    )
    monkeypatch.setattr(linux, "_sysfs_text_multi", lambda *a, **k: "")
    monkeypatch.setattr(linux, "_sysfs_text", lambda *a, **k: "")
    monkeypatch.setattr(linux, "_find_usb_device_dir", lambda real: None)
    monkeypatch.setattr(
        linux,
        "_blkid_field",
        lambda path, field: {"TYPE": "vfat", "LABEL": "USB", "UUID": "1234-ABCD"}.get(field),
    )
    return linux.LinuxDeviceProvider()


def test_a_partitioned_disk_is_enumerated_not_rejected(provider):
    result = provider.list_devices(include_internal=True)
    assert result.devices, f"no devices enumerated (warnings: {result.warnings})"
    assert [d.path for d in result.devices] == ["/dev/sda"]
    assert result.devices[0].partitions, "the partition was dropped"


def test_partition_offset_comes_from_sysfs(provider):
    part = provider.list_devices(include_internal=True).devices[0].partitions[0]
    assert part.offset_bytes == START_SECTOR * 512


def test_partition_metadata_is_enriched(provider):
    part = provider.list_devices(include_internal=True).devices[0].partitions[0]
    assert part.filesystem == "vfat"
    assert part.label == "USB"
    assert part.uuid == "1234-ABCD"
    assert part.path == "/dev/sda1"


def test_enrichment_does_not_mutate_the_frozen_record(provider):
    """The fix rebuilds the record; nothing may write through the old object."""
    result = provider.list_devices(include_internal=True)
    part = result.devices[0].partitions[0]
    with pytest.raises(FrozenInstanceError):
        part.offset_bytes = 0  # type: ignore[misc]


def test_enumeration_is_repeatable(provider):
    """Enrichment must not depend on state left behind by a previous call."""
    first = provider.list_devices(include_internal=True).devices[0].partitions[0]
    second = provider.list_devices(include_internal=True).devices[0].partitions[0]
    assert (first.offset_bytes, first.label, first.uuid) == (
        second.offset_bytes,
        second.label,
        second.uuid,
    )


def test_partition_without_a_sysfs_start_keeps_its_own_offset(monkeypatch, provider):
    """An unreadable /sys start must not zero out a known offset."""
    monkeypatch.setattr(linux, "_sysfs_int", lambda name, attr: 0)
    part = provider.list_devices(include_internal=True).devices[0].partitions[0]
    assert part.offset_bytes == 0  # unknown stays unknown, never invented
    assert part.label == "USB"  # but the rest is still enriched


def test_partition_model_stays_immutable():
    """Immutability is a safety property; the fix must not be 'make it mutable'."""
    part = Partition(index=1, path="/dev/sda1", offset_bytes=0, size_bytes=100)
    with pytest.raises(FrozenInstanceError):
        part.offset_bytes = 1048576  # type: ignore[misc]
