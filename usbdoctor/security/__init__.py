"""Safety-critical security primitives.

Three layers protect the user's data:

1. :class:`SafetyPolicy` decides *whether* an operation may target a device at
   all (system disks, internal disks, mounted volumes...).
2. :class:`ConsentBroker` issues short-lived, device-bound tokens; every
   modifying operation must present one, mirroring the exact text the user
   approved in the UI.
3. :class:`GuardedWriter` re-verifies the device identity immediately before
   each write and records the action in the audit log.
"""
