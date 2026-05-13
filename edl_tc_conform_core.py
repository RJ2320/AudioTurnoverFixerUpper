#!/usr/bin/env python3
"""
edl_tc_conform_core.py

Salvage workflow for picture-edit EDLs that reference camera dailies (.mov/.mxf)
instead of production WAVs. Matches each event by BWF/iXML timecode + duration
against a production audio folder, then rewrites the event so that:

  - Roll Name column becomes the WAV's Sound Roll (iXML <TAPE>)
  - * FROM CLIP NAME comment becomes the WAV's basename

Assumes camera and sound were TC-jam-synced. Assumes 23.976 NDF at 48 kHz
unless the WAV's bext.description sSPEED line says otherwise.

CLI:
    python edl_tc_conform_core.py <input.edl> <audio_folder> -o <out.edl>
                                  [--report <report.json>] [--fps 23.976]
                                  [--tolerance-frames 1]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# EDL parsing
# ---------------------------------------------------------------------------

EVENT_LINE_RE = re.compile(
    r'^(?P<evnum>\d{3,})\s+'
    r'(?P<roll>\S.*?)\s+'
    r'(?P<track>V|A|AA|NONE|A[1-9]|B[1-9])'
    r'(?P<ws>\s+)'
    r'(?P<edit>C|D|W|K)'
    r'(?P<dur>\s+\d+)?'
    r'\s+'
    r'(?P<src_in>\d{2}:\d{2}:\d{2}:\d{2})\s+'
    r'(?P<src_out>\d{2}:\d{2}:\d{2}:\d{2})\s+'
    r'(?P<rec_in>\d{2}:\d{2}:\d{2}:\d{2})\s+'
    r'(?P<rec_out>\d{2}:\d{2}:\d{2}:\d{2})'
    r'(?P<rest>.*)$'
)

FROM_CLIP_RE = re.compile(r'^\* FROM CLIP NAME:\s*(.+?)\s*$')


def tc_to_frames(tc: str) -> int:
    """`HH:MM:SS:FF` → integer frame number (NDF, treating each second as
    `fps` frames; works correctly for 23.976 NDF since the EDL uses label
    arithmetic, not real time)."""
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 60 + m) * 60 + s) * 24 + f


# ---------------------------------------------------------------------------
# WAV metadata scan
# ---------------------------------------------------------------------------

SPEED_RE = re.compile(r'^sSPEED=([\d.]+)', re.MULTILINE)


@dataclass
class WavRecord:
    path: Path
    filename: str
    sound_roll: Optional[str]      # iXML <TAPE>
    sample_rate: int
    frame_count: int               # audio frames (samples)
    time_reference: int            # samples from midnight
    fps: float                     # 23.976, 24, 25, 29.97, 30...
    start_label: int               # frame label (24fps-style) at start
    end_label: int                 # frame label (24fps-style) at end (exclusive)
    shoot_date: Optional[str] = None   # YYYY-MM-DD, sourced from a sound report CSV
    scene: Optional[str] = None
    take: Optional[str] = None
    error: Optional[str] = None


def _samples_per_label_frame(sample_rate: int, fps: float) -> float:
    """Samples-per-frame in NDF label time.
    For 23.976 NDF the label clock advances at 24 fps but real time advances
    at 24000/1001 fps, so labels and samples have a constant ratio:
        samples_per_label = sample_rate * 1001 / 24000  (for 23.976)
    Generalize: if abs(fps - 23.976) < 0.01 → use 24000/1001 fraction;
    otherwise treat fps as the literal label rate.
    """
    if abs(fps - 23.976) < 0.05:
        return sample_rate * 1001.0 / 24000.0
    if abs(fps - 29.97) < 0.05:
        return sample_rate * 1001.0 / 30000.0
    return sample_rate / fps


def read_wav_record(path: Path, default_fps: float) -> WavRecord:
    try:
        from wavinfo import WavInfoReader
    except ImportError:
        return WavRecord(path, path.name, None, 0, 0, 0, default_fps, 0, 0,
                         error="wavinfo not installed")

    try:
        w = WavInfoReader(str(path))
    except Exception as e:
        return WavRecord(path, path.name, None, 0, 0, 0, default_fps, 0, 0,
                         error=f"read error: {e}")

    tape = None
    try:
        if w.ixml is not None:
            t = getattr(w.ixml, "tape", None)
            if t:
                tape = t.strip()
    except Exception:
        pass

    fps = default_fps
    try:
        m = SPEED_RE.search(w.bext.description or "")
        if m:
            fps = float(m.group(1))
    except Exception:
        pass

    sr = w.fmt.sample_rate
    fc = w.data.frame_count
    if w.bext is None:
        return WavRecord(path, path.name, tape, sr, fc, 0, fps, 0, 0,
                         error="no BWF bext chunk")
    tref = w.bext.time_reference

    spf = _samples_per_label_frame(sr, fps)
    start_label = int(round(tref / spf))
    end_label = int(round((tref + fc) / spf))

    return WavRecord(
        path=path,
        filename=path.name,
        sound_roll=tape,
        sample_rate=sr,
        frame_count=fc,
        time_reference=tref,
        fps=fps,
        start_label=start_label,
        end_label=end_label,
    )


def _parse_sound_report_csv(csv_path: Path) -> dict:
    """Parse a sound mixer's per-roll CSV (e.g. `001_Report.csv`).

    Returns:
        {
            "date": "YYYY-MM-DD" or None,
            "rows": { wav_filename_lower: {"scene": ..., "take": ...} }
        }
    The CSV layout has a freeform header (Project/Producer/Director/...
    /Date/Roll/...) then a row of column headers starting with
    "File Name,Scene,Take,...", then data rows.
    """
    import csv
    out = {"date": None, "rows": {}}
    try:
        text = csv_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    lines = text.splitlines()
    header_row_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Date:"):
            # CSV: `Date:,"07/28/25",`
            parts = list(csv.reader([ln]))[0]
            if len(parts) >= 2:
                raw = parts[1].strip().strip('"')
                # Tolerate MM/DD/YY, MM/DD/YYYY, YYYY-MM-DD
                from datetime import datetime
                for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
                    try:
                        out["date"] = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
        if ln.lstrip().startswith("File Name,Scene,Take"):
            header_row_idx = i
            break
    if header_row_idx is not None:
        reader = csv.reader(lines[header_row_idx:])
        headers = next(reader, [])
        for row in reader:
            if not row or not row[0].strip():
                continue
            cells = dict(zip(headers, row))
            fn = (cells.get("File Name") or "").strip()
            if not fn:
                continue
            out["rows"][fn.lower()] = {
                "scene": (cells.get("Scene") or "").strip().strip('"'),
                "take":  (cells.get("Take") or "").strip().strip('"'),
            }
    return out


def scan_audio_folder(
    folder: Path,
    default_fps: float = 23.976,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    wav_date_fallback: Optional[dict[str, str]] = None,
) -> list[WavRecord]:
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(folder)

    wavs: list[Path] = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() != ".wav":
            continue
        if p.name.startswith("._"):
            continue
        # Skip sync-proxy files (e.g. *_synctemp.16i.sb.wav from AAtranslator
        # or similar) — they carry the original TC and double the candidate
        # pool without adding information.
        lower = p.name.lower()
        if "_synctemp" in lower or lower.endswith(".sb.wav"):
            continue
        wavs.append(p)

    # Pre-parse any sound report CSVs we find (one per roll folder),
    # keyed by the WAV's containing directory so we can stamp shoot
    # date / scene / take onto each WavRecord below.
    csv_by_dir: dict[Path, dict] = {}
    for p in folder.rglob("*_Report.csv"):
        csv_by_dir[p.parent.resolve()] = _parse_sound_report_csv(p)

    records: list[WavRecord] = []
    total = len(wavs)
    for i, p in enumerate(wavs, 1):
        rec = read_wav_record(p, default_fps)
        rep = csv_by_dir.get(p.parent.resolve())
        if rep:
            rec.shoot_date = rep.get("date")
            row = rep.get("rows", {}).get(p.name.lower())
            if row:
                rec.scene = row.get("scene") or None
                rec.take  = row.get("take") or None
        # If the CSV didn't give us a date (no CSV, or no Date: row),
        # fall back to the ingest-tree map.
        if rec.shoot_date is None and wav_date_fallback:
            rec.shoot_date = wav_date_fallback.get(p.name.lower())
        records.append(rec)
        if progress_cb:
            progress_cb(i, total, p.name)
    return records


# ---------------------------------------------------------------------------
# Dailies scanner — map camera_clip_id -> shoot_date from the editor's
# EDITORIALS folder tree. The folder name `HRFD_YYMMDD_<DAY>` carries the
# shoot date; the .mov filenames carry the camera clip IDs the picture
# editor used in their EDL/XML.
# ---------------------------------------------------------------------------

import re as _re

# Shoot-day folder names. Match any of:
#   HRFD_250729_SHOOTDAY2     (project-prefix, 6-digit YYMMDD)
#   20260410_ChiPickup         (8-digit YYYYMMDD prefix, common Pomfort/YoYotta)
#   250729_PickupDay           (6-digit YYMMDD prefix)
# We then assume 20YY years (true for any modern shoot).
_DAILY_DATE_RES: list[_re.Pattern] = [
    _re.compile(r"^[A-Za-z]+_(\d{2})(\d{2})(\d{2})_", _re.IGNORECASE),     # HRFD_YYMMDD_...
    _re.compile(r"^(20\d{2})(\d{2})(\d{2})[_-]", _re.IGNORECASE),          # YYYYMMDD_...
    _re.compile(r"^(\d{2})(\d{2})(\d{2})[_-][A-Za-z]", _re.IGNORECASE),    # YYMMDD_NAME
]


def _date_from_folder_name(name: str) -> Optional[str]:
    """Return YYYY-MM-DD if `name` matches one of the known shoot-day
    folder conventions, else None."""
    for rx in _DAILY_DATE_RES:
        m = rx.match(name)
        if not m:
            continue
        a, b, c = m.groups()
        if len(a) == 4:
            yyyy, mm, dd = a, b, c
        else:
            yyyy, mm, dd = f"20{a}", b, c
        # Plausibility check — month 1-12, day 1-31
        if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
            continue
        return f"{yyyy}-{mm}-{dd}"
    return None


def _clip_id_from_mov_name(name: str) -> str:
    """`A038_C012_0814AT.mov` -> `A038_C012_0814AT`.
    Also handles audio-only variants like `A038_C012_0814AT.mov_R`."""
    # Strip everything from the first ".mov" onward.
    lower = name.lower()
    idx = lower.find(".mov")
    if idx >= 0:
        return name[:idx]
    # Generic: strip any final extension
    return Path(name).stem


def scan_dailies(
    root: Path,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Walk a dailies/ingest root and return (clip_dates, wav_dates).

    `clip_dates`: {camera_clip_id: YYYY-MM-DD} from MOV/MXF/R3D/BRAW under
    each `HRFD_YYMMDD_<DAY>/EDITORIALS` subtree.

    `wav_dates`: {wav_filename_lower: YYYY-MM-DD} from WAVs under each
    `HRFD_YYMMDD_<DAY>/AUDIO` subtree — useful for filling in shoot dates
    on rolls whose CSV sound report is missing.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    clip_dates: dict[str, str] = {}
    wav_dates: dict[str, str] = {}
    shoot_dirs: list[tuple[Path, str]] = []
    # Look one or two levels deep — shoot-day folders are typically
    # immediate children of an ingest root (e.g. 01_OCN/HRFD_250728_*).
    candidates = list(root.iterdir())
    for c in list(candidates):
        if c.is_dir():
            try:
                candidates.extend(c.iterdir())
            except OSError:
                pass
    for p in candidates:
        if not p.is_dir():
            continue
        date_str = _date_from_folder_name(p.name)
        if date_str:
            shoot_dirs.append((p, date_str))

    total = len(shoot_dirs)
    for i, (sd, date_str) in enumerate(shoot_dirs, 1):
        # Sweep the whole shoot-day folder for camera clips and WAVs.
        # We can't assume "EDITORIALS" / "AUDIO" subfolder names because
        # pickup-day ingests (Pomfort/YoYotta) use names like
        # HORRIFIED_CARD01, HORRIFIED_AUDIO, etc.
        for f in sd.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            name_lower = f.name.lower()
            if ext in (".mov", ".mxf", ".r3d", ".braw"):
                clip_dates.setdefault(_clip_id_from_mov_name(f.name), date_str)
            elif ext == ".wav":
                if "_synctemp" in name_lower or name_lower.endswith(".sb.wav"):
                    continue
                wav_dates.setdefault(name_lower, date_str)
        if progress_cb:
            progress_cb(i, total, sd.name)
    return clip_dates, wav_dates


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

@dataclass
class Match:
    event: str
    src_in: str
    src_out: str
    wav: Optional[WavRecord]
    candidates: list[WavRecord] = field(default_factory=list)
    ambiguous: bool = False


def find_wav_for_event(
    records: list[WavRecord],
    src_in_frames: int,
    src_out_frames: int,
    tolerance: int = 1,
) -> tuple[Optional[WavRecord], list[WavRecord]]:
    """Return (best_match, all_candidates).

    Candidate = a WAV whose [start_label, end_label] contains
    [src_in_frames, src_out_frames] within `tolerance` frames at both ends.
    When multiple candidates exist (e.g. TC overlap across days), the one
    with the smallest gap to the event window wins; the full candidate list
    is also returned so the caller can flag ambiguity.
    """
    cands: list[WavRecord] = []
    for r in records:
        if r.sound_roll is None or r.frame_count == 0:
            continue
        if (r.start_label - tolerance) <= src_in_frames and \
           (r.end_label + tolerance) >= src_out_frames:
            cands.append(r)

    if not cands:
        return None, []

    def gap(r: WavRecord) -> int:
        return (src_in_frames - r.start_label) + (r.end_label - src_out_frames)

    cands.sort(key=gap)
    return cands[0], cands


# ---------------------------------------------------------------------------
# Rewriter
# ---------------------------------------------------------------------------

@dataclass
class ConformResult:
    output_path: Path
    total_events: int = 0
    rewritten_events: int = 0
    unmatched: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    matched: list[dict] = field(default_factory=list)


def _rewrite_event_line(line: str, new_roll: str) -> str:
    """Replace the Roll Name field in place, preserving the original column
    positions so downstream CMX3600 parsers (EdiLoad, Pro Tools) still
    align. The roll occupies span [roll_start, track_start); pad or
    truncate `new_roll` to fit that span minus one trailing space."""
    if line.endswith("\r\n"):
        ending, body = "\r\n", line[:-2]
    elif line.endswith("\n"):
        ending, body = "\n", line[:-1]
    else:
        ending, body = "", line
    m = EVENT_LINE_RE.match(body)
    if not m:
        return line
    roll_start = m.start("roll")
    track_start = m.start("track")
    field_width = track_start - roll_start  # includes trailing space(s)
    if field_width < 2:
        field_width = max(len(new_roll) + 1, 2)
    # leave at least one space before track
    target_width = field_width - 1
    if len(new_roll) > target_width:
        roll_text = new_roll[:target_width]
    else:
        roll_text = new_roll.ljust(target_width)
    return body[:roll_start] + roll_text + " " + body[track_start:] + ending


def _rewrite_from_clip_line(line: str, new_clip: str) -> str:
    if line.endswith("\r\n"):
        ending, body = "\r\n", line[:-2]
    elif line.endswith("\n"):
        ending, body = "\n", line[:-1]
    else:
        ending, body = "", line
    if not FROM_CLIP_RE.match(body):
        return line
    return f"* FROM CLIP NAME: {new_clip}{ending}"


def conform_edl(
    edl_path: Path,
    records: list[WavRecord],
    output_path: Optional[Path] = None,
    tolerance_frames: int = 1,
) -> ConformResult:
    edl_path = Path(edl_path)
    if output_path is None:
        output_path = edl_path.with_name(edl_path.stem + "_TCConform.edl")

    raw = edl_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines(keepends=True)

    # Find each event line; the FROM CLIP NAME line follows somewhere before
    # the next event line.
    event_indices: list[tuple[int, "re.Match[str]"]] = []
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        m = EVENT_LINE_RE.match(body)
        if m:
            event_indices.append((i, m))

    result = ConformResult(output_path=output_path, total_events=len(event_indices))

    # Pass 1 — gather raw candidates per event.
    raw_per_event: list[tuple[int, "re.Match[str]", list[WavRecord]]] = []
    for k, (idx, m) in enumerate(event_indices):
        src_in = tc_to_frames(m["src_in"])
        src_out = tc_to_frames(m["src_out"])
        _, cands = find_wav_for_event(records, src_in, src_out, tolerance_frames)
        raw_per_event.append((idx, m, cands))

    # Pass 2 — camera-roll cohort vote. Events sharing a camera roll
    # (the EDL's Roll Name column, e.g. X002C014) were shot on the same
    # day, so the correct sound roll for them should appear in most of
    # their candidate lists. Pick the sound roll with the highest
    # cohort-wide candidate frequency.
    from collections import Counter, defaultdict
    cohort_votes: dict[str, Counter[str]] = defaultdict(Counter)
    for _, m, cands in raw_per_event:
        camera_roll = m["roll"].strip()
        seen = set()
        for c in cands:
            if c.sound_roll and c.sound_roll not in seen:
                cohort_votes[camera_roll][c.sound_roll] += 1
                seen.add(c.sound_roll)
    preferred_roll_for: dict[str, Optional[str]] = {
        cam: (votes.most_common(1)[0][0] if votes else None)
        for cam, votes in cohort_votes.items()
    }

    # Pass 3 — rewrite using cohort preference when available.
    for k, (idx, m, cands) in enumerate(raw_per_event):
        evnum = m["evnum"]
        src_in = tc_to_frames(m["src_in"])
        src_out = tc_to_frames(m["src_out"])
        next_idx = event_indices[k + 1][0] if k + 1 < len(event_indices) else len(lines)
        camera_roll = m["roll"].strip()

        if not cands:
            result.unmatched.append(dict(
                event=evnum, src_in=m["src_in"], src_out=m["src_out"],
                roll=camera_roll,
            ))
            continue

        pref = preferred_roll_for.get(camera_roll)
        filtered = [c for c in cands if c.sound_roll == pref] if pref else []
        chosen_pool = filtered if filtered else cands
        best = chosen_pool[0]  # already sorted by smallest gap in find_wav_for_event

        # Rewrite the event line's Roll Name
        lines[idx] = _rewrite_event_line(lines[idx], best.sound_roll)

        # Rewrite the FROM CLIP NAME line in the trailing comment block
        for j in range(idx + 1, next_idx):
            if FROM_CLIP_RE.match(lines[j].rstrip("\r\n")):
                lines[j] = _rewrite_from_clip_line(lines[j], best.filename)
                break

        result.rewritten_events += 1
        info = dict(
            event=evnum, src_in=m["src_in"], src_out=m["src_out"],
            sound_roll=best.sound_roll, wav=best.filename,
            candidates=[c.filename for c in cands] if len(cands) > 1 else None,
        )
        result.matched.append(info)
        if len(chosen_pool) > 1:
            result.ambiguous.append(info)

    output_path.write_text("".join(lines), encoding="utf-8", newline="")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("edl")
    p.add_argument("audio_folder")
    p.add_argument("-o", "--output")
    p.add_argument("--report")
    p.add_argument("--fps", type=float, default=23.976)
    p.add_argument("--tolerance-frames", type=int, default=1)
    args = p.parse_args()

    print(f"Scanning {args.audio_folder}...", file=sys.stderr)

    def cb(i, total, name):
        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] {name}", file=sys.stderr)

    records = scan_audio_folder(Path(args.audio_folder), args.fps, progress_cb=cb)
    print(f"Indexed {len(records)} WAVs.", file=sys.stderr)

    result = conform_edl(
        Path(args.edl), records,
        output_path=Path(args.output) if args.output else None,
        tolerance_frames=args.tolerance_frames,
    )

    print(
        f"\nTotal events: {result.total_events}\n"
        f"Rewritten: {result.rewritten_events}\n"
        f"Unmatched: {len(result.unmatched)}\n"
        f"Ambiguous (multiple WAV candidates): {len(result.ambiguous)}\n"
        f"\nOutput: {result.output_path}",
        file=sys.stderr,
    )

    if args.report:
        Path(args.report).write_text(json.dumps(dict(
            output=str(result.output_path),
            total_events=result.total_events,
            rewritten_events=result.rewritten_events,
            matched=result.matched,
            unmatched=result.unmatched,
            ambiguous=result.ambiguous,
        ), indent=2), encoding="utf-8")
        print(f"Report: {args.report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
