# Score IR v0.1

Use this reference when creating or reviewing the intermediate `score.json`
consumed by `scripts/compile_midi.py`. The IR is a normalized **written
timeline**: screenshot overlaps are already deduplicated, and same-pitch tie
chains are already merged into one note whose duration may cross barlines.

## Minimal complete example

```json
{
  "schema_version": "0.1",
  "source": {
    "path": "score.pdf",
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "score": {
    "measure_count": 4,
    "ppq": 480,
    "tempo_map": [
      {"measure": 1, "onset": "0", "bpm": "120", "beat_unit": "1"}
    ],
    "meter_map": [
      {"measure": 1, "numerator": 4, "denominator": 4}
    ],
    "key_map": [
      {"measure": 1, "fifths": 0, "mode": "major"}
    ]
  },
  "parts": [
    {
      "id": "melody",
      "name": "Melody",
      "channel": 0,
      "program": 0,
      "notes": [
        {
          "measure": 1,
          "onset": "0",
          "duration": "1",
          "midi_note": 60,
          "velocity": 80,
          "confidence": "confirmed",
          "evidence": [
            {"asset": "evidence/page-1.png", "page": 1, "measure": 1}
          ]
        },
        {
          "measure": 3,
          "onset": "3",
          "duration": "5",
          "midi_note": 67,
          "velocity": 80,
          "confidence": "confirmed",
          "evidence": [
            {"asset": "evidence/page-1.png", "page": 1, "measure": 3,
             "note": "Tie continues through measure 4"}
          ]
        }
      ]
    }
  ],
  "uncertainties": []
}
```

The all-zero hash only illustrates the field shape. It is not valid provenance
for a deliverable IR. In a real IR, calculate SHA-256 from the unchanged source
file and keep that source accessible when running the validator.

## Exact-number rule

`onset`, `duration`, `bpm`, and `beat_unit` are JSON strings containing a non-negative
integer or fraction, such as `"0"`, `"3/2"`, or `"245/2"`. Do not use JSON
floating-point numbers. A note starting halfway through beat 2 is represented
exactly, and triplets do not accumulate rounding error.

`onset` and `duration` use **quarter-note beats**. In 6/8, one full measure is
`6 * 4 / 8 = 3` quarter-note beats. A note may extend beyond its starting
measure, but it may not extend beyond the declared score end.

## Top-level fields

- `schema_version`: must be `"0.1"`.
- `source.path`: the one canonical source PDF/image path. Relative paths resolve from the IR file and are preferred for portable deliverables that do not expose local directories.
  By default, the path must resolve to an accessible file; a missing source is
  a validation error.
- `source.sha256`: 64 hexadecimal characters. The validator recomputes it by
  default and requires an exact match with the accessible source file.
- `score`: written timeline and MIDI resolution.
- `parts`: one object per independent MIDI part/track.
- `uncertainties`: explicit unresolved or disclosed issues.

Additional provenance fields may be included, but the compiler must not treat
them as musical facts unless this schema defines them.

## `score`

- `measure_count`: number of complete written measures to retain, including
  silent leading and terminal measures.
- `ppq`: MIDI pulses per quarter note. Every exact event position and duration
  must convert to an integer tick at this PPQ. Each emitted track must also
  have an event often enough that no delta exceeds MIDI's four-byte VLQ limit;
  the validator checks this before compilation.
- `tempo_map`: `{measure, onset, bpm, beat_unit}` events. `beat_unit` is the
  metronome-mark beat expressed in quarter-note units: eighth=`"1/2"`,
  quarter=`"1"`, dotted quarter=`"3/2"`, and half=`"2"`. MIDI quarter-note
  BPM is `bpm * beat_unit`. A tempo is required at measure 1, onset `"0"`;
  mid-measure changes are allowed. Never copy a dotted-quarter marking into
  `bpm` while leaving `beat_unit` at `"1"`.
- `meter_map`: `{measure, numerator, denominator}` changes at measure starts.
  The denominator must be a power of two, and measure 1 must define a meter.
- `key_map`: `{measure, fifths, mode}` changes at measure starts. `fifths` is
  from -7 to 7; `mode` is `"major"` or `"minor"`.

Maps should be sorted and may not contain duplicate positions. v0.1 treats
every written measure as a complete measure under its active meter.

## `parts` and notes

Each part contains:

- `id`: stable unique identifier.
- `name`: user-facing MIDI track name.
- `channel`: 0-15. Channel 9 (the General MIDI percussion channel) is excluded
  in v0.1.
- `program`: General MIDI program number 0-127. If the score gives no
  instrument, use an explicitly disclosed placeholder rather than claiming it
  is source evidence.
- `notes`: normalized note attacks.

Each note contains:

- `measure`: printed/written starting measure, 1-based.
- `onset`: quarter-note offset from that measure start.
- `duration`: sounding duration in quarter-note beats. Merge every confirmed
  same-pitch tie chain, including chains that cross barlines.
- `midi_note`: 0-127.
- `velocity`: 1-127.
- `confidence`: `"confirmed"`, `"inferred"`, or `"unresolved"`.
- `evidence` (optional): list of source evidence objects. An object can contain
  `asset`, `page`, `measure`, `region`, and a short `note`.

Evidence objects are review pointers, not independently authenticated inputs.
The validator verifies the canonical source file and hash but does not check
that every cited asset exists in an evidence manifest. The delivery workflow
must cross-check those pointers against the manifest and source imagery.

`inferred` notes are reported as warnings. `unresolved` notes block normal
validation and compilation. Within one part, two events of the same MIDI pitch
must not overlap. Consecutive same-pitch events may meet at the same tick when
the score explicitly retriggers the note. Independent voices with overlapping
events of the same pitch therefore require separate unambiguous parts; if that
separation is not defensible from the source, v0.1 must stop.

## Uncertainties

Each uncertainty has at least:

```json
{
  "id": "m44-fall",
  "description": "Downward fall has no specified pitch range",
  "severity": "disclosed",
  "location": {"measure": 44, "part": "melody"}
}
```

`blocking` means the musical timeline cannot be honestly compiled until the
issue is resolved. `disclosed` records a known limitation that does not change
the confirmed note timeline. The validator rejects blocking uncertainties by
default.

## v0.1 scope boundary

v0.1 supports clear printed or screenshot-based scores that can be normalized
to a linear written timeline with complete measures. It supports multiple
parts and changes of meter, tempo, and key at declared positions.

Do not encode these as if v0.1 understood them:

- pickup or other incomplete measures;
- repeat expansion, volta endings, D.C., D.S., or Coda navigation;
- transposing instruments or written-versus-concert-pitch conversion;
- percussion notation or General MIDI drum mapping;
- unquantified ornaments, glissandi, falls, pedal, or rubato.

If any excluded feature affects pitch, timing, or playback order, add a
`blocking` uncertainty and stop. A disclosed uncertainty is sufficient only
when the confirmed MIDI timeline remains unambiguous.

## Validation

```powershell
python scripts/validate_score_ir.py score.json
```

The command prints a JSON report and exits with status 0 only when there are no
errors. Warnings remain visible. Normal audited validation requires access to
the unchanged source and verifies its SHA-256; an unavailable source or hash
mismatch blocks delivery. `--no-source-hash-check` skips both accessibility and
hash verification for diagnostics only. CLI overrides for unresolved notes or
blocking uncertainties likewise are not the normal audited delivery path.
Audit paths are redacted to filenames by default; `--include-local-paths`
opts into absolute paths for local diagnostics.
