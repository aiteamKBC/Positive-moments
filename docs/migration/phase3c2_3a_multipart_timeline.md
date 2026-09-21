# Phase 3C2.3A: the real multi-part seam and timeline

Investigation only. No model call, no render, no write, no Graph call.

Target: **Andrew-Scheduling Professional (SP) Jan 2026**, 2026-09-16,
`25e85615-aa7a-5f49-bb40-078d7c7b65d0`.

## 1. Both open questions are now answered

| Observation | Verdict |
| --- | --- |
| cue timeline starts at 167 min | **Correct provider semantics.** The origin is already in part 1's *raw* VTT, before any offset. 103 existing legacy production sessions share the pattern. |
| 61 s backward seam overlap | **Real double coverage.** The same audio is represented twice, by two divergent transcriptions. Legacy has no deduplication either. |

## 2. Part inventory

Both parts belong to **one Teams call**, `be51850b-a2fc-464f-a888-4d5199584c54`.

| | part 1 (primary) | part 2 |
| --- | --- | --- |
| ptid sha12 | `6c48911ce4bc` | `e6db98d758f1` |
| createdDateTime | 08:13:41.151256Z | 12:12:37.232921Z |
| provider end | 12:13:41.151256Z (**exactly +4 h**) | 13:00:27.492921Z |
| provider span | 14,400.0 s | 2,870.26 s |
| content bytes / sha12 | 74,429 / `0aa774ffc774` | 54,449 / `1276bcf66142` |
| raw cues | 350 | 251 |
| raw first cue | **10,026,775 → 10,031,735 ms** | 6,850 → 7,170 ms |
| raw last cue | 14,395,777 → 14,403,937 ms | 2,857,770 → 2,858,410 ms |
| raw span | 4,377,162 ms (72.95 min) | 2,851,560 ms (47.53 min) |
| offset applied | 0 | **14,336,082 ms** |

## 3. Why the origin is 167 minutes

**Part 1's raw VTT already begins at 10,026,775 ms with offset 0.** Nothing in
our pipeline introduced it.

Offset arithmetic, exact:

```
part2.createdDateTime - part1.createdDateTime
  = 12:12:37.232921 - 08:13:41.151256 = 14,336.081665 s = 14,336,082 ms
stored part_offset_ms                    = 14,336,082 ms      MATCH
```

Wall-clock reconstruction against the call start (08:13:41.151):

| | offset | wall clock | vs schedule |
| --- | ---: | --- | ---: |
| first canonical cue | 10,026,775 ms | **11:00:47.9Z** | +47.9 s after `scheduled_start` 11:00 |
| last canonical cue | 17,195,372 ms | **13:00:16.5Z** | +16.5 s after `scheduled_end` 13:00 |

The Teams call was opened at 08:13 and left running; Andrew's lecture occupies
11:00:47–13:00:16 inside it. `02:47:06` means **"2 h 47 min since the call
began"**, not "2 h 47 min into the lecture".

Competing explanations tested: **(A/C) provider call-relative timestamps —
CONFIRMED**. (B) double-applied offset — **refuted**, the origin is in the raw
file with offset 0 and the canonical first cue equals the raw first cue exactly.
(D) other transformation — **refuted**, canonical first cue = raw first cue,
byte for byte.

**Duration is a span, not an endpoint:** 17,195,372 − 10,026,775 = 7,168,597 ms
= **119.48 min**, matching the combined transcript and `duration_difference_ms = 0`.
`last cue = 04:46:35` does **not** mean a 4 h 46 lecture.

## 4. The seam

```
part 1 content ends   14,403,937 ms   (12:13:45Z)
part 2 content starts 14,342,932 ms   (12:12:44Z)
overlap                   61,005 ms   (61.005 s)
part 1 cues in window: 6      part 2 cues in window: 9
```

### Is it duplicated dialogue?

Text comparison says no; **speech density says yes**, and density is decisive.

| test | result |
| --- | --- |
| exact normalized (speaker, text) duplicates | **0** |
| part 1 overlap cues with a time-proximate identical twin | 0 / 6 |
| part 1 overlap text found anywhere in part 2 (8-token probe) | 0 / 6 |
| best close match ≥ 0.50 | none |
| difflib ratio over the window | 0.0158 (control, adjacent windows: 0.0319) |
| jaccard over the window | 0.1927 (control: 0.2294) |
| cross-correlation over ±180 s | flat ~0.28, **no peak** |
| **part 1 speech in the 61 s window** | **50,085 ms = 82.1 %** |
| **part 2 speech in the same window** | **50,405 ms = 82.6 %** |
| **combined** | **100,490 ms = 164.7 %** |
| control: single-part density over 15 comparable 61 s windows | min 0 %, **median 89.9 %, max 102.6 %** |

Each part alone shows a perfectly normal ~82 % density; together they claim
**164.7 % of a 61-second window**, which one audio stream cannot produce —
especially as both sides are dominated by the *same* speaker hash. The window
is therefore covered twice by two independent ASR passes whose wording and
segmentation diverge so far that no token-level test recognises them as the
same speech.

**Conclusion: real duplicate coverage, ~61 s, 15 cues, no duplicate text.**

## 5. Legacy behaviour — traced from executable code

`Combine Transcripts` (child workflow, 6,077 chars) does exactly this:

```js
validParts.sort((a, b) => a.partCreatedMs - b.partCreatedMs);
const sessionStartMs = Math.min(...validParts.map(p => p.partCreatedMs));
const offsetMs = part.partCreatedMs - sessionStartMs;      // identical to ours
shiftVttTimestamps(part.text, offsetMs);                   // add offset only
combinedSections.filter(Boolean).join('\n\n');             // plain concatenation
durationMinutes = Math.round((range.max - range.min) / 60000);
```

Therefore legacy:

- **preserves** cue timestamps and only shifts by the createdDateTime delta;
- **never** subtracts the first cue or normalizes to a 00:00 origin;
- **performs no seam deduplication**, no overlap removal and no re-sorting;
- computes duration as `max − min`, i.e. a span.

### Legacy evidence is call-relative — proven on production data

Scanning all 7,209 legacy checklist rows with evidence:

- hour buckets `{0: 42834, 1: 27271, 2: 10926, 3: 468, 7: 1, 15: 6}`;
- **103 of 634** sessions have evidence exceeding their own stored duration by
  more than 10 minutes;
- for those, **`max − min` ≈ the stored duration** (deltas of 1–4 min), with
  origins such as `01:49:50`, `01:27:16`, `01:16:05`, `01:03:22`.

That is precisely Andrew's shape. Legacy QA has been writing call-relative
evidence into production for 16 % of sessions.

### Real legacy multi-part fixture: **NO**

Decoding all 651 legacy `session_id`s yielded 641 callIds, of which exactly
**one** carries more than one session: call `c8159bc1…`, 2026-02-20,
"Ray - MSP - Managing Successful Programmes", two rows (durations 1 h 49 and
2 h 04, both evidence starting ~00:19). Legacy produced **two separate QA
sessions**, not one combined session — most likely the 21:00 and 23:00 Master
runs picking different artifacts. So no production example of legacy
*combining* parts exists, and behaviour had to be derived from the code above.

## 6. Legacy replay parity on Andrew

| dimension | result |
| --- | --- |
| `PART_SELECTION_PARITY` | **exact** — 2 parts, same two artifacts, ordered by `createdDateTime` |
| `OFFSET_PARITY` | **exact** — 14,336,082 ms by both, from the same formula |
| `CUE_TIMESTAMP_PARITY` | **exact** — canonical first cue = raw first cue = 10,026,775 ms |
| `SEAM_PARITY` | **exact** — legacy also concatenates without dedup, so legacy would carry the same 61 s double coverage |
| `DURATION_PARITY` | **exact** — `max − min` = 7,168,597 ms = 119 min, `duration_difference_ms = 0` |

Our pipeline reproduces legacy multi-part behaviour with no divergence.

## 7. Downstream coordinate systems

| consumer | assumption | status for a non-zero origin |
| --- | --- | --- |
| **QA evidence** | cue timestamps as-is | **works** — matches legacy and 103 production precedents |
| **Positive Clips → FFmpeg** | `clip.start/end` → `EXTRACT(EPOCH …)` → `start_seconds` → `ffmpeg -ss` | **unverified** — correct only if the recording spans the whole call |
| **Lecture Parts** | `_zone()` filters `lo*duration <= start_seconds <= hi*duration` | **broken** — assumes a 0-origin |

`planner._zone` is the concrete failure. For Andrew, `duration = 7168.6 s`
while every cue is ≥ 10,026.8 s, so `cut_1` (28–38 % → 2,007–2,724 s) and
`cut_2` (62–72 % → 4,445–5,161 s) both select **zero cues** and
`build_candidate_windows` raises `no transcript cues fall inside the cut_1
zone`. This affects every lecture with a non-zero origin, not only multi-part
ones.

The media question could not be settled from persisted data: only **1 of 651**
legacy sessions has `recording_duration_seconds`, and that one has a ~0 origin
(evidence 00:02:18–02:00:09 inside a 02:26:16 recording), so it does not
exercise the call-relative case. Answering it needs one recording's true
duration for a non-zero-origin session — deliberately not fetched here.

## 8. Classification: **D — MULTIPLE_ISSUES**

1. **`EXPECTED_PROVIDER_CALL_RELATIVE_TIMELINE`** for the 167-minute origin —
   correct source semantics, needs a presentation/media coordinate layer.
2. **`PROVIDER_DUPLICATE_PART_OVERLAP_REQUIRES_DEDUP`** for the seam — 61 s of
   audio counted twice.

Not `SAFE_AS_IS`, and not `COMBINATION_OFFSET_DEFECT`: the offset is provably
correct and byte-identical to legacy.

## 9. Proposed correction (designed, **not implemented**)

Nothing existing is mutated. Both changes are additive and versioned.

### 9a. Seam deduplication — new canonical parser version

- New version `canonical_cues_v2_seam_dedup` alongside
  the existing parser version; existing documents and fingerprints untouched
  and still reproducible.
- Deterministic rule: within the interval covered by two parts, keep the cues
  of the **earlier-created part** and drop the later part's cues that start
  before the earlier part's last cue end. Earlier-part-wins is chosen because
  part 1's transcription of that window is contiguous with everything before
  it; the alternative (later-wins) would splice mid-sentence.
- Record `seam_overlap_ms`, `seam_dropped_cue_count` and the rule version in
  `lecture_transcript_documents.metadata`.
- **Tables affected:** `lecture_transcript_documents`, `lecture_transcript_cues`
  (new rows under the new version only).
- **Reprocessing scope:** multi-part lectures only — currently **one** (Andrew).
- **Downstream invalidation:** none, because no 3A/3B output exists for Andrew.

### 9b. Presentation coordinate — additive, not a rewrite

Keep the canonical call-relative timestamp as the single source of truth (it is
what legacy stores and what the recording most likely uses), and add a derived
`lecture_origin_ms` on the document plus a presentation offset for consumers
that need 0-origin. **Option 3** — store both — is the right answer:

- Option 1 alone breaks Lecture Parts and risks Positive Clips.
- Option 2 alone breaks parity with 103 legacy production rows and would make
  our evidence disagree with every historical session.
- Option 3 preserves legacy parity exactly while giving media consumers a
  coordinate they can use, and lets each consumer declare which it wants.

**Do not implement** until the recording coordinate question (§7) is settled —
choosing the media coordinate before knowing whether recordings are call-length
or lecture-length would be guessing.

## 10. Before Andrew's Phase 3A may start

1. Decide and implement the seam rule (9a) — otherwise the model sees 61 s of
   duplicated speech and may cite the same moment twice.
2. Settle the recording coordinate empirically for one non-zero-origin session.
3. Fix `planner._zone` to tolerate a non-zero origin (Lecture Parts only; not a
   QA blocker).
4. Re-run 2C1→2C4 for Andrew under the new parser version.
5. Then one scoped Phase 3A call, then 3B, then a scoped DRY_RUN.

Items 1 and 4 are mandatory before 3A. Items 2 and 3 block Positive Clips and
Lecture Parts, not the QA canary.
