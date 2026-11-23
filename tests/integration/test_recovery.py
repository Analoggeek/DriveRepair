"""Recovery tests: imaging, resume, carving and FAT/exFAT file recovery."""

from __future__ import annotations

import struct

import pytest

from tests.fixtures import images as F
from usbdoctor.core.io import DiskReader
from usbdoctor.recovery import Carver, FatRecovery, ImageCreator, ImageMap
from usbdoctor.recovery.signatures import find_signatures, supported_extensions


class TestImaging:
    def test_image_is_byte_identical(self, tmp_path):
        src = tmp_path / "src.img"
        F.build_fat32(src, 8 << 20)
        out = tmp_path / "backup.img"
        result = ImageCreator(str(src), str(out)).run()
        assert result.good_bytes == result.total_bytes
        assert result.bad_bytes == 0
        assert out.read_bytes() == src.read_bytes()
        assert (tmp_path / "backup.img.map").exists()

    def test_resume_completes_a_partial_image(self, tmp_path):
        src = tmp_path / "src.img"
        F.build_fat32(src, 16 << 20)
        out = tmp_path / "partial.img"
        block = 1 << 20
        image_map = ImageMap(str(src), str(out), block, src.stat().st_size)
        half = len(image_map.states) // 2
        for i in range(half):
            image_map.states[i] = ord("+")
        image_map.states[-1] = ord("-")  # pretend the tail failed
        with open(out, "wb") as fh:
            fh.truncate(src.stat().st_size)
        data = src.read_bytes()
        with open(out, "r+b") as fh:
            fh.write(data[: half * block])
        image_map.save()

        resumed = ImageCreator(str(src), str(out), block_size=block, resume=True).run()
        assert resumed.good_bytes == resumed.total_bytes
        assert resumed.bad_bytes == 0
        assert out.read_bytes() == data

    def test_map_survives_reload(self, tmp_path):
        src = tmp_path / "src.img"
        F.build_fat32(src, 4 << 20)
        out = tmp_path / "i.img"
        ImageCreator(str(src), str(out)).run()
        loaded = ImageMap.load(out.with_name(out.name + ".map"))
        assert loaded.total_bytes == src.stat().st_size
        assert loaded.percent() == pytest.approx(100.0)

    def test_zero_capacity_source_is_refused(self, tmp_path):
        from usbdoctor.core.errors import UsbDoctorError

        src = tmp_path / "zero.img"
        src.write_bytes(b"")
        with pytest.raises(UsbDoctorError):
            ImageCreator(str(src), str(tmp_path / "o.img")).run()


class TestCarving:
    def test_recovers_consecutive_files_of_known_types(self, tmp_path):
        raw = tmp_path / "raw.img"
        F._mk(raw, 4 << 20)
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + b"IEND\xaeB`\x82"
        pdf = b"%PDF-1.7\n" + b"x" * 300 + b"%%EOF"
        with open(raw, "r+b") as fh:
            fh.seek(512 * 1024)
            fh.write(jpeg + png + pdf)
        with DiskReader(str(raw)) as reader:
            result = Carver(reader, str(tmp_path / "out")).carve()
        assert len(result.files) == 3
        kinds = sorted({f.signature for f in result.files})
        assert kinds == ["JPEG image", "PDF document", "PNG image"]
        assert all(f.complete for f in result.files)
        assert result.by_category() == {"image": 2, "document": 1}
        # Content must round-trip exactly.
        from pathlib import Path

        payloads = {f.signature: Path(f.path).read_bytes() for f in result.files}
        assert payloads["JPEG image"] == jpeg
        assert payloads["PNG image"] == png
        assert payloads["PDF document"] == pdf

    def test_manifest_is_written(self, tmp_path):
        raw = tmp_path / "raw.img"
        F._mk(raw, 1 << 20)
        with open(raw, "r+b") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9")
        with DiskReader(str(raw)) as reader:
            Carver(reader, str(tmp_path / "out")).carve()
        manifest = (tmp_path / "out" / "recovery_manifest.json").read_text()
        assert "recovered_" in manifest
        assert '"file_count": 1' in manifest

    def test_category_filter_is_respected(self, tmp_path):
        raw = tmp_path / "raw.img"
        F._mk(raw, 1 << 20)
        with open(raw, "r+b") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")
            fh.write(b"%PDF-1.7\n" + b"x" * 300 + b"%%EOF")
        with DiskReader(str(raw)) as reader:
            result = Carver(reader, str(tmp_path / "out"), categories={"document"}).carve()
        assert [f.category for f in result.files] == ["document"]

    def test_signature_scanner_finds_known_headers(self):
        data = b"\x00" * 10 + b"\xff\xd8\xff" + b"\x00" * 10 + b"%PDF-1.4" + b"\x00" * 10
        found = find_signatures(data)
        keys = [sig.key for _, sig in found]
        assert "jpeg" in keys and "pdf" in keys

    def test_supported_extensions_are_reported(self):
        exts = supported_extensions()
        for required in (".jpg", ".png", ".pdf", ".zip", ".mp3"):
            assert required in exts


class TestFatFileRecovery:
    def test_recovers_named_file_with_exact_content(self, tmp_path):
        vol = tmp_path / "vol.img"
        geo = F.build_fat32(vol, 16 << 20)
        payload = b"QUARTERLY REPORT CONTENTS" * 10
        F.add_fat32_file(vol, "report.txt", payload, geo)
        with DiskReader(str(vol)) as reader:
            result = FatRecovery(reader, str(tmp_path / "out")).recover()
        assert result.files_recovered == 1
        assert result.entries[0].name == "REPORT.TXT"
        recovered = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
        assert len(recovered) == 1
        assert recovered[0].read_bytes() == payload

    def test_recovers_deleted_file_from_marked_entry(self, tmp_path):
        vol = tmp_path / "vol.img"
        geo = F.build_fat32(vol, 16 << 20)
        payload = b"TOP SECRET PAYLOAD" * 4
        F.add_fat32_file(vol, "secret.txt", payload, geo)
        data_start = (geo["reserved"] + geo["num_fats"] * geo["fat_sectors"]) * 512
        with open(vol, "r+b") as fh:  # 0xE5 is how FAT marks a deleted file
            fh.seek(data_start + 32)
            fh.write(b"\xe5")
        with DiskReader(str(vol)) as reader:
            result = FatRecovery(reader, str(tmp_path / "out")).recover()
        assert result.deleted_found == 1
        assert result.files_recovered == 1
        entry = result.entries[0]
        assert entry.deleted is True
        assert "_deleted" in entry.output_path
        with open(entry.output_path, "rb") as fh:
            assert fh.read() == payload

    def test_deleted_entries_skipped_when_disabled(self, tmp_path):
        vol = tmp_path / "vol.img"
        geo = F.build_fat32(vol, 16 << 20)
        F.add_fat32_file(vol, "gone.txt", b"data" * 100, geo)
        data_start = (geo["reserved"] + geo["num_fats"] * geo["fat_sectors"]) * 512
        with open(vol, "r+b") as fh:
            fh.seek(data_start + 32)
            fh.write(b"\xe5")
        with DiskReader(str(vol)) as reader:
            result = FatRecovery(reader, str(tmp_path / "out"), include_deleted=False).recover()
        assert result.deleted_found == 1
        assert result.files_recovered == 0

    def test_exfat_file_recovery(self, tmp_path):
        vol = tmp_path / "exfat.img"
        geo = F.build_exfat(vol, 32 << 20)
        payload = b"\xff\xd8\xffPHOTO-DATA" * 20
        F.add_exfat_file(vol, "photo.jpg", payload, geo)
        with DiskReader(str(vol)) as reader:
            result = FatRecovery(reader, str(tmp_path / "out")).recover()
        assert result.volume_type == "exfat"
        assert result.files_recovered == 1
        recovered = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
        assert recovered[0].read_bytes().startswith(b"\xff\xd8\xff")

    def test_output_is_never_written_to_the_source(self, tmp_path):
        """Recovered data must land in a different tree than the source."""
        vol = tmp_path / "vol.img"
        geo = F.build_fat32(vol, 8 << 20)
        F.add_fat32_file(vol, "a.txt", b"payload", geo)
        before = vol.read_bytes()
        with DiskReader(str(vol)) as reader:
            FatRecovery(reader, str(tmp_path / "out")).recover()
        assert vol.read_bytes() == before
