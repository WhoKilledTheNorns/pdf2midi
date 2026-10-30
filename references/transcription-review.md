# Transcription review for PDF and image scores

Use this reference only when reconstructing notation from PDF pages, scans, screenshots, or mixed visual sources.

## 1. Establish the evidence inventory

1. Hash the source before processing and leave it unchanged.
   v0.1 accepts exactly one canonical source file. For a PDF, that PDF hash is
   the provenance root; embedded images and rendered pages are derived assets,
   not additional canonical inputs.
2. Record PDF page count, page size, encryption, text-layer status, and embedded-image inventory.
3. Prefer native embedded images for note reading. Render whole pages as layout evidence or when no embedded images exist.
4. Give every evidence image a stable identifier and record its printed measure range and visible staves.

The evidence manifest binds derived file hashes to the canonical source, but
the IR validator does not authenticate each note's free-form evidence entry.
Confirm that cited assets occur in the manifest and visually inspect every
cited region. Manifest PDF metadata can expose author or producer details, so
review the manifest before external sharing.

Interface artifacts are not notation. Exclude playback lines, selection colors, editor highlights, page decorations, video overlays, and application controls.

## 2. Freeze the written timeline

Create a structure table before transcribing notes:

- part, staff, clef, and voice identity;
- printed first and last measure numbers;
- meter, tempo, and key changes with their effective measure and onset;
- leading, internal, and terminal empty measures;
- pickup, repeats, endings, navigation marks, and final barline;
- screenshot overlap and missing-range analysis.

When screenshots overlap, align them by printed measure numbers and compare the shared notes. A repeated screenshot range is not a musical repeat. Never append both copies.

## 3. Transcribe with measure closure

For each part and measure:

1. Determine pitches using clef, key signature, accidentals, octave signs, and any parallel notation.
2. Determine durations jointly from noteheads, stems, flags, beams, dots, tuplets, rests, and any simplified-notation underlines.
3. Verify that voices close to the active meter. A visually plausible note sequence that does not close is unresolved.
4. Preserve simultaneous notes as separate events at the same onset and keep independent voices separate.
5. Track every tie segment across system and page boundaries. Merge only adjacent same-pitch segments explicitly joined by a tie; a slur or phrase mark does not suppress retriggering.

Beam direction matters. In an ambiguous three-note group, distinguish `sixteenth + sixteenth + eighth` from `eighth + sixteenth + sixteenth` by inspecting both beam counts and the measure's remaining duration.

v0.1 cannot safely flatten independent voices whose same pitch overlaps within
one part/channel. If those voices cannot be separated into unambiguous parts,
record a blocking uncertainty and stop.

## 4. Confidence and stopping rules

Use these statuses:

- `confirmed`: directly supported by readable notation;
- `inferred`: a constrained interpretation supported by context but not fully legible;
- `unresolved`: more than one musically valid interpretation remains.

Any structural uncertainty, unresolved pitch, unresolved duration, or missing measure is blocking in v0.1. Record the evidence location and request a clearer source or user decision. Do not fill the gap with a musically likely guess.

Unspecified performance details are different from unreadable notation. A missing instrument name, dynamic, or bend range may use a clearly disclosed neutral playback default, while the written pitch and rhythm still remain confirmed.

## 5. Event-level review

Before compilation, verify:

- first note absolute measure/onset;
- last note onset and release position;
- every nonempty and empty measure range;
- cross-measure tie chains;
- repeated-section equivalence;
- active tempo, meter, and key at each change;
- note range and part assignment;
- total written duration to the final barline.

After compilation, compare the MIDI back to the canonical IR. File readability alone is insufficient: a structurally valid MIDI can still contain duplicated screenshot ranges, a wrong BPM, shortened empty tails, or a tie retriggered at a barline.
