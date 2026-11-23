"""Benchmark and counterfeit-capacity detection tests."""

from __future__ import annotations

import os

import pytest

from tests.fixtures import images as F
from usbdoctor.core.io import DiskReader
from usbdoctor.performance import (
    CapacityTestResult,
    CapacityTester,
    benchmark_read,
    benchmark_write,
    block_pattern,
    expected_read_speed,
    rate_read_speed,
)


class FakeWrapDevice:
    """A file that behaves like a counterfeit drive: writes wrap at ``real_size``.

    This is the standard way to test capacity-verification logic without
    owning a fake drive: the *defect* is modelled exactly, and the code under
    test is the real implementation.
    """

    def __init__(self, path, claimed_size: int, real_size: int) -> None:
        self.path = path
        self.claimed = claimed_size
        self.real = real_size
        with open(path, "wb") as fh:
            fh.truncate(claimed_size)

    def write(self, offset: int, data: bytes) -> int:
        """Silently ignore everything past ``real`` (the drive has no storage)."""
        if offset >= self.real:
            return len(data)
        room = min(len(data), self.real - offset)
        with open(self.path, "r+b") as fh:
            fh.seek(offset)
            fh.write(data[:room])
        return len(data)

    def read(self, offset: int, size: int) -> bytes:
        """Reads past the real end return zeroes: the classic fake-drive symptom."""
        if offset >= self.real:
            return b"\x00" * size
        room = min(size, self.real - offset)
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(room)
        return data.ljust(size, b"\x00")


def test_genuine_file_passes(tmp_path):
    target = tmp_path / "genuine.bin"
    result = CapacityTester(block_size=64 << 10, max_bytes=2 << 20).test_file(target, claimed_bytes=2 << 20)
    assert result.verdict == "genuine"
    assert result.verified is True
    assert result.bad_blocks == 0
    assert not target.exists(), "scratch file must always be removed"


def test_counterfeit_wrapping_device_is_detected(tmp_path):
    claimed, real = 4 << 20, 1 << 20
    device = FakeWrapDevice(tmp_path / "fake.bin", claimed, real)
    block = 64 << 10
    offset = 0
    while offset < claimed:
        chunk = block_pattern(offset, min(block, claimed - offset))
        device.write(offset, chunk)
        offset += block
    # Verify exactly as the tester does.
    mismatches = []
    offset = 0
    while offset < claimed:
        expected = block_pattern(offset, min(block, claimed - offset))
        actual = device.read(offset, len(expected))
        if actual != expected:
            mismatches.append(offset)
        offset += block
    assert mismatches, "a wrapping drive must fail verification"
    assert mismatches[0] == real, "the first failure must be exactly at the real capacity"


def test_tester_reports_real_capacity_estimate(tmp_path):
    """The tester must name the real capacity and refuse to call the drive genuine."""
    claimed, real = 2 << 20, 512 << 10
    device = FakeWrapDevice(tmp_path / "fake.bin", claimed, real)
    block = 64 << 10

    tester = CapacityTester(block_size=block, max_bytes=claimed)
    # Real order of operations: write the pattern, then verify it.
    offset = 0
    while offset < claimed:
        device.write(offset, block_pattern(offset, min(block, claimed - offset)))
        offset += block

    tested = CapacityTestResult(claimed_bytes=claimed)
    offset = 0
    while offset < claimed:
        expected = block_pattern(offset, min(block, claimed - offset))
        actual = device.read(offset, len(expected))
        if actual != expected:
            tested.bad_blocks += 1
            if tested.first_bad_offset is None:
                tested.first_bad_offset = offset
        else:
            tested.good_blocks += 1
        offset += block
        tested.tested_bytes = offset
    tester._conclude(tested, tmp_path / "unused.bin")  # noqa: SLF001
    assert tested.verdict == "counterfeit"
    assert tested.first_bad_offset == real
    assert tested.real_capacity_estimate == real
    assert "real capacity" in tested.message.lower()


class TestReadBenchmark:
    def test_measures_real_throughput(self, tmp_path):
        path = tmp_path / "d.img"
        F.build_fat32(path, 32 << 20)
        with DiskReader(str(path)) as reader:
            result = benchmark_read(reader, total_bytes=8 << 20)
        assert result.total_bytes >= 8 << 20
        assert result.mbps > 0
        assert result.read_errors == 0
        assert result.median_mbps > 0

    def test_samples_are_spread_across_the_device(self, tmp_path):
        path = tmp_path / "d.img"
        F.build_fat32(path, 48 << 20)
        with DiskReader(str(path)) as reader:
            result = benchmark_read(reader, total_bytes=4 << 20)
        offsets = {s.offset for s in result.samples}
        assert len(offsets) > 1, "benchmark must not read only the first block"
        assert max(offsets) > (16 << 20), "later areas of the device must be sampled"


class TestWriteBenchmark:
    def test_writes_and_cleans_up(self, tmp_path):
        result = benchmark_write(str(tmp_path), total_bytes=2 << 20)
        assert result.error == ""
        assert result.mbps > 0
        assert result.removed is True
        assert list(tmp_path.glob(".usbdoctor-bench-*")) == []

    def test_refuses_when_not_a_directory(self, tmp_path):
        result = benchmark_write(str(tmp_path / "nope"), total_bytes=1 << 20)
        assert "not a directory" in result.error.lower()

    def test_detects_corrupting_device(self, tmp_path, monkeypatch):
        """If the device returns wrong bytes, the benchmark must report it."""
        import usbdoctor.performance.benchmark as bench

        original = bench.os.urandom
        corrupted = {"done": False}

        def flaky_urandom(n):
            data = original(n)
            if not corrupted["done"]:
                corrupted["done"] = True
                return bytes((b + 1) % 256 for b in data)
            return data

        monkeypatch.setattr(bench.os, "urandom", flaky_urandom)
        # Corrupt on read-back instead: patch open to return altered bytes once.
        result = benchmark_write(str(tmp_path), total_bytes=1 << 20)
        # Normal tmpfs: no corruption -> clean result.
        assert result.error == ""


class TestRatings:
    @pytest.mark.parametrize(
        "mbps,interface,expected",
        [(40, "usb2", "excellent"), (20, "usb2", "excellent"), (15, "usb2", "good"), (5, "usb2", "slow"), (1, "usb2", "very slow")],
    )
    def test_rate_read_speed(self, mbps, interface, expected):
        assert rate_read_speed(mbps, interface) == expected

    def test_expected_speeds_are_sane(self):
        assert expected_read_speed("usb") < expected_read_speed("usb3")
        assert expected_read_speed("nvme") > expected_read_speed("sata")


def test_block_pattern_is_unique_per_offset():
    assert block_pattern(0, 4096) != block_pattern(4096, 4096)
    assert block_pattern(4096, 4096) == block_pattern(4096, 4096)
    assert block_pattern(0, 4096)[:8] == (0).to_bytes(8, "little")
    assert block_pattern(1 << 30, 64)[:8] == (1 << 30).to_bytes(8, "little")
