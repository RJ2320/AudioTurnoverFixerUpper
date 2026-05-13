#!/usr/bin/env python3
"""
edl_xml_conform.py

Read an FCP7 XML (`File → Export → Final Cut Pro XML` in Premiere), match each
audio clipitem in a chosen sequence against a production audio folder by
BWF/iXML timecode, and emit a deduplicated CMX3600 EDL of reference clips
suitable for EdiLoad v4 → Pro Tools field-recorder expansion.

The output EDL has one event per unique (camera_file, source_in) tuple, so
Pro Tools' Field Recorder → Match Criteria → Expand can pull every iso
channel of each take from the loaded production WAVs, regardless of how
many tracks the XML had.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

from edl_tc_conform_core import (
    scan_audio_folder, find_wav_for_event, WavRecord, scan_dailies,
    _date_from_folder_name,
)
import urllib.parse as _urlparse


# ---------------------------------------------------------------------------
# FCP7 XML parsing
# ---------------------------------------------------------------------------

@dataclass
class XmlFile:
    file_id: str
    name: str
    pathurl: Optional[str]
    tc_string: Optional[str]            # "HH:MM:SS:FF" or None
    tc_label_frames: int                # file's start TC expressed in 24fps labels
    rate_timebase: int
    rate_ntsc: bool


def _parse_tc(tc: str, timebase: int, ntsc: bool) -> int:
    """Convert HH:MM:SS:FF (at timebase/ntsc) into 24fps-style label frames.
    For 23.976 NDF (timebase=24 ntsc=TRUE) we keep the labels as-is.
    For other rates we normalise to a 24-label-per-second clock since that's
    what `edl_tc_conform_core.tc_to_frames` uses on the WAV side."""
    # Premiere uses ';' as separator for drop-frame TC.
    parts = tc.replace(";", ":").split(":")
    h, m, s, f = (int(x) for x in parts)
    return ((h * 60 + m) * 60 + s) * 24 + f


def _file_from_node(node: ET.Element) -> Optional[XmlFile]:
    fid = node.get("id") or ""
    name = node.findtext("name")
    if not name and not node.find("pathurl") is not None:
        return None
    pathurl = node.findtext("pathurl")
    tc_node = node.find("timecode")
    tc_string = tc_node.findtext("string") if tc_node is not None else None
    tb = 24
    ntsc = True
    if tc_node is not None:
        try:
            tb = int(tc_node.findtext("rate/timebase") or "24")
        except Exception:
            pass
        ntsc = (tc_node.findtext("rate/ntsc") or "TRUE").upper() == "TRUE"
    tc_label = _parse_tc(tc_string, tb, ntsc) if tc_string else 0
    return XmlFile(
        file_id=fid, name=name or "", pathurl=pathurl,
        tc_string=tc_string, tc_label_frames=tc_label,
        rate_timebase=tb, rate_ntsc=ntsc,
    )


def parse_fcp7_xml(
    xml_path: Path, sequence_name: str
) -> tuple[list[dict], int]:
    """Return (clips, sequence_label_rate). Each clip dict has:
        camera_roll, file_name, source_in, source_out, rec_in, rec_out
    All TC values in 24fps label frames (works for 23.976 NDF directly).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # 1) Build file-id → XmlFile map across the whole document (file defs
    #    can live anywhere; later references just use <file id="..."/>)
    file_defs: dict[str, XmlFile] = {}
    for fnode in root.iter("file"):
        if fnode.find("name") is None and fnode.find("pathurl") is None:
            continue  # bare reference
        xf = _file_from_node(fnode)
        if xf and xf.file_id and xf.file_id not in file_defs:
            file_defs[xf.file_id] = xf

    # 2) Locate the chosen sequence
    target = None
    for seq in root.findall(".//sequence"):
        if (seq.findtext("name") or "") == sequence_name:
            target = seq
            break
    if target is None:
        # case-insensitive fallback
        for seq in root.findall(".//sequence"):
            if (seq.findtext("name") or "").lower() == sequence_name.lower():
                target = seq
                break
    if target is None:
        names = [seq.findtext("name") for seq in root.findall(".//sequence")]
        raise ValueError(
            f"Sequence {sequence_name!r} not found. Available: {names[:10]}..."
        )

    seq_tb = int(target.findtext("rate/timebase") or "24")
    # 3) Walk audio tracks
    audio = target.find("media/audio")
    if audio is None:
        return [], seq_tb

    clips_out: list[dict] = []
    for t_idx, track in enumerate(audio.findall("track")):
        for cl in track.findall("clipitem"):
            fnode = cl.find("file")
            if fnode is None:
                continue
            # Either inline definition or bare reference
            if fnode.find("name") is None and fnode.find("pathurl") is None:
                # bare reference: <file id="file-X"/>
                fid = fnode.get("id") or ""
                xf = file_defs.get(fid)
            else:
                xf = _file_from_node(fnode)
                if xf and xf.file_id and xf.file_id not in file_defs:
                    file_defs[xf.file_id] = xf
            if xf is None:
                continue

            try:
                in_f  = int(cl.findtext("in"))
                out_f = int(cl.findtext("out"))
                start = int(cl.findtext("start"))
                end   = int(cl.findtext("end"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end < 0 or in_f < 0 or out_f < 0:
                continue
            if end <= start or out_f <= in_f:
                continue

            # Source TC labels = file start + clipitem offset
            source_in_label  = xf.tc_label_frames + in_f
            source_out_label = xf.tc_label_frames + out_f

            # Camera roll = strip extension from file name
            camera_roll = Path(xf.name).stem if xf.name else ""

            # Extract a shoot date directly from the dailies pathurl, if
            # present. This is a backup when the dailies-folder scan
            # doesn't have an entry for this camera clip ID (e.g. pickup
            # cards under non-standard folder names).
            path_date = None
            if xf.pathurl:
                try:
                    decoded = _urlparse.unquote(xf.pathurl)
                    for part in decoded.split("/"):
                        d = _date_from_folder_name(part)
                        if d:
                            path_date = d
                            break
                except Exception:
                    pass

            clips_out.append(dict(
                track_index=t_idx,
                camera_roll=camera_roll,
                file_name=xf.name,
                source_in=source_in_label,
                source_out=source_out_label,
                rec_in=start,
                rec_out=end,
                path_date=path_date,
                # Refs for XML rewrite mode:
                _clipitem=cl,
                _orig_in=in_f,
                _orig_out=out_f,
                _orig_file_tc=xf.tc_label_frames,
            ))

    return clips_out, seq_tb, tree, target


# ---------------------------------------------------------------------------
# Matching (cohort vote on camera roll, same as the EDL conformer)
# ---------------------------------------------------------------------------

def _date_for_clip(clip: dict, clip_dates: dict[str, str] | None) -> str | None:
    """Resolve a clip's shoot date. Tries, in order:
        1. clip_dates lookup by camera_clip_id (from the dailies scan)
        2. the date already extracted from the clipitem's pathurl
           (`path_date` populated in parse_fcp7_xml)
    """
    if clip_dates:
        cam = clip.get("camera_roll") or ""
        if cam:
            candidates = [cam]
            for sep in ("_R", "_L"):
                if cam.endswith(sep):
                    candidates.append(cam[: -len(sep)])
            if ".mov" in cam.lower():
                candidates.append(cam.split(".mov", 1)[0])
            for k in candidates:
                if k in clip_dates:
                    return clip_dates[k]
    return clip.get("path_date")


def match_clips(
    clips: list[dict], records: list[WavRecord], tolerance: int = 1,
    clip_dates: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (matched, unmatched). Each matched dict gets sound_roll + wav.

    If `clip_dates` is provided, candidates are restricted to WAVs whose
    `shoot_date` matches the clip's shoot date (looked up from the dailies
    scanner). WAVs missing a date are excluded only when at least one
    dated candidate exists — fallback to the legacy cohort vote when no
    dated match is available."""
    cand_lists: list[list[WavRecord]] = []
    for c in clips:
        _, cands = find_wav_for_event(records, c["source_in"], c["source_out"], tolerance)
        cand_lists.append(cands)

    # Camera-roll cohort vote (fallback path when dates are missing)
    cohort: dict[str, Counter] = defaultdict(Counter)
    for c, cands in zip(clips, cand_lists):
        seen = set()
        for r in cands:
            if r.sound_roll and r.sound_roll not in seen:
                cohort[c["camera_roll"]][r.sound_roll] += 1
                seen.add(r.sound_roll)
    preferred = {cam: v.most_common(1)[0][0] for cam, v in cohort.items() if v}

    matched, unmatched = [], []
    for c, cands in zip(clips, cand_lists):
        if not cands:
            unmatched.append(c)
            continue
        # First filter: shoot-date constraint
        date = _date_for_clip(c, clip_dates)
        pool = cands
        if date:
            dated = [r for r in cands if r.shoot_date == date]
            if dated:
                pool = dated
            else:
                # Strict mode: the camera clip's shoot date is known, but
                # no candidate WAV shares it. Refuse to pick a wrong-day
                # WAV — leave the clip unmatched so the editor can spot it
                # and chase it down (often a missing/un-ingested sound
                # roll for that day, or a WAV with no Date in its CSV).
                c2 = dict(c)
                c2["reason"] = f"no WAV with shoot_date={date} covers TC range"
                unmatched.append(c2)
                continue
        # Second filter: camera-roll cohort sound-roll preference
        pref = preferred.get(c["camera_roll"])
        if pref:
            pref_pool = [r for r in pool if r.sound_roll == pref]
            if pref_pool:
                pool = pref_pool
        best = pool[0]
        out = dict(c)
        out["sound_roll"] = best.sound_roll
        out["wav"] = best.filename
        out["candidates"] = len(cands)
        out["shoot_date"] = date
        matched.append(out)
    return matched, unmatched


# ---------------------------------------------------------------------------
# EDL emission
# ---------------------------------------------------------------------------

def _label_to_tc(n: int) -> str:
    n = max(0, int(n))
    f = n % 24; s = (n // 24) % 60; m = (n // (24 * 60)) % 60; h = n // (24 * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def emit_reference_edl(
    matched: list[dict], output_path: Path, title: str,
    rec_start_label: int = 24 * 3600,   # 01:00:00:00 default offset
    dedup: bool = False,
    seq_start_label: int | None = None,
):
    """Emit a reference EDL of the matched clips.

    Default (timeline mode): one EDL event per XML clipitem, preserving
    each cut's source TC and timeline position. Pro Tools' field-recorder
    expansion then places iso channels at every reference clip — i.e. the
    full edit, not just one instance per take.

    `dedup=True`: collapse to one event per unique (camera_roll, source_in,
    source_out, wav) tuple with packed/monotonic record TC. Useful for
    take inventory rather than cut reconstruction.

    `seq_start_label`: timeline rec-TC offset added to every event in
    timeline mode (default 01:00:00:00). Premiere XML rec frames are
    relative to the sequence start, so this lines events up with whatever
    head TC the destination session expects.
    """
    if dedup:
        # Take-inventory dedup: one event per unique source clip, ignoring
        # how many times or where it appears in the timeline.
        seen = set()
        kept = []
        for m in matched:
            key = (m["camera_roll"], m["source_in"], m["source_out"], m["wav"])
            if key in seen:
                continue
            seen.add(key)
            kept.append(m)
        kept.sort(key=lambda x: (x["camera_roll"], x["source_in"]))
    else:
        # Timeline mode: one event per unique cut position. Premiere XMLs
        # explode each multitrack source (mix L / mix R / boom / lavs) into
        # a separate clipitem on each audio track; all share the same
        # rec_in/source_in. Collapsing on (rec_in, source_in, wav) keeps
        # every distinct cut while removing the multitrack duplicates,
        # since Pro Tools' field-recorder expansion produces all iso
        # channels from a single reference clip.
        seen = set()
        kept = []
        for m in sorted(matched, key=lambda x: (x["rec_in"], x["source_in"])):
            key = (m["rec_in"], m["source_in"], m["source_out"], m["wav"])
            if key in seen:
                continue
            seen.add(key)
            kept.append(m)

    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    cursor = rec_start_label
    seq_offset = seq_start_label if seq_start_label is not None else rec_start_label
    for i, m in enumerate(kept, 1):
        src_in  = _label_to_tc(m["source_in"])
        src_out = _label_to_tc(m["source_out"])
        if dedup:
            dur = max(1, m["source_out"] - m["source_in"])
            rec_in  = _label_to_tc(cursor)
            rec_out = _label_to_tc(cursor + dur)
            cursor += dur
        else:
            rec_in  = _label_to_tc(seq_offset + m["rec_in"])
            rec_out = _label_to_tc(seq_offset + m["rec_out"])
        roll_field = (m["sound_roll"] or "")[:8].ljust(8)
        lines.append(
            f"{i:03d}  {roll_field} A     C        "
            f"{src_in} {src_out} {rec_in} {rec_out} "
        )
        lines.append(f"* FROM CLIP NAME: {m['wav']}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\r\n")
    return len(kept)


# ---------------------------------------------------------------------------
# XML rewrite — emit a relinked FCP7 XML pointing at production WAVs
# ---------------------------------------------------------------------------

def _path_to_url(p: Path) -> str:
    """Convert a Path to a `file://localhost/...` URL the way Premiere XMLs
    expect. Spaces and reserved chars are percent-encoded; backslashes
    become forward slashes."""
    import urllib.parse
    s = str(p).replace("\\", "/")
    # Strip leading slash on POSIX so we end up with file://localhost/...
    if s.startswith("/"):
        s = s[1:]
    return "file://localhost/" + urllib.parse.quote(s, safe="/:")


def _make_wav_file_element(file_id: str, wav, project_tb: int = 24):
    """Build a fresh <file> definition for a production WAV.

    Children are emitted in the canonical xmeml v4 order (duration, rate,
    name, pathurl, timecode, media) — Premiere's parser is forgiving but
    Resolve's xmeml importer is stricter and can crash on out-of-order
    children. The file uses the project's frame rate (e.g. 24/TRUE) so
    clipitem <in>/<out> math stays in frames; the audio sample rate is
    carried in <media><audio><samplecharacteristics><samplerate>.
    """
    import xml.etree.ElementTree as ET
    sr = wav.sample_rate or 48000
    fps = wav.fps if wav.fps else 23.976
    is_ntsc = "TRUE" if abs(fps - 23.976) < 0.05 or abs(fps - 29.97) < 0.05 else "FALSE"
    dur_frames = wav.end_label - wav.start_label
    tc_str = _label_to_tc(wav.start_label)

    f = ET.Element("file", id=file_id)

    ET.SubElement(f, "duration").text = str(dur_frames)

    rate = ET.SubElement(f, "rate")
    ET.SubElement(rate, "timebase").text = str(project_tb)
    ET.SubElement(rate, "ntsc").text = is_ntsc

    ET.SubElement(f, "name").text = wav.filename
    ET.SubElement(f, "pathurl").text = _path_to_url(wav.path)

    tc = ET.SubElement(f, "timecode")
    tc_rate = ET.SubElement(tc, "rate")
    ET.SubElement(tc_rate, "timebase").text = str(project_tb)
    ET.SubElement(tc_rate, "ntsc").text = is_ntsc
    ET.SubElement(tc, "string").text = tc_str
    ET.SubElement(tc, "frame").text = str(wav.start_label)
    ET.SubElement(tc, "displayformat").text = "NDF"

    media = ET.SubElement(f, "media")
    audio = ET.SubElement(media, "audio")
    sc = ET.SubElement(audio, "samplecharacteristics")
    ET.SubElement(sc, "depth").text = "24"
    ET.SubElement(sc, "samplerate").text = str(sr)
    ET.SubElement(audio, "channelcount").text = "1"
    return f


def emit_relinked_xml(
    tree, matched: list[dict], output_path: Path,
    project_tb: int = 24,
) -> int:
    """Rewrite the XML so matched audio clipitems reference production WAVs.

    For each matched clipitem:
      - Replace the <file> child with one pointing at the production WAV
        (full <file> def on first use, bare `<file id=.../>` on later uses)
      - Adjust the clipitem's <in>/<out> so that source TC = wav.start_tc + in
        equals the original source TC
      - Update <name>

    Unmatched clipitems (SFX/mp3/iPhone) are left untouched.
    """
    import xml.etree.ElementTree as ET

    # Build a registry: wav_path -> (file_id, defined_yet)
    wav_file_ids: dict[str, str] = {}
    wav_defined: set[str] = set()
    counter = 0

    rewritten = 0
    for m in matched:
        cl = m.get("_clipitem")
        if cl is None:
            continue
        # The matched record needs sound_roll + wav + a WavRecord-like
        # for TC math. We rebuild minimal WAV info from the match dict.
        # Find the actual WavRecord by filename via a side channel —
        # callers pass `_wavrec` populated by match_clips_with_records().
        wav = m.get("_wavrec")
        if wav is None:
            continue

        # Compute new clipitem in/out so source TC stays correct from WAV start
        new_in  = m["source_in"]  - wav.start_label
        new_out = m["source_out"] - wav.start_label
        if new_in < 0 or new_out <= new_in:
            continue   # mathematically impossible — skip the rewrite for this one

        # Allocate / find file id for this WAV
        key = str(wav.path)
        if key not in wav_file_ids:
            counter += 1
            wav_file_ids[key] = f"file-wav-conform-{counter}"
        file_id = wav_file_ids[key]

        # Strip stale references that pointed at the camera dailies. Leaving
        # any of these in place caused Resolve/Premiere to crash on import.
        for tag in (
            "masterclipid",      # bin lookup to the camera dailies' master
            "pproTicksIn",       # Premiere high-res ticks tied to old source rate
            "pproTicksOut",
            "sourcetrack",       # trackindex into the old source's channels
            "file",              # the old <file> child, replaced below
        ):
            for el in list(cl.findall(tag)):
                cl.remove(el)

        # New file element (full def on first occurrence, bare ref after).
        if file_id not in wav_defined:
            new_file = _make_wav_file_element(file_id, wav, project_tb)
            wav_defined.add(file_id)
        else:
            new_file = ET.Element("file", id=file_id)
        cl.append(new_file)

        # Update in/out and duration
        in_el  = cl.find("in")
        out_el = cl.find("out")
        if in_el is not None:  in_el.text  = str(new_in)
        if out_el is not None: out_el.text = str(new_out)
        dur_el = cl.find("duration")
        if dur_el is not None:
            dur_el.text = str(wav.end_label - wav.start_label)

        # Update name
        name_el = cl.find("name")
        if name_el is not None:
            name_el.text = wav.filename

        rewritten += 1

    # ElementTree drops the DOCTYPE and emits a non-standard declaration
    # (single quotes, lowercase utf-8). Premiere and Resolve identify FCP7
    # XML by the `<!DOCTYPE xmeml>` line; without it the import silently
    # fails or hangs. So write the body via ET, then prepend a standard
    # XML declaration + DOCTYPE + UTF-8 BOM.
    import io
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    body = buf.getvalue()

    with open(output_path, "wb") as f:
        f.write(b"\xef\xbb\xbf")  # UTF-8 BOM (matches Premiere's exports)
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b"<!DOCTYPE xmeml>\n")
        f.write(body)
    return rewritten


def match_clips_with_records(
    clips: list[dict], records: list[WavRecord], tolerance: int = 1,
    clip_dates: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Same as match_clips but also attaches the WavRecord (`_wavrec`) to
    each matched dict so the XML rewriter can do TC math."""
    cand_lists: list[list[WavRecord]] = []
    for c in clips:
        _, cands = find_wav_for_event(records, c["source_in"], c["source_out"], tolerance)
        cand_lists.append(cands)

    cohort: dict[str, Counter] = defaultdict(Counter)
    for c, cands in zip(clips, cand_lists):
        seen = set()
        for r in cands:
            if r.sound_roll and r.sound_roll not in seen:
                cohort[c["camera_roll"]][r.sound_roll] += 1
                seen.add(r.sound_roll)
    preferred = {cam: v.most_common(1)[0][0] for cam, v in cohort.items() if v}

    matched, unmatched = [], []
    for c, cands in zip(clips, cand_lists):
        if not cands:
            unmatched.append(c)
            continue
        date = _date_for_clip(c, clip_dates)
        pool = cands
        if date:
            dated = [r for r in cands if r.shoot_date == date]
            if dated:
                pool = dated
            else:
                # Strict: refuse to relink to a wrong-day WAV. Leave the
                # clip's original camera-daily reference in place so the
                # editor can see it failed instead of silently mis-linking.
                c2 = dict(c)
                c2["reason"] = f"no WAV with shoot_date={date} covers TC range"
                unmatched.append(c2)
                continue
        pref = preferred.get(c["camera_roll"])
        if pref:
            pref_pool = [r for r in pool if r.sound_roll == pref]
            if pref_pool:
                pool = pref_pool
        best = pool[0]
        out = dict(c)
        out["sound_roll"] = best.sound_roll
        out["wav"] = best.filename
        out["_wavrec"] = best
        out["candidates"] = len(cands)
        out["shoot_date"] = date
        matched.append(out)
    return matched, unmatched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("xml")
    p.add_argument("audio_folder")
    p.add_argument("--sequence", required=True,
                   help="exact name of the sequence to conform")
    p.add_argument("-o", "--output", required=True, help="output EDL path")
    p.add_argument("--report", help="optional JSON report path")
    p.add_argument("--fps", type=float, default=23.976)
    p.add_argument("--tolerance-frames", type=int, default=1)
    p.add_argument("--dedup", action="store_true",
                   help="EDL only: collapse to one event per unique take")
    p.add_argument("--format", choices=("edl", "xml"), default="edl",
                   help="Output format: edl (reference clips for EdiLoad → Pro Tools) "
                        "or xml (relinked FCP7 XML for Resolve / Premiere / Pro Tools)")
    p.add_argument("--dailies", help="optional dailies/ingest root (e.g. "
                   "K:\\...\\01_Ingest\\01_OCN). When provided, the matcher "
                   "constrains WAV candidates to those shot on the same date "
                   "as the daily that owns each camera clip.")
    args = p.parse_args()

    print(f"Parsing XML sequence {args.sequence!r}...", file=sys.stderr)
    clips, seq_tb, tree, target = parse_fcp7_xml(Path(args.xml), args.sequence)
    print(f"  {len(clips)} audio clips", file=sys.stderr)

    clip_dates = None
    wav_date_fallback = None
    if args.dailies:
        print(f"Scanning dailies root {args.dailies}...", file=sys.stderr)
        clip_dates, wav_date_fallback = scan_dailies(Path(args.dailies))
        print(f"  mapped {len(clip_dates)} clip IDs and {len(wav_date_fallback)} WAVs to dates",
              file=sys.stderr)

    print(f"Scanning {args.audio_folder}...", file=sys.stderr)
    def cb(i, total, name):
        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] {name}", file=sys.stderr)
    records = scan_audio_folder(Path(args.audio_folder), args.fps, progress_cb=cb,
                                wav_date_fallback=wav_date_fallback)
    print(f"  {len(records)} WAVs indexed", file=sys.stderr)

    if args.format == "xml":
        matched, unmatched = match_clips_with_records(
            clips, records, args.tolerance_frames, clip_dates=clip_dates,
        )
        n_written = emit_relinked_xml(tree, matched, Path(args.output))
    else:
        matched, unmatched = match_clips(
            clips, records, args.tolerance_frames, clip_dates=clip_dates,
        )
        n_written = emit_reference_edl(
            matched, Path(args.output), title=args.sequence, dedup=args.dedup,
        )

    print(
        f"\nXML audio clips: {len(clips)}\n"
        f"Matched: {len(matched)}\n"
        f"Unmatched: {len(unmatched)}\n"
        f"Unique reference events written: {n_written}\n"
        f"\nOutput EDL: {args.output}",
        file=sys.stderr,
    )

    if args.report:
        Path(args.report).write_text(json.dumps(dict(
            sequence=args.sequence,
            xml_clips=len(clips),
            matched_count=len(matched),
            unmatched_count=len(unmatched),
            unique_events=n_written,
            unmatched=[dict(camera_roll=u["camera_roll"],
                            file_name=u["file_name"],
                            source_in=_label_to_tc(u["source_in"]),
                            source_out=_label_to_tc(u["source_out"]))
                       for u in unmatched[:500]],
        ), indent=2), encoding="utf-8")
        print(f"Report: {args.report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
