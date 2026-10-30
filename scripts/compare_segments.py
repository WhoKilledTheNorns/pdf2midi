#!/usr/bin/env python3
"""Compare complete written-segment semantics in an audited score IR."""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional, Sequence, Tuple


MapEvent = Tuple[Fraction, Dict[str, object]]


def rational(value: object, field: str, *, positive: bool = False) -> Fraction:
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("invalid rational %s=%r" % (field, value)) from exc
    if positive and parsed <= 0:
        raise ValueError("%s must be positive" % field)
    return parsed


def integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % field)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer" % field) from exc
    if str(parsed) != str(value):
        raise ValueError("%s must be an integer" % field)
    return parsed


def measure_number(value: object, field: str, measure_count: int) -> int:
    measure = integer(value, field)
    if not 1 <= measure <= measure_count:
        raise ValueError("%s is outside 1..%d" % (field, measure_count))
    return measure


def object_list(value: object, field: str, *, nonempty: bool = False) -> List[Dict[str, object]]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty list" if nonempty else "a list"
        raise ValueError("%s must be %s" % (field, qualifier))
    objects = [item for item in value if isinstance(item, dict)]
    if len(objects) != len(value):
        raise ValueError("every item in %s must be an object" % field)
    return objects


def build_measure_grid(
    score: Dict[str, object]
) -> Tuple[int, List[Fraction], List[Fraction], List[MapEvent]]:
    measure_count = integer(score.get("measure_count"), "score.measure_count")
    if measure_count <= 0:
        raise ValueError("score.measure_count must be positive")

    raw_map = object_list(score.get("meter_map"), "score.meter_map", nonempty=True)
    normalized: List[Tuple[int, Dict[str, object]]] = []
    seen = set()
    for index, raw in enumerate(raw_map):
        field = "score.meter_map[%d]" % index
        measure = measure_number(raw.get("measure"), field + ".measure", measure_count)
        onset = rational(raw.get("onset", "0"), field + ".onset")
        if onset != 0:
            raise ValueError("%s must occur at a measure boundary" % field)
        numerator = integer(raw.get("numerator"), field + ".numerator")
        denominator = integer(raw.get("denominator"), field + ".denominator")
        if not 1 <= numerator <= 255:
            raise ValueError("%s.numerator must be 1..255" % field)
        if not 1 <= denominator <= 1024 or denominator & (denominator - 1):
            raise ValueError("%s.denominator must be a power of two in 1..1024" % field)
        if measure in seen:
            raise ValueError("duplicate meter event at measure %d" % measure)
        seen.add(measure)
        clocks = integer(raw.get("clocks_per_click", 24), field + ".clocks_per_click")
        thirty_seconds = integer(
            raw.get("notated_32nds_per_quarter", 8),
            field + ".notated_32nds_per_quarter",
        )
        if not 0 <= clocks <= 255 or not 0 <= thirty_seconds <= 255:
            raise ValueError("%s MIDI metrical fields must be 0..255" % field)
        normalized.append(
            (
                measure,
                {
                    "numerator": numerator,
                    "denominator": denominator,
                    "clocks_per_click": clocks,
                    "notated_32nds_per_quarter": thirty_seconds,
                },
            )
        )
    normalized.sort(key=lambda item: item[0])
    if normalized[0][0] != 1:
        raise ValueError("score.meter_map must define the meter at measure 1")

    starts = [Fraction(0)] * (measure_count + 2)
    lengths = [Fraction(0)] * (measure_count + 1)
    changes = {measure: state for measure, state in normalized}
    active: Optional[Dict[str, object]] = None
    for measure in range(1, measure_count + 1):
        if measure in changes:
            active = changes[measure]
        if active is None:
            raise ValueError("no active meter for measure %d" % measure)
        if measure > 1:
            starts[measure] = starts[measure - 1] + lengths[measure - 1]
        lengths[measure] = Fraction(
            int(active["numerator"]) * 4, int(active["denominator"])
        )
    starts[measure_count + 1] = starts[measure_count] + lengths[measure_count]
    events = [(starts[measure], state) for measure, state in normalized]
    return measure_count, starts, lengths, events


def absolute_position(
    raw: Dict[str, object],
    field: str,
    starts: Sequence[Fraction],
    lengths: Sequence[Fraction],
    measure_count: int,
) -> Fraction:
    measure = measure_number(raw.get("measure"), field + ".measure", measure_count)
    onset = rational(raw.get("onset", "0"), field + ".onset")
    if onset < 0 or onset >= lengths[measure]:
        raise ValueError("%s.onset is outside measure %d" % (field, measure))
    return starts[measure] + onset


def build_tempo_events(
    score: Dict[str, object],
    starts: Sequence[Fraction],
    lengths: Sequence[Fraction],
    measure_count: int,
) -> List[MapEvent]:
    rows = object_list(score.get("tempo_map"), "score.tempo_map", nonempty=True)
    events: List[MapEvent] = []
    seen = set()
    for index, raw in enumerate(rows):
        field = "score.tempo_map[%d]" % index
        position = absolute_position(raw, field, starts, lengths, measure_count)
        if position in seen:
            raise ValueError("duplicate tempo event at quarter %s" % position)
        seen.add(position)
        bpm = rational(raw.get("bpm"), field + ".bpm", positive=True)
        beat_unit = rational(raw.get("beat_unit"), field + ".beat_unit", positive=True)
        events.append((position, {"bpm": str(bpm),
                                  "beat_unit_quarters": str(beat_unit),
                                  "quarter_bpm": str(bpm * beat_unit)}))
    events.sort(key=lambda event: event[0])
    if events[0][0] != 0:
        raise ValueError("score.tempo_map must define tempo at measure 1 onset 0")
    return events


def build_key_events(
    score: Dict[str, object],
    starts: Sequence[Fraction],
    lengths: Sequence[Fraction],
    measure_count: int,
) -> List[MapEvent]:
    rows = object_list(score.get("key_map"), "score.key_map", nonempty=True)
    events: List[MapEvent] = []
    seen = set()
    for index, raw in enumerate(rows):
        field = "score.key_map[%d]" % index
        onset = rational(raw.get("onset", "0"), field + ".onset")
        if onset != 0:
            raise ValueError("%s must occur at a measure boundary" % field)
        position = absolute_position(raw, field, starts, lengths, measure_count)
        if position in seen:
            raise ValueError("duplicate key event at quarter %s" % position)
        seen.add(position)
        fifths = integer(raw.get("fifths"), field + ".fifths")
        mode = raw.get("mode")
        if not -7 <= fifths <= 7:
            raise ValueError("%s.fifths must be -7..7" % field)
        if not isinstance(mode, str) or mode not in {"major", "minor"}:
            raise ValueError("%s.mode must be major or minor" % field)
        events.append((position, {"fifths": fifths, "mode": mode}))
    events.sort(key=lambda event: event[0])
    if events[0][0] != 0:
        raise ValueError("score.key_map must define the key at measure 1 onset 0")
    return events


def canonical_map(events: Sequence[MapEvent], start: Fraction, end: Fraction) -> List[Dict[str, object]]:
    active: Optional[Dict[str, object]] = None
    for position, state in events:
        if position <= start:
            active = state
        else:
            break
    if active is None:
        raise ValueError("map has no effective state at segment start")
    timeline: List[Dict[str, object]] = [
        {"relative_quarter": "0", "kind": "effective_at_start", **active}
    ]
    for position, state in events:
        if start < position < end:
            timeline.append(
                {
                    "relative_quarter": str(position - start),
                    "kind": "change",
                    **state,
                }
            )
    return timeline


def multiset_difference(
    left: List[Dict[str, object]], right: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    remaining = list(right)
    difference: List[Dict[str, object]] = []
    for item in left:
        try:
            index = remaining.index(item)
        except ValueError:
            difference.append(item)
        else:
            remaining.pop(index)
    return difference


def position_differences(
    left: Sequence[Dict[str, object]], right: Sequence[Dict[str, object]]
) -> List[Dict[str, object]]:
    left_at = {str(item["relative_quarter"]): item for item in left}
    right_at = {str(item["relative_quarter"]): item for item in right}
    positions = sorted(set(left_at) | set(right_at), key=lambda value: Fraction(value))
    return [
        {"relative_quarter": position, "a": left_at.get(position), "b": right_at.get(position)}
        for position in positions
        if left_at.get(position) != right_at.get(position)
    ]


def map_report(
    events: Sequence[MapEvent],
    a_start: Fraction,
    a_end: Fraction,
    b_start: Fraction,
    b_end: Fraction,
) -> Dict[str, object]:
    a_timeline = canonical_map(events, a_start, a_end)
    b_timeline = canonical_map(events, b_start, b_end)
    missing_in_b = multiset_difference(a_timeline, b_timeline)
    extra_in_b = multiset_difference(b_timeline, a_timeline)
    return {
        "equivalent": not missing_in_b and not extra_in_b,
        "a_timeline": a_timeline,
        "b_timeline": b_timeline,
        "missing_in_b": missing_in_b,
        "extra_in_b": extra_in_b,
        "position_differences": position_differences(a_timeline, b_timeline),
    }


def canonical_note(
    note: Dict[str, object],
    field: str,
    segment_start: Fraction,
    starts: Sequence[Fraction],
    lengths: Sequence[Fraction],
    measure_count: int,
) -> Tuple[Fraction, Fraction, Dict[str, object]]:
    measure = measure_number(note.get("measure"), field + ".measure", measure_count)
    onset = rational(note.get("onset"), field + ".onset")
    if onset < 0 or onset >= lengths[measure]:
        raise ValueError("%s.onset is outside measure %d" % (field, measure))
    duration = rational(note.get("duration"), field + ".duration", positive=True)
    absolute_start = starts[measure] + onset
    absolute_end = absolute_start + duration
    if absolute_end > starts[measure_count + 1]:
        raise ValueError("%s extends beyond the written timeline" % field)
    midi_note = integer(note.get("midi_note"), field + ".midi_note")
    velocity = integer(note.get("velocity"), field + ".velocity")
    confidence = note.get("confidence")
    if not 0 <= midi_note <= 127:
        raise ValueError("%s.midi_note must be 0..127" % field)
    if not 1 <= velocity <= 127:
        raise ValueError("%s.velocity must be 1..127" % field)
    if not isinstance(confidence, str) or confidence not in {
        "confirmed",
        "inferred",
        "unresolved",
    }:
        raise ValueError("%s.confidence is invalid" % field)
    relative_start = absolute_start - segment_start
    event = {
        "relative_onset_quarters": str(relative_start),
        "duration_quarters": str(duration),
        "midi_note": midi_note,
        "velocity": velocity,
        "confidence": confidence,
        "carry_in": relative_start < 0,
    }
    return absolute_start, absolute_end, event


def note_sort_key(event: Dict[str, object]) -> Tuple[object, ...]:
    return (
        Fraction(str(event["relative_onset_quarters"])),
        int(event["midi_note"]),
        Fraction(str(event["duration_quarters"])),
        int(event["velocity"]),
        str(event["confidence"]),
    )


def events_for_range(
    part: Dict[str, object],
    segment_start: Fraction,
    segment_end: Fraction,
    starts: Sequence[Fraction],
    lengths: Sequence[Fraction],
    measure_count: int,
) -> List[Dict[str, object]]:
    part_id = part.get("id")
    notes = object_list(part.get("notes"), "part %r notes" % part_id)
    selected: List[Dict[str, object]] = []
    for index, note in enumerate(notes):
        absolute_start, absolute_end, event = canonical_note(
            note,
            "part %r notes[%d]" % (part_id, index),
            segment_start,
            starts,
            lengths,
            measure_count,
        )
        if absolute_start < segment_end and absolute_end > segment_start:
            selected.append(event)
    return sorted(selected, key=note_sort_key)


def selected_parts(ir: Dict[str, object], selection: str) -> List[Dict[str, object]]:
    parts = object_list(ir.get("parts"), "IR field 'parts'", nonempty=True)
    ids = [part.get("id") for part in parts]
    if any(not isinstance(part_id, str) or not part_id for part_id in ids):
        raise ValueError("every part must have a non-empty string id")
    if len(set(ids)) != len(ids):
        raise ValueError("part ids must be unique")
    if selection == "all":
        return parts
    matches = [part for part in parts if part.get("id") == selection]
    if not matches:
        raise ValueError("part id not found: %s" % selection)
    return matches


def timeline_report(
    a_start_measure: int,
    a_end_measure: int,
    b_start_measure: int,
    b_end_measure: int,
    a_start: Fraction,
    a_end: Fraction,
    b_start: Fraction,
    b_end: Fraction,
) -> Dict[str, object]:
    a_count = a_end_measure - a_start_measure + 1
    b_count = b_end_measure - b_start_measure + 1
    a_total = a_end - a_start
    b_total = b_end - b_start
    same_measure_count = a_count == b_count
    same_total = a_total == b_total
    differences: List[Dict[str, object]] = []
    if not same_measure_count:
        differences.append({"field": "measure_count", "a": a_count, "b": b_count})
    if not same_total:
        differences.append({"field": "total_quarters", "a": str(a_total), "b": str(b_total)})
    return {
        "equivalent": same_measure_count and same_total,
        "a_measure_count": a_count,
        "b_measure_count": b_count,
        "same_measure_count": same_measure_count,
        "a_total_quarters": str(a_total),
        "b_total_quarters": str(b_total),
        "same_total_quarters": same_total,
        "a_absolute_start_quarter": str(a_start),
        "b_absolute_start_quarter": str(b_start),
        "differences": differences,
    }


def compare(
    ir: Dict[str, object], selection: str, a_start: int, a_end: int, b_start: int, b_end: int
) -> Dict[str, object]:
    if min(a_start, b_start) < 1 or a_end < a_start or b_end < b_start:
        raise ValueError("measure ranges must be positive and ordered")
    score = ir.get("score")
    if not isinstance(score, dict):
        raise ValueError("IR field 'score' must be an object")
    measure_count, starts, lengths, meter_events = build_measure_grid(score)
    if max(a_end, b_end) > measure_count:
        raise ValueError("measure ranges must lie inside 1..%d" % measure_count)

    a_start_quarter = starts[a_start]
    a_end_quarter = starts[a_end] + lengths[a_end]
    b_start_quarter = starts[b_start]
    b_end_quarter = starts[b_end] + lengths[b_end]
    timeline = timeline_report(
        a_start,
        a_end,
        b_start,
        b_end,
        a_start_quarter,
        a_end_quarter,
        b_start_quarter,
        b_end_quarter,
    )

    maps = {
        "tempo": map_report(
            build_tempo_events(score, starts, lengths, measure_count),
            a_start_quarter,
            a_end_quarter,
            b_start_quarter,
            b_end_quarter,
        ),
        "meter": map_report(
            meter_events,
            a_start_quarter,
            a_end_quarter,
            b_start_quarter,
            b_end_quarter,
        ),
        "key": map_report(
            build_key_events(score, starts, lengths, measure_count),
            a_start_quarter,
            a_end_quarter,
            b_start_quarter,
            b_end_quarter,
        ),
    }
    maps_equivalent = all(bool(report["equivalent"]) for report in maps.values())

    part_reports: List[Dict[str, object]] = []
    notes_equivalent = True
    for part in selected_parts(ir, selection):
        a_events = events_for_range(
            part, a_start_quarter, a_end_quarter, starts, lengths, measure_count
        )
        b_events = events_for_range(
            part, b_start_quarter, b_end_quarter, starts, lengths, measure_count
        )
        missing_in_b = multiset_difference(a_events, b_events)
        extra_in_b = multiset_difference(b_events, a_events)
        equivalent = not missing_in_b and not extra_in_b
        notes_equivalent = notes_equivalent and equivalent
        part_reports.append(
            {
                "part_id": part.get("id"),
                "equivalent": equivalent,
                "a_event_count": len(a_events),
                "b_event_count": len(b_events),
                "a_events": a_events,
                "b_events": b_events,
                "missing_in_b": missing_in_b,
                "extra_in_b": extra_in_b,
            }
        )

    equivalent = bool(timeline["equivalent"]) and maps_equivalent and notes_equivalent
    return {
        "ok": equivalent,
        "equivalent": equivalent,
        "comparison_basis": (
            "complete written-segment semantics: measure count and total quarter length; "
            "effective tempo, meter, and key at segment start plus in-segment changes; "
            "all intersecting notes with full relative onset and duration (including carry-in), "
            "pitch, velocity, and confidence"
        ),
        "part_selection": selection,
        "range_a": [a_start, a_end],
        "range_b": [b_start, b_end],
        "same_measure_count": timeline["same_measure_count"],
        "timeline": timeline,
        "maps": {"equivalent": maps_equivalent, **maps},
        "notes": {"equivalent": notes_equivalent, "parts": part_reports},
        "parts": part_reports,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare complete written semantics of two measure ranges in score IR."
    )
    parser.add_argument("ir", type=Path, help="score IR JSON")
    parser.add_argument("--part", default="all", help="part id or 'all' (default: all)")
    parser.add_argument("--a-start", type=int, required=True)
    parser.add_argument("--a-end", type=int, required=True)
    parser.add_argument("--b-start", type=int, required=True)
    parser.add_argument("--b-end", type=int, required=True)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        ir = json.loads(args.ir.read_text(encoding="utf-8"))
        if not isinstance(ir, dict):
            raise ValueError("IR root must be a JSON object")
        report = compare(ir, args.part, args.a_start, args.a_end, args.b_start, args.b_end)
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if report["equivalent"] else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
