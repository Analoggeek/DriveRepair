# USB Doctor AI — User Manual

Version 1.0.0

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Installing](#2-installing)
3. [The interface](#3-the-interface)
4. [Dashboard](#4-dashboard)
5. [Devices](#5-devices)
6. [Health Scan](#6-health-scan)
7. [Repair Center](#7-repair-center)
8. [Data Recovery](#8-data-recovery)
9. [Performance](#9-performance)
10. [AI Assistant](#10-ai-assistant)
11. [History](#11-history)
12. [Reports](#12-reports)
13. [Settings](#13-settings)
14. [What the verdicts mean](#14-what-the-verdicts-mean)
15. [Honest limits](#15-honest-limits)

---

## 1. Before you start

**Back up anything irreplaceable first.** USB Doctor AI is careful — it refuses to write
without your approval and keeps a rollback copy of every change — but a failing drive can
die at any time, including during a scan.

**Run it elevated.** Windows: right-click → *Run as administrator*. macOS/Linux: `sudo` or
the equivalent. Without elevation the operating system refuses raw device reads, and the app
will tell you so rather than showing partial results as if they were complete.

**One drive at a time.** Unplug other removable drives while repairing, so a wrong selection
cannot affect the wrong device.

---

## 2. Installing

### Windows 10 / 11

1. Run `USBDoctorAI-Setup-1.0.0.exe`.
2. Windows SmartScreen may warn about an unsigned build. If you built it yourself, choose
   *More info → Run anyway*. A signed release shows the publisher name.
3. The installer asks for administrator rights because raw device access needs them.
4. Optional: install [smartmontools](https://www.smartmontools.org/) for SMART attributes on
   USB bridges that pass them through. The app works without it and tells you when SMART is
   unavailable rather than inventing values.

### macOS 11+

1. Open the DMG and drag **USB Doctor AI** to *Applications*.
2. First launch: right-click the app → *Open* (Gatekeeper confirmation for unsigned builds).
3. Allow access to removable volumes when macOS asks.
4. Optional: `brew install smartmontools ddrescue` for SMART and the faster imaging engine.

---

## 3. The interface

The window has a sidebar with ten pages and a top bar with the current device context.

| Page | Purpose |
| --- | --- |
| **Dashboard** | At-a-glance state, the newest scan, and what the app can/cannot do |
| **Devices** | Everything the OS reports, with what could be verified |
| **Health Scan** | Run the diagnosis; watch the ten-step workflow |
| **Repair Center** | Review applicable repairs, their risk, and run them |
| **Data Recovery** | Image a drive, then recover files |
| **Performance** | Measured throughput and counterfeit-capacity testing |
| **AI Assistant** | Plain-language explanation of the evidence |
| **History** | Every scan, repair and recovery stored locally |
| **Reports** | Export Markdown / HTML / JSON |
| **Settings** | Safety policy, tools, privacy |

**Keyboard:** `Ctrl+1`…`Ctrl+0` jump to pages, `Ctrl+T` toggles dark/light,
`Ctrl+Shift+R` rescans devices.

The **Scan & Repair Automatically** button in the top bar always runs a read-only scan first,
then offers only low-risk, evidence-backed repairs — which you still confirm.

---

## 4. Dashboard

- Three counters: connected devices, scans recorded, repairs completed (from local history).
- **Selected drive** — identity, capacity, file system, VID/PID, serial, firmware. Values the
  drive did not report are shown as *not reported*, never guessed.
- **What this app can and cannot do** — the honest capability list, always visible.
- **Connected devices** — click any card to select it, *Scan* to start immediately.
- **Latest scan** — verdict and headline for the selected drive.

---

## 5. Devices

Lists every storage device the OS exposes. Columns:

| Column | Meaning |
| --- | --- |
| **Drive** | Vendor + model, and the OS path used for all raw access |
| **Capacity** | Size as reported by the OS |
| **Interface** | Bus and the negotiated link speed (e.g. *USB 2.0 High Speed (480 Mbps)*) |
| **File system** | What the OS mounted, or `unknown` |
| **Health** | A hint only where the OS exposes one; otherwise *not reported* |
| **Write** | *writable* or *protected* — this is what the safety policy will allow |

Internal disks are hidden until you tick **show internal disks**, and the running system disk
is always refused for any write.

Selecting a row shows the raw identity including partition offsets and any enumeration
limitations. Notes under the table explain anything the platform could not report.

---

## 6. Health Scan

### Setup

- **Device** — the drive to scan.
- **Mode** — SAFE, SMART REPAIR or ADVANCED (see [section 14](#14-what-the-verdicts-mean)).
- **Surface** — how long the read-only surface scan may run: 1, 5, 15 minutes, or *Full scan*
  (may take hours on a large or slow drive).
- Checkboxes for SMART, benchmarking and capacity verification.

### What happens

The scan is strictly read-only. Progress is real: each stage below emits an event when it
actually starts, and the progress bar reflects bytes read, not a timer.

1. **Identity** — model, serial, firmware, VID/PID, negotiated speed.
2. **Partition table** — MBR/GPT parse, CRC checks, backup-header presence.
3. **File system** — FAT12/16/32, exFAT, NTFS and ext structure validation at the correct
   offset, including the backup boot regions.
4. **SMART** — `smartctl` attributes and health, when the bridge passes them through.
5. **Surface** — every block read; unreadable regions are bisected to the sector and recorded.
6. **Capacity** — whether the highest address is actually reachable.
7. **Benchmark** — eight samples spread across the address space.
8. **Analysis** — evidence codes → health verdict → applicable repairs → diagnosis.

### Reading the result

- **Verdict badge** — EXCELLENT, GOOD, WARNING, CRITICAL, FAILING or UNKNOWN.
- **Findings** — each with a severity, a plain-language explanation and an evidence code.
- **Confidence** — *low* means little hardware evidence was available; *high* means SMART or
  multiple independent checks agreed.
- **Limitations** — everything the scan could not do, stated explicitly. This list is not
  decoration: if SMART is missing, you will see why.

Cancel stops the scan after the current block; partial results are labelled as partial.

---

## 7. Repair Center

1. Choose the device and mode, press **Load plan**.
2. Each operation shows its title, risk level, what it does and why it applies.
3. **Dry run** simulates the write and reports exactly what would change — nothing is written.
4. **Approve & Repair** shows a confirmation listing every operation. After you accept, a
   single-use consent token is minted for this device *and its current identity*.
5. During the run a rollback copy is written first, the operation is applied, and the result
   is **verified by re-reading the device**.
6. The result table shows status and verification for each step.

If the media is swapped between approval and execution, the run is refused: the identity
signature no longer matches.

The bottom of the page shows the permanent-failure message, which appears only when SMART,
media errors or capacity evidence genuinely confirm hardware failure.

---

## 8. Data Recovery

Recovery always works on a copy where possible.

### Step 1 — Create an image

Enter a destination (put it on a **different** drive). The imager copies every readable
sector, records unreadable regions instead of aborting, and writes a sidecar map. If a run is
interrupted, running it again resumes from the map — already-good regions are skipped.

GNU `ddrescue` is used when installed (it is more patient with failing media); otherwise the
built-in imager is used. The report tells you which engine ran.

### Step 2 — Recover files

| Method | Use it when |
| --- | --- |
| **Automatic** | Default. Walks the file system, then carves if metadata is gone |
| **File-system walk** | The volume still mounts or its metadata is intact — keeps original names |
| **Content carving** | Metadata destroyed — finds files by their internal signatures |

Options: **include deleted entries** recovers files whose directory entries were marked
deleted but whose data has not been overwritten.

Results show the count, total bytes recovered, method used and output folder. Zero files is a
real answer, not a failure — the usual cause is that the data was overwritten, and the app
says so.

Recovered files are written with restrictive permissions (`0600`). File names are sanitised,
never taken raw from a corrupt directory entry.

---

## 9. Performance

### Read performance

Median, peak and slowest sample of a read benchmark spread across the address space. The
rating compares the measurement with what the negotiated interface should deliver, so a USB 2
drive is never judged against USB 3 expectations.

### Capacity verification

This is the only reliable counterfeit test.

1. Tick the consent box.
2. Confirm the dialog.
3. The app writes a pattern file across the claimed size, reads every block back, compares it
   byte for byte, and deletes the file.

A genuine drive verifies. A counterfeit drive fails at the point where writes wrap around to
the start of the real memory — the app reports that offset as the real capacity and warns that
data past it is already lost.

Do not unplug the drive, and do not run this on a drive holding data you have not backed up.

---

## 10. AI Assistant

The assistant explains the evidence from your last scan:

- what each finding means in ordinary language,
- whether the problem is a *file-system* issue (repairable) or a *hardware* issue (not),
- the safest order to proceed,
- related known issues with their sources.

It has no ability to invent a repair, add an operation, or execute anything. It can only
describe and order what the safety planner already allowed.

**Research this model** queries only allow-listed HTTPS vendor and standards pages, and shows
every URL so you can read the original guidance yourself. With *offline mode* enabled it
reports that network research is disabled instead of substituting invented text.

---

## 11. History

Every scan and repair is stored locally in SQLite:

- **Scans** — when, which device, verdict, confidence, mode.
- **Repairs** — operation, risk, status, whether verification passed, and the rollback file.
- **Recoveries** — method, file count, bytes.

Serial numbers are stored as one-way hashes. **Forget the selected device** deletes that
device's records; *Settings → Delete all local history* clears everything. Report files and
recovered data on disk are untouched by either.

---

## 12. Reports

**Save report files** writes three files sharing one base name:

- `.md` — readable Markdown for tickets or wikis,
- `.html` — a self-contained page (no external assets) you can email or archive,
- `.json` — the complete structured record for tooling.

Fields the hardware did not provide are printed as *not reported*. That is deliberate: an
honest gap is more useful than a plausible-looking number.

---

## 13. Settings

| Setting | Effect |
| --- | --- |
| **Allow internal disks** | Internal drives become selectable; the running system disk stays refused |
| **Allow repairs on mounted volumes** | Off by default — the OS may cache stale metadata |
| **Offline mode** | No network requests at all; research reports offline instead of inventing |
| **Remind to image before medium-risk repairs** | Recommended, on by default |
| **Delete all local history** | Removes every stored scan/repair/recovery record |

The tool list shows what was detected and what each tool adds. Missing tools only remove
optional capabilities; the built-in imager, FAT/exFAT recovery, carving and all metadata
repairs work without any of them.

---

## 14. What the verdicts mean

| Verdict | Meaning |
| --- | --- |
| **EXCELLENT** | All checks passed and the device reports good health |
| **GOOD** | No errors found; hardware health data was limited but nothing indicated a problem |
| **WARNING** | Problems found that are usually repairable — back up, then repair |
| **CRITICAL** | Serious or multiple problems; copy data now, repairs may still help |
| **FAILING** | Hardware failure evidence: SMART failure, confirmed media errors or capacity mismatch |
| **UNKNOWN** | Not enough information — the device exposed nothing to judge |

### Modes

| Mode | Includes |
| --- | --- |
| **SAFE** | Read-only checks only. Nothing can be written. |
| **SMART REPAIR** | + low-risk repairs that rebuild from copies already on the disk |
| **ADVANCED** | + medium/high-risk operations, each individually confirmed |

### Risk levels

| Risk | Typical operation | Reversible? |
| --- | --- | --- |
| **None** | Imaging, verification | Nothing changes |
| **Low** | Restore a boot sector from its own backup copy | Yes — rollback file written |
| **Medium** | Rebuild a partition table entry | Yes, but verify before use |
| **High / Critical** | Reformat, wipe signatures | Destructive; always manual |

---

## 15. Honest limits

- **Physically damaged memory cannot be repaired.** Worn flash cells, bad NAND blocks and
  damaged platters are permanent. The app can copy what is still readable.
- **A failed controller cannot be revived by software.** Manufacturing tools exist for some
  controllers, but they erase the drive and must match the exact revision. The app does not
  run them, and warns when it is the only remaining option.
- **Counterfeit capacity cannot be fixed.** Data written past the real end is already gone.
- **USB bridges often hide SMART.** That is a limitation of the bridge, not a failure of the
  scan; the app says so instead of showing a fake health score.
- **Flash wear is not reversible.** If the reallocated-sector count keeps growing between
  scans, replace the drive.

When the app cannot help, it says so — including in Hindi/Urdu with the friendly
end-of-duty message, because that is kinder than a fake "repair" button.
