#!/usr/bin/env python3
"""
edl_sound_roll_core.py

Core logic for the RollPull app. No GUI dependencies — importable as a library
and runnable as a CLI.

Workflow:
    1. Recursively scan a production audio folder for .wav files.
    2. For each WAV, read its Sound Roll from BWF/iXML metadata.
       (iXML <TAPE> primary, BEXT.originator_reference fallback.)
    3. Parse the input EDL. For every event whose FROM CLIP NAME (or
       TO CLIP NAME, for dissolves) references a .wav file present in
       the scanned pool, replace the Roll Name field on the event line
       with the WAV's Sound Roll.
    4. Write the rewritten EDL alongside a JSON report of matches and
       misses.

The original EDL is never modified. Output filename is
`<original>_RollPull.edl` in the same directory unless overridden.
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

# Event line:  EVNUM  ROLLNAME  TRACK  TYPE  ...
# TRACK is V, A, AA, A1-A9, or B1-B9.  TYPE is C, D, W, K (with optional
# duration after for D/W/K).  Roll Name is everything between the two-space
# delimiter after EVNUM and the single-space delimiter before TRACK.
EVENT_LINE_RE = re.compile(
    r'^(?P<evnum>\d{3,})  '
    r'(?P<roll>.+?) '
    r'(?P<track>V|A|AA|A[1-9]|B[1-9])'
    r'(?P<ws>\s+)'
    r'(?P<edit>C|D|W|K)'
    r'(?P<rest>.*)$'
)

FROM_CLIP_RE = re.compile(r'^\* FROM CLIP NAME:\s*(.+?)\s*$')
TO_CLIP_RE   = re.compile(r'^\* TO CLIP NAME:\s*(.+?)\s*$')

WAV_EXT_RE = re.compile(r'\.wav$', re.IGNORECASE)


def looks_like_wav(name: str) -> bool:
    return bool(name) and bool(WAV_EXT_RE.search(name.strip()))


# ---------------------------------------------------------------------------
# WAV metadata scanning
# ---------------------------------------------------------------------------

@dataclass
class WavRecord:
    """One scanned WAV file."""
    filename: str           # basename, case preserved
    path: Path
    sound_roll: Optional[str] = None
    source: str = ""        # 'ixml.tape' | 'bext.orig_ref' | 'none'
    error: Optional[str] = None


def read_sound_roll(path: Path) -> tuple[Optional[str], str, Optional[str]]:
    """
    Return (sound_roll, source, error).

    Tries iXML <TAPE> first (the standard production-sound location),
    then falls back to BEXT.originator_reference, which some recorders
    populate with the sound roll instead.
    """
    try:
        from wavinfo import WavInfoReader
    except ImportError:
        return None, "none", "wavinfo library not installed"

    try:
        info = WavInfoReader(str(path))
    except Exception as e:
        return None, "none", f"read error: {e}"

    # iXML <TAPE> — primary
    try:
        if info.ixml is not None and getattr(info.ixml, "tape", None):
            tape = info.ixml.tape.strip()
            if tape:
                return tape, "ixml.tape", None
    except Exception:
        pass

    # BEXT.originator_reference — fallback (some recorders put roll here)
    try:
        if info.bext is not None:
            orig_ref = getattr(info.bext, "originator_reference", None)
            if orig_ref:
                orig_ref = orig_ref.strip()
                if orig_ref:
                    return orig_ref, "bext.orig_ref", None
    except Exception:
        pass

    return None, "none", None


def scan_audio_folder(
    folder: Path,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> dict[str, WavRecord]:
    """
    Walk the folder recursively, collecting WAV metadata into a dict keyed by
    lowercase filename (basename). If duplicate filenames are encountered,
    the first one wins and subsequent paths are recorded in the WavRecord.

    progress_cb(current, total, filename) is called once per file if provided.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not a directory")

    # Two-pass: first list all WAVs (so we know the total for progress),
    # then read metadata.
    all_wavs: list[Path] = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() != ".wav":
            continue
        # Skip macOS resource-fork sidecars copied from HFS+/APFS volumes.
        if p.name.startswith("._"):
            continue
        all_wavs.append(p)

    by_name: dict[str, WavRecord] = {}
    total = len(all_wavs)
    for i, p in enumerate(all_wavs, 1):
        key = p.name.lower()
        if key in by_name:
            # Duplicate — skip but don't error
            if progress_cb:
                progress_cb(i, total, p.name)
            continue
        roll, source, err = read_sound_roll(p)
        by_name[key] = WavRecord(
            filename=p.name,
            path=p,
            sound_roll=roll,
            source=source,
            error=err,
        )
        if progress_cb:
            progress_cb(i, total, p.name)

    return by_name


# ---------------------------------------------------------------------------
# EDL rewriting
# ---------------------------------------------------------------------------

@dataclass
class RewriteResult:
    output_path: Path
    total_events: int = 0
    rewritten_events: int = 0
    wav_referenced_events: int = 0
    matched: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    no_roll: list[dict] = field(default_factory=list)


def _replace_roll_on_event_line(line: str, new_roll: str) -> str:
    """Rewrite the Roll Name on a single event line, preserving spacing."""
    # Detect line ending
    if line.endswith("\r\n"):
        ending = "\r\n"
        body = line[:-2]
    elif line.endswith("\n"):
        ending = "\n"
        body = line[:-1]
    else:
        ending = ""
        body = line

    m = EVENT_LINE_RE.match(body)
    if not m:
        return line

    return (
        f"{m['evnum']}  {new_roll} {m['track']}{m['ws']}{m['edit']}{m['rest']}"
        + ending
    )


def _collect_event_blocks(lines: list[str]) -> list[dict]:
    """
    Walk through EDL lines and return a list of "event blocks".

    Each block represents a single event (possibly split across two event
    lines for a dissolve) and carries:
        - line_indices: list of indices into `lines` that are event lines
        - evnum: event number (string)
        - from_clip / to_clip: extracted from the comment lines following
    """
    # Find every event line and its index
    event_lines: list[tuple[int, str]] = []  # (line_idx, evnum)
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        m = EVENT_LINE_RE.match(body)
        if m:
            event_lines.append((i, m["evnum"]))

    # Group consecutive event lines with the same event number; comments
    # following the LAST event line in the group belong to the whole group.
    blocks: list[dict] = []
    k = 0
    while k < len(event_lines):
        idx, evnum = event_lines[k]
        group = [(idx, evnum)]
        while k + 1 < len(event_lines) and event_lines[k + 1][1] == evnum:
            k += 1
            group.append(event_lines[k])
        last_idx = group[-1][0]
        # Scan forward from last_idx+1 until the next event line (or EOF) to
        # collect comments
        next_event_idx = (
            event_lines[k + 1][0] if k + 1 < len(event_lines) else len(lines)
        )
        from_clip: Optional[str] = None
        to_clip: Optional[str] = None
        for j in range(last_idx + 1, next_event_idx):
            comment_text = lines[j].rstrip("\r\n")
            # The source EDL sometimes packs two comments on one line with
            # an embedded \n; handle both.
            for sub in re.split(r"\n", comment_text):
                fm = FROM_CLIP_RE.match(sub)
                tm = TO_CLIP_RE.match(sub)
                if fm and from_clip is None:
                    from_clip = fm.group(1)
                if tm and to_clip is None:
                    to_clip = tm.group(1)
        blocks.append(
            dict(
                line_indices=[g[0] for g in group],
                evnum=evnum,
                from_clip=from_clip,
                to_clip=to_clip,
            )
        )
        k += 1
    return blocks


def rewrite_edl(
    edl_path: Path,
    wav_index: dict[str, WavRecord],
    output_path: Optional[Path] = None,
) -> RewriteResult:
    """
    Rewrite the EDL so that any event referencing a .wav file has its Roll
    Name field replaced with the WAV's Sound Roll metadata.
    """
    edl_path = Path(edl_path)
    if output_path is None:
        output_path = edl_path.with_name(edl_path.stem + "_RollPull.edl")
    else:
        output_path = Path(output_path)

    with edl_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    lines = raw.splitlines(keepends=True)

    blocks = _collect_event_blocks(lines)
    result = RewriteResult(output_path=output_path, total_events=len(blocks))

    for blk in blocks:
        from_clip = blk["from_clip"]
        to_clip = blk["to_clip"]
        line_indices = blk["line_indices"]
        evnum = blk["evnum"]

        # We rewrite based on FROM for the first event line, TO for any
        # additional event lines in a dissolve group.
        if not (looks_like_wav(from_clip or "") or looks_like_wav(to_clip or "")):
            continue

        result.wav_referenced_events += 1

        # First event line uses FROM
        if looks_like_wav(from_clip or ""):
            key = (from_clip or "").lower().strip()
            rec = wav_index.get(key)
            if rec is None:
                result.unmatched.append(
                    dict(event=evnum, side="from", clip=from_clip)
                )
            elif not rec.sound_roll:
                result.no_roll.append(
                    dict(
                        event=evnum,
                        side="from",
                        clip=from_clip,
                        path=str(rec.path),
                        error=rec.error,
                    )
                )
            else:
                lines[line_indices[0]] = _replace_roll_on_event_line(
                    lines[line_indices[0]], rec.sound_roll
                )
                result.rewritten_events += 1
                result.matched.append(
                    dict(
                        event=evnum,
                        side="from",
                        clip=from_clip,
                        sound_roll=rec.sound_roll,
                        source=rec.source,
                    )
                )

        # Additional event lines in a dissolve use TO
        if len(line_indices) > 1 and looks_like_wav(to_clip or ""):
            key = (to_clip or "").lower().strip()
            rec = wav_index.get(key)
            if rec is None:
                result.unmatched.append(
                    dict(event=evnum, side="to", clip=to_clip)
                )
            elif not rec.sound_roll:
                result.no_roll.append(
                    dict(
                        event=evnum,
                        side="to",
                        clip=to_clip,
                        path=str(rec.path),
                        error=rec.error,
                    )
                )
            else:
                for li in line_indices[1:]:
                    lines[li] = _replace_roll_on_event_line(
                        lines[li], rec.sound_roll
                    )
                    result.rewritten_events += 1
                result.matched.append(
                    dict(
                        event=evnum,
                        side="to",
                        clip=to_clip,
                        sound_roll=rec.sound_roll,
                        source=rec.source,
                    )
                )

    output_path.write_text("".join(lines), encoding="utf-8", newline="")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Rewrite an EDL's Roll Name field using Sound Roll "
                    "metadata read from a production audio folder."
    )
    p.add_argument("edl", help="path to input EDL")
    p.add_argument("audio_folder", help="folder of production WAVs (scanned recursively)")
    p.add_argument("-o", "--output", help="output EDL path (default: <input>_RollPull.edl)")
    p.add_argument("--report", help="optional JSON report path")
    args = p.parse_args()

    edl_path = Path(args.edl)
    folder = Path(args.audio_folder)

    print(f"Scanning {folder} for WAVs...", file=sys.stderr)

    def cb(i, total, name):
        if i % 50 == 0 or i == total:
            print(f"  [{i}/{total}] {name}", file=sys.stderr)

    wav_index = scan_audio_folder(folder, progress_cb=cb)
    print(f"Indexed {len(wav_index)} WAVs.", file=sys.stderr)

    result = rewrite_edl(
        edl_path,
        wav_index,
        output_path=Path(args.output) if args.output else None,
    )

    print(
        f"\nTotal events: {result.total_events}\n"
        f"Events referencing WAVs: {result.wav_referenced_events}\n"
        f"Event lines rewritten: {result.rewritten_events}\n"
        f"Unmatched (WAV not in folder): {len(result.unmatched)}\n"
        f"No-roll (WAV present but no metadata): {len(result.no_roll)}\n"
        f"\nOutput: {result.output_path}",
        file=sys.stderr,
    )

    if args.report:
        Path(args.report).write_text(
            json.dumps(
                dict(
                    output=str(result.output_path),
                    total_events=result.total_events,
                    wav_referenced_events=result.wav_referenced_events,
                    rewritten_events=result.rewritten_events,
                    matched=result.matched,
                    unmatched=result.unmatched,
                    no_roll=result.no_roll,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Report: {args.report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
