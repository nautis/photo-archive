# photo-archive

Consolidate photos and videos from local drives, external drives, Google Photos Takeout, and Apple Photos exports to a single date-organized cloud archive — without uploading duplicates.

A single Python script that walks your media, reads true capture dates, MD5-dedupes against a persistent manifest, and uploads in parallel via [rclone](https://rclone.org) to any remote it supports (Backblaze B2, S3, Google Drive, Dropbox, etc.). By default, source files are never modified or deleted — opt into move semantics with `--move`.

## Why this exists

If you have photos scattered across:

- multiple external drives accumulated over the years (some still formatted for Windows),
- a Google Photos Takeout export,
- an Apple Photos library (extracted with [`osxphotos`](https://github.com/RhetTbull/osxphotos)),
- iCloud / Photos.app exports,
- ad-hoc folders from old Macs/PCs,

…and you want them all in one cloud bucket, sorted into `Photos/2017/May/` and friends, with no duplicates and no destroyed dates — that's what this does.

It is particularly careful about three scenarios that defeat naive scripts:

1. **Google Takeout** strips or modifies in-file EXIF for many photos but provides a JSON sidecar containing the real `photoTakenTime`. This tool reads those sidecars, including their unpredictably-truncated filenames (`IMG_1234.jpg.supplemen.json`, `.suppl.json`, etc.) — Google truncates the suffix to fit the 255-byte filename limit.
2. **Apple Photos** doesn't store original capture dates the same way for every photo. The companion tool [`osxphotos`](https://github.com/RhetTbull/osxphotos) can export originals plus exiftool-format JSON sidecars (`--sidecar json`) that contain canonical dates from the Photos SQLite database. This tool reads those sidecars too, in the same priority slot as Takeout sidecars.
3. **Files copied between drives** often have their mtime overwritten by the copy operation, making mtime an unreliable date source. This tool uses mtime only as the *last* fallback after every EXIF tag has been tried, and labels mtime-sourced placements differently in the manifest so you can audit them later.

## What it does

- Walks a source directory recursively, skipping junk (`.DS_Store`, `@eaDir`, `Thumbs.db`, dotfiles, AppleDouble files, plus Windows-format-drive cruft like `$RECYCLE.BIN` and `System Volume Information`).
- Groups sidecar files by directory + filename stem so Live Photos (`IMG_1234.HEIC` + `IMG_1234.MOV`), RAW+JPEG pairs, and `.AAE` edits stay together with the primary file's date.
- For each file, resolves a capture date using this priority chain:
  1. **Sidecar JSON** — Google Takeout `photoTakenTime.timestamp` OR osxphotos exiftool-format `EXIF:DateTimeOriginal` (same lookup pattern, both schemas handled)
  2. EXIF `DateTimeOriginal`
  3. EXIF `CreateDate`
  4. EXIF `MediaCreateDate` (videos)
  5. EXIF `TrackCreateDate` (videos)
  6. `FileModifyDate` (filesystem mtime — fallback only, status is `copied_mtime`)
  7. Otherwise → `Unknown/`
- Computes MD5 of each file (streamed) and dedupes against a persistent SQLite manifest. A file already on the remote — even renamed, even from a different source drive — is detected and skipped.
- Uploads via `rclone copyto … --checksum --no-traverse` so each upload is checksum-verified and doesn't require listing the destination first.
- Sorts photos and videos into separate top-level buckets:
  ```
  remote:bucket/path/
  ├── Photos/2008/Aug/
  ├── Photos/2017/May/
  ├── Videos/2024/Dec/
  └── Unknown/
      ├── Photos/
      └── Videos/
  ```
- Auto-renames target collisions: if `IMG_1234.jpg` already exists at the target with different content, the new one becomes `IMG_1234-1.jpg`.
- **Resumable** — interrupt anytime, re-run, picks up exactly where it stopped. The resume mechanism (source path + size + mtime lookup) is so cheap that you can run it ten times in a row and the 2nd through 10th runs finish in ~zero seconds with no uploads.
- **Drive-disconnect resilient** — if a USB drive vanishes mid-run (cable bump, sleep wake failure), the script catches `FileNotFoundError` per-file and exits cleanly with a reconnect message. No 89k-line crash spam, no manifest corruption.
- **Parallel** — multiple worker threads run rclone uploads concurrently. Default `--workers 6` is typically 3-5x faster than serial for archives full of small files (the per-file overhead, not bandwidth, is usually the bottleneck).

## Output layout

```
remote:bucket/path/
├── Photos/
│   ├── 2008/Aug/
│   ├── 2017/May/
│   └── 2024/Dec/
├── Videos/
│   └── 2024/Dec/
└── Unknown/
    ├── Photos/    # files with no sidecar JSON, no EXIF, and no mtime — rare
    └── Videos/
```

Month folders use three-letter English abbreviations (`Jan`, `Feb`, …, `Dec`) regardless of system locale.

## Requirements

- Python 3.8 or newer (standard library only)
- [rclone](https://rclone.org/install/) with at least one remote configured
- [ExifTool](https://exiftool.org) — install via Homebrew (`brew install exiftool`) or your package manager
- Optional: [osxphotos](https://github.com/RhetTbull/osxphotos) for Apple Photos library exports
- A few dozen MB of free disk for the SQLite manifest

## Quick start

1. Configure your rclone remote once:
   ```sh
   rclone config
   # follow prompts to add a B2/S3/etc. remote, e.g. named "b2"
   rclone listremotes
   # b2:
   ```

2. Dry-run to see where files would land without uploading:
   ```sh
   python3 photo_archive.py /path/to/source \
     --dest b2:my-bucket/photo-archive \
     --dry-run --limit 50
   ```

3. Run for real:
   ```sh
   python3 photo_archive.py /path/to/source \
     --dest b2:my-bucket/photo-archive
   ```

   `Ctrl-C` any time and re-run to resume.

## Command-line flags

| Flag | Purpose |
|---|---|
| `source` (positional) | Source directory to walk |
| `--dest REMOTE:PATH` | rclone destination root (required) |
| `--dry-run` | Walk, hash, and report — but skip uploads. Does not write to the manifest, so subsequent real runs aren't affected. |
| `--limit N` | Process at most N file groups (smoke test) |
| `--manifest PATH` | SQLite manifest path. Default: `~/.photo-archive/manifest.db` |
| `--log-dir PATH` | Per-run log directory. Default: `~/.photo-archive/logs/` |
| `--exif-batch N` | Files per `exiftool` invocation. Default: 200 |
| `--workers N` | Concurrent upload workers. Default 6. Set to 1 for serial. Higher (10-12) on a fast connection. |
| `--skip-dir NAME` | Directory **name** (not path) to skip during walk. Repeatable. Stacks on top of built-in skips. Useful for `iTunes Media`, project directories, etc. |
| `--move` | **Destructive.** Delete each source file after successful upload (`copied` or `copied_mtime` status only — duplicates are NOT deleted, see Caveats). For backups, leave this off. |

## Run output

```
copy   /src/IMG_1234.JPG -> b2:.../Photos/2024/Dec/IMG_1234.JPG    # EXIF date
copyT  /src/old_photo.jpg -> b2:.../Photos/2008/Aug/old_photo.jpg  # sidecar JSON date (Takeout or osxphotos)
copy*  /src/screenshot.png -> b2:.../Photos/2026/Apr/screenshot... # mtime fallback
dup    /src/copy_of_kitten.jpg (md5 already at b2:.../Photos/...)  # MD5 dedup hit
unk    /src/something.jpg -> b2:.../Unknown/Photos/something.jpg   # no date at all (rare)
rm     /src/old_photo.jpg (uploaded)                               # --move, after upload succeeded
```

End-of-run summary:

```
DONE in 14157.4s. groups=89206 totals={'copied': 26415, 'copied_mtime': 28533,
'duplicate': 11488, 'unknown_date': 0, 'skipped_resume': 870, 'errors': 0,
'moved_deleted': 0}
```

The `copied_mtime` count is broken out from `copied` so you can later audit the less-authoritative dates:

```sh
sqlite3 ~/.photo-archive/manifest.db \
  "SELECT source_path, target_path FROM files WHERE status='copied_mtime';"
```

## How the manifest works

The script keeps a small SQLite database at `~/.photo-archive/manifest.db` with two tables, in WAL mode for concurrent access by worker threads:

- **`files`** — one row per source file. Keyed by `(source_path, size, mtime)`. Stores the file's MD5, status (`copied`, `copied_mtime`, `duplicate`, `unknown_date`, `error`), target path, and resolved capture date. This table is what makes resume cheap: a re-run looks up the same `(source_path, size, mtime)` tuple and skips immediately if the row exists.
- **`md5_index`** — keyed by MD5 hash. Used for cross-source dedup. When you run on Drive A and then Drive B, files that exist on both drives get hashed once on each, but only uploaded once. The Drive B run records them as `duplicate` with a pointer back to the Drive A target path.

The manifest is the source of truth. The remote is a write-target, not a read-source — the script doesn't list the remote on every run. If you delete the manifest you can rebuild it from scratch by running again, but you'll re-hash every file (and won't re-upload anything because the per-file collision check before each upload still fires).

## Apple Photos workflow

Apple Photos library bundles (`~/Pictures/Photos Library.photoslibrary`) are TCC-protected — direct `ls`/`du` returns "Operation not permitted." The clean workflow is:

```sh
# 1. Install osxphotos (prebuilt arm64 binary recommended; the Homebrew formula doesn't exist
#    and the PyPI version is stale).
curl -sSL https://github.com/RhetTbull/osxphotos/releases/latest/download/osxphotos_MacOS_exe_darwin_arm64.zip -o /tmp/osxphotos.zip
unzip /tmp/osxphotos.zip -d ~/.local/bin/

# 2. Export originals + sidecar JSONs to a staging dir.
#    First run will trigger a "Terminal would like to access your Photos library" prompt — click Allow.
#    --download-missing --use-photokit handles iCloud "Optimize Mac Storage" libraries.
osxphotos export ~/photos-staged \
  --download-missing --use-photokit \
  --sidecar json --ramdb --retry 3

# 3. Upload + dedup against everything else in the manifest.
python3 photo_archive.py ~/photos-staged --dest b2:my-bucket/photo-archive

# 4. Clean up the staging dir.
rm -rf ~/photos-staged
```

Why `--sidecar json` instead of `--exiftool`: the latter mutates the exported file's bytes (writes new EXIF), which breaks MD5 dedup against the same photo from other sources (e.g., Google Takeout). The sidecar approach keeps original bytes intact.

## Drive resilience (long external-drive runs)

Plugging a USB drive in for a multi-hour or multi-day run? Two settings to know:

```sh
# 1. Disable disk sleep so the drive doesn't get spun down mid-read and fail to wake.
sudo pmset -a disksleep 0

# 2. Wrap the script in caffeinate to prevent system sleep during the run.
caffeinate -is python3 photo_archive.py /Volumes/MyDrive --dest b2:bucket/path
```

Revert `disksleep` after the run with `sudo pmset -a disksleep 10`.

If the drive disconnects anyway (cable, controller failure, etc.), the script exits cleanly with a "source root no longer accessible" message — no crash, no manifest corruption. Reconnect the drive and re-run; resume will skip everything already uploaded.

On MacBooks: `caffeinate -is` lets you close the lid mid-run as long as you're on AC power. On battery, lid-close still triggers sleep regardless of caffeinate.

## Caveats

- **First run on a large archive is slow.** The bottleneck is upload bandwidth + B2/S3 per-file overhead, not the script. A 1 TB photo drive of small JPEGs is realistically a 12-24 hour run with `--workers 6`. Subsequent runs on the same source are ~instant via resume.
- **mtime fallback can place files in the wrong year.** If you copied photos between drives years ago without preserving timestamps, their mtime may be the copy date, not the capture date. These get tagged `copied_mtime` so you can find and re-sort them later.
- **`--move` does not delete duplicates.** If a source file MD5-matches something already on the remote, the source stays put — safer if a remote file got deleted out-of-band. To force-delete duplicates, re-run without `--move` (which records the dedup hit), then `rm` the remaining source files manually after verifying.
- **Backblaze B2 buckets** auto-create a `.bzEmpty` placeholder file at the root. It's harmless. `rclone lsf … --recursive --files-only | wc -l` will return one more than your actual file count.
- **Google Takeout sidecar names are unpredictably truncated.** Full form is `{photo}.supplemental-metadata.json`, but Google often truncates to `.supplemen.json`, `.suppl.json`, `.supp.json`, etc., to fit the 255-byte filename limit. This tool tries common truncations and falls back to a directory glob — but the glob is suppressed for directories with no `.json` files at all (a major speedup on archives without sidecars).
- **Live Photos** (HEIC + MOV pair) and RAW+JPEG pairs are grouped by directory + filename stem. The primary file's date is used for the whole group.
- **Symlinks** are followed (since `os.walk` follows them by default). If your source has symlinks pointing outside the source tree, those targets will be processed too.
- **Default skip directories** (always skipped, no flag needed): `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `@eaDir`, `.AppleDouble`, `$RECYCLE.BIN`, `System Volume Information`, `.bzvol`, `RECYCLER`, `lost+found`. To skip more, use `--skip-dir NAME`.

## What this tool does NOT do

- It does not modify or delete source files **unless `--move` is set**. By default, copy-only.
- It does not download from cloud sources. Use Google Takeout / iCloud export / `osxphotos` first, then point this at the resulting local directory.
- It does not rename files (other than collision-rename `name-1.ext` when target paths collide).
- It does not strip or modify EXIF on uploaded files. The bytes you upload match the bytes on disk.
- It does not run AI-based date guessing or filename parsing. If a file has no real date metadata and no sidecar, mtime is the only fallback before Unknown.

## License

MIT — see [LICENSE](LICENSE).
