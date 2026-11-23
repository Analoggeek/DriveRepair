# The safety contract

This document states what the software guarantees, where each guarantee lives in the code, and
how it is tested. If a future change breaks one of these, a test should fail.

---

## 1. Diagnosis never writes

`DoctorService._run_scan` uses `DiskReader` exclusively. `DiskReader` has no write method at
all — write paths live in separate classes (`MetadataWriter`, `ImageCreator`, `Carver`) that a
scan never constructs.

**Tested by:** `tests/integration/test_service.py::TestServiceRecovery::test_recover_writes_files_outside_the_source_and_logs_it`
compares the source image before and after; `tests/integration/test_recovery.py` asserts the
source volume is unmodified after recovery.

---

## 2. Nothing is written without a consent token

`RepairExecutor.execute` computes which plans require consent. If any do and no token is
supplied, it raises `UnsafeOperationError` before touching the device:

```python
needs_consent = [p for p in plans if p.requires_consent and p.risk.value != "none"]
if needs_consent and not dry_run:
    if not consent_token:
        raise UnsafeOperationError("Repair cannot start without your explicit approval.")
```

`dry_run=True` is the only path that proceeds without a token, and it writes nothing.

**Tested by:** `test_service.py::TestServiceRepair::test_full_damage_scan_approve_repair_verify_cycle`
asserts the refusal; `test_repair_dry_run_needs_no_approval_and_writes_nothing` asserts the
dry-run leaves the file byte-identical.

---

## 3. A token is single-use and bound to the device *and* its identity

`ConsentBroker.grant()` stores `(path, DeviceSignature, expiry, used=False)`.

`ConsentBroker.consume()` checks, in order: existence, `used`, expiry, path mismatch, then
`signature.matches()`. Any failure raises and the token is not consumed twice.

`DeviceSignature` is derived from the device path plus size/serial/partition layout, so
swapping in a different drive of the same model invalidates it.

**Tested by:** `test_service.py::TestServiceRepair::test_consent_token_is_single_use` and the
identity tests in `tests/unit/test_security.py`.

---

## 4. Protected targets are refused by policy

`SecurityPolicy.check_writable(device, purpose)` refuses:

- the running system disk (`DeviceRef.is_system_disk`),
- internal disks unless explicitly allowed in Settings,
- mounted volumes unless explicitly allowed,
- known protected paths.

The UI calls the *same* method to decide whether to enable a button, so the button state and
the enforcement can never disagree.

**Tested by:** `tests/unit/test_security.py`.

---

## 5. Every change is verified by re-reading

Each `RepairOperation` implements `verify(ctx)`. The executor calls it after `apply()` and
records `verified` in the audit log, the history DB and the report. `verified=False` is shown
to the user as *NOT verified* — it is never smoothed over.

**Tested by:** every repair integration test asserts `result.verified is True` after a
successful repair.

---

## 6. A rollback snapshot precedes every write

Metadata writers capture the original bytes of the region they are about to change and store
them in a rollback file whose path is recorded in the history DB
(`repairs.rollback_file`). The report tells the user where it is.

---

## 7. Recovery never touches the source

- `DoctorService.recover` opens the source through `DiskReader` (read-only) and refuses an
  output directory that resolves to the source, its parent, or its child.
- Recovered files are created with mode `0600`.
- File names come from directory entries through `safe_filename`, which strips path
  separators, control characters and reserved names.

**Tested by:** `test_service.py::TestServiceRecovery::test_recovery_refuses_to_write_next_to_the_source_image`.

---

## 8. Hardware damage is reported, never "repaired"

`evaluate_health` sets `permanent_failure_suspected` / `confirmed_failure` only from:

- SMART overall health failure,
- growing reallocated/pending sector attributes,
- confirmed media read errors (re-read on a second pass),
- a capacity shortfall proven by the write-verify test.

When that flag is set, `DiagnosticAssistant` sets `can_repair_in_software=False`,
`replacement_advised=True`, and emits the friendly message. It never proposes a repair for
those conditions, and the knowledge base marks the corresponding entries
`software_fixable=False`.

**Tested by:** `test_service.py::TestDiagnosticAssistant::test_hardware_failure_produces_the_friendly_message`
and `test_knowledge.py`'s assertion that hardware entries never claim software can fix them.

---

## 9. Network access is allow-listed and provenance is preserved

`knowledge/online.py` refuses any URL whose scheme is not `https` or whose host is not in
`ALLOWED_HOSTS`. Redirects are re-checked. Every fetched item carries its URL, and the report
prints it. Offline mode returns `status="offline"` with an empty snippet — it never substitutes
invented content.

**Tested by:** `test_service.py::TestOnlineResearch` (HTTP refused, unknown host refused,
offline mode returns empty, not invented).

---

## 10. The assistant cannot act

`DiagnosticAssistant` has no reference to `RepairExecutor`, `ConsentBroker` or
`DoctorService`. Its `recommended_plans` is a filtered, re-ordered subset of the plans it was
handed. A test asserts that with no plans supplied it recommends nothing.

**Tested by:** `test_service.py::TestDiagnosticAssistant::test_assistant_cannot_invent_plans`.

---

## What the software deliberately does not offer

These are absent, not merely disabled — there is no code path to enable them:

| Capability | Why it is absent |
| --- | --- |
| Firmware flashing | Wrong-revision flashes brick drives; there is no safe generic path |
| Controller manufacturing tools | They erase everything and are revision-specific |
| Automatic formatting | Destroys the metadata that makes recovery possible |
| Forcing writes on a self-locked controller | Accelerates data loss on failing media |
| Bundled third-party binaries | Supply-chain risk; tools are detected, not shipped |

When a user asks for one of these, the app explains the limitation and what to do instead.
