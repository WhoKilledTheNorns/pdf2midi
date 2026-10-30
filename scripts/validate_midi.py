#!/usr/bin/env python3
"""Independently parse and validate a Standard MIDI file, optionally against score IR v0.1."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


class MidiParseError(ValueError):
    pass


RATIONAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?$")


def read_vlq(data: bytes, position: int, limit: int) -> Tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= limit:
            raise MidiParseError("truncated VLQ")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, position
    raise MidiParseError("VLQ exceeds four bytes")


def channel_data_length(status: int) -> int:
    high = status & 0xF0
    if high in (0xC0, 0xD0):
        return 1
    if 0x80 <= high <= 0xE0:
        return 2
    raise MidiParseError(f"unsupported MIDI status 0x{status:02X}")


def parse_track(payload: bytes, track_index: int) -> Dict[str, Any]:
    position = 0
    tick = 0
    running_status: Optional[int] = None
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    eot_positions: List[Tuple[int, int]] = []
    used_running_status = 0

    while position < len(payload):
        try:
            delta, position = read_vlq(payload, position, len(payload))
            tick += delta
            if position >= len(payload):
                raise MidiParseError("delta time is not followed by an event")
            first = payload[position]
            if first < 0x80:
                if running_status is None:
                    raise MidiParseError("data byte encountered without running status")
                status = running_status
                used_running_status += 1
            else:
                status = first
                position += 1
                if 0x80 <= status <= 0xEF:
                    running_status = status
                else:
                    running_status = None

            if status == 0xFF:
                if position >= len(payload):
                    raise MidiParseError("truncated meta event type")
                meta_type = payload[position]
                position += 1
                length, position = read_vlq(payload, position, len(payload))
                if position + length > len(payload):
                    raise MidiParseError("truncated meta event payload")
                body = payload[position:position + length]
                position += length
                events.append({"tick": tick, "kind": "meta", "type": meta_type, "data": body})
                if meta_type == 0x2F:
                    eot_positions.append((tick, position))
            elif status in (0xF0, 0xF7):
                length, position = read_vlq(payload, position, len(payload))
                if position + length > len(payload):
                    raise MidiParseError("truncated SysEx payload")
                body = payload[position:position + length]
                position += length
                events.append({"tick": tick, "kind": "sysex", "status": status, "data": body})
            elif 0x80 <= status <= 0xEF:
                length = channel_data_length(status)
                if position + length > len(payload):
                    raise MidiParseError("truncated channel message")
                body = payload[position:position + length]
                if any(byte >= 0x80 for byte in body):
                    raise MidiParseError("status byte found where channel data was expected")
                position += length
                events.append({"tick": tick, "kind": "midi", "status": status, "data": body})
            else:
                raise MidiParseError(f"unsupported system status 0x{status:02X}")
        except MidiParseError as exc:
            errors.append(f"track {track_index}: {exc} at byte {position}")
            break

    if len(eot_positions) != 1:
        errors.append(f"track {track_index}: expected exactly one End-of-Track, found {len(eot_positions)}")
    elif eot_positions[0][1] != len(payload):
        errors.append(f"track {track_index}: End-of-Track is not the final event")
    if eot_positions:
        eot_event = [event for event in events if event["kind"] == "meta" and event["type"] == 0x2F][0]
        if eot_event["data"]:
            errors.append(f"track {track_index}: End-of-Track payload must be empty")

    return {
        "events": events,
        "errors": errors,
        "last_tick": tick,
        "eot_tick": eot_positions[0][0] if len(eot_positions) == 1 else None,
        "used_running_status": used_running_status,
    }


def parse_midi(data: bytes) -> Dict[str, Any]:
    errors: List[str] = []
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiParseError("missing MThd header")
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length != 6:
        raise MidiParseError(f"MThd length must be 6, found {header_length}")
    format_type, declared_tracks, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        errors.append("SMPTE timing is outside audited-score-to-midi v0.1 scope")
    position = 14
    tracks = []
    for index in range(declared_tracks):
        if position + 8 > len(data):
            errors.append(f"missing MTrk header for declared track {index}")
            break
        if data[position:position + 4] != b"MTrk":
            errors.append(f"track {index} does not begin with MTrk")
            break
        length = struct.unpack(">I", data[position + 4:position + 8])[0]
        start = position + 8
        end = start + length
        if end > len(data):
            errors.append(f"track {index} length exceeds file boundary")
            break
        parsed = parse_track(data[start:end], index)
        parsed["declared_bytes"] = length
        tracks.append(parsed)
        errors.extend(parsed["errors"])
        position = end
    if len(tracks) != declared_tracks:
        errors.append(f"declared {declared_tracks} tracks but parsed {len(tracks)}")
    if position != len(data):
        errors.append(f"{len(data) - position} trailing or unparsed bytes remain")
    return {
        "format": format_type,
        "declared_tracks": declared_tracks,
        "division": division,
        "tracks": tracks,
        "errors": errors,
    }


def signed_byte(value: int) -> int:
    return value - 256 if value >= 128 else value


def display_path(path: Path, include_local_paths: bool) -> str:
    return str(path.resolve()) if include_local_paths else path.name


def audit_track(track: Dict[str, Any], track_index: int) -> Dict[str, Any]:
    errors: List[str] = []
    active: Dict[Tuple[int, int], Tuple[int, int]] = {}
    notes = []
    programs = []
    tempos = []
    meters = []
    keys = []
    track_names = []
    markers = []
    allowed_meta = (
        {0x03, 0x06, 0x2F, 0x51, 0x58, 0x59}
        if track_index == 0
        else {0x03, 0x2F}
    )

    for event in track["events"]:
        if event["kind"] == "meta":
            kind = event["type"]
            body = event["data"]
            if kind not in allowed_meta:
                errors.append(
                    f"track {track_index}: unsupported meta event 0x{kind:02X} "
                    f"at tick {event['tick']} is not emitted by audited-score-to-midi v0.1"
                )
            if kind == 0x03:
                track_names.append({"tick": event["tick"], "text": body.decode("utf-8", "replace")})
            elif kind == 0x06:
                markers.append({"tick": event["tick"], "text": body.decode("utf-8", "replace")})
            elif kind == 0x51:
                if len(body) != 3:
                    errors.append(f"track {track_index}: tempo meta must contain 3 bytes")
                else:
                    tempo_value = int.from_bytes(body, "big")
                    if tempo_value == 0:
                        errors.append(f"track {track_index}: tempo must be greater than zero")
                    tempos.append({"tick": event["tick"], "microseconds_per_quarter": tempo_value})
            elif kind == 0x58:
                if len(body) != 4:
                    errors.append(f"track {track_index}: time-signature meta must contain 4 bytes")
                else:
                    if body[0] == 0:
                        errors.append(f"track {track_index}: time-signature numerator must be greater than zero")
                    if body[1] > 10:
                        errors.append(f"track {track_index}: time-signature denominator exceeds v0.1 limit")
                    meters.append({"tick": event["tick"], "numerator": body[0],
                                   "denominator": 1 << body[1], "clocks_per_click": body[2],
                                   "notated_32nds_per_quarter": body[3]})
            elif kind == 0x59:
                if len(body) != 2:
                    errors.append(f"track {track_index}: key-signature meta must contain 2 bytes")
                else:
                    fifths = signed_byte(body[0])
                    if not -7 <= fifths <= 7:
                        errors.append(f"track {track_index}: key-signature fifths must be -7..7")
                    if body[1] not in (0, 1):
                        errors.append(f"track {track_index}: key-signature mode must be 0 or 1")
                    keys.append({"tick": event["tick"], "fifths": signed_byte(body[0]),
                                 "mode": "minor" if body[1] else "major"})
        elif event["kind"] == "sysex":
            errors.append(
                f"track {track_index}: SysEx event at tick {event['tick']} "
                "is not represented in score IR v0.1"
            )
        elif event["kind"] == "midi":
            status = event["status"]
            high = status & 0xF0
            channel = status & 0x0F
            body = event["data"]
            if high == 0xC0:
                programs.append({"tick": event["tick"], "channel": channel, "program": body[0]})
                continue
            if high not in (0x80, 0x90):
                errors.append(
                    f"track {track_index}: unsupported channel message 0x{high:02X} "
                    f"at tick {event['tick']} is not represented in score IR v0.1"
                )
                continue
            midi_note = body[0]
            velocity = body[1]
            key = (channel, midi_note)
            is_on = high == 0x90 and velocity > 0
            if is_on:
                if key in active:
                    errors.append(f"track {track_index}: overlapping note-on for channel {channel}, note {midi_note}, tick {event['tick']}")
                else:
                    active[key] = (event["tick"], velocity)
            else:
                if key not in active:
                    errors.append(f"track {track_index}: note-off without note-on for channel {channel}, note {midi_note}, tick {event['tick']}")
                else:
                    start, attack_velocity = active.pop(key)
                    if event["tick"] <= start:
                        errors.append(f"track {track_index}: non-positive note duration for channel {channel}, note {midi_note}")
                    notes.append({"start_tick": start, "end_tick": event["tick"], "channel": channel,
                                  "midi_note": midi_note, "velocity": attack_velocity,
                                  "release_velocity": velocity,
                                  "note_off_status": high})
    for (channel, midi_note), (start, _velocity) in active.items():
        errors.append(f"track {track_index}: unterminated note channel {channel}, note {midi_note}, start {start}")
    notes.sort(key=lambda row: (row["start_tick"], row["end_tick"], row["channel"], row["midi_note"], row["velocity"]))
    note_range = None if not notes else [min(row["midi_note"] for row in notes), max(row["midi_note"] for row in notes)]
    return {
        "track_index": track_index,
        "track_names": track_names,
        "markers": markers,
        "eot_tick": track["eot_tick"],
        "last_tick": track["last_tick"],
        "used_running_status": track["used_running_status"],
        "programs": programs,
        "tempos": tempos,
        "meters": meters,
        "keys": keys,
        "notes": notes,
        "note_count": len(notes),
        "note_range": note_range,
        "errors": track["errors"] + errors,
    }


def ir_fraction(value: Any, field: str) -> Fraction:
    if not isinstance(value, str):
        raise MidiParseError(f"{field} must be a rational string")
    if not RATIONAL_RE.fullmatch(value):
        raise MidiParseError(f"{field} must use non-negative integer or numerator/denominator syntax")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise MidiParseError(f"{field} is not a valid rational") from exc


def expected_from_ir(ir: Dict[str, Any]) -> Dict[str, Any]:
    score = ir["score"]
    measure_count = score["measure_count"]
    ppq = score["ppq"]
    meter_map = sorted(score["meter_map"], key=lambda row: row["measure"])
    active = None
    changes = {row["measure"]: row for row in meter_map}
    starts = [Fraction(0)] * (measure_count + 2)
    lengths = [Fraction(0)] * (measure_count + 1)
    cursor = Fraction(0)
    for measure in range(1, measure_count + 1):
        if measure in changes:
            active = changes[measure]
        if active is None:
            raise MidiParseError(f"IR has no meter for measure {measure}")
        starts[measure] = cursor
        lengths[measure] = Fraction(active["numerator"] * 4, active["denominator"])
        cursor += lengths[measure]
    starts[measure_count + 1] = cursor
    total_ticks_fraction = cursor * ppq
    if total_ticks_fraction.denominator != 1:
        raise MidiParseError("IR total duration is not representable at its PPQ")

    def event_tick(raw: Dict[str, Any], field: str) -> int:
        measure = raw["measure"]
        onset = ir_fraction(raw.get("onset", "0"), field + ".onset")
        value = (starts[measure] + onset) * ppq
        if value.denominator != 1:
            raise MidiParseError(f"{field} position is not representable at PPQ={ppq}")
        return value.numerator

    tempos = []
    for index, raw in enumerate(score["tempo_map"]):
        bpm = ir_fraction(raw["bpm"], f"score.tempo_map[{index}].bpm")
        beat_unit = ir_fraction(raw["beat_unit"], f"score.tempo_map[{index}].beat_unit")
        if bpm <= 0 or beat_unit <= 0:
            raise MidiParseError(f"score.tempo_map[{index}] bpm and beat_unit must be positive")
        usec = Fraction(60_000_000, 1) / (bpm * beat_unit)
        rounded = (usec.numerator + usec.denominator // 2) // usec.denominator
        tempos.append({"tick": event_tick(raw, f"score.tempo_map[{index}]"),
                       "microseconds_per_quarter": rounded})
    tempos.sort(key=lambda row: row["tick"])
    meters = []
    for raw in meter_map:
        meters.append({"tick": int(starts[raw["measure"]] * ppq), "numerator": raw["numerator"],
                       "denominator": raw["denominator"], "clocks_per_click": raw.get("clocks_per_click", 24),
                       "notated_32nds_per_quarter": raw.get("notated_32nds_per_quarter", 8)})
    keys = []
    for index, raw in enumerate(score.get("key_map", [])):
        keys.append({"tick": event_tick(raw, f"score.key_map[{index}]"), "fifths": raw["fifths"],
                     "mode": raw["mode"]})
    keys.sort(key=lambda row: row["tick"])

    parts = []
    for part_index, part in enumerate(ir["parts"]):
        notes = []
        for note_index, raw in enumerate(part["notes"]):
            start = event_tick(raw, f"parts[{part_index}].notes[{note_index}]")
            duration = ir_fraction(raw["duration"], f"parts[{part_index}].notes[{note_index}].duration") * ppq
            if duration.denominator != 1:
                raise MidiParseError(f"parts[{part_index}].notes[{note_index}] duration is not representable")
            notes.append({"start_tick": start, "end_tick": start + duration.numerator,
                          "channel": part["channel"], "midi_note": raw["midi_note"],
                          "velocity": raw["velocity"], "release_velocity": 64,
                          "note_off_status": 0x80})
        notes.sort(key=lambda row: (row["start_tick"], row["end_tick"], row["channel"], row["midi_note"], row["velocity"]))
        parts.append({"id": part["id"], "name": part["name"], "channel": part["channel"],
                      "program": part["program"], "notes": notes})
    return {"ppq": ppq, "total_ticks": total_ticks_fraction.numerator, "tempos": tempos,
            "meters": meters, "keys": keys, "parts": parts,
            "conductor_track_names": [{"tick": 0, "text": "Conductor"}],
            "conductor_markers": [{"tick": total_ticks_fraction.numerator,
                                    "text": f"End after measure {measure_count}"}]}


def first_difference(expected: List[Dict[str, Any]], actual: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if expected == actual:
        return None
    limit = min(len(expected), len(actual))
    for index in range(limit):
        if expected[index] != actual[index]:
            return {"index": index, "expected": expected[index], "actual": actual[index]}
    return {"index": limit, "expected": expected[limit] if len(expected) > limit else None,
            "actual": actual[limit] if len(actual) > limit else None,
            "expected_count": len(expected), "actual_count": len(actual)}


def compare_ir(parsed: Dict[str, Any], audited_tracks: List[Dict[str, Any]], ir: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    differences: Dict[str, Any] = {}
    expected = expected_from_ir(ir)
    if parsed["format"] != 1:
        errors.append(f"MIDI format is {parsed['format']}, expected 1")
    if parsed["division"] != expected["ppq"]:
        errors.append(f"PPQ is {parsed['division']}, expected {expected['ppq']}")
    if len(audited_tracks) != len(expected["parts"]) + 1:
        errors.append(f"track count is {len(audited_tracks)}, expected {len(expected['parts']) + 1}")
    for index, track in enumerate(audited_tracks):
        if track["eot_tick"] != expected["total_ticks"]:
            errors.append(f"track {index} EOT is {track['eot_tick']}, expected {expected['total_ticks']}")
    if audited_tracks:
        conductor = audited_tracks[0]
        if conductor["track_names"] != expected["conductor_track_names"]:
            errors.append("conductor track name differs from compiler output")
            differences["conductor_track_names"] = {
                "expected": expected["conductor_track_names"],
                "actual": conductor["track_names"],
            }
        if conductor["markers"] != expected["conductor_markers"]:
            errors.append("conductor end marker differs from compiler output")
            differences["conductor_markers"] = {
                "expected": expected["conductor_markers"],
                "actual": conductor["markers"],
            }
        for name, actual in (("tempo_map", conductor["tempos"]), ("meter_map", conductor["meters"]),
                             ("key_map", conductor["keys"])):
            target = expected[{"tempo_map": "tempos", "meter_map": "meters", "key_map": "keys"}[name]]
            difference = first_difference(target, actual)
            if difference:
                errors.append(f"{name} differs from IR")
                differences[name] = difference
    for part_index, target in enumerate(expected["parts"]):
        track_index = part_index + 1
        if track_index >= len(audited_tracks):
            break
        actual_track = audited_tracks[track_index]
        expected_names = [{"tick": 0, "text": target["name"]}]
        if actual_track["track_names"] != expected_names:
            errors.append(f"part {target['id']} track name differs from IR")
            differences[f"part:{target['id']}:track_name"] = {
                "expected": expected_names,
                "actual": actual_track["track_names"],
            }
        difference = first_difference(target["notes"], actual_track["notes"])
        if difference:
            errors.append(f"part {target['id']} note events differ from IR")
            differences[f"part:{target['id']}:notes"] = difference
        expected_programs = [{"tick": 0, "channel": target["channel"], "program": target["program"]}]
        programs = actual_track["programs"]
        if programs != expected_programs:
            errors.append(f"part {target['id']} program/channel differs from IR")
            differences[f"part:{target['id']}:program"] = {"expected": expected_programs, "actual": programs}
    return {"matches": not errors, "errors": errors, "differences": differences,
            "expected_total_ticks": expected["total_ticks"]}


def validate_file(
    midi_path: Path, ir_path: Optional[Path], *, include_local_paths: bool = False
) -> Dict[str, Any]:
    data = midi_path.read_bytes()
    parsed = parse_midi(data)
    audited = [audit_track(track, index) for index, track in enumerate(parsed["tracks"])]
    errors = list(parsed["errors"])
    for track in audited:
        for error in track["errors"]:
            if error not in errors:
                errors.append(error)
    if parsed["format"] != 1:
        errors.append(f"SMF format {parsed['format']} is outside v0.1 output contract (expected 1)")
    if parsed["division"] & 0x8000:
        errors.append("SMPTE timing is outside v0.1 output contract")
    elif parsed["division"] == 0:
        errors.append("PPQ division must be greater than zero")
    if len(audited) < 2:
        errors.append("v0.1 output requires a conductor track and at least one musical track")
    if audited and audited[0]["note_count"]:
        errors.append("conductor track contains note events")
    if audited and audited[0]["programs"]:
        errors.append("conductor track contains program changes")
    for track in audited[1:]:
        if track["tempos"] or track["meters"] or track["keys"]:
            errors.append(f"track {track['track_index']} contains conductor meta events")
    comparison = None
    provenance: Optional[Dict[str, Any]] = None
    if ir_path is not None:
        try:
            raw_ir = ir_path.read_bytes()
            ir = json.loads(raw_ir.decode("utf-8"))
            source = ir.get("source")
            if not isinstance(source, dict):
                raise MidiParseError("IR source must be an object")
            source_path = source.get("path")
            source_sha = source.get("sha256")
            if not isinstance(source_path, str) or not isinstance(source_sha, str):
                raise MidiParseError("IR source path and sha256 must be strings")
            resolved_source = Path(source_path)
            if not resolved_source.is_absolute():
                resolved_source = ir_path.resolve().parent / resolved_source
            provenance = {
                "ir_path": display_path(ir_path, include_local_paths),
                "ir_sha256": hashlib.sha256(raw_ir).hexdigest(),
                "source_path_declared": (
                    source_path if include_local_paths else Path(source_path).name
                ),
                "source_path_resolved": display_path(resolved_source, include_local_paths),
                "source_sha256_declared": source_sha.lower(),
                "source_hash_verified": False,
                "local_paths_included": include_local_paths,
            }
            if not resolved_source.is_file():
                raise MidiParseError("IR source file is unavailable for audit binding")
            try:
                digest = hashlib.sha256()
                with resolved_source.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                raise MidiParseError(
                    "IR source file could not be read for audit binding"
                ) from exc
            actual_source_sha = digest.hexdigest()
            provenance["source_sha256_actual"] = actual_source_sha
            if actual_source_sha.lower() != source_sha.lower():
                raise MidiParseError("IR source SHA-256 does not match the declared hash")
            provenance["source_hash_verified"] = True
            comparison = compare_ir(parsed, audited, ir)
            errors.extend(comparison["errors"])
        except OSError as exc:
            message = (
                type(exc).__name__ + ": unable to read " +
                display_path(ir_path, include_local_paths)
            )
            errors.append(f"could not compare IR: {message}")
            comparison = {"matches": False, "errors": [message], "differences": {}}
        except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError,
                MidiParseError, ZeroDivisionError) as exc:
            errors.append(f"could not compare IR: {exc}")
            comparison = {"matches": False, "errors": [str(exc)], "differences": {}}

    track_summaries = []
    for track in audited:
        track_summaries.append({key: value for key, value in track.items() if key not in {"notes", "errors"}})
    return {
        "valid": not errors,
        "file": display_path(midi_path, include_local_paths),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": parsed["format"],
        "declared_tracks": parsed["declared_tracks"],
        "ppq": None if parsed["division"] & 0x8000 else parsed["division"],
        "tracks": track_summaries,
        "provenance": provenance,
        "ir_comparison": comparison,
        "privacy": {"local_paths_included": include_local_paths},
        "errors": errors,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("midi", type=Path, help="MIDI file to validate")
    parser.add_argument("--ir", type=Path, help="optional score IR JSON for event-level comparison")
    parser.add_argument("--report", "--audit", dest="report", type=Path,
                        help="write the validation report JSON to this path (--audit is a compatibility alias)")
    parser.add_argument("--include-local-paths", action="store_true",
                        help="include absolute MIDI, IR, and source paths in the audit")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = validate_file(
            args.midi, args.ir, include_local_paths=args.include_local_paths
        )
    except OSError as exc:
        report = {
            "valid": False,
            "file": display_path(args.midi, args.include_local_paths),
            "privacy": {"local_paths_included": args.include_local_paths},
            "errors": [
                type(exc).__name__ + ": unable to read " +
                display_path(args.midi, args.include_local_paths)
            ],
        }
    except (MidiParseError, struct.error) as exc:
        report = {
            "valid": False,
            "file": display_path(args.midi, args.include_local_paths),
            "privacy": {"local_paths_included": args.include_local_paths},
            "errors": [str(exc)],
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
