"""Unit tests: sizes, hashing, atomic files, subprocess safety, device model."""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from usbdoctor.core.errors import ToolExecutionError, UnsafeOperationError
from usbdoctor.core.model import DeviceRef, DeviceSignature, Partition
from usbdoctor.core.util import (
    atomic_write_json,
    digest_reader,
    human_size,
    parse_size,
    read_json,
    run_process,
    safe_filename,
    sha256_file,
)


class TestSizes:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "unknown"),
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024 ** 3, "1.0 GiB"),
            (31_268_454_400, "29.1 GiB"),
        ],
    )
    def test_human_size(self, value, expected):
        assert human_size(value) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [("4G", 4 * 1024 ** 3), ("512MiB", 512 * 1024 ** 2), ("1048576", 1048576), ("1Tb", 1024 ** 4)],
    )
    def test_parse_size(self, text, expected):
        assert parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "4X", "-5G"])
    def test_parse_size_rejects_garbage(self, text):
        with pytest.raises(ValueError):
            parse_size(text)


class TestFiles:
    def test_atomic_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "data.json")
            atomic_write_json(path, {"a": 1})
            assert read_json(path) == {"a": 1}
            if os.name != "nt":
                # Windows has no POSIX mode bits; chmod there only toggles the
                # read-only flag, so this assertion cannot hold by construction.
                assert oct(os.stat(path).st_mode)[-3:] == "600"

    def test_read_json_missing_returns_default(self):
        assert read_json("/nonexistent/xyz.json", default={"x": 1}) == {"x": 1}

    def test_sha256_file_matches_hashlib(self):
        import hashlib

        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"usb doctor" * 1000)
            path = fh.name
        try:
            assert sha256_file(path) == hashlib.sha256(b"usb doctor" * 1000).hexdigest()
        finally:
            os.unlink(path)


class TestDigestReader:
    def test_small_stream(self):
        data = b"0123456789" * 100
        digest = digest_reader(lambda off, size: data[off : off + size], length=len(data))
        import hashlib

        assert digest == hashlib.sha256(data).hexdigest()

    def test_sampled_large_stream_is_stable(self):
        total = 100 << 20
        reader = lambda off, size: b"x" * size  # noqa: E731 - constant device
        a = digest_reader(reader, length=total, budget=1 << 20)
        b = digest_reader(reader, length=total, budget=1 << 20)
        assert a == b
        assert len(a) == 64


class TestSubprocess:
    """These use the current interpreter, not shell utilities.

    ``echo`` and ``sleep`` are not programs on Windows — one is a cmd builtin and
    the other does not exist — so a test written around them passes on Linux and
    macOS and fails for every Windows user's build. ``sys.executable`` is the one
    command guaranteed to exist wherever the tests run.
    """

    def test_argv_is_not_shell_interpreted(self):
        # A classic injection payload must be treated as a literal argument.
        payload = "hello; rm -rf /"
        result = run_process([sys.executable, "-c", "import sys; print(sys.argv[1])", payload])
        assert result.ok, result.stderr
        assert payload in result.stdout

    def test_missing_tool_raises_tool_error(self):
        with pytest.raises(ToolExecutionError) as excinfo:
            run_process(["definitely-not-a-real-binary-xyz"])
        assert "not found" in str(excinfo.value).lower()

    def test_timeout_raises(self):
        with pytest.raises(ToolExecutionError):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)


class TestSafeFilename:
    @pytest.mark.parametrize(
        "raw,forbidden",
        [("../../etc/passwd", "/"), ("a:b*c?d", "*"), ("", None), ("\x00\x01", "\x00")],
    )
    def test_strips_dangerous_characters(self, raw, forbidden):
        cleaned = safe_filename(raw)
        if forbidden is not None:
            assert forbidden not in cleaned
        assert cleaned


class TestDeviceModel:
    def test_display_name_avoids_duplication(self):
        device = DeviceRef(path="/dev/sdb", vendor="SanDisk", model="SanDisk Cruzer Blade")
        assert device.display_name == "SanDisk Cruzer Blade"
        assert device.to_dict()["display_name"] == "SanDisk Cruzer Blade"

    def test_display_name_falls_back(self):
        assert DeviceRef(path="/dev/sdb").display_name == "Unknown device"

    def test_is_usb(self):
        assert DeviceRef(path="/dev/sdb", bus="USB").is_usb
        assert not DeviceRef(path="/dev/sda", bus="sata").is_usb

    def test_partition_serialisation(self):
        part = Partition(index=1, path="/dev/sdb1", offset_bytes=1048576, size_bytes=4096, filesystem="fat32")
        assert part.to_dict()["offset_bytes"] == 1048576

    def test_signature_matching(self):
        a = DeviceSignature(path="/dev/sdb", size_bytes=100, head_sha256="h", tail_sha256="t")
        b = DeviceSignature(path="/dev/sdb", size_bytes=100, head_sha256="h", tail_sha256="t")
        c = DeviceSignature(path="/dev/sdb", size_bytes=100, head_sha256="DIFFERENT", tail_sha256="t")
        assert a.matches(b)
        assert not a.matches(c)


class TestErrors:
    def test_error_carries_context(self):
        err = UnsafeOperationError("nope", hint="try again", device="/dev/sdb")
        payload = err.to_dict()
        assert payload["code"] == "UNSAFE_OPERATION"
        assert payload["hint"] == "try again"
        assert payload["context"]["device"] == "/dev/sdb"
