#!/usr/bin/env python3
"""Validate audited-score-to-midi score IR v0.1.

The validator is intentionally independent from the MIDI compiler.  It uses
only the Python standard library and emits a machine-readable JSON report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCHEMA_VERSION = "0.1"
RATIONAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_MIDI_DELTA = 0x0FFFFFFF


class Reporter:
    def __init__(self) -> None:
        self.errors: List[Dict[str, str]] = []
        self.warnings: List[Dict[str, str]] = []

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append({"code": code, "path": path, "message": message})

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})


def as_object(value: Any, path: str, reporter: Reporter) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        reporter.error("TYPE_OBJECT", path, "must be a JSON object")
        return None
    return value


def as_list(value: Any, path: str, reporter: Reporter) -> Optional[List[Any]]:
    if not isinstance(value, list):
        reporter.error("TYPE_ARRAY", path, "must be a JSON array")
        return None
    return value


def required_string(obj: Dict[str, Any], key: str, path: str, reporter: Reporter) -> Optional[str]:
    value = obj.get(key)
    field = f"{path}.{key}"
    if not isinstance(value, str) or not value.strip():
        reporter.error("TYPE_STRING", field, "must be a non-empty string")
        return None
    return value


def required_int(
    obj: Dict[str, Any], key: str, path: str, reporter: Reporter,
    minimum: Optional[int] = None, maximum: Optional[int] = None,
) -> Optional[int]:
    value = obj.get(key)
    field = f"{path}.{key}"
    if isinstance(value, bool) or not isinstance(value, int):
        reporter.error("TYPE_INTEGER", field, "must be an integer")
        return None
    if minimum is not None and value < minimum:
        reporter.error("INTEGER_RANGE", field, f"must be >= {minimum}")
        return None
    if maximum is not None and value > maximum:
        reporter.error("INTEGER_RANGE", field, f"must be <= {maximum}")
        return None
    return value


def rational(value: Any, path: str, reporter: Reporter, *, positive: bool = False) -> Optional[Fraction]:
    if not isinstance(value, str):
        reporter.error("RATIONAL_STRING", path, "must be a rational string such as '0', '3/2', or '122'")
        return None
    if not RATIONAL_RE.fullmatch(value):
        reporter.error("RATIONAL_FORMAT", path, "must use non-negative integer or numerator/denominator syntax")
        return None
    result = Fraction(value)
    if positive and result <= 0:
        reporter.error("RATIONAL_POSITIVE", path, "must be greater than zero")
        return None
    return result


def is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def position_key(measure: int, onset: Fraction) -> Tuple[int, Fraction]:
    return measure, onset


def display_path(path: Path, include_local_paths: bool) -> str:
    return str(path.resolve()) if include_local_paths else path.name


def check_delta_gaps(
    ticks: Sequence[int], path: str, reporter: Reporter
) -> None:
    ordered = sorted(set(ticks))
    for previous, current in zip(ordered, ordered[1:]):
        gap = current - previous
        if gap > MAX_MIDI_DELTA:
            reporter.error(
                "MIDI_DELTA_TOO_LARGE",
                path,
                f"event gap {gap} ticks exceeds the four-byte MIDI VLQ limit "
                f"of {MAX_MIDI_DELTA}",
            )


def validate_ir(
    document: Any,
    ir_path: Path,
    *,
    allow_blocking: bool = False,
    allow_unresolved: bool = False,
    check_source_hash: bool = True,
    include_local_paths: bool = False,
) -> Dict[str, Any]:
    reporter = Reporter()
    provenance: Dict[str, Any] = {
        "ir_path": display_path(ir_path, include_local_paths),
        "source_hash_check_skipped": not check_source_hash,
        "local_paths_included": include_local_paths,
    }
    root = as_object(document, "$", reporter)
    summary: Dict[str, Any] = {}
    if root is None:
        return {"valid": False, "schema_version": None, "provenance": provenance,
                "privacy": {"local_paths_included": include_local_paths},
                "errors": reporter.errors, "warnings": reporter.warnings,
                "summary": summary}

    if root.get("schema_version") != SCHEMA_VERSION:
        reporter.error("SCHEMA_VERSION", "$.schema_version", f"must equal '{SCHEMA_VERSION}'")

    source = as_object(root.get("source"), "$.source", reporter)
    if source is not None:
        source_path = required_string(source, "path", "$.source", reporter)
        source_sha = required_string(source, "sha256", "$.source", reporter)
        provenance["source_path_declared"] = (
            source_path if include_local_paths or not source_path else Path(source_path).name
        )
        provenance["source_sha256_declared"] = source_sha.lower() if isinstance(source_sha, str) else None
        provenance["source_hash_verified"] = False
        if source_sha is not None and not SHA256_RE.fullmatch(source_sha):
            reporter.error("SHA256_FORMAT", "$.source.sha256", "must contain exactly 64 hexadecimal characters")
        if source_path and source_sha and SHA256_RE.fullmatch(source_sha) and check_source_hash:
            candidate = Path(source_path)
            if not candidate.is_absolute():
                candidate = ir_path.parent / candidate
            provenance["source_path_resolved"] = display_path(
                candidate, include_local_paths
            )
            if candidate.is_file():
                try:
                    digest = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(block)
                except OSError:
                    reporter.error(
                        "SOURCE_READ_ERROR",
                        "$.source.path",
                        "canonical source could not be read for SHA-256 verification",
                    )
                else:
                    actual = digest.hexdigest()
                    provenance["source_sha256_actual"] = actual
                    if actual.lower() != source_sha.lower():
                        reporter.error("SOURCE_HASH_MISMATCH", "$.source.sha256",
                                       f"declared {source_sha.lower()} but file hashes to {actual}")
                    else:
                        provenance["source_hash_verified"] = True
            else:
                reporter.error("SOURCE_NOT_FOUND", "$.source.path",
                               "source file is unavailable; audited validation requires SHA-256 verification")

    score = as_object(root.get("score"), "$.score", reporter)
    measure_count: Optional[int] = None
    ppq: Optional[int] = None
    meter_map: List[Tuple[int, int, int, str]] = []
    tempo_map: List[Tuple[int, Fraction, Fraction, str]] = []
    key_map: List[Tuple[int, int, str, str]] = []
    forbidden_score_fields = {
        "pickup", "pickup_quarters", "repeat_graph", "repeats", "navigation",
        "transposition", "percussion",
    }

    if score is not None:
        measure_count = required_int(score, "measure_count", "$.score", reporter, 1)
        ppq = required_int(score, "ppq", "$.score", reporter, 1, 32767)
        for field in sorted(forbidden_score_fields.intersection(score)):
            reporter.error("UNSUPPORTED_V01", f"$.score.{field}",
                           f"'{field}' is outside the v0.1 written-timeline scope")

        meters = as_list(score.get("meter_map"), "$.score.meter_map", reporter)
        if meters is not None:
            if not meters:
                reporter.error("METER_MAP_EMPTY", "$.score.meter_map", "must contain a meter at measure 1")
            seen_measures = set()
            previous_measure = 0
            for index, raw in enumerate(meters):
                path = f"$.score.meter_map[{index}]"
                item = as_object(raw, path, reporter)
                if item is None:
                    continue
                measure = required_int(item, "measure", path, reporter, 1, measure_count)
                numerator = required_int(item, "numerator", path, reporter, 1, 255)
                denominator = required_int(item, "denominator", path, reporter, 1, 1024)
                if "onset" in item:
                    meter_onset = rational(item.get("onset"), f"{path}.onset", reporter)
                    if meter_onset is not None and meter_onset != 0:
                        reporter.error("METER_ONSET", f"{path}.onset",
                                       "v0.1 meter changes must occur at measure onset '0'")
                if "clocks_per_click" in item:
                    required_int(item, "clocks_per_click", path, reporter, 0, 255)
                if "notated_32nds_per_quarter" in item:
                    required_int(item, "notated_32nds_per_quarter", path, reporter, 0, 255)
                if denominator is not None and not is_power_of_two(denominator):
                    reporter.error("METER_DENOMINATOR", f"{path}.denominator", "must be a power of two")
                if measure is not None:
                    if measure in seen_measures:
                        reporter.error("METER_DUPLICATE", f"{path}.measure", "duplicate meter change measure")
                    seen_measures.add(measure)
                    if measure < previous_measure:
                        reporter.warn("MAP_UNSORTED", path, "meter_map should be sorted by measure")
                    previous_measure = measure
                if measure is not None and numerator is not None and denominator is not None and is_power_of_two(denominator):
                    meter_map.append((measure, numerator, denominator, path))
            if meter_map and min(row[0] for row in meter_map) != 1:
                reporter.error("METER_START", "$.score.meter_map", "first active meter must start at measure 1")

        tempos = as_list(score.get("tempo_map"), "$.score.tempo_map", reporter)
        if tempos is not None:
            if not tempos:
                reporter.error("TEMPO_MAP_EMPTY", "$.score.tempo_map", "must contain a tempo at measure 1 onset 0")
            seen_positions = set()
            previous_position: Optional[Tuple[int, Fraction]] = None
            for index, raw in enumerate(tempos):
                path = f"$.score.tempo_map[{index}]"
                item = as_object(raw, path, reporter)
                if item is None:
                    continue
                measure = required_int(item, "measure", path, reporter, 1, measure_count)
                onset = rational(item.get("onset"), f"{path}.onset", reporter)
                bpm = rational(item.get("bpm"), f"{path}.bpm", reporter, positive=True)
                beat_unit = rational(item.get("beat_unit"), f"{path}.beat_unit", reporter, positive=True)
                if measure is not None and onset is not None:
                    pos = position_key(measure, onset)
                    if pos in seen_positions:
                        reporter.error("TEMPO_DUPLICATE", path, "duplicate tempo position")
                    seen_positions.add(pos)
                    if previous_position is not None and pos < previous_position:
                        reporter.warn("MAP_UNSORTED", path, "tempo_map should be sorted by measure and onset")
                    previous_position = pos
                if measure is not None and onset is not None and bpm is not None and beat_unit is not None:
                    quarter_bpm = bpm * beat_unit
                    tempo_map.append((measure, onset, quarter_bpm, path))
                    raw_tempo = Fraction(60_000_000, 1) / quarter_bpm
                    midi_tempo = (raw_tempo.numerator + raw_tempo.denominator // 2) // raw_tempo.denominator
                    if not 1 <= midi_tempo <= 0xFFFFFF:
                        reporter.error("TEMPO_MIDI_RANGE", f"{path}.bpm",
                                       "tempo cannot be represented by the 3-byte MIDI tempo value")
            if tempo_map and not any(row[0] == 1 and row[1] == 0 for row in tempo_map):
                reporter.error("TEMPO_START", "$.score.tempo_map", "a tempo must exist at measure 1 onset '0'")

        keys = as_list(score.get("key_map"), "$.score.key_map", reporter)
        if keys is not None:
            if not keys:
                reporter.error("KEY_MAP_EMPTY", "$.score.key_map", "must contain a key at measure 1")
            seen_measures = set()
            previous_measure = 0
            for index, raw in enumerate(keys):
                path = f"$.score.key_map[{index}]"
                item = as_object(raw, path, reporter)
                if item is None:
                    continue
                measure = required_int(item, "measure", path, reporter, 1, measure_count)
                fifths = required_int(item, "fifths", path, reporter, -7, 7)
                if "onset" in item:
                    key_onset = rational(item.get("onset"), f"{path}.onset", reporter)
                    if key_onset is not None and key_onset != 0:
                        reporter.error("KEY_ONSET", f"{path}.onset",
                                       "v0.1 key changes must occur at measure onset '0'")
                mode = item.get("mode")
                if mode not in ("major", "minor"):
                    reporter.error("KEY_MODE", f"{path}.mode", "must be 'major' or 'minor'")
                if measure is not None:
                    if measure in seen_measures:
                        reporter.error("KEY_DUPLICATE", f"{path}.measure", "duplicate key change measure")
                    seen_measures.add(measure)
                    if measure < previous_measure:
                        reporter.warn("MAP_UNSORTED", path, "key_map should be sorted by measure")
                    previous_measure = measure
                if measure is not None and fifths is not None and mode in ("major", "minor"):
                    key_map.append((measure, fifths, mode, path))
            if key_map and min(row[0] for row in key_map) != 1:
                reporter.error("KEY_START", "$.score.key_map", "first active key must start at measure 1")

    measure_starts: Dict[int, Fraction] = {}
    measure_lengths: Dict[int, Fraction] = {}
    total_quarters: Optional[Fraction] = None
    total_ticks: Optional[int] = None
    if measure_count is not None and meter_map and min(row[0] for row in meter_map) == 1:
        changes = {measure: (numerator, denominator) for measure, numerator, denominator, _ in meter_map}
        current: Optional[Tuple[int, int]] = None
        cursor = Fraction(0)
        for measure in range(1, measure_count + 1):
            if measure in changes:
                current = changes[measure]
            if current is None:
                break
            measure_starts[measure] = cursor
            length = Fraction(current[0] * 4, current[1])
            measure_lengths[measure] = length
            cursor += length
        if len(measure_starts) == measure_count:
            total_quarters = cursor
            summary["total_quarter_beats"] = fraction_text(cursor)
            if ppq is not None:
                ticks = cursor * ppq
                if ticks.denominator != 1:
                    reporter.error("PPQ_NOT_DIVISIBLE", "$.score.ppq",
                                   f"score end {fraction_text(cursor)} quarters is not representable at PPQ {ppq}")
                else:
                    summary["expected_end_tick"] = ticks.numerator
                    total_ticks = ticks.numerator
                for measure, start in measure_starts.items():
                    if (start * ppq).denominator != 1:
                        reporter.error("PPQ_NOT_DIVISIBLE", f"$.score.measure[{measure}]",
                                       f"measure {measure} start {fraction_text(start)} is not representable at PPQ {ppq}")

    if ppq is not None:
        for measure, _numerator, _denominator, path in meter_map:
            if measure in measure_starts and (measure_starts[measure] * ppq).denominator != 1:
                reporter.error("PPQ_NOT_DIVISIBLE", path,
                               f"meter event at measure {measure} is not representable at PPQ {ppq}")
        for measure, _fifths, _mode, path in key_map:
            if measure in measure_starts and (measure_starts[measure] * ppq).denominator != 1:
                reporter.error("PPQ_NOT_DIVISIBLE", path,
                               f"key event at measure {measure} is not representable at PPQ {ppq}")

    for measure, onset, _bpm, path in tempo_map:
        if measure in measure_lengths:
            if onset < 0 or onset >= measure_lengths[measure]:
                reporter.error("ONSET_OUTSIDE_MEASURE", f"{path}.onset",
                               f"must be within [0, {fraction_text(measure_lengths[measure])})")
            elif ppq is not None:
                absolute = measure_starts[measure] + onset
                if (absolute * ppq).denominator != 1:
                    reporter.error("PPQ_NOT_DIVISIBLE", f"{path}.onset",
                                   f"position is not representable at PPQ {ppq}")

    if ppq is not None and total_ticks is not None:
        conductor_ticks = [0, total_ticks]
        for measure, _numerator, _denominator, _path in meter_map:
            if measure in measure_starts:
                value = measure_starts[measure] * ppq
                if value.denominator == 1:
                    conductor_ticks.append(value.numerator)
        for measure, _fifths, _mode, _path in key_map:
            if measure in measure_starts:
                value = measure_starts[measure] * ppq
                if value.denominator == 1:
                    conductor_ticks.append(value.numerator)
        for measure, onset, _bpm, _path in tempo_map:
            if (
                measure in measure_lengths
                and 0 <= onset < measure_lengths[measure]
            ):
                value = (measure_starts[measure] + onset) * ppq
                if value.denominator == 1:
                    conductor_ticks.append(value.numerator)
        check_delta_gaps(conductor_ticks, "$.score", reporter)

    parts = as_list(root.get("parts"), "$.parts", reporter)
    part_ids = set()
    channels = set()
    note_count = 0
    per_part_intervals: Dict[str, Dict[int, List[Tuple[Fraction, Fraction, str]]]] = {}
    if parts is not None:
        if not parts:
            reporter.error("PARTS_EMPTY", "$.parts", "must contain at least one musical part")
        for part_index, raw_part in enumerate(parts):
            path = f"$.parts[{part_index}]"
            part = as_object(raw_part, path, reporter)
            if part is None:
                continue
            part_id = required_string(part, "id", path, reporter)
            required_string(part, "name", path, reporter)
            channel = required_int(part, "channel", path, reporter, 0, 15)
            required_int(part, "program", path, reporter, 0, 127)
            if part_id:
                if part_id in part_ids:
                    reporter.error("PART_ID_DUPLICATE", f"{path}.id", "part id must be unique")
                part_ids.add(part_id)
            if channel is not None:
                if channel == 9:
                    reporter.error("PERCUSSION_UNSUPPORTED", f"{path}.channel",
                                   "MIDI channel 9 percussion is outside the v0.1 scope")
                if channel in channels:
                    reporter.error("CHANNEL_REUSED", f"{path}.channel", "each v0.1 part must use a distinct MIDI channel")
                channels.add(channel)
            if "transposition" in part or part.get("is_percussion"):
                reporter.error("UNSUPPORTED_V01", path,
                               "transposing and percussion parts are outside the v0.1 scope")

            notes = as_list(part.get("notes"), f"{path}.notes", reporter)
            part_event_ticks = [0]
            interval_key = part_id or f"part-{part_index}"
            pitch_intervals: Dict[int, List[Tuple[Fraction, Fraction, str]]] = {}
            per_part_intervals[interval_key] = pitch_intervals
            if notes is None:
                continue
            for note_index, raw_note in enumerate(notes):
                note_count += 1
                note_path = f"{path}.notes[{note_index}]"
                note = as_object(raw_note, note_path, reporter)
                if note is None:
                    continue
                measure = required_int(note, "measure", note_path, reporter, 1, measure_count)
                onset = rational(note.get("onset"), f"{note_path}.onset", reporter)
                duration = rational(note.get("duration"), f"{note_path}.duration", reporter, positive=True)
                midi_note = required_int(note, "midi_note", note_path, reporter, 0, 127)
                required_int(note, "velocity", note_path, reporter, 1, 127)
                confidence = note.get("confidence")
                if confidence not in ("confirmed", "inferred", "unresolved"):
                    reporter.error("CONFIDENCE", f"{note_path}.confidence",
                                   "must be 'confirmed', 'inferred', or 'unresolved'")
                elif confidence == "inferred":
                    reporter.warn("NOTE_INFERRED", f"{note_path}.confidence", "note is an inference, not direct confirmation")
                elif confidence == "unresolved":
                    if allow_unresolved:
                        reporter.warn("NOTE_UNRESOLVED_ALLOWED", f"{note_path}.confidence",
                                      "unresolved note was allowed by CLI override")
                    else:
                        reporter.error("NOTE_UNRESOLVED", f"{note_path}.confidence",
                                       "resolve or remove the note before MIDI compilation")

                if "evidence" not in note:
                    reporter.warn("NOTE_WITHOUT_EVIDENCE", note_path, "audited notes should reference source evidence")
                else:
                    evidence = as_list(note.get("evidence"), f"{note_path}.evidence", reporter)
                    if evidence is not None:
                        if not evidence:
                            reporter.warn("NOTE_WITHOUT_EVIDENCE", f"{note_path}.evidence",
                                          "evidence list is empty")
                        for evidence_index, evidence_item in enumerate(evidence):
                            if not isinstance(evidence_item, dict):
                                reporter.error("EVIDENCE_OBJECT",
                                               f"{note_path}.evidence[{evidence_index}]",
                                               "must be an object")

                if measure is not None and onset is not None and duration is not None and measure in measure_lengths:
                    length = measure_lengths[measure]
                    if onset < 0 or onset >= length:
                        reporter.error("ONSET_OUTSIDE_MEASURE", f"{note_path}.onset",
                                       f"must be within [0, {fraction_text(length)})")
                        continue
                    start = measure_starts[measure] + onset
                    end = start + duration
                    if total_quarters is not None and end > total_quarters:
                        reporter.error("NOTE_AFTER_SCORE_END", f"{note_path}.duration",
                                       f"note ends at {fraction_text(end)}, after score end {fraction_text(total_quarters)}")
                    if ppq is not None:
                        start_ticks = start * ppq
                        duration_ticks = duration * ppq
                        if start_ticks.denominator != 1:
                            reporter.error("PPQ_NOT_DIVISIBLE", f"{note_path}.onset",
                                           f"note start is not representable at PPQ {ppq}")
                        if duration_ticks.denominator != 1:
                            reporter.error("PPQ_NOT_DIVISIBLE", f"{note_path}.duration",
                                           f"duration is not representable at PPQ {ppq}")
                        if (
                            start_ticks.denominator == 1
                            and duration_ticks.denominator == 1
                            and (total_quarters is None or end <= total_quarters)
                        ):
                            part_event_ticks.extend(
                                [
                                    start_ticks.numerator,
                                    start_ticks.numerator + duration_ticks.numerator,
                                ]
                            )
                    if midi_note is not None:
                        pitch_intervals.setdefault(midi_note, []).append((start, end, note_path))

            for midi_note, intervals in pitch_intervals.items():
                intervals.sort(key=lambda row: (row[0], row[1]))
                previous: Optional[Tuple[Fraction, Fraction, str]] = None
                for current in intervals:
                    if previous is not None and current[0] < previous[1]:
                        reporter.error("SAME_PITCH_OVERLAP", current[2],
                                       f"MIDI note {midi_note} overlaps {previous[2]} in the same part")
                    if previous is None or current[1] > previous[1]:
                        previous = current
            if total_ticks is not None:
                part_event_ticks.append(total_ticks)
                check_delta_gaps(part_event_ticks, path, reporter)

    uncertainties = as_list(root.get("uncertainties"), "$.uncertainties", reporter)
    if uncertainties is not None:
        for index, raw in enumerate(uncertainties):
            path = f"$.uncertainties[{index}]"
            item = as_object(raw, path, reporter)
            if item is None:
                continue
            required_string(item, "id", path, reporter)
            required_string(item, "description", path, reporter)
            severity = item.get("severity")
            if severity not in ("blocking", "disclosed"):
                reporter.error("UNCERTAINTY_SEVERITY", f"{path}.severity",
                               "must be 'blocking' or 'disclosed'")
            elif severity == "blocking":
                if allow_blocking:
                    reporter.warn("BLOCKING_UNCERTAINTY_ALLOWED", path,
                                  "blocking uncertainty was allowed by CLI override")
                else:
                    reporter.error("BLOCKING_UNCERTAINTY", path,
                                   "resolve this uncertainty before compiling MIDI")
            else:
                reporter.warn("DISCLOSED_UNCERTAINTY", path, "non-blocking uncertainty is disclosed")

    summary.update({
        "measure_count": measure_count,
        "ppq": ppq,
        "part_count": len(parts) if isinstance(parts, list) else 0,
        "note_count": note_count,
        "tempo_event_count": len(tempo_map),
        "meter_event_count": len(meter_map),
        "key_event_count": len(key_map),
    })
    return {
        "valid": not reporter.errors,
        "schema_version": root.get("schema_version"),
        "provenance": provenance,
        "privacy": {"local_paths_included": include_local_paths},
        "errors": reporter.errors,
        "warnings": reporter.warnings,
        "summary": summary,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate audited score IR v0.1")
    parser.add_argument("score_ir", type=Path, help="path to score IR JSON")
    parser.add_argument("--output", "--report", dest="output", type=Path,
                        help="write the JSON report to this path")
    parser.add_argument("--allow-blocking-uncertainties", action="store_true",
                        help="downgrade blocking uncertainties to warnings")
    parser.add_argument("--allow-unresolved", action="store_true",
                        help="downgrade unresolved notes to warnings")
    parser.add_argument("--no-source-hash-check", action="store_true",
                        help="skip source accessibility and SHA-256 verification for diagnostics only")
    parser.add_argument("--include-local-paths", action="store_true",
                        help="include absolute IR and source paths in the audit")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        raw_ir = args.score_ir.read_bytes()
        document = json.loads(raw_ir.decode("utf-8"))
    except FileNotFoundError:
        report = {"valid": False, "schema_version": None,
                  "errors": [{"code": "IR_NOT_FOUND", "path": "$",
                              "message": "file not found: " +
                              display_path(args.score_ir, args.include_local_paths)}],
                  "privacy": {"local_paths_included": args.include_local_paths},
                  "warnings": [], "summary": {}}
    except OSError as exc:
        report = {"valid": False, "schema_version": None,
                  "errors": [{"code": "IR_READ_ERROR", "path": "$",
                              "message": type(exc).__name__ + ": unable to read " +
                              display_path(args.score_ir, args.include_local_paths)}],
                  "privacy": {"local_paths_included": args.include_local_paths},
                  "warnings": [], "summary": {}}
    except (UnicodeError, json.JSONDecodeError) as exc:
        report = {"valid": False, "schema_version": None,
                  "errors": [{"code": "IR_READ_ERROR", "path": "$", "message": str(exc)}],
                  "privacy": {"local_paths_included": args.include_local_paths},
                  "warnings": [], "summary": {}}
    else:
        report = validate_ir(
            document,
            args.score_ir.resolve(),
            allow_blocking=args.allow_blocking_uncertainties,
            allow_unresolved=args.allow_unresolved,
            check_source_hash=not args.no_source_hash_check,
            include_local_paths=args.include_local_paths,
        )
        report.setdefault("provenance", {})["ir_sha256"] = hashlib.sha256(raw_ir).hexdigest()

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
