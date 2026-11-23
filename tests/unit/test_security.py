"""Security tests: the guarantees that protect the user's data."""

from __future__ import annotations

import os
import tempfile

import pytest

from usbdoctor.core.errors import DeviceIdentityChangedError, UnsafeOperationError
from usbdoctor.core.io import create_sparse_image
from usbdoctor.core.logging import AuditLog
from usbdoctor.core.model import DeviceRef
from usbdoctor.security.consent import ConsentBroker
from usbdoctor.security.policy import PolicySettings, SafetyPolicy
from usbdoctor.security.writer_guard import GuardedWriter, aligned, compute_signature


@pytest.fixture()
def image(tmp_path):
    path = tmp_path / "disk.img"
    create_sparse_image(path, 4 * 1024 * 1024)
    with open(path, "r+b") as fh:
        fh.write(b"\xeb\x3c\x90MSDOS5.0" + b"\x00" * 497)
        fh.seek(510)
        fh.write(b"\x55\xaa")
    return path


@pytest.fixture()
def policy():
    return SafetyPolicy(PolicySettings(allow_internal_disks=True))


class TestSafetyPolicy:
    def test_system_disk_is_never_writable(self, image, policy):
        device = DeviceRef(path=str(image), kind="disk", is_system_disk=True)
        with pytest.raises(UnsafeOperationError) as excinfo:
            policy.check_writable(device, "format")
        assert "operating system" in str(excinfo.value).lower()

    def test_real_system_disk_detected(self, policy):
        # On this machine the root device must be flagged so the UI can explain it.
        assert isinstance(policy.system_disks, set)

    def test_read_only_device_blocked(self, image, policy):
        device = DeviceRef(path=str(image), kind="disk", read_only=True)
        with pytest.raises(UnsafeOperationError):
            policy.check_writable(device, "write")

    def test_mounted_device_blocked(self, image, policy):
        device = DeviceRef(path=str(image), kind="disk", mountpoints=["/media/usb"])
        with pytest.raises(UnsafeOperationError) as excinfo:
            policy.check_writable(device, "write")
        assert "mounted" in str(excinfo.value).lower()

    def test_internal_disk_blocked_unless_enabled(self, image):
        strict = SafetyPolicy(PolicySettings(allow_internal_disks=False))
        device = DeviceRef(path=str(image), kind="disk", bus="sata")
        with pytest.raises(UnsafeOperationError):
            strict.check_writable(device, "write")
        SafetyPolicy(PolicySettings(allow_internal_disks=True)).check_writable(device, "write")

    def test_protected_path_blocked(self, image):
        pol = SafetyPolicy(PolicySettings(allow_internal_disks=True, protected_paths=[str(image)]))
        with pytest.raises(UnsafeOperationError):
            pol.check_writable(DeviceRef(path=str(image), kind="image"), "write")

    def test_usb_device_allowed(self, image, policy):
        device = DeviceRef(path=str(image), kind="disk", bus="usb")
        policy.check_writable(device, "write")  # must not raise


class TestGuardedWriter:
    def test_identity_change_blocks_write(self, image, policy):
        device = DeviceRef(path=str(image), kind="image")
        sig = compute_signature(str(image))
        with open(image, "r+b") as fh:  # simulate a swapped / modified device
            fh.seek(0)
            fh.write(b"\x00" * 512)
        with pytest.raises(DeviceIdentityChangedError):
            with GuardedWriter(device, expected_signature=sig, policy=policy):
                pass

    def test_dry_run_does_not_touch_disk(self, image, policy):
        device = DeviceRef(path=str(image), kind="image")
        sig = compute_signature(str(image))
        before = image.read_bytes()
        with GuardedWriter(device, expected_signature=sig, policy=policy, dry_run=True) as writer:
            assert writer.write(0, b"\xAA" * 512) == 512
        assert image.read_bytes() == before

    def test_sector_alignment_of_byte_range(self):
        assert aligned(510, 2, 512) == (0, 512)
        assert aligned(4096, 512, 512) == (4096, 512)
        assert aligned(1023, 2, 512) == (512, 1024)

    def test_sub_sector_write_does_preserve_neighbours(self, image, policy):
        """A 2-byte signature patch must not disturb the bytes around it."""
        device = DeviceRef(path=str(image), kind="image")
        sig = compute_signature(str(image))
        with open(image, "rb") as fh:
            original = fh.read(1024)
        with GuardedWriter(device, expected_signature=sig, policy=policy) as writer:
            writer.write(510, b"\xAA\xBB")
        with open(image, "rb") as fh:
            after = fh.read(1024)
        assert after[510:512] == b"\xAA\xBB"
        assert after[:510] == original[:510]
        assert after[512:] == original[512:]

    def test_write_is_audited(self, image, policy, tmp_path):
        audit = AuditLog(tmp_path / "audit.jsonl")
        device = DeviceRef(path=str(image), kind="image")
        sig = compute_signature(str(image))
        with GuardedWriter(device, expected_signature=sig, policy=policy, audit=audit, operation="test") as w:
            w.write(0, b"\x00" * 512)
        entries = audit.tail()
        assert [e["action"] for e in entries] == ["test:start", "test:finish"]
        assert entries[-1]["outcome"] == "ok"


class TestConsentBroker:
    def test_token_is_single_use(self, image):
        broker = ConsentBroker()
        sig = compute_signature(str(image))
        token = broker.grant(operation_id="op", device_path=str(image), signature=sig, risks=["format"])
        broker.consume(token.token, device_path=str(image), signature=sig)
        with pytest.raises(UnsafeOperationError):
            broker.consume(token.token, device_path=str(image), signature=sig)

    def test_token_bound_to_device(self, image):
        broker = ConsentBroker()
        sig = compute_signature(str(image))
        token = broker.grant(operation_id="op", device_path=str(image), signature=sig, risks=[])
        with pytest.raises(UnsafeOperationError):
            broker.consume(token.token, device_path="/dev/other", signature=sig)

    def test_token_expires(self, image):
        broker = ConsentBroker(ttl_seconds=-1)  # already expired
        sig = compute_signature(str(image))
        token = broker.grant(operation_id="op", device_path=str(image), signature=sig, risks=[])
        with pytest.raises(UnsafeOperationError):
            broker.consume(token.token, device_path=str(image), signature=sig)

    def test_token_invalidated_by_identity_change(self, image):
        broker = ConsentBroker()
        sig = compute_signature(str(image))
        token = broker.grant(operation_id="op", device_path=str(image), signature=sig, risks=[])
        with open(image, "r+b") as fh:
            fh.write(b"\xff" * 512)
        changed = compute_signature(str(image))
        with pytest.raises(UnsafeOperationError):
            broker.consume(token.token, device_path=str(image), signature=changed)

    def test_unknown_token_rejected(self, image):
        broker = ConsentBroker()
        with pytest.raises(UnsafeOperationError):
            broker.consume("made-up", device_path=str(image), signature=compute_signature(str(image)))
