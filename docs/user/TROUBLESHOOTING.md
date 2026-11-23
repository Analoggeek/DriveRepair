# Troubleshooting

## The drive does not appear in the list

1. Press **Refresh** (or `Ctrl+Shift+R`). Enumeration is a snapshot, not a watch.
2. Try a different port, directly on the machine — hubs and front-panel ports are the most
   common cause.
3. Windows: Device Manager → *Disk drives*. If it is not there, the OS cannot see it and no
   software tool can either.
4. macOS: `diskutil list` in Terminal. If it is missing there, the hardware is not
   enumerating.
5. Linux: `lsblk` and `dmesg | tail -30` — kernel messages tell you whether the device
   attached, reset, or was rejected.
6. If it appears in Device Manager but not here, the app was not elevated. Restart it as
   administrator.

## "SMART unavailable"

Normal for most USB flash drives and bridges. USB mass storage does not standardise SMART, and
many controllers do not forward it at all. The app reports the reason it could not read SMART
rather than guessing.

To get SMART from a supported external drive:
- Install [smartmontools](https://www.smartmontools.org/) (official).
- Use a bridge that passes ATA pass-through (many SATA-to-USB enclosures do; most cheap flash
  bridges do not).
- On macOS, SMART over USB requires a compatible bridge; internal drives work with
  `smartctl -d sat`.

## "Permission denied" or reads fail immediately

- Windows: run as administrator. Raw `\\.\PhysicalDriveN` access requires elevation.
- macOS: allow the app to access removable volumes when prompted; check
  *System Settings → Privacy & Security → Files and Folders*.
- Linux: raw block devices are root-only by default (`/dev/sd*` is `brw-rw---- root disk`).
  Run with `sudo` or add yourself to the `disk` group — the latter gives permanent raw access,
  so choose deliberately.

## Everything reads as "unknown" file system

The volume may genuinely be RAW (see [RAW volumes](#the-drive-shows-as-raw--asks-to-format)),
or the file system may start at a partition offset rather than the start of the disk. Scan the
device again and look at the *Findings* — the analyser probes standard partition offsets and
will report what it found where.

## The drive shows as RAW / asks to format

**Do not format.** Formatting writes new metadata over the recoverable one.

1. Go to **Data Recovery** and create an image first.
2. Run a **Health Scan** in SMART REPAIR mode and look for a `*_BOOT_SECTOR_DESTROYED` code.
   FAT32 and exFAT both keep a complete backup copy of their boot region, so this is usually
   repairable without touching your files.
3. If no repair applies, recover files from the image: **Automatic** walks the file system if
   it can, otherwise carves by content.

## Repair was refused

Read the message — refusals are specific:

| Message | Cause | Fix |
| --- | --- | --- |
| *Repair cannot start without your explicit approval* | No consent token | Use **Approve & Repair**, not a direct execute |
| *consent token was already used* | Single use by design | Approve again |
| *device changed identity* | Media was swapped or re-plugged | Scan again before repairing |
| *Consent expired* | More than 15 minutes passed | Approve again |
| *The device is not writable* | Read-only switch, OS policy, or controller lock | See below |

## The drive is write-protected

1. SD cards and some adaptors have a physical switch — check it first.
2. Windows: `diskpart` → `list disk` → `select disk N` → `attributes disk` → if *Read-only:
   Yes*, `attributes disk clear readonly`.
3. If the controller locked itself after write failures, that lock is a symptom of hardware
   failure. **Do not force it** — copy your data off and replace the drive.

## Capacity test says the drive is counterfeit

Your drive reports more space than it has. Everything written past the real capacity has been
silently discarded or wrapped onto existing data.

1. Recover files **immediately** — the ones still within the real capacity may be intact.
2. Never store anything important on it again.
3. Some controllers can be re-capped to their true size with a controller-specific
   manufacturing tool. That erases the drive, requires the exact model and revision, and is not
   something this app does for you.

## Recovery found zero files

In order of likelihood:

1. **The data was overwritten.** Flash storage reuses blocks aggressively. If the drive kept
   being used after deletion, the data is gone.
2. **The file system could not be walked** and carving found no signatures — try
   **Content carving** explicitly, and image the drive first so the scan is not interrupted by
   read errors.
3. **You scanned the wrong offset.** Scan the whole device, not just an empty partition.

Zero is an honest result. The app will not pad the report with guesswork.

## Imaging is very slow or stalls

- Failing media: this is expected. The imager records bad regions and continues rather than
  retrying forever. Install `ddrescue` for finer-grained retry control.
- Use **Cancel**, then run it again — the map lets it resume where it stopped.
- Do not image over USB 1.1 or through a hub while doing anything else on that bus.

## The scan stops at some percentage and stays there

Some blocks take a long time to fail (the drive retries internally). The surface scan bisects
failed regions down to single sectors, which is slow by design — it is what lets the app say
*which* sectors are bad. Lower the **Surface** time budget if you only need a quick pass.

## The app says it is not elevated

Elevation affects more than convenience:

- SMART queries are refused.
- Raw device opens are refused on Windows and Linux.
- The capacity test cannot write its pattern file.

Restart with *Run as administrator* (Windows) or `sudo` (macOS/Linux). The app continues to
work read-only in the meantime and flags the limitation in every report.

## Dark text on a dark background, or clipped labels

Both were fixed in 1.0.0 (theme link colours and layout size policies). If you see either,
you are running an older build — rebuild or reinstall.

## Antivirus or SmartScreen blocks the installer

Unsigned builds trigger this. Either build it yourself from source (see
`docs/developer/BUILD.md`) or use a signed release. Never disable your antivirus to run an
installer from an untrusted source.

## Where are my files and settings?

| Platform | Location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\USBDoctorAI\` |
| macOS | `~/Library/Application Support/USBDoctorAI/` |
| Linux | `~/.local/share/USBDoctorAI/` |

It contains `history.db` (scans/repairs/settings), `audit.jsonl` (every state-changing
operation with its outcome) and `recovered/` (default recovery output).

Set `USBDOCTOR_STATE_DIR` to override it, or pass `--state-dir` on the command line.

## Collecting diagnostics for a bug report

```bash
usbdoctor status --json > status.json
usbdoctor scan /dev/sdX --json > scan.json     # read-only
```

Attach both, plus the platform, the exact drive model and what you expected. Do not attach
recovered personal files.
