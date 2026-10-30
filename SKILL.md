---
name: audited-score-to-midi
description: Convert clear printed sheet music in PDF or image form into a measure-aligned Standard MIDI file with an evidence-backed audit report. Use when BPM, meter, rests, ties, overlapping score screenshots, or terminal empty measures must be preserved; do not use for audio-to-MIDI, composition, or silently guessing unsupported navigation, transposing, percussion, or unreadable notation.
---

# Audited Score to MIDI

Produce a source-preserving MIDI transcription whose pitches, event timing, tempo map, and final measure boundary can be independently checked.

## Route the input

- Prefer a native notation source such as MusicXML or a MuseScore project when the user supplies one. Treat PDF or raster input as visual evidence, not structured music data.
- Use exactly one canonical source file in v0.1. For a PDF, its SHA-256 is the provenance root and extracted images are derived review aids bound through the evidence manifest. If the user supplies several separate images, request one ordered PDF or an explicitly selected canonical raster instead of hashing a folder or inferring file order.
- For a PDF, run `scripts/extract_score_evidence.py` when Poppler is available. Inspect embedded images before rendered pages; screenshots often retain more notation detail than the composed PDF page.
- If the input is an audio recording, route away from this skill. This skill does not perform audio transcription.

## Freeze the score structure first

Before entering notes, establish the written timeline: part and staff identities, clefs, printed measure numbers, total measure count, meter/tempo/key changes, pickup status, navigation marks, and terminal barline. Read [references/transcription-review.md](references/transcription-review.md) for PDF, scan, or screenshot reconstruction.

Do not concatenate screenshots in file order. Reconcile overlaps by printed measure number and musical context. Ignore playback cursors, selections, highlights, watermarks, and application chrome.

Stop and report a blocking uncertainty instead of guessing when v0.1 encounters any of these:

- an unreadable, cropped, or missing structural measure;
- any pickup, incomplete, or irregular measure; v0.1 represents only complete active-meter measures;
- any repeats, first/second endings, D.C., D.S., Segno, or Coda; v0.1 does not expand navigation even when the intended order appears explicit;
- transposing instruments, percussion notation, tablature, cross-staff voices, or ambiguous multiple voices, including overlapping same-pitch voices that cannot be represented safely in one v0.1 part/channel;
- tempo text without a numeric interpretation, or an ornament, bend, fall, pedal, or articulation without a defensible MIDI realization.

## Build the canonical score IR

Read [references/score-ir-schema.md](references/score-ir-schema.md), then create a `score.json` before writing MIDI.

- Express onset, duration, BPM, and metronome beat unit as rational strings, never binary floats. Normalize tempo with `beat_unit` rather than assuming every marking is quarter-note BPM.
- Preserve leading, internal, and terminal empty measures.
- Normalize same-pitch tie chains into one continuous note event, including ties crossing barlines. Do not merge repeated notes without a tie.
- Reconcile rhythm from all available evidence: staff notation, beams, rests, simplified-notation underlines, and per-measure duration closure.
- Record source hash, evidence locations, confidence, and unresolved items. Never promote an inference to confirmed evidence silently.
- Treat the evidence manifest as the provenance bridge from the canonical source to derived images. The IR validator checks the canonical source hash but does not independently authenticate each note's free-form `evidence` entry; before delivery, verify that every cited asset exists in the manifest and inspect the cited region.
- Do not invent instruments, dynamics, pedal, or ornament parameters. If the source omits them, use a disclosed neutral default only when needed for MIDI playback.

Validate before compilation:

```powershell
python scripts/validate_score_ir.py score.json --report score.ir-audit.json
```

Do not compile while the IR has blocking uncertainties or unresolved notes.

## Compile and independently validate

Compile the validated IR with the deterministic writer:

```powershell
python scripts/compile_midi.py score.json output.mid
```

Then use the independent parser; it must not rely on the compiler's internal representation:

```powershell
python scripts/validate_midi.py output.mid --ir score.json --report output.audit.json
```

For repeated written passages, use `scripts/compare_segments.py` to compare the complete segment semantics—timeline length, active tempo/meter/key state, changes, carry-in notes, and note events—instead of copying by visual resemblance alone.

The result must satisfy all of these invariants:

- Standard MIDI Format 1 with one conductor track and one track per score part;
- tempo, meter, and key meta-events at the correct absolute ticks;
- every note-on paired with exactly one note-off, with note-off ordered before a same-tick retrigger;
- no event outside the written timeline;
- no SysEx, controller, pitch-bend, aftertouch, MIDI Port, or other playback event outside the v0.1 compiler's explicit event set;
- every track's End-of-Track at the end of the final complete measure, not at the final sounding note;
- exact IR-to-MIDI agreement for part, pitch, onset, duration, and event count;
- source file unchanged and its hash reported.

When available, also open the result with an OS sequencer or notation program as a compatibility check. This supplements, but does not replace, binary validation.

## Deliver

Preserve the source and save outputs separately. Return links to the canonical `score.json`, its IR audit, the MIDI, the independent MIDI audit, and the evidence manifest when visual extraction was used. State the measure count, BPM/tempo-map status, meter, key signature, track count, duration, final sounding position, silent tail, defaults, and any notation intentionally left unencoded. Generated manifests and audits redact local paths by default; use `--include-local-paths` only for a local report that needs them. The evidence manifest still retains PDF metadata, so review every JSON deliverable before external sharing.
