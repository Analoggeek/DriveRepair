"""Windows enumeration must cope with the values Windows actually returns.

`Get-Disk` reports CIM enums as integers on some builds and as names on others
(``BusType: "NVMe"``). The provider passed them straight to ``int()``, so on a
modern Windows machine enumeration raised
``invalid literal for int() with base 10: 'NVMe'`` and the whole device list -
and every command that needs it - failed.
"""

from __future__ import annotations

import pytest

windows = pytest.importorskip("usbdoctor.devices.windows", reason="importable everywhere")
from usbdoctor.devices.windows import BUS_TYPE, MEDIA_TYPE, WindowsDeviceProvider, _enum_code


class TestEnumDecoding:
    @pytest.mark.parametrize("value", [17, "17", 17.0])
    def test_numbers_are_numbers(self, value):
        assert _enum_code(value, BUS_TYPE) == 17

    @pytest.mark.parametrize("value", ["NVMe", "nvme", "Nvme"])
    def test_names_decode_to_the_same_code(self, value):
        assert _enum_code(value, BUS_TYPE) == 17
        assert BUS_TYPE[_enum_code(value, BUS_TYPE)] == "nvme"

    @pytest.mark.parametrize("value", [None, "", "  ", True, False])
    def test_junk_is_unknown_not_an_exception(self, value):
        assert _enum_code(value, BUS_TYPE) == 0

    def test_an_unrecognised_name_does_not_raise(self):
        assert _enum_code("SomeFutureBus", BUS_TYPE) == 0

    def test_media_type_names_work_too(self):
        assert MEDIA_TYPE.get(_enum_code("SSD", MEDIA_TYPE)) == "SSD"
        assert MEDIA_TYPE.get(_enum_code("HDD", MEDIA_TYPE)) == "HDD"
        assert MEDIA_TYPE.get(_enum_code(4, MEDIA_TYPE)) == "SSD"


class TestDiskConversion:
    """A disk record shaped like the one that crashed, end to end."""

    @staticmethod
    def _disk(**overrides):
        disk = {
            "Number": 1,
            "BusType": "NVMe",
            "MediaType": "SSD",
            "FriendlyName": "Samsung SSD 990 PRO 1TB",
            "SerialNumber": "S6Z1NJ0T",
            "FirmwareVersion": "4B2QJXD7",
            "Size": "1000204886016",
            "IsReadOnly": False,
            "IsBoot": False,
            "IsSystem": False,
            "HealthStatus": "Healthy",
            "PartitionStyle": "GPT",
            "OperationalStatus": "OK",
            "UniqueId": "abc",
            "IsOffline": False,
        }
        disk.update(overrides)
        return disk

    def test_a_disk_with_named_enums_converts(self):
        provider = WindowsDeviceProvider()
        device = provider._from_disk(self._disk(), {}, [])
        assert device is not None
        assert device.bus == "nvme"
        assert device.native["msft_disk"]["media_type"] == "SSD"
        assert device.size_bytes == 1000204886016
        assert device.path == "\\\\.\\PhysicalDrive1"

    def test_numeric_enums_still_work(self):
        provider = WindowsDeviceProvider()
        device = provider._from_disk(self._disk(BusType=7, MediaType=4), {}, [])
        assert device is not None
        assert device.bus == "usb"
        assert device.removable is True

    def test_an_unknown_bus_does_not_raise(self):
        provider = WindowsDeviceProvider()
        device = provider._from_disk(self._disk(BusType="SomethingNew"), {}, [])
        assert device is not None
        assert device.bus is None

    def test_a_disk_without_a_number_is_skipped(self):
        provider = WindowsDeviceProvider()
        assert provider._from_disk(self._disk(Number=None), {}, []) is None
