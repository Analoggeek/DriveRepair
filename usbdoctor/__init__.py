"""USB Doctor AI - Intelligent Storage Diagnosis, Repair & Recovery Suite.

A cross-platform (Windows / macOS / Linux) storage maintenance engine built on
real operating-system hardware APIs.  No simulated results anywhere: every
value surfaced by this package is either read from the kernel/OS, parsed from
on-disk metadata, or explicitly reported as unavailable.

Public entry points are documented in ``docs/ARCHITECTURE.md``.
"""

from usbdoctor.version import (
    APP_NAME,
    APP_TAGLINE,
    __version__,
    BUILD_CHANNEL,
)

__all__ = ["APP_NAME", "APP_TAGLINE", "__version__", "BUILD_CHANNEL"]
