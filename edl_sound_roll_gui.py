#!/usr/bin/env python3
"""
RollPull — EDL Sound Roll Conformer (GUI)

Two tabs:
  1. EDL Roll Pull — rewrite a CMX3600 EDL's Roll Name column from each
     referenced WAV's iXML/BWF Sound Roll metadata.
  2. XML TC Conform — read a Premiere FCP7 XML, match every audio clip
     against a production audio folder by BWF timecode (+ camera-roll
     cohort vote to disambiguate TC overlaps across days), and emit a
     deduplicated reference EDL suitable for EdiLoad → Pro Tools field
     recorder expansion.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QStatusBar, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from edl_sound_roll_core import (
    RewriteResult, rewrite_edl, scan_audio_folder as scan_for_rollpull,
)
from edl_tc_conform_core import scan_audio_folder as scan_for_tc, scan_dailies
from edl_xml_conform import (
    parse_fcp7_xml, match_clips, match_clips_with_records,
    emit_reference_edl, emit_relinked_xml,
)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class RollPullWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_ok = Signal(object)        # list[RewriteResult]
    failed = Signal(str)

    def __init__(self, edl_paths: list[Path], audio_folder: Path,
                 output_path: Path | None):
        super().__init__()
        self.edl_paths = edl_paths
        self.audio_folder = audio_folder
        # output_path is only honoured when there's exactly one EDL; for
        # batch runs each EDL gets its own auto-named output next to source.
        self.output_path = output_path

    def run(self):
        try:
            self.log.emit(f"Scanning {self.audio_folder} for WAVs...")
            wav_index = scan_for_rollpull(
                self.audio_folder,
                progress_cb=lambda i, t, n: self.progress.emit(i, t, n),
            )
            self.log.emit(f"Indexed {len(wav_index)} WAVs.")
            with_roll = sum(1 for r in wav_index.values() if r.sound_roll)
            self.log.emit(f"  With Sound Roll: {with_roll}  Without: {len(wav_index) - with_roll}")

            results = []
            n = len(self.edl_paths)
            single_out = self.output_path if n == 1 else None
            for i, edl in enumerate(self.edl_paths, 1):
                self.log.emit(f"\n[{i}/{n}] Rewriting EDL: {edl.name}")
                result = rewrite_edl(edl, wav_index, output_path=single_out)
                self.log.emit(
                    f"  events: {result.total_events}, wav-ref: {result.wav_referenced_events}, "
                    f"rewritten: {result.rewritten_events}, unmatched: {len(result.unmatched)}"
                )
                results.append(result)
            self.finished_ok.emit(results)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")


class XmlConformWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, xml_path: Path, audio_folder: Path, output_path: Path,
                 sequence: str | None, dedup: bool = False, fmt: str = "edl",
                 dailies_folder: Path | None = None):
        super().__init__()
        self.xml_path = xml_path
        self.audio_folder = audio_folder
        self.output_path = output_path
        self.sequence = sequence
        self.dedup = dedup
        self.fmt = fmt
        self.dailies_folder = dailies_folder

    def run(self):
        try:
            self.log.emit(f"Parsing XML: {self.xml_path.name}")
            seq = self.sequence
            if not seq:
                # auto-pick the sequence with the most audio clips
                import xml.etree.ElementTree as ET
                root = ET.parse(self.xml_path).getroot()
                best, best_n = None, -1
                for s in root.findall(".//sequence"):
                    n = sum(len(t.findall("clipitem"))
                            for t in (s.find("media/audio") or ET.Element("x")).findall("track"))
                    if n > best_n:
                        best_n, best = n, s.findtext("name") or ""
                seq = best
                self.log.emit(f"Auto-selected sequence: {seq!r} ({best_n} audio clips)")

            clips, _, tree, target = parse_fcp7_xml(self.xml_path, seq)
            self.log.emit(f"  parsed {len(clips)} audio clipitems")

            # Scan dailies FIRST so the audio scan can fall back to its
            # wav→date map for any roll missing a sound report CSV.
            clip_dates = None
            wav_date_fallback = None
            if self.dailies_folder:
                self.log.emit(f"Scanning dailies root {self.dailies_folder}...")
                clip_dates, wav_date_fallback = scan_dailies(self.dailies_folder)
                self.log.emit(
                    f"  mapped {len(clip_dates)} camera clip IDs and "
                    f"{len(wav_date_fallback)} WAVs to shoot dates"
                )

            self.log.emit(f"\nScanning {self.audio_folder} for WAVs...")
            records = scan_for_tc(
                self.audio_folder, 23.976,
                progress_cb=lambda i, t, n: self.progress.emit(i, t, n),
                wav_date_fallback=wav_date_fallback,
            )
            self.log.emit(f"  indexed {len(records)} WAVs")

            self.log.emit("\nMatching clips by TC + camera-roll cohort vote"
                          + (" + shoot-date constraint" if clip_dates else "") + "...")
            if self.fmt == "xml":
                matched, unmatched = match_clips_with_records(
                    clips, records, tolerance=1, clip_dates=clip_dates,
                )
            else:
                matched, unmatched = match_clips(
                    clips, records, tolerance=1, clip_dates=clip_dates,
                )
            date_rejected = sum(1 for u in unmatched if u.get("reason"))
            tc_rejected   = len(unmatched) - date_rejected
            self.log.emit(
                f"  matched: {len(matched)}  unmatched: {len(unmatched)} "
                f"({tc_rejected} no TC match, {date_rejected} wrong shoot day)"
            )

            if self.fmt == "xml":
                n = emit_relinked_xml(tree, matched, self.output_path)
                self.log.emit(f"  wrote relinked XML ({n} clipitems rewritten)")
            else:
                n = emit_reference_edl(
                    matched, self.output_path, title=seq or "Reference",
                    dedup=self.dedup,
                )
                self.log.emit(f"  wrote reference EDL ({n} events)")
            self.finished_ok.emit(dict(
                sequence=seq,
                fmt=self.fmt,
                xml_clips=len(clips),
                matched=matched,
                unmatched=unmatched,
                unique_events=n,
                output_path=self.output_path,
            ))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------

def _mono_font() -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(10)
    return f


class _BaseTab(QWidget):
    """Common scaffolding: file-row helpers, log panel, progress, run/cancel."""

    def __init__(self):
        super().__init__()
        self.worker: QThread | None = None
        self.last_result = None
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 12, 12, 12)
        self._root.setSpacing(10)

    def _add_inputs_group(self, title: str) -> QGridLayout:
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self._root.addWidget(box)
        return grid

    def _add_run_row(self, on_run, on_cancel, on_save_report) -> None:
        row = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.setMinimumHeight(34)
        bf = self.run_btn.font(); bf.setBold(True); self.run_btn.setFont(bf)
        self.run_btn.clicked.connect(on_run)
        row.addWidget(self.run_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMinimumHeight(34)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(on_cancel)
        row.addWidget(self.cancel_btn)

        self.save_report_btn = QPushButton("Save Report (JSON)…")
        self.save_report_btn.setMinimumHeight(34)
        self.save_report_btn.setEnabled(False)
        self.save_report_btn.clicked.connect(on_save_report)
        row.addWidget(self.save_report_btn)
        row.addStretch(1)
        self._root.addLayout(row)

    def _add_progress_and_log(self):
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self._root.addWidget(self.progress)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #888;")
        self._root.addWidget(self.progress_label)

        log_box = QGroupBox("Log")
        lay = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(_mono_font())
        lay.addWidget(self.log_view)
        self._root.addWidget(log_box, 1)

    def _log(self, msg: str):
        self.log_view.moveCursor(QTextCursor.End)
        self.log_view.insertPlainText(msg + "\n")
        self.log_view.moveCursor(QTextCursor.End)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, name: str):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        self.progress_label.setText(f"[{current}/{total}] {name}")

    @Slot(str)
    def _on_failed(self, msg: str):
        self._log("\n!!! ERROR !!!\n" + msg)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_label.setText("Failed.")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        QMessageBox.critical(self, "Error", msg.splitlines()[0])

    def _start_run(self):
        self.log_view.clear()
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress_label.setText("Starting…")
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.save_report_btn.setEnabled(False)
        self.last_result = None

    def _end_run(self):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self._log("Cancelled.")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress_label.setText("Cancelled.")
            self._end_run()


# ---------------------------------------------------------------------------
# Tab 1 — EDL Roll Pull
# ---------------------------------------------------------------------------

class RollPullTab(_BaseTab):

    def __init__(self):
        super().__init__()
        grid = self._add_inputs_group("Inputs")

        grid.addWidget(QLabel("EDL files:"), 0, 0, Qt.AlignTop)
        self.edl_list = QListWidget()
        self.edl_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.edl_list.setMinimumHeight(90)
        grid.addWidget(self.edl_list, 0, 1)
        btn_col = QVBoxLayout()
        add_btn = QPushButton("Add…"); add_btn.clicked.connect(self._add_edls)
        rm_btn  = QPushButton("Remove"); rm_btn.clicked.connect(self._remove_edls)
        clear_btn = QPushButton("Clear"); clear_btn.clicked.connect(self.edl_list.clear)
        btn_col.addWidget(add_btn); btn_col.addWidget(rm_btn); btn_col.addWidget(clear_btn)
        btn_col.addStretch(1)
        col_holder = QWidget(); col_holder.setLayout(btn_col)
        grid.addWidget(col_holder, 0, 2)

        grid.addWidget(QLabel("Production audio folder:"), 1, 0)
        self.audio_edit = QLineEdit()
        self.audio_edit.setPlaceholderText("Top-level folder of production WAVs (scanned recursively)")
        grid.addWidget(self.audio_edit, 1, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_audio); grid.addWidget(b, 1, 2)

        grid.addWidget(QLabel("Output EDL (optional):"), 2, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Single EDL only — batch mode auto-names <input>_RollPull.edl next to source")
        grid.addWidget(self.output_edit, 2, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_output); grid.addWidget(b, 2, 2)

        self._add_run_row(self._run, self._cancel, self._save_report)
        self._add_progress_and_log()

    def _add_edls(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select EDL files", "", "EDL (*.edl);;All (*)"
        )
        existing = {self.edl_list.item(i).text() for i in range(self.edl_list.count())}
        for p in paths:
            if p not in existing:
                self.edl_list.addItem(p)

    def _remove_edls(self):
        for item in self.edl_list.selectedItems():
            self.edl_list.takeItem(self.edl_list.row(item))

    def _pick_audio(self):
        p = QFileDialog.getExistingDirectory(self, "Select production audio folder")
        if p: self.audio_edit.setText(p)

    def _pick_output(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save output EDL as", "", "EDL (*.edl)")
        if p: self.output_edit.setText(p)

    def _run(self):
        edl_paths = [Path(self.edl_list.item(i).text())
                     for i in range(self.edl_list.count())]
        audio_str = self.audio_edit.text().strip()
        out_str = self.output_edit.text().strip()
        if not edl_paths:
            QMessageBox.warning(self, "Missing EDLs", "Add at least one EDL file."); return
        bad = [p for p in edl_paths if not p.is_file()]
        if bad:
            QMessageBox.warning(self, "EDL not found", "\n".join(str(p) for p in bad)); return
        if not audio_str or not Path(audio_str).is_dir():
            QMessageBox.warning(self, "Missing audio folder", "Choose a valid audio folder."); return
        if out_str and len(edl_paths) > 1:
            QMessageBox.information(
                self, "Output path ignored",
                "Batch mode auto-names outputs; the Output EDL field is only used when a single EDL is queued."
            )
            out_str = ""

        self._start_run()
        self.worker = RollPullWorker(
            edl_paths, Path(audio_str), Path(out_str) if out_str else None
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    @Slot(object)
    def _on_finished_ok(self, results: list):
        self.last_result = results
        total_events = sum(r.total_events for r in results)
        rewrites = sum(r.rewritten_events for r in results)
        unmatched = sum(len(r.unmatched) for r in results)
        no_roll = sum(len(r.no_roll) for r in results)
        self._log(
            f"\n=== Batch summary ({len(results)} EDL{'s' if len(results)!=1 else ''}) ===\n"
            f"Total events: {total_events}\n"
            f"Event lines rewritten: {rewrites}\n"
            f"Unmatched: {unmatched}  No-roll: {no_roll}"
        )
        for r in results:
            self._log(
                f"  • {r.output_path.name}: rewrote {r.rewritten_events}, "
                f"unmatched {len(r.unmatched)}, no-roll {len(r.no_roll)}"
            )
        self.progress.setRange(0, 100); self.progress.setValue(100)
        self.progress_label.setText("Done.")
        self._end_run()
        self.save_report_btn.setEnabled(True)

    def _save_report(self):
        if not self.last_result: return
        results = self.last_result
        default = results[0].output_path.parent / "RollPull_batch_report.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report", str(default), "JSON (*.json)",
        )
        if not path: return
        data = dict(runs=[
            dict(
                output=str(r.output_path),
                total_events=r.total_events,
                wav_referenced_events=r.wav_referenced_events,
                rewritten_events=r.rewritten_events,
                matched=r.matched,
                unmatched=r.unmatched,
                no_roll=r.no_roll,
            ) for r in results
        ])
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tab 2 — XML TC Conform
# ---------------------------------------------------------------------------

class XmlConformTab(_BaseTab):

    def __init__(self):
        super().__init__()
        grid = self._add_inputs_group("Inputs")

        grid.addWidget(QLabel("FCP7 XML file:"), 0, 0)
        self.xml_edit = QLineEdit()
        self.xml_edit.setPlaceholderText("Premiere export: File → Export → Final Cut Pro XML")
        grid.addWidget(self.xml_edit, 0, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_xml); grid.addWidget(b, 0, 2)

        grid.addWidget(QLabel("Production audio folder:"), 1, 0)
        self.audio_edit = QLineEdit()
        self.audio_edit.setPlaceholderText("Top-level folder of production WAVs (scanned recursively)")
        grid.addWidget(self.audio_edit, 1, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_audio); grid.addWidget(b, 1, 2)

        grid.addWidget(QLabel("Dailies root (optional):"), 2, 0)
        self.dailies_edit = QLineEdit()
        self.dailies_edit.setPlaceholderText(
            "Ingest root containing HRFD_YYMMDD_<DAY>/EDITORIALS/... — enables "
            "shoot-date constraint to disambiguate same-TC takes across days"
        )
        grid.addWidget(self.dailies_edit, 2, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_dailies); grid.addWidget(b, 2, 2)

        grid.addWidget(QLabel("Sequence name (optional):"), 3, 0)
        self.seq_edit = QLineEdit()
        self.seq_edit.setPlaceholderText("Leave blank to auto-pick the sequence with the most audio clips")
        grid.addWidget(self.seq_edit, 3, 1, 1, 2)

        fmt_row = QHBoxLayout()
        self.fmt_xml = QRadioButton("Relinked XML  (recommended — full multi-track, Resolve / Premiere / Pro Tools)")
        self.fmt_edl = QRadioButton("Reference EDL  (single-track, for EdiLoad → Pro Tools field-recorder expand)")
        self.fmt_xml.setChecked(True)
        self.fmt_group = QButtonGroup(self)
        self.fmt_group.addButton(self.fmt_xml); self.fmt_group.addButton(self.fmt_edl)
        fmt_row.addWidget(self.fmt_xml); fmt_row.addWidget(self.fmt_edl); fmt_row.addStretch(1)
        fmt_wrap = QWidget(); fmt_wrap.setLayout(fmt_row)
        grid.addWidget(fmt_wrap, 4, 1, 1, 2)

        grid.addWidget(QLabel("Output file:"), 5, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("/path/to/output.xml (or .edl)")
        grid.addWidget(self.output_edit, 5, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_output); grid.addWidget(b, 5, 2)

        self.dedup_check = QCheckBox(
            "EDL only: deduplicate by take (off = preserve every timeline cut). "
            "XML mode always preserves the full timeline."
        )
        self.dedup_check.setChecked(False)
        grid.addWidget(self.dedup_check, 6, 0, 1, 3)

        self._add_run_row(self._run, self._cancel, self._save_report)
        self._add_progress_and_log()

    def _pick_xml(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select Premiere XML", "", "XML (*.xml);;All (*)")
        if p: self.xml_edit.setText(p)

    def _pick_audio(self):
        p = QFileDialog.getExistingDirectory(self, "Select production audio folder")
        if p: self.audio_edit.setText(p)

    def _pick_dailies(self):
        p = QFileDialog.getExistingDirectory(self, "Select dailies / ingest root (contains HRFD_*)")
        if p: self.dailies_edit.setText(p)

    def _pick_output(self):
        if self.fmt_xml.isChecked():
            p, _ = QFileDialog.getSaveFileName(self, "Save relinked XML as", "", "FCP7 XML (*.xml)")
        else:
            p, _ = QFileDialog.getSaveFileName(self, "Save reference EDL as", "", "EDL (*.edl)")
        if p: self.output_edit.setText(p)

    def _run(self):
        xml_str = self.xml_edit.text().strip()
        audio_str = self.audio_edit.text().strip()
        out_str = self.output_edit.text().strip()
        seq_str = self.seq_edit.text().strip() or None
        if not xml_str or not Path(xml_str).is_file():
            QMessageBox.warning(self, "Missing XML", "Choose a valid XML file."); return
        if not audio_str or not Path(audio_str).is_dir():
            QMessageBox.warning(self, "Missing audio folder", "Choose a valid audio folder."); return
        if not out_str:
            QMessageBox.warning(self, "Missing output", "Choose where to save the reference EDL."); return

        self._start_run()
        fmt = "xml" if self.fmt_xml.isChecked() else "edl"
        dailies_str = self.dailies_edit.text().strip()
        dailies = Path(dailies_str) if dailies_str else None
        if dailies and not dailies.is_dir():
            QMessageBox.warning(self, "Bad dailies path", f"Not a directory: {dailies}"); return
        self.worker = XmlConformWorker(
            Path(xml_str), Path(audio_str), Path(out_str), seq_str,
            dedup=self.dedup_check.isChecked(), fmt=fmt, dailies_folder=dailies,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    @Slot(object)
    def _on_finished_ok(self, result: dict):
        self.last_result = result
        unit = "clipitems rewritten" if result.get("fmt") == "xml" else "reference events written"
        self._log(
            f"\n=== Summary ===\n"
            f"Sequence: {result['sequence']}\n"
            f"XML audio clips parsed: {result['xml_clips']}\n"
            f"Matched: {len(result['matched'])}\n"
            f"Unmatched: {len(result['unmatched'])}\n"
            f"{result['unique_events']} {unit}\n"
            f"\nOutput: {result['output_path']}"
        )
        if result['unmatched']:
            self._log("\n--- Unmatched (first 30) ---")
            for u in result['unmatched'][:30]:
                self._log(f"  {u.get('camera_roll',''):<30}  {u.get('file_name','')}")
            if len(result['unmatched']) > 30:
                self._log(f"  ... and {len(result['unmatched']) - 30} more")
        self.progress.setRange(0, 100); self.progress.setValue(100)
        self.progress_label.setText("Done.")
        self._end_run()
        self.save_report_btn.setEnabled(True)

    def _save_report(self):
        if not self.last_result: return
        out_path = Path(self.last_result["output_path"])
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report",
            str(out_path.with_suffix(".report.json")),
            "JSON (*.json)",
        )
        if not path: return
        # Strip non-serialisable WavRecord refs from matched dicts
        def clean(rows):
            out = []
            for r in rows:
                d = {k: v for k, v in r.items() if not hasattr(v, "__dataclass_fields__")}
                out.append(d)
            return out
        data = dict(
            output=str(out_path),
            sequence=self.last_result["sequence"],
            xml_clips=self.last_result["xml_clips"],
            unique_events=self.last_result["unique_events"],
            matched=clean(self.last_result["matched"]),
            unmatched=clean(self.last_result["unmatched"]),
        )
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AudioTurnoverFixerupper")
        self.resize(960, 760)

        tabs = QTabWidget()
        tabs.addTab(RollPullTab(), "EDL Roll Pull")
        tabs.addTab(XmlConformTab(), "XML TC Conform")
        self.setCentralWidget(tabs)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready.")


THEME_QSS = """
QWidget { background-color: #0B0712; color: #F3E8FF; }
QMainWindow { background-color: #0B0712; }
QGroupBox {
    border: 1px solid #6B21A8; border-radius: 6px;
    margin-top: 10px; padding-top: 12px;
    font-weight: bold;
    background-color: #15091F;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: #F472B6;
}
QPushButton {
    background-color: #9333EA; color: white;
    border: none; border-radius: 4px; padding: 6px 14px;
    font-weight: 600;
}
QPushButton:hover { background-color: #C026D3; }
QPushButton:pressed { background-color: #6B21A8; }
QPushButton:disabled { background-color: #2D1B47; color: #6B5B8A; }
QLineEdit, QTextEdit, QListWidget {
    background-color: #1A0B2E; color: #F3E8FF;
    border: 1px solid #6B21A8; border-radius: 4px; padding: 4px;
    selection-background-color: #EC4899; selection-color: black;
}
QLineEdit:focus, QTextEdit:focus, QListWidget:focus { border: 1px solid #EC4899; }
QListWidget::item { padding: 2px 4px; }
QListWidget::item:selected { background-color: #EC4899; color: black; }
QTabWidget::pane {
    border: 1px solid #6B21A8; border-radius: 4px;
    background: #15091F; top: -1px;
}
QTabBar::tab {
    background-color: #1A0B2E; color: #C084FC;
    padding: 8px 18px; margin-right: 2px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    font-weight: 600;
}
QTabBar::tab:selected { background-color: #EC4899; color: black; }
QTabBar::tab:hover:!selected { background-color: #6B21A8; color: white; }
QProgressBar {
    border: 1px solid #6B21A8; border-radius: 4px;
    text-align: center; background-color: #1A0B2E; color: #F3E8FF;
}
QProgressBar::chunk { background-color: #EC4899; }
QStatusBar { background-color: #15091F; color: #C084FC; }
QLabel { background: transparent; color: #F3E8FF; }
QCheckBox { background: transparent; color: #F3E8FF; spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #9333EA; border-radius: 3px;
    background-color: #1A0B2E;
}
QCheckBox::indicator:checked { background-color: #EC4899; border: 1px solid #EC4899; }
QScrollBar:vertical { background: #15091F; width: 12px; border: none; }
QScrollBar::handle:vertical { background: #6B21A8; border-radius: 6px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #EC4899; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #15091F; height: 12px; border: none; }
QScrollBar::handle:horizontal { background: #6B21A8; border-radius: 6px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #EC4899; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AudioTurnoverFixerupper")
    app.setStyleSheet(THEME_QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
