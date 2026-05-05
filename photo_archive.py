#!/usr/bin/env python3
"""
photo_archive.py — copy photos and videos from a source dir to a remote sorted
by capture date into Photos/YYYY/Mmm and Videos/YYYY/Mmm. Dedupes by MD5
against a persistent SQLite manifest. Resumable: re-running on the same source
skips files already processed. Source is never modified.

Date priority:
    Google Takeout JSON > EXIF DateTimeOriginal > CreateDate >
    MediaCreateDate > TrackCreateDate > FileModifyDate (mtime)
Files with no resolvable date go to Unknown/.

Usage:
    photo_archive.py /path/to/source --dest remote:bucket/path
                     [--dry-run] [--limit N]
                     [--manifest ~/.photo-archive/manifest.db]

Requires: rclone (with the chosen remote configured), exiftool, Python 3.8+.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff",
    ".bmp", ".webp", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng",
    ".orf", ".rw2", ".raf", ".srw", ".pef",
}
VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".mts",
    ".m2ts", ".3gp", ".mpg", ".mpeg", ".webm",
}
SIDECAR_EXTS = {".aae", ".xmp", ".thm", ".lrv"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS | SIDECAR_EXTS
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}
SKIP_DIR_NAMES = {".Trashes", ".Spotlight-V100", ".fseventsd", "@eaDir", ".AppleDouble",
                  "$RECYCLE.BIN", "System Volume Information", ".bzvol",
                  "RECYCLER", "lost+found"}

PHOTO_PREF = [".heic", ".heif", ".dng", ".cr3", ".cr2", ".nef", ".arw",
              ".raf", ".rw2", ".orf", ".srw", ".pef", ".raw",
              ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"]
VIDEO_PREF = [".mov", ".mp4", ".m4v", ".mkv", ".avi", ".mts", ".m2ts",
              ".webm", ".mpg", ".mpeg", ".3gp", ".wmv", ".flv"]

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def media_kind(ext):
    e = ext.lower()
    if e in PHOTO_EXTS:
        return "photo"
    if e in VIDEO_EXTS:
        return "video"
    return "sidecar"


def init_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    # WAL mode lets readers run concurrently with writers; persistent across connections.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            source_path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            md5 TEXT,
            status TEXT NOT NULL,
            target_path TEXT,
            kind TEXT,
            shutter_date TEXT,
            processed_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS md5_index (
            md5 TEXT PRIMARY KEY,
            target_path TEXT NOT NULL,
            kind TEXT,
            first_seen TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)")
    conn.commit()
    return conn


_thread_local = threading.local()


def get_thread_conn(manifest_path):
    """Per-thread SQLite connection. Each worker thread opens its own conn against
    the same DB file. WAL mode (set once in init_db) makes this safe."""
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = sqlite3.connect(str(manifest_path), timeout=30.0)
        _thread_local.conn.execute("PRAGMA synchronous=NORMAL")
    return _thread_local.conn


def md5_file(path, chunk=1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def walk_media(root, extra_skip_dirs=None):
    """
    Walk root for media files. Returns (media_paths, json_dirs).
    json_dirs is the set of directories containing any *.json file — used
    to skip the slow iterdir() fallback in find_takeout_sidecar() for
    directories that can't possibly hold a Takeout/osxphotos sidecar.
    """
    skip = SKIP_DIR_NAMES | set(extra_skip_dirs or [])
    root = Path(root).resolve()
    media_paths = []
    json_dirs = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith("._")]
        has_json = False
        for name in filenames:
            if name in SKIP_NAMES or name.startswith("._"):
                continue
            if name.lower().endswith(".json"):
                has_json = True
            ext = Path(name).suffix.lower()
            if ext in MEDIA_EXTS:
                media_paths.append(Path(dirpath) / name)
        if has_json:
            json_dirs.add(dirpath)
    return media_paths, json_dirs


def group_sidecars(paths):
    """
    Group files by directory + base stem so Live Photos (.HEIC + .MOV) and
    edits (.AAE) and RAW+JPG pairs travel together.
    Returns list of dicts: {primary, members, kind}
    """
    by_key = defaultdict(list)
    for p in paths:
        key = (str(p.parent), p.stem.lower())
        by_key[key].append(p)

    groups = []
    for (_, _), members in by_key.items():
        kinds = {media_kind(m.suffix) for m in members}
        if "photo" in kinds:
            primary_kind = "photo"
            primary = pick_primary(members, PHOTO_PREF, "photo")
        elif "video" in kinds:
            primary_kind = "video"
            primary = pick_primary(members, VIDEO_PREF, "video")
        else:
            # sidecars only — shouldn't happen, but skip
            continue
        groups.append({
            "primary": primary,
            "members": members,
            "kind": primary_kind,
        })
    return groups


def pick_primary(members, pref_list, kind):
    """Pick the primary file among a sidecar group based on extension preference."""
    candidates = [m for m in members if media_kind(m.suffix) == kind]
    candidates.sort(key=lambda p: (
        pref_list.index(p.suffix.lower()) if p.suffix.lower() in pref_list else 999,
        p.name,
    ))
    return candidates[0]


def find_takeout_sidecar(photo_path, json_dirs=None):
    """
    Look for a JSON sidecar that carries date metadata for `photo_path`.
    Handles two formats with the same lookup pattern:
    - Google Takeout: `{photo}.supplemental-metadata.json` and its
      unpredictable truncations (`.supplemen.json`, `.suppl.json`, etc.) due
      to Google fitting the suffix into the 255-byte filename limit.
    - osxphotos `--sidecar json`: `{photo}.json` (exiftool-format).

    `json_dirs`, if supplied, is the set of directories known to contain at
    least one *.json file — when supplied, the slow iterdir() fallback is
    skipped for parents not in the set. Massive speedup on archives without
    sidecars (e.g. an old photo backup drive).
    """
    photo_name = photo_path.name
    parent = photo_path.parent
    # Candidates in priority order: full suffix, common truncations, bare .json
    candidates = [
        parent / f"{photo_name}.supplemental-metadata.json",
        parent / f"{photo_name}.supplemen.json",
        parent / f"{photo_name}.suppl.json",
        parent / f"{photo_name}.supp.json",
        parent / f"{photo_name}.su.json",
        parent / f"{photo_name}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # Skip the expensive iterdir fallback if this dir is known to contain
    # no .json files at all.
    if json_dirs is not None and str(parent) not in json_dirs:
        return None
    # Fallback: any file in same dir starting with photo_name + "." and ending .json
    try:
        for entry in parent.iterdir():
            n = entry.name
            if n.startswith(photo_name + ".") and n.endswith(".json") and entry.is_file():
                return entry
    except OSError:
        pass
    return None


def read_takeout_date(json_path):
    """
    Parse a JSON sidecar for a capture date. Handles two formats:
    1. Google Takeout: dict with `photoTakenTime.timestamp` (Unix epoch).
    2. osxphotos `--sidecar json`: list of dicts with exiftool-style
       `Group:Tag` keys like `EXIF:DateTimeOriginal` (YYYY:MM:DD HH:MM:SS).
    Returns datetime or None.
    """
    try:
        with open(json_path, "rb") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    # Format 1: Google Takeout
    if isinstance(data, dict):
        pt = data.get("photoTakenTime") or data.get("creationTime")
        if isinstance(pt, dict) and pt.get("timestamp"):
            try:
                dt = datetime.utcfromtimestamp(int(pt["timestamp"]))
                if 1980 <= dt.year <= 2100:
                    return dt
            except (TypeError, ValueError):
                pass

    # Format 2: osxphotos / exiftool — list of records (or single dict)
    records = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    # Priority order of date tags (most authoritative first)
    primary_tags = ("datetimeoriginal", "createdate", "mediacreatedate",
                    "trackcreatedate", "datecreated")
    for tag in primary_tags:
        for rec in records:
            if not isinstance(rec, dict):
                continue
            for key, val in rec.items():
                if key.lower().split(":")[-1] == tag:
                    dt = parse_exif_dt(val)
                    if dt and 1980 <= dt.year <= 2100:
                        return dt
    return None


def read_takeout_dates(paths, log, json_dirs=None):
    """Look up Takeout JSON sidecars for each path. Returns {abs_path: datetime}."""
    out = {}
    for p in paths:
        sidecar = find_takeout_sidecar(p, json_dirs=json_dirs)
        if sidecar is None:
            continue
        dt = read_takeout_date(sidecar)
        if dt and 1980 <= dt.year <= 2100:
            out[str(p.resolve())] = dt
    return out


def read_exif_dates(paths, log):
    """
    Batch-read dates via exiftool. Returns {path_str: (datetime, source)}
    where source is 'exif', 'mtime', or 'none'.
    """
    if not paths:
        return {}
    cmd = [
        "exiftool", "-j", "-q", "-fast",
        "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
        "-TrackCreateDate", "-FileModifyDate",
        *[str(p) for p in paths],
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log.warning("exiftool timeout on batch of %d files", len(paths))
        return {str(p): (None, "none") for p in paths}
    if not out.stdout.strip():
        return {str(p): (None, "none") for p in paths}
    try:
        records = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        log.warning("exiftool JSON decode failed: %s", e)
        return {str(p): (None, "none") for p in paths}

    result = {}
    for rec in records:
        src = rec.get("SourceFile")
        if not src:
            continue
        exif_dt = (parse_exif_dt(rec.get("DateTimeOriginal"))
                   or parse_exif_dt(rec.get("CreateDate"))
                   or parse_exif_dt(rec.get("MediaCreateDate"))
                   or parse_exif_dt(rec.get("TrackCreateDate")))
        if exif_dt:
            result[os.path.abspath(src)] = (exif_dt, "exif")
        else:
            mtime_dt = parse_exif_dt(rec.get("FileModifyDate"))
            if mtime_dt:
                result[os.path.abspath(src)] = (mtime_dt, "mtime")
            else:
                result[os.path.abspath(src)] = (None, "none")
    for p in paths:
        result.setdefault(str(p.resolve()), (None, "none"))
    return result


def parse_exif_dt(value):
    if not value or not isinstance(value, str):
        return None
    # exiftool format: "YYYY:MM:DD HH:MM:SS" with optional subseconds and tz.
    # Date portion uses ':' separators, so '-' only appears in negative tz offset.
    s = value.split(".")[0]
    for tz in ("+", "-", "Z"):
        if tz in s[10:]:
            s = s[: s.index(tz, 10)]
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year < 1980 or dt.year > 2100:
                return None
            return dt
        except ValueError:
            continue
    return None


def target_for(group, shutter_dt, kind, dest_root):
    """Return (target_dir, target_paths_for_each_member) under dest_root."""
    if shutter_dt is None:
        sub = f"Unknown/{kind.capitalize()}s"
    else:
        sub = f"{kind.capitalize()}s/{shutter_dt.year}/{MONTH_ABBR[shutter_dt.month - 1]}"
    target_dir = f"{dest_root.rstrip('/')}/{sub}"
    return target_dir


def rclone_exists(remote_path):
    """Return True if a path exists on the remote."""
    out = subprocess.run(
        ["rclone", "lsf", remote_path, "--max-depth", "1"],
        capture_output=True, text=True,
    )
    return out.returncode == 0 and out.stdout.strip() != ""


def rclone_copy(src, dest_dir, dest_name, log, dry_run=False):
    """Copy a single file via rclone, with collision-avoidance rename."""
    final_name = dest_name
    full_dest = f"{dest_dir}/{final_name}"
    n = 1
    while rclone_exists(full_dest):
        stem = Path(dest_name).stem
        suffix = Path(dest_name).suffix
        final_name = f"{stem}-{n}{suffix}"
        full_dest = f"{dest_dir}/{final_name}"
        n += 1
        if n > 999:
            raise RuntimeError(f"too many collisions for {dest_name}")

    if dry_run:
        log.info("[dry-run] would copy %s -> %s", src, full_dest)
        return full_dest

    cmd = [
        "rclone", "copyto", str(src), full_dest,
        "--b2-hard-delete", "--checksum", "--no-traverse",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"rclone copy failed: {out.stderr.strip()}")
    return full_dest


def already_processed(conn, path):
    """Returns the manifest row (or None). Raises FileNotFoundError if path is gone."""
    st = path.stat()
    row = conn.execute(
        "SELECT status, md5, target_path FROM files WHERE source_path = ? AND size = ? AND mtime = ?",
        (str(path), st.st_size, st.st_mtime),
    ).fetchone()
    return row


def source_root_alive(source_root):
    """True if the source root directory is still accessible. Used to detect drive disconnects."""
    try:
        return Path(source_root).is_dir() and any(True for _ in os.scandir(source_root))
    except (OSError, FileNotFoundError):
        return False


def md5_seen(conn, md5):
    row = conn.execute(
        "SELECT target_path FROM md5_index WHERE md5 = ?", (md5,)
    ).fetchone()
    return row[0] if row else None


def record(conn, path, md5, status, target_path, kind, shutter_dt):
    st = path.stat()
    conn.execute("""
        INSERT OR REPLACE INTO files
        (source_path, size, mtime, md5, status, target_path, kind, shutter_date, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(path), st.st_size, st.st_mtime, md5, status, target_path,
        kind, shutter_dt.isoformat() if shutter_dt else None,
        datetime.utcnow().isoformat(),
    ))
    if md5 and status in ("copied", "copied_mtime", "unknown_date") and target_path:
        conn.execute("""
            INSERT OR IGNORE INTO md5_index (md5, target_path, kind, first_seen)
            VALUES (?, ?, ?, ?)
        """, (md5, target_path, kind, datetime.utcnow().isoformat()))
    conn.commit()


def process_group(group, manifest_path, dest_root, log, dry_run, exif_cache, takeout_cache, move_after_upload=False):
    conn = get_thread_conn(manifest_path)
    """Process one sidecar group. Returns counts dict."""
    counts = {"copied": 0, "copied_mtime": 0, "duplicate": 0, "unknown_date": 0, "skipped_resume": 0, "errors": 0, "moved_deleted": 0}
    primary = group["primary"]
    members = group["members"]
    kind = group["kind"]

    # Resume check on primary — if primary was processed cleanly, skip group.
    try:
        prior = already_processed(conn, primary)
    except FileNotFoundError:
        log.warning("source vanished (drive disconnect?): %s", primary)
        counts["errors"] += 1
        return counts
    if prior and prior[0] in ("copied", "copied_mtime", "duplicate", "unknown_date"):
        counts["skipped_resume"] += len(members)
        return counts

    # Date priority: Takeout JSON > EXIF chain > mtime fallback
    primary_key = str(primary.resolve())
    shutter_dt = takeout_cache.get(primary_key)
    date_src = "takeout" if shutter_dt else None

    if shutter_dt is None:
        # Try other group members for a Takeout sidecar
        for m in members:
            alt_dt = takeout_cache.get(str(m.resolve()))
            if alt_dt:
                shutter_dt, date_src = alt_dt, "takeout"
                break

    if shutter_dt is None:
        shutter_dt, date_src = exif_cache.get(primary_key, (None, "none"))
        if shutter_dt is None:
            # Try other photo members for an EXIF date before settling for mtime.
            for m in members:
                if media_kind(m.suffix) == "photo":
                    alt_dt, alt_src = exif_cache.get(str(m.resolve()), (None, "none"))
                    if alt_dt and alt_src == "exif":
                        shutter_dt, date_src = alt_dt, alt_src
                        break

    target_dir = target_for(group, shutter_dt, kind, dest_root)
    if shutter_dt is None:
        status_when_placed = "unknown_date"
    elif date_src == "mtime":
        status_when_placed = "copied_mtime"
    else:  # takeout or exif
        status_when_placed = "copied"

    for m in members:
        try:
            md5 = md5_file(m)
            existing = md5_seen(conn, md5)
            if existing:
                log.info("dup  %s (md5 already at %s)", m, existing)
                record(conn, m, md5, "duplicate", existing, media_kind(m.suffix), shutter_dt)
                counts["duplicate"] += 1
                continue

            final = rclone_copy(m, target_dir, m.name, log, dry_run=dry_run)
            status = status_when_placed
            tag = {"copied": "copy", "copied_mtime": "copy*", "unknown_date": "unk "}[status]
            if date_src == "takeout":
                tag = "copyT"
            log.info("%s %s -> %s", tag, m, final)
            # Don't pollute the manifest in dry-run — otherwise resume on the
            # real run would skip files that were never actually uploaded.
            if not dry_run:
                record(conn, m, md5, status, final, media_kind(m.suffix), shutter_dt)
            counts[status] = counts.get(status, 0) + 1

            if move_after_upload and not dry_run and status in ("copied", "copied_mtime"):
                try:
                    m.unlink()
                    log.info("rm   %s (uploaded)", m)
                    counts["moved_deleted"] = counts.get("moved_deleted", 0) + 1
                except OSError as e:
                    log.warning("could not delete source %s: %s", m, e)
        except Exception as e:
            log.error("error on %s: %s", m, e)
            try:
                record(conn, m, None, "error", None, media_kind(m.suffix), shutter_dt)
            except Exception:
                pass
            counts["errors"] += 1
    return counts


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = log_dir / f"run-{ts}.log"
    log = logging.getLogger("photo-archive")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(logfile)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)
    log.info("log file: %s", logfile)
    return log


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Source directory to scan")
    ap.add_argument("--dest", required=True,
                    help="rclone destination root, e.g. b2:my-bucket/photo-archive")
    ap.add_argument("--manifest", default=str(Path.home() / ".photo-archive" / "manifest.db"),
                    help="SQLite manifest path")
    ap.add_argument("--log-dir", default=str(Path.home() / ".photo-archive" / "logs"))
    ap.add_argument("--dry-run", action="store_true", help="Walk and hash but don't upload")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N groups (smoke test)")
    ap.add_argument("--exif-batch", type=int, default=200, help="exiftool batch size")
    ap.add_argument("--workers", type=int, default=6,
                    help="Concurrent upload workers. 1 = serial. Default 6 — the "
                         "bottleneck is B2's per-file overhead, not bandwidth, so "
                         "concurrency multiplies throughput. Set higher (10-12) on "
                         "a fast connection.")
    ap.add_argument("--skip-dir", action="append", default=[],
                    help="Directory NAME (not path) to skip. Repeatable. "
                         "Stacks on top of built-in skips ($RECYCLE.BIN etc.). "
                         "Example: --skip-dir 'iTunes Media' --skip-dir Library.")
    ap.add_argument("--move", action="store_true",
                    help="DELETE source files after successful upload. Files that "
                         "MD5-match an existing B2 copy (status=duplicate) are NOT "
                         "deleted — re-run without --move to inspect those, or "
                         "delete manually after verifying on B2.")
    args = ap.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        sys.exit(f"source not a directory: {source}")

    log = setup_logging(Path(args.log_dir))
    log.info("source: %s", source)
    log.info("dest:   %s", args.dest)
    log.info("manifest: %s", args.manifest)
    if args.dry_run:
        log.info("DRY RUN — no uploads will be performed")

    conn = init_db(Path(args.manifest))

    log.info("walking source...")
    if args.skip_dir:
        log.info("extra skip dirs: %s", args.skip_dir)
    paths, json_dirs = walk_media(source, extra_skip_dirs=args.skip_dir)
    log.info("dirs containing .json sidecars: %d", len(json_dirs))
    log.info("found %d media files", len(paths))

    log.info("grouping sidecars...")
    groups = group_sidecars(paths)
    log.info("formed %d groups", len(groups))

    if args.limit:
        groups = groups[: args.limit]
        log.info("limit applied: processing %d groups", len(groups))

    # Pre-fetch all primary EXIF dates in batches.
    log.info("reading EXIF dates from primaries...")
    exif_cache = {}
    primaries = [g["primary"] for g in groups]
    # Also fetch for photo members so we can fall back if primary lacks EXIF.
    extra_photos = [m for g in groups for m in g["members"]
                    if media_kind(m.suffix) == "photo" and m != g["primary"]]
    to_read = primaries + extra_photos
    for i in range(0, len(to_read), args.exif_batch):
        batch = to_read[i : i + args.exif_batch]
        cache = read_exif_dates(batch, log)
        # exiftool returns paths as it received them; normalize to absolute
        for k, v in cache.items():
            exif_cache[os.path.abspath(k)] = v
        log.info("  exif progress: %d / %d", min(i + args.exif_batch, len(to_read)), len(to_read))

    log.info("scanning for sidecar JSON dates (Takeout / osxphotos)...")
    takeout_cache = read_takeout_dates(to_read, log, json_dirs=json_dirs)
    log.info("  sidecar dates resolved: %d / %d files", len(takeout_cache), len(to_read))

    totals = {"copied": 0, "copied_mtime": 0, "duplicate": 0, "unknown_date": 0, "skipped_resume": 0, "errors": 0, "moved_deleted": 0}
    t0 = time.time()
    log.info("processing %d groups with %d workers", len(groups), args.workers)
    manifest_path = Path(args.manifest)

    if args.workers <= 1:
        # Serial path (kept for debugging and --workers 1 testing)
        for idx, group in enumerate(groups, 1):
            if idx % 100 == 0 and not source_root_alive(source):
                log.error("source root no longer accessible: %s — aborting run, "
                         "re-run after reconnecting", source)
                break
            counts = process_group(group, manifest_path, args.dest, log, args.dry_run,
                                   exif_cache, takeout_cache, move_after_upload=args.move)
            for k, v in counts.items():
                totals[k] += v
            if idx % 50 == 0:
                elapsed = time.time() - t0
                log.info("progress: %d / %d groups, %.1fs elapsed, totals=%s",
                         idx, len(groups), elapsed, totals)
    else:
        # Parallel path
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(process_group, group, manifest_path, args.dest, log,
                                args.dry_run, exif_cache, takeout_cache, args.move)
                for group in groups
            ]
            aborted = False
            for idx, fut in enumerate(as_completed(futures), 1):
                # Check drive every 100 completions; on disconnect, stop submitting
                # and let in-flight tasks fail naturally (they'll record as errors).
                if idx % 100 == 0 and not source_root_alive(source):
                    if not aborted:
                        log.error("source root no longer accessible: %s — cancelling "
                                  "remaining work", source)
                        aborted = True
                    for f in futures:
                        f.cancel()
                try:
                    counts = fut.result()
                except Exception as e:
                    log.error("worker exception: %s", e)
                    totals["errors"] += 1
                    continue
                for k, v in counts.items():
                    totals[k] += v
                if idx % 50 == 0:
                    elapsed = time.time() - t0
                    rate = idx / elapsed if elapsed else 0
                    log.info("progress: %d / %d groups, %.1fs elapsed (%.1f/s), totals=%s",
                             idx, len(groups), elapsed, rate, totals)

    log.info("=" * 60)
    log.info("DONE in %.1fs. groups=%d totals=%s", time.time() - t0, len(groups), totals)


if __name__ == "__main__":
    main()
