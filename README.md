# photo-archive

Consolidate photos and videos from local drives, external drives, and Google Photos Takeout exports to a single date-organized cloud archive — without uploading duplicates.

A single Python script that walks your media, reads true capture dates, MD5-dedupes against a persistent manifest, and uploads via [rclone](https://rclone.org) to any remote it supports (Backblaze B2, S3, Google Drive, Dropbox, etc.). Source files are never modified or deleted.

## Why this exists

If you have photos scattered across:

- multiple external drives accumulated over the years,
- a Google Photos export (Takeout `.zip`s),
- iCloud / Photos.app exports,
- ad-hoc folders from old Macs/PCs,

…and you want them all in one cloud bucket, sorted into `Photos/2017/May/` and friends, with no duplicates and no destroyed dates — that's what this does.

It is particularly careful about two scenarios that defeat naive scripts:

1. **Google Takeout** strips or modifies in-file EXIF for many photos but provides a JSON sidecar containing the real `photoTakenTime`. This tool reads those sidecars (including their unpredictably-truncated filenames like `IMG_1234.jpg.supplemen.json`) and uses them as the highest-priority date source.
2. **Files copied between drives** often have their mtime overwritten by the copy operation, making mtime an unreliable date source. This tool uses mtime only as the *last* fallback after every EXIF tag has been tried, and labels mtime-sourced placements differently in the manifest so you can audit them later.

## What it does

- Walks a source directory recursively, skipping junk (`.DS_Store`, `@eaDir`, `Thumbs.db`, dotfiles, AppleDouble files).
- Groups sidecar files by directory + filename stem so Live Photos (`IMG_1234.HEIC` + `IMG_1234.MOV`), RAW+JPEG pairs, and `.AAE` edits stay together with the primary file's date.
- For each file, resolves a capture date using this priority chain:
  1. Google Takeout JSON sidecar (`photoTakenTime.timestamp`)
  2. EXIF `DateTimeOriginal`
  3. EXIF `CreateDate`
  4. EXIF `MediaCreateDate` (videos)
  5. EXIF `TrackCreateDate` (videos)
  6. `FileModifyDate` (filesystem mtime — fallback only)
  7. Otherwise → `Unknown/`
- Computes MD5 of each file (streamed, ~1 MB at a time) and dedupes against a persistent SQLite manifest. A file already at the destination — even renamed, even from a different source drive — is detected and skipped.
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
- **Resumable** — if interrupted at any time, re-running on the same source picks up exactly where it stopped. The resume mechanism (source path + size + mtime lookup) is so cheap that you can run it on the same source ten times in a row and the 2nd through 10th runs finish in ~zero seconds with no uploads.

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
    ├── Photos/    # files with no Takeout sidecar, no EXIF, and no mtime
    └── Videos/
```

Month folders use three-letter English abbreviations (`Jan`, `Feb`, …, `Dec`) regardless of system locale.

## Requirements

- Python 3.8 or newer
- [rclone](https://rclone.org/install/) with at least one remote configured
- [ExifTool](https://exiftool.org) — install via Homebrew (`brew install exiftool`) or your package manager
- A few dozen MB of free disk for the manifest (it stays small even for hundreds of thousands of files)

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

   You can `Ctrl-C` any time and re-run to resume. The manifest is updated after every file, not in batches.

## Command-line flags

| Flag | Purpose |
|---|---|
| `source` (positional) | Source directory to walk |
| `--dest REMOTE:PATH` | rclone destination root (required) |
| `--dry-run` | Walk, hash, and report — but skip uploads |
| `--limit N` | Process at most N file groups (smoke test) |
| `--manifest PATH` | SQLite manifest path. Default: `~/.photo-archive/manifest.db` |
| `--log-dir PATH` | Per-run log directory. Default: `~/.photo-archive/logs/` |
| `--exif-batch N` | Files per `exiftool` invocation. Default: 200 |

## Run output

```
copy   /src/IMG_1234.JPG -> b2:.../Photos/2024/Dec/IMG_1234.JPG    # EXIF date
copyT  /src/old_photo.jpg -> b2:.../Photos/2008/Aug/old_photo.jpg  # Google Takeout JSON date
copy*  /src/screenshot.png -> b2:.../Photos/2026/Apr/screenshot... # mtime fallback
dup    /src/copy_of_kitten.jpg (md5 already at b2:.../Photos/...)  # MD5 dedup hit
unk    /src/something.jpg -> b2:.../Unknown/Photos/something.jpg   # no date at all (rare)
```

End-of-run summary:

```
DONE in 4263.7s. groups=18234 totals={'copied': 14112, 'copied_mtime': 287,
'duplicate': 3461, 'unknown_date': 0, 'skipped_resume': 0, 'errors': 0}
```

`copied_mtime` is broken out from `copied` so you can later audit the less-authoritative dates with one query:

```sh
sqlite3 ~/.photo-archive/manifest.db \
  "SELECT source_path, target_path FROM files WHERE status='copied_mtime';"
```

## How the manifest works

The script keeps a small SQLite database at `~/.photo-archive/manifest.db` with two tables:

- **`files`** — one row per source file. Keyed by `(source_path, size, mtime)`. Stores the file's MD5, status (`copied`, `copied_mtime`, `duplicate`, `unknown_date`, `error`), target path, and resolved capture date. This table is what makes resume cheap: a re-run looks up the same `(source_path, size, mtime)` tuple and skips immediately if the row exists.
- **`md5_index`** — keyed by MD5 hash. Used for cross-source dedup. When you run on Drive A and then Drive B, files that exist on both drives get hashed once on each, but only uploaded once. The Drive B run records them as `duplicate` with a pointer back to the Drive A target path.

The manifest is the source of truth. The remote is a write-target, not a read-source — the script doesn't list the remote on every run (which would be slow and expensive on cloud storage), it consults the manifest. If you delete the manifest you can rebuild it from scratch by running again, but you'll re-hash every file (and won't re-upload anything because the destination collision check still fires).

## Caveats

- **First run on a large archive is slow.** The bottleneck is upload bandwidth + B2/S3 small-file overhead, not the script. A typical 40 GB Google Takeout takes 2–10 hours depending on connection. Subsequent runs on the same source are ~instant.
- **mtime fallback can place files in the wrong year.** If you copied photos between drives years ago without preserving timestamps, their mtime may be the copy date, not the capture date. Files placed via mtime fallback are tagged `copied_mtime` so you can find and re-sort them later if needed.
- **Backblaze B2 buckets** auto-create a `.bzEmpty` placeholder file at the root. It's harmless. `rclone lsf … --recursive --files-only | wc -l` will return one more than your actual file count.
- **Google Takeout sidecar names are unpredictably truncated.** The full suffix is `.supplemental-metadata.json` but Google truncates it (e.g., `.supplemen.json`, `.suppl.json`) when the photo's filename is long. This tool tries the common truncations and falls back to a glob match in the same directory.
- **Live Photos** (HEIC + MOV pair) and RAW+JPEG pairs are grouped by directory + filename stem. The primary file's date is used for the whole group.
- **Symlinks** are followed (since `os.walk` follows them by default). If your source has symlinks pointing outside the source tree, those targets will be processed too.

## What this tool does NOT do

- It does not move or delete source files. Ever.
- It does not download from cloud sources. Use Google Takeout / iCloud export / etc. first, then point this at the resulting local directory.
- It does not rename files (other than collision-rename `name-1.ext` when target paths collide).
- It does not strip or modify EXIF.
- It does not run AI-based date guessing or filename parsing. If a file has no real date metadata, mtime is the only fallback before Unknown.

## License

MIT — see [LICENSE](LICENSE).
