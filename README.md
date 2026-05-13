# RollPull — EDL Sound Roll Conformer

A small desktop app that rewrites a picture-edit EDL's **Roll Name** field
using the **Sound Roll** metadata read from each referenced production WAV.
The output EDL is intended to be loaded into **EdiLoad v4** so that
*Match Roll Names* succeeds and you can export Conform Reference Tracks for
Pro Tools field-recorder expansion.

## Why

EdiLoad's *Match Roll Names* compares the EDL's Roll Name column against the
**Sound Roll** field embedded in each WAV's BWF/iXML metadata. When the
picture edit was cut against camera audio, the EDL's Roll Name column is full
of camera rolls (`A017C006_251211_ANX9`, etc.) and the match fails for every
event. RollPull fixes this by:

1. Walking your production audio folder recursively.
2. Reading the iXML `<TAPE>` element (with a BEXT `OriginatorReference`
   fallback) on every WAV.
3. For each EDL event whose `* FROM CLIP NAME:` (or `* TO CLIP NAME:` for
   dissolves) references one of those WAV files, replacing the Roll Name
   field on the event line with the WAV's Sound Roll.

Music, SFX library cues, MOS shots, and any event whose clip name is not a
`.wav` file are left untouched.

## Install

```bash
pip install PySide6 wavinfo
```

Tested with Python 3.11+. On Apple Silicon Mac with Conda:

```bash
conda create -n rollpull python=3.12
conda activate rollpull
pip install PySide6 wavinfo
```

## Run (GUI)

```bash
python3 edl_sound_roll_gui.py
```

1. **EDL file** — pick the picture-edit audio EDL.
2. **Production audio folder** — pick the top-level folder containing all
   production WAVs (subfolders OK; it scans recursively).
3. **Output EDL** — optional. Defaults to `<input>_RollPull.edl` next to the
   original.
4. **Run.** The Log panel shows progress, summary, and any unmatched events.
5. **Save Report** writes a JSON report of matched / unmatched / no-roll
   events for spot-checking against EdiLoad.

## Run (CLI)

```bash
python3 edl_sound_roll_core.py /path/to/picture.edl /path/to/audio_folder \
    -o /path/to/output.edl --report /path/to/report.json
```

## EdiLoad v4 workflow

1. Open EdiLoad v4.
2. **Open List** → choose the rewritten EDL (`*_RollPull.edl`).
3. **WAV File List** → **Load Folder** → choose the same production audio
   folder. Let it scan.
4. With the WAV list still open, run **Match Roll Names** (Window menu).
   Events whose WAVs were found and tagged should turn green.
5. Clean the list (remove duplicates, irrelevant events), then **Export →
   Pro Tools Session** (Conform Reference Tracks).
6. In Pro Tools: import the session, drop all production WAVs into the
   Workspace, select the reference clips, then **Clip → Field Recorder
   Channels → Match Criteria** (timecode + roll), then **Expand Channels to
   New Tracks**. All iso channels (MixL, MixR, Boom, lavs) land at the cut.

## Notes / gotchas

- **Filename matching is case-insensitive.** EDL `3T06_6.WAV` will match a
  file named `3t06_6.wav`.
- **Sound Roll source priority:**
    1. iXML `<TAPE>` (where Sound Devices, Zaxcom, etc. write it)
    2. BEXT `OriginatorReference` (some older recorders)
    3. None → event is reported in the **no_roll** list and left untouched.
- **Duplicate filenames** across folders are tolerated; the first one
  encountered wins. If you have two `3T06_1.WAV` files with different
  metadata, organize your audio folder so only the canonical copy is in the
  scan path.
- **Dissolves** (two event lines sharing an event number with FROM/TO clip
  names) are handled — the first event line uses FROM, the second uses TO.
- **Channel-split files** (your case — `3T06_1.WAV` channel 1, `3T06_2.WAV`
  channel 2 …) are matched verbatim. The EDL references the specific channel
  filename and gets that channel's Sound Roll, which is correct because all
  channels of the same take share a Sound Roll anyway.

## Files

- `edl_sound_roll_core.py` — pure-Python core. Importable as a library, also
  runs as a CLI. No GUI dependencies.
- `edl_sound_roll_gui.py` — PySide6 GUI on top of the core.
- `README.md` — this file.

## License

Use freely for Exponent Films projects and beyond.
