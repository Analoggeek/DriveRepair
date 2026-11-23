# Architecture

## Design principles

1. **No simulated data.** Every value in the UI comes from a real system call, a real device
   read, or a computation over those. When something is unavailable, that fact is propagated
   as data (`None`, an empty list, an explicit reason string) — not filled in.
2. **One safety core.** The GUI, CLI and HTTP API all call `DoctorService`. There is exactly
   one place where a repair can be planned and exactly one where it can execute.
3. **Read before write, always.** Diagnosis never writes. Repairs require a consent token
   bound to the device path *and* its identity signature. The executor re-verifies that
   signature immediately before the first byte is written.
4. **Verify everything you change.** Every repair re-reads the affected region and compares
   against what it intended to write. `verified=False` is a real, reportable outcome.
5. **Prefer the standard library.** The engine, CLI and web API run with zero third-party
   packages. PySide6 is needed only for the desktop window.

---

## Layer map

```
┌──────────────────────────────────────────────────────────────┐
│  Frontends                                                   │
│  app/ (PySide6)   cli.py   frontend/web + webapi/ (WSGI+SSE) │
└───────────────────────────┬──────────────────────────────────┘
                            │  one API, three presentations
┌───────────────────────────▼──────────────────────────────────┐
│  service/engine.py — DoctorService                           │
│  scan · plan · approve · execute · image · recover ·         │
│  capacity · research · history · settings · jobs/events      │
└───────────────────────────┬──────────────────────────────────┘
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ analysis/     │   │ repair/       │   │ recovery/     │
│ tables, fs,   │   │ registry,     │   │ imaging,      │
│ smart, probe, │   │ planner,      │   │ carving,      │
│ health        │   │ executor,     │   │ fat_recovery  │
│               │   │ rollback      │   │               │
└───────┬───────┘   └───────┬───────┘   └───────┬───────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  security/   policy · consent · writer_guard                 │
│  devices/    windows · linux · macos · base                  │
│  core/       model · io · platform · errors · events · log   │
└──────────────────────────────────────────────────────────────┘
```

Supporting: `knowledge/` (curated DB + allow-listed research), `ai/` (deterministic
diagnosis), `db/` (SQLite history), `performance/` (benchmarks, capacity), `report.py`.

---

## Data flow of a scan

```
DoctorService.scan_async(path, options)
  └─ start_job()                     ─► EventBus: job_started
     └─ thread: _run_scan()
        ├─ compute_signature()        identity snapshot for safety binding
        ├─ DiskReader + parse_mbr/gpt ─► partition_issues
        ├─ analyze_volume()           ─► filesystem_issues, detected fs
        ├─ read_smart()               ─► SmartResult (may be unavailable)
        ├─ scan_surface()             ─► read_errors, bytes scanned  ─► progress events
        ├─ check_reported_capacity()  ─► tail reachable?
        ├─ benchmark_read()           ─► throughput samples
        ├─ collect_evidence()/plan_repairs() ─► RepairPlan[]
        ├─ evaluate_health()          ─► HealthReport (verdict + findings)
        ├─ DiagnosticAssistant.diagnose() ─► Diagnosis
        └─ HistoryStore.start/finish_scan() ─► ScanResult ─► job.result
                                            ─► EventBus: job_finished
```

The result object is stored on the job, so a late subscriber can fetch it via
`GET /api/jobs/detail` even if it missed the event.

## Data flow of a repair

```
plan(device, mode)      → RepairPlan[] (each with risk, consent requirement, rationale)
approve(device, ops)    → ConsentToken(path, signature, expiry, single-use) + audit entry
execute(device, token)
  └─ RepairExecutor
     ├─ re-verify identity signature      ← refuses if media swapped
     ├─ broker.consume(token)             ← single-use, path+signature bound
     ├─ for each plan (ordered by risk):
     │    ├─ runner.apply(ctx)            ← ctx carries policy + audit + emitter
     │    ├─ rollback snapshot written first
     │    ├─ runner.verify(ctx)           ← re-reads the device
     │    └─ audit.record("repair:<id>:finish", verified=…)
     └─ ExecutionReport(results)
```

A `MEDIUM`-or-higher plan is never auto-run in `safe` or `smart` mode, and no plan runs
without a token in either of those modes.

## Safety invariants (enforced in code, not convention)

| Invariant | Where |
| --- | --- |
| No write without consent | `RepairExecutor.execute` raises `UnsafeOperationError` |
| Token is single-use | `ConsentBroker.consume` sets `used` |
| Token is device + identity bound | `ConsentBroker.consume` compares path and `DeviceSignature` |
| Refuses system disk | `SafetyPolicy.check_writable` |
| Refuses mounted-volume writes by default | same |
| Never writes recovered files to the source | `DoctorService.recover` compares resolved paths |
| Imaging/recovery open the source read-only | `DiskReader` has no write path; repair writers are separate classes |
| Only allow-listed HTTPS hosts | `knowledge/online.is_allowed` |
| Assistant cannot create operations | `DiagnosticAssistant` has no planner or executor reference |

These are covered by tests in `tests/integration/test_repair_engine.py`,
`tests/integration/test_service.py` and `tests/unit/test_security.py`.

## Platform abstraction

`devices/` returns `DeviceRef` records with a normalised shape regardless of source:

| Platform | Enumeration | Raw access |
| --- | --- | --- |
| Windows | PowerShell `Get-Disk`/`Get-Partition` + CIM for USB descriptors | `\\.\PhysicalDriveN` opened with `FILE_FLAG_NO_BUFFERING` |
| Linux | `lsblk --json`, sysfs, udev | `/dev/sdX` with `O_DIRECT` where supported |
| macOS | `diskutil list -plist`, `system_profiler`, IOKit registry | `/dev/rdiskN` (raw character device) |

Each module populates `DeviceRef.limitations` when it cannot answer something, and the UI
shows those notes verbatim.

## Testing strategy

- `tests/fixtures/images.py` builds real FAT32, exFAT, NTFS and ext images at byte level.
- Damage is applied as real byte mutations (`corrupt_mbr_signature`,
  `wipe_fat32_primary_boot_keep_backup`, `desync_fat_copies`, …).
- Integration tests assert on the *result*: the repaired bytes, the recovered file content,
  the re-scan verdict.
- Security tests assert that refusals actually raise, not merely that a flag is set.
- CLI tests run `main()` and check exit codes, including the safety gate (`EXIT_UNSAFE`).

## Extending it

**A new repair operation:** subclass `RepairOperation` in `repair/operations/`, register it in
`repair/registry.py`, and add a planner rule in `repair/planner.py` keyed on an evidence code.
The operation must implement `apply()` and `verify()`; the executor handles consent, rollback
and audit for you.

**A new file-system validator:** add a module under `analysis/filesystems/`, return issues
with stable `code` values, and wire it into `detect.analyze_volume`. Add a knowledge-base entry
for each new code so the assistant can explain it.

**A new knowledge entry:** append to `knowledge/database.py` — `error_codes`, `steps`,
`verification`, `sources` (HTTPS only) and `software_fixable` are required by the tests.
