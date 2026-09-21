# Phase 6A — Media recording coordinate validation

**Status: PROVEN against real media.** The transform is implemented, versioned
and covered by tests.

This document replaces the earlier investigation note of the same name. That
note ended undecided because the `driveItem` route returned 403 and no media
duration had been measured. Both of those are now resolved, and one of its
conclusions turned out to be wrong — see §7.

---

## 1. The question

> Given a time on the canonical transcript timeline, which recording file is
> that moment in, and how many seconds into that file does it sit?

The legacy pipeline assumed `media_time == transcript_time`. This phase had to
establish what is actually true, against the bytes.

---

## 2. Media access: the 403 was route-specific, not permission-wide

The previous investigation stopped at:

```json
"drive_item_error": { "code": "accessDenied", "status": 403 }
```

and was about to be reported as a tenant permission blocker.

It is not. The **driveItem** route is blocked for this application
registration; the **callRecording content** route is not:

```
GET /users/{userId}/onlineMeetings/{meetingId}/recordings/{recordingId}/content
```

| Lecture | Result |
| --- | --- |
| Andrew-Scheduling Professional (SP) Jan 2026 | `HTTP 206` `video/mp4` |
| Risk Management | `HTTP 206` `video/mp4` |
| Project Planning & Control (PPC) \| Andrew | `HTTP 206` `video/mp4` |

The route honours `Range`, so a multi-hundred-megabyte recording can be read a
few megabytes at a time.

**No tenant change is required and none was requested.** `Files.Read.All` was
not asked for. The existing application credentials are sufficient.

Two operational consequences:

* Graph **streams** the bytes rather than redirecting to a signed storage URL,
  so any reader must present the bearer token. For local diagnostics this was
  handled with a loopback range-proxy so that no token ever appeared in a
  process argument list.
* Recordings are addressed by `(user_id, meeting_id, recording_id)`. Nothing in
  the platform stores a media URL.

---

## 3. What the media actually is

FFprobe, over range requests, against every transcript part of the three
mandated lectures:

| Lecture | Part | Container duration | `start_time` | Video | Audio | Size |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| Andrew-Scheduling | 1 | 14 400.064 s | 0.000 | h264 | aac | 914.9 MB |
| Andrew-Scheduling | 2 | 2 870.336 s | 0.000 | h264 | aac | 349.7 MB |
| Risk Management | 1 | 14 400.064 s | 0.000 | h264 | aac | 943.0 MB |
| Risk Management | 2 | 924.544 s | 0.000 | h264 | aac | 10.3 MB |
| PPC | 1 | 12 619.841 s | 0.000 | h264 | aac | 326.5 MB |

Two facts settle the earlier doubt:

1. **The four-hour figures are real.** The previous note suspected they were a
   call-segment artefact. They are the true container durations.
2. **Every stream starts at 0.000 s.** There is no edit list to compensate for.

---

## 4. The structure nobody had seen: one recording per transcript part

Microsoft caps both a transcript and a recording at four hours, so a long call
produces several transcript parts **and several recordings** — one per part.

| Lecture | Part | `part_offset_ms` | `recording.createdDateTime` − `part.provider_created_at` | Graph window | Measured media |
| --- | ---: | ---: | ---: | ---: | ---: |
| PPC | 1 | 0 | **+0.000 s** | 12 619.8 s | 12 619.841 s |
| Andrew | 1 | 0 | **+0.000 s** | 14 400.0 s | 14 400.064 s |
| Andrew | 2 | 14 336 082 | **+0.000 s** | 2 870.3 s | 2 870.336 s |
| Risk | 1 | 0 | **+0.000 s** | 14 400.0 s | 14 400.064 s |
| Risk | 2 | 14 338 368 | **+0.000 s** | 924.5 s | 924.544 s |

So within a part, **media time is canonical time minus that part's offset**.

Consecutive recordings **overlap** — part 2 starts 64.0 s before part 1's media
ends for Andrew, 61.6 s for Risk — because Teams begins the next segment before
closing the last. A canonical instant inside the overlap therefore exists in
two files, and the transform has to make a choice to remain a function. The
later part wins.

---

## 5. Empirical proof against the audio

Arithmetic alone was not accepted. A transcript records when somebody was
speaking; so does the audio. FFmpeg `silencedetect` measured the speech/silence
pattern at candidate positions, and it was compared with the pattern the
transcript predicts (0.5 s resolution, Jaccard overlap on the speech class).

### 5.1 Candidate comparison

`A` = the transform. `B` = "the timeline starts at the first cue", the
planner's zero-origin assumption. `D` = an in-range control, `A + 180 s`, which
is always readable and always wrong — without it a meaningless score could look
like a pass.

| Lecture | Part | A (transform) | B (zero-origin) | D (+180 s control) |
| --- | ---: | ---: | ---: | ---: |
| Andrew | 1 | **0.903** | 0.000 | 0.433 |
| Andrew | 2 | **0.786** | 0.664 | 0.673 |
| Risk | 1 | **0.693** | 0.000 | 0.467 |
| Risk | 2 | **0.664** | 0.622 | 0.000 |
| PPC | 1 | **0.805** | 0.771 | 0.555 |

**A wins every comparison.** Where `B` scores 0.000 the recording is simply
silent at the position it predicts — the meeting had not started yet. Where `B`
scores close to `A`, it is because for a single-part lecture starting near zero
the two candidates are only a few seconds apart; there is no disagreement to
detect, which is the point.

### 5.2 Lag scan — the decisive test

Comparing two candidates only asks "which of these". The lag scan asks "of
every offset from −300 s to +300 s, which one aligns?".

| Lecture | Part | Best lag | Jaccard at peak | Jaccard at lag 0 | Background | Prominence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Andrew | 1 | **+1 s** | 0.905 | 0.872 | 0.429 | +0.443 |
| Andrew | 2 | **+1 s** | 0.866 | 0.840 | 0.764 | +0.076 |
| Risk | 1 | **+1 s** | 0.782 | 0.724 | 0.554 | +0.170 |
| PPC | 1 | **+1 s** | 0.870 | 0.831 | 0.769 | +0.062 |

Out of 601 candidate offsets, the maximum sits **1 second** from the
transform's prediction — every time, on four independently recorded parts of
three different lectures. That 1 s is a property of the measurement, not of the
mapping: `silencedetect` needs a silence to persist before it reports one, so
detected speech onsets land slightly late. A coordinate error would move the
peak by the size of the error, not by a constant.

Where prominence is low (Andrew part 2, PPC) the stretch is near-continuous
speech, which makes the test less sensitive — but the peak location is
unambiguous in all four.

Risk Management part 2 carries only 66 s of transcript in a 924 s recording, too
little to scan; it is covered by the candidate comparison above, where the
+180 s control scores **0.000** against the transform's 0.664.

**Andrew — the mandatory case — is proven at both parts.**

---

## 6. The transform

`app/media/coordinates.py`, version **`call_relative_part_media_v1`**.

```
part          = the part with the greatest canonical offset <= t
                (the later part owns the overlap)
media_time(t) = t - part.canonical_offset_seconds
```

A canonical **range** that crosses a recording boundary is returned as several
`MediaSegment`s, contiguous on the canonical timeline and non-overlapping, so
concatenating them reproduces the span exactly once — including the seconds
that genuinely exist in two files.

It **refuses rather than clamps**. A time that was never recorded raises
`OUTSIDE_RECORDED_MEDIA` instead of returning the nearest frame, because a
clamped coordinate produces a confidently delivered clip of the wrong moment.

### The one measured exception

`TAIL_TOLERANCE_SECONDS = 1.0`. A sweep of all 23 measured lectures found
exactly one, **Martech – Fri**, whose final cue ends **0.243 s** after the last
frame of its media. A cue's end time is rounded and a recording stops when the
host stops it, so a fractional overrun at the very end is normal. The tolerance
applies only to the tail, only by clamping to media that exists, and never at
the start; 30 s past the end is still refused.

---

## 7. What this overturns

The previous note recorded that `callRecording.createdDateTime` exactly equals
transcript part 1's `provider_created_at`, and concluded this "would make the
legacy identity mapping correct". That conclusion was wrong, for a reason it
could not have seen without the media:

* The origin offset really is zero — but only **per part**.
* A lecture's canonical timeline spans **all** its parts. Andrew's last cue is
  at 17 194.5 s, which does not exist in a 14 400 s file. Risk's last cue is at
  14 404.4 s — only **4.3 s** past the end of part 1, which is exactly the kind
  of error that produces a plausible-looking clip of the wrong thing.

The identity mapping is therefore correct for a single-part lecture starting
near zero, and wrong for every multipart lecture.

---

## 8. The planner fix

`automation/lecture_parts/planner.py::_zone` computed its candidate zones as
fractions of the **recording length**, assuming the lecture runs `0 → duration`.
For Andrew the cut-1 zone was computed as 3 600–6 048 s, a stretch containing no
cues at all, and planning raised *"no transcript cues fall inside the cut_1
zone"* before the AI was ever asked.

The planner now works on a `LectureTimeline` derived from the transcript: the
first cue's start to the last cue's end. Consequently:

* zones are fractions of **the lecture**, wherever it begins;
* part 1 starts at the lecture's first cue, not at 0.0 — for Andrew that
  removes nearly three hours of empty call from "the first third of the
  lecture";
* part 3 ends at the lecture's last cue, not at a recording length that for a
  multipart lecture is shorter than the lecture itself;
* shares are computed against the lecture span;
* the old guard that rejected any transcript running more than 120 s past "the"
  recording is gone — coverage is now `app.media.coordinates`' question,
  answered against every recording rather than one of them.

`SplitPlan` carries `timeline_start_seconds` and `timeline_end_seconds` so a
later reader never has to guess which axis a stored plan used.
`recording_duration_seconds` is retained as provenance only.

---

## 9. What was persisted

Migration **017** adds `public.lecture_recording_parts`: one row per (lecture,
transcript part) carrying the canonical offset, the recording identity and the
**measured** duration, with `media_duration_source` recording how it was
obtained. It stores no URL and no token.

**All 23 lectures with a selected transcript were measured; 0 unavailable.**
21 are single-part; Andrew-Scheduling and Risk Management have two parts each.

The duration is read from the MP4 movie header (`app/media/mp4.py`) rather than
by shelling out to FFprobe, because the Python planner cannot assume FFprobe is
installed. The two agree:

| Lecture | Part | FFprobe | Movie header | Δ |
| --- | ---: | ---: | ---: | ---: |
| PPC | 1 | 12 619.841 s | 12 619.840 s | 0.001 s |
| Andrew | 1 | 14 400.064 s | 14 400.064 s | 0.000 s |
| Andrew | 2 | 2 870.336 s | 2 870.336 s | 0.000 s |
| Risk | 1 | 14 400.064 s | 14 400.064 s | 0.000 s |
| Risk | 2 | 924.544 s | 924.544 s | 0.000 s |

---

## 10. Files

| File | What it is |
| --- | --- |
| `app/media/coordinates.py` | The one versioned transform |
| `app/media/mp4.py` | Movie-header duration reader |
| `app/media/recordings.py` | Correlate parts to recordings and measure them |
| `app/db/repositories/recording_parts.py` | Persistence |
| `app/db/migrations/017_create_recording_media_tables.sql` | Schema |
| `automation/lecture_parts/planner.py` | Zero-origin fix |
| `tests/unit/test_media_coordinates.py` | 24 tests |
| `tests/unit/test_mp4_movie_header.py` | 10 tests |
| `tests/unit/test_lecture_split_timeline.py` | 13 tests |
| `tests/integration/test_media_timeline_real_lectures.py` | 11 tests, real registry |

---

## 11. Still open

Nothing blocks the media phases. Two things are worth recording:

1. **Diagnostics need FFmpeg.** The production worker image ships it; the
   Python platform does not use it. A static build was fetched into the
   session scratchpad for this phase and installed nothing system-wide.
2. **A part that crosses a recording boundary needs concatenation.** The
   transform reports it (`crosses_recording_boundary`) and returns the
   segments; Phase 6C has to cut and join them rather than assuming one source
   file. The existing `qa_lecture_part_assets` schema carries a single
   `source_drive_id`/`source_item_id` pair and will need extending.
