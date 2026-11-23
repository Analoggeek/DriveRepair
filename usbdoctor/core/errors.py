"""Exception hierarchy used across USB Doctor AI.

Every failure mode that the UI must be able to explain to a non-expert user is
represented by a dedicated class carrying a ``user_message`` (plain language,
English) and an optional ``hint`` describing what the user can safely do next.
"""

from __future__ import annotations

from typing import Any, Optional


class UsbDoctorError(Exception):
    """Base class for all engine errors."""

    code = "USBDOCTOR_ERROR"

    def __init__(self, message: str, *, hint: Optional[str] = None, **context: Any) -> None:
        super().__init__(message)
        self.user_message = message
        self.hint = hint
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.user_message,
            "hint": self.hint,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.user_message


class PlatformUnsupportedError(UsbDoctorError):
    """The current operating system cannot perform this operation."""

    code = "PLATFORM_UNSUPPORTED"


class PrivilegeError(UsbDoctorError):
    """Administrator / root privileges are required and were not granted."""

    code = "PRIVILEGE_REQUIRED"


class DeviceNotFoundError(UsbDoctorError):
    """A requested device is no longer connected or never existed."""

    code = "DEVICE_NOT_FOUND"


class DeviceIdentityChangedError(UsbDoctorError):
    """The device at a path changed identity between two checks (replug/swapped).

    Raised by the write-guard immediately before any modifying operation when
    the on-disk signature no longer matches the signature captured at scan time.
    """

    code = "DEVICE_IDENTITY_CHANGED"


class UnsafeOperationError(UsbDoctorError):
    """A requested operation violates the safety policy (e.g. writing to the
    system disk, formatting without consent, flashing firmware on mismatch)."""

    code = "UNSAFE_OPERATION"


class ToolUnavailableError(UsbDoctorError):
    """An external helper tool (smartctl, ddrescue, ...) is not installed."""

    code = "TOOL_UNAVAILABLE"


class ToolExecutionError(UsbDoctorError):
    """An external helper tool ran but failed."""

    code = "TOOL_FAILED"

    def __init__(
        self,
        message: str,
        *,
        hint: Optional[str] = None,
        command: Optional[list[str]] = None,
        returncode: Optional[int] = None,
        stderr: str = "",
        **context: Any,
    ) -> None:
        super().__init__(message, hint=hint, **context)
        self.command = command
        self.returncode = returncode
        self.stderr = stderr

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["returncode"] = self.returncode
        data["command"] = self.command
        data["stderr"] = self.stderr[-4000:]
        return data


class ImageFormatError(UsbDoctorError):
    """A disk image / metadata structure could not be parsed."""

    code = "IMAGE_PARSE_ERROR"


class KnowledgeSourceError(UsbDoctorError):
    """An online knowledge lookup failed (network, TLS, or trust failure)."""

    code = "KNOWLEDGE_SOURCE_ERROR"


class IntegrityError(UsbDoctorError):
    """Downloaded data did not match its published hash / signature."""

    code = "INTEGRITY_CHECK_FAILED"


class BusyDeviceError(UsbDoctorError):
    """The device is mounted / in use and cannot be safely operated on."""

    code = "DEVICE_BUSY"
