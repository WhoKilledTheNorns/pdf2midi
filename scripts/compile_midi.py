#!/usr/bin/env python3
"""Compile audited-score IR v0.1 into a deterministic SMF Format 1 MIDI file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


class CompileError(ValueError):
    pass


RATIONAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CompileError(message)


def rational(value: Any, field: str, *, positive: bool = False) -> Fraction:
    require(isinstance(value, str), f"{field} must be a rational string")
    require(bool(RATIONAL_RE.fullmatch(value)),
            f"{field} must use non-negative integer or numerator/denominator syntax")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise CompileError(f"{field} is not a valid rational: {value!r}") from exc
    if positive:
        require(result > 0, f"{field} must be positive")
    return result


def fraction_to_ticks(value: Fraction, ppq: int, field: str) -> int:
    ticks = value * ppq
    require(ticks.denominator == 1, f"{field}={value} is not representable at PPQ={ppq}")
    return ticks.numerator


def measure_number(entry: Dict[str, Any], field: str, measure_count: int) -> int:
    value = entry.get("measure")
    require(isinstance(value, int) and not isinstance(value, bool), f"{field}.measure must be an integer")
    require(1 <= value <= measure_count, f"{field}.measure is outside 1..{measure_count}")
    return value


def build_measure_grid(score: Dict[str, Any]) -> Tuple[List[Fraction], List[Fraction], List[Dict[str, Any]]]:
    measure_count = score.get("measure_count")
    require(isinstance(measure_count, int) and not isinstance(measure_count, bool) and measure_count > 0,
            "score.measure_count must be a positive integer")
    meter_map = score.get("meter_map")
    require(isinstance(meter_map, list) and meter_map, "score.meter_map must be a non-empty list")

    normalized: List[Dict[str, Any]] = []
    seen = set()
    for index, raw in enumerate(meter_map):
        require(isinstance(raw, dict), f"score.meter_map[{index}] must be an object")
        measure = measure_number(raw, f"score.meter_map[{index}]", measure_count)
        onset = rational(raw.get("onset", "0"), f"score.meter_map[{index}].onset")
        require(onset == 0, "v0.1 meter changes must occur at a measure boundary")
        numerator = raw.get("numerator")
        denominator = raw.get("denominator")
        require(isinstance(numerator, int) and 1 <= numerator <= 255,
                f"score.meter_map[{index}].numerator must be 1..255")
        require(isinstance(denominator, int) and 1 <= denominator <= 1024 and
                denominator & (denominator - 1) == 0,
                f"score.meter_map[{index}].denominator must be a power of two in 1..1024")
        require((measure, onset) not in seen, f"duplicate meter event at measure {measure}")
        seen.add((measure, onset))
        normalized.append({
            "measure": measure,
            "onset": onset,
            "numerator": numerator,
            "denominator": denominator,
            "clocks_per_click": raw.get("clocks_per_click", 24),
            "notated_32nds_per_quarter": raw.get("notated_32nds_per_quarter", 8),
        })
    normalized.sort(key=lambda row: (row["measure"], row["onset"]))
    require(normalized[0]["measure"] == 1, "score.meter_map must define the meter at measure 1")

    starts: List[Fraction] = [Fraction(0)] * (measure_count + 2)
    lengths: List[Fraction] = [Fraction(0)] * (measure_count + 1)
    active = None
    by_measure = {row["measure"]: row for row in normalized}
    for measure in range(1, measure_count + 1):
        if measure in by_measure:
            active = by_measure[measure]
        require(active is not None, f"no active meter for measure {measure}")
        starts[measure] = starts[measure - 1] if measure == 1 else starts[measure - 1] + lengths[measure - 1]
        lengths[measure] = Fraction(active["numerator"] * 4, active["denominator"])
    starts[measure_count + 1] = starts[measure_count] + lengths[measure_count]
    return starts, lengths, normalized


def absolute_quarters(entry: Dict[str, Any], field: str, starts: Sequence[Fraction],
                      lengths: Sequence[Fraction], measure_count: int) -> Fraction:
    measure = measure_number(entry, field, measure_count)
    onset = rational(entry.get("onset", "0"), f"{field}.onset")
    require(0 <= onset < lengths[measure], f"{field}.onset is outside measure {measure}")
    return starts[measure] + onset


def assert_no_blockers(ir: Dict[str, Any]) -> None:
    uncertainties = ir.get("uncertainties", [])
    require(isinstance(uncertainties, list), "uncertainties must be a list")
    for index, item in enumerate(uncertainties):
        require(isinstance(item, dict), f"uncertainties[{index}] must be an object")
        severity = str(item.get("severity", item.get("status", ""))).lower()
        require(not item.get("blocking") and severity not in {"blocking", "unresolved"},
                f"blocking uncertainty remains at uncertainties[{index}]")


def vlq(value: int) -> bytes:
    require(isinstance(value, int) and 0 <= value <= 0x0FFFFFFF, "delta time is outside MIDI VLQ range")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def meta(meta_type: int, payload: bytes) -> bytes:
    return bytes([0xFF, meta_type]) + vlq(len(payload)) + payload


def text_meta(meta_type: int, value: str) -> bytes:
    return meta(meta_type, value.encode("utf-8"))


MidiRow = Tuple[int, int, int, bytes]


def make_track(rows: Iterable[MidiRow], total_ticks: int) -> bytes:
    ordered = sorted(rows, key=lambda row: (row[0], row[1], row[2]))
    data = bytearray()
    last_tick = 0
    for tick, _priority, _sequence, payload in ordered:
        require(0 <= tick <= total_ticks, f"event tick {tick} is outside the score timeline")
        require(tick >= last_tick, "internal event ordering failure")
        data += vlq(tick - last_tick)
        data += payload
        last_tick = tick
    data += vlq(total_ticks - last_tick)
    data += meta(0x2F, b"")
    return b"MTrk" + struct.pack(">I", len(data)) + bytes(data)


def tempo_to_microseconds(bpm: Fraction, field: str) -> int:
    raw = Fraction(60_000_000, 1) / bpm
    value = (raw.numerator + raw.denominator // 2) // raw.denominator
    require(1 <= value <= 0xFFFFFF, f"{field} cannot be represented as a MIDI tempo")
    return value


def compile_ir(ir: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
    require(isinstance(ir, dict), "IR root must be an object")
    require(ir.get("schema_version") == "0.1", "schema_version must be '0.1'")
    assert_no_blockers(ir)
    score = ir.get("score")
    require(isinstance(score, dict), "score must be an object")
    measure_count = score.get("measure_count")
    ppq = score.get("ppq")
    require(isinstance(ppq, int) and not isinstance(ppq, bool) and 1 <= ppq <= 0x7FFF,
            "score.ppq must be an integer in 1..32767")
    starts, lengths, meters = build_measure_grid(score)
    total_quarters = starts[measure_count + 1]
    total_ticks = fraction_to_ticks(total_quarters, ppq, "score total duration")

    conductor: List[MidiRow] = [(0, 0, 0, text_meta(0x03, "Conductor"))]
    sequence = 1

    tempo_map = score.get("tempo_map")
    require(isinstance(tempo_map, list) and tempo_map, "score.tempo_map must be a non-empty list")
    tempo_seen = set()
    tempo_summary = []
    for index, raw in enumerate(tempo_map):
        require(isinstance(raw, dict), f"score.tempo_map[{index}] must be an object")
        quarter = absolute_quarters(raw, f"score.tempo_map[{index}]", starts, lengths, measure_count)
        tick = fraction_to_ticks(quarter, ppq, f"score.tempo_map[{index}]")
        require(tick not in tempo_seen, f"duplicate tempo event at tick {tick}")
        tempo_seen.add(tick)
        bpm = rational(raw.get("bpm"), f"score.tempo_map[{index}].bpm", positive=True)
        beat_unit = rational(raw.get("beat_unit"), f"score.tempo_map[{index}].beat_unit", positive=True)
        quarter_bpm = bpm * beat_unit
        usec = tempo_to_microseconds(quarter_bpm, f"score.tempo_map[{index}]")
        conductor.append((tick, 1, sequence, meta(0x51, usec.to_bytes(3, "big"))))
        sequence += 1
        tempo_summary.append({"tick": tick, "bpm": str(bpm),
                              "beat_unit_quarters": str(beat_unit),
                              "quarter_bpm": str(quarter_bpm),
                              "microseconds_per_quarter": usec})
    require(0 in tempo_seen, "score.tempo_map must define tempo at measure 1 onset 0")

    meter_summary = []
    for raw in meters:
        tick = fraction_to_ticks(starts[raw["measure"]], ppq, "meter event")
        denominator_exponent = raw["denominator"].bit_length() - 1
        clocks = raw["clocks_per_click"]
        thirty_seconds = raw["notated_32nds_per_quarter"]
        require(isinstance(clocks, int) and 0 <= clocks <= 255, "clocks_per_click must be 0..255")
        require(isinstance(thirty_seconds, int) and 0 <= thirty_seconds <= 255,
                "notated_32nds_per_quarter must be 0..255")
        payload = bytes([raw["numerator"], denominator_exponent, clocks, thirty_seconds])
        conductor.append((tick, 2, sequence, meta(0x58, payload)))
        sequence += 1
        meter_summary.append({"tick": tick, "numerator": raw["numerator"],
                              "denominator": raw["denominator"]})

    key_map = score.get("key_map")
    require(isinstance(key_map, list) and key_map, "score.key_map must be a non-empty list")
    key_seen = set()
    key_summary = []
    for index, raw in enumerate(key_map):
        require(isinstance(raw, dict), f"score.key_map[{index}] must be an object")
        key_onset = rational(raw.get("onset", "0"), f"score.key_map[{index}].onset")
        require(key_onset == 0, "v0.1 key changes must occur at a measure boundary")
        quarter = absolute_quarters(raw, f"score.key_map[{index}]", starts, lengths, measure_count)
        tick = fraction_to_ticks(quarter, ppq, f"score.key_map[{index}]")
        require(tick not in key_seen, f"duplicate key event at tick {tick}")
        key_seen.add(tick)
        fifths = raw.get("fifths")
        mode = raw.get("mode")
        require(isinstance(fifths, int) and -7 <= fifths <= 7,
                f"score.key_map[{index}].fifths must be -7..7")
        require(mode in {"major", "minor"}, f"score.key_map[{index}].mode must be major or minor")
        conductor.append((tick, 3, sequence, meta(0x59, bytes([fifths & 0xFF, mode == "minor"]))))
        sequence += 1
        key_summary.append({"tick": tick, "fifths": fifths, "mode": mode})
    require(0 in key_seen, "score.key_map must define the key at measure 1 onset 0")

    conductor.append((total_ticks, 4, sequence, text_meta(0x06, f"End after measure {measure_count}")))

    parts = ir.get("parts")
    require(isinstance(parts, list) and parts, "parts must be a non-empty list")
    track_chunks = []
    part_summaries = []
    ids = set()
    channels = set()
    for part_index, part in enumerate(parts):
        field = f"parts[{part_index}]"
        require(isinstance(part, dict), f"{field} must be an object")
        part_id = part.get("id")
        require(isinstance(part_id, str) and part_id.strip(), f"{field}.id must be a non-empty string")
        require(part_id not in ids, f"duplicate part id: {part_id}")
        ids.add(part_id)
        name = part.get("name")
        require(isinstance(name, str) and name.strip(), f"{field}.name must be a non-empty string")
        channel = part.get("channel")
        program = part.get("program")
        require(isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel <= 15,
                f"{field}.channel must be 0..15")
        require(channel != 9, f"{field}.channel 9 is percussion and is outside v0.1 scope")
        require(channel not in channels, f"duplicate MIDI channel {channel}")
        channels.add(channel)
        require(isinstance(program, int) and not isinstance(program, bool) and 0 <= program <= 127,
                f"{field}.program must be 0..127")
        notes = part.get("notes")
        require(isinstance(notes, list), f"{field}.notes must be a list")

        rows: List[MidiRow] = [
            (0, 0, 0, text_meta(0x03, name)),
            (0, 1, 1, bytes([0xC0 | channel, program])),
        ]
        intervals: Dict[int, List[Tuple[int, int, int]]] = {}
        note_summary = []
        seq = 2
        for note_index, raw in enumerate(notes):
            note_field = f"{field}.notes[{note_index}]"
            require(isinstance(raw, dict), f"{note_field} must be an object")
            confidence = raw.get("confidence")
            require(confidence in {"confirmed", "inferred"},
                    f"{note_field}.confidence={confidence!r} is not compilable")
            start_q = absolute_quarters(raw, note_field, starts, lengths, measure_count)
            duration = rational(raw.get("duration"), f"{note_field}.duration", positive=True)
            end_q = start_q + duration
            require(end_q <= total_quarters, f"{note_field} ends after the score timeline")
            start_tick = fraction_to_ticks(start_q, ppq, f"{note_field}.start")
            end_tick = fraction_to_ticks(end_q, ppq, f"{note_field}.end")
            midi_note = raw.get("midi_note")
            velocity = raw.get("velocity")
            require(isinstance(midi_note, int) and not isinstance(midi_note, bool) and 0 <= midi_note <= 127,
                    f"{note_field}.midi_note must be 0..127")
            require(isinstance(velocity, int) and not isinstance(velocity, bool) and 1 <= velocity <= 127,
                    f"{note_field}.velocity must be 1..127")
            intervals.setdefault(midi_note, []).append((start_tick, end_tick, note_index))
            rows.append((end_tick, 2, seq, bytes([0x80 | channel, midi_note, 64])))
            seq += 1
            rows.append((start_tick, 3, seq, bytes([0x90 | channel, midi_note, velocity])))
            seq += 1
            note_summary.append({"start_tick": start_tick, "end_tick": end_tick,
                                 "midi_note": midi_note, "velocity": velocity})

        for midi_note, spans in intervals.items():
            spans.sort()
            for previous, current in zip(spans, spans[1:]):
                require(previous[1] <= current[0],
                        f"{field} has overlapping MIDI note {midi_note} at note indexes "
                        f"{previous[2]} and {current[2]}")
        track_chunks.append(make_track(rows, total_ticks))
        part_summaries.append({"id": part_id, "name": name, "channel": channel,
                               "program": program, "note_count": len(note_summary)})

    conductor_chunk = make_track(conductor, total_ticks)
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(track_chunks) + 1, ppq)
    payload = header + conductor_chunk + b"".join(track_chunks)
    summary = {
        "schema_version": "0.1",
        "format": 1,
        "tracks": len(track_chunks) + 1,
        "ppq": ppq,
        "measure_count": measure_count,
        "total_quarters": str(total_quarters),
        "total_ticks": total_ticks,
        "tempo_map": tempo_summary,
        "meter_map": meter_summary,
        "key_map": key_summary,
        "parts": part_summaries,
        "note_count": sum(item["note_count"] for item in part_summaries),
    }
    return payload, summary


def atomic_write(path: Path, payload: bytes, force: bool) -> None:
    path = path.resolve()
    require(force or not path.exists(), f"output already exists: {path}; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent), delete=False)
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ir", type=Path, help="audited-score IR JSON")
    parser.add_argument("output", type=Path, help="output .mid path")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args()
    try:
        ir = json.loads(args.ir.read_text(encoding="utf-8"))
        payload, summary = compile_ir(ir)
        atomic_write(args.output, payload, args.force)
        summary["output"] = str(args.output.resolve())
        summary["bytes"] = len(payload)
        summary["sha256"] = hashlib.sha256(payload).hexdigest()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, json.JSONDecodeError, CompileError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
