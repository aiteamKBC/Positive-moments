"""
Just enough MP4 to answer "how long is this file, really?".

WHY NOT FFPROBE
---------------
FFprobe is the authority and Phase 6A used it, but it is a binary that only the
media worker image is guaranteed to have. The Python platform needs the same
number to plan a cut, and shelling out to a tool that may not be installed is a
runtime dependency the planner should not carry.

So this reads the MP4 movie header directly. Teams recordings are written
faststart - `ftyp` then `moov` - so the header arrives in one range request and
the whole answer costs a few megabytes instead of a gigabyte. Phase 6A checked
this reader against FFprobe on five real recordings and the durations agreed to
the millisecond on every one.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It is not a demuxer. It reads `mvhd` for the movie duration and walks each
`trak` for its handler and codec, and it stops there. Anything that needs frame
accuracy should use FFprobe against the same bytes.
"""
import struct
from dataclasses import dataclass, field


MOVIE_HEADER_SOURCE = "mp4_movie_header"

# The moov atom on the measured recordings is about 4 MB. Asking for a little
# more costs nothing and avoids a second round trip on a longer lecture.
DEFAULT_HEADER_BYTES = 8 * 1024 * 1024


class Mp4ReadError(Exception):
    """The bytes were not a readable MP4 movie header."""


@dataclass(frozen=True)
class TrackInfo:
    handler: str
    codec: str | None
    duration_seconds: float


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    timescale: int
    source: str
    container: str = "mp4"
    tracks: list[TrackInfo] = field(default_factory=list)

    @property
    def video_codec(self) -> str | None:
        return next((t.codec for t in self.tracks if t.handler == "vide"), None)

    @property
    def audio_codec(self) -> str | None:
        return next((t.codec for t in self.tracks if t.handler == "soun"), None)


def _boxes(buffer: bytes, start: int, end: int):
    """
    Yield (name, body_offset, declared_end) for each box in [start, end).

    `declared_end` is what the box header CLAIMS, not what the buffer happens
    to hold. A caller that needs the whole box - `read_movie_header` does -
    compares the two and refuses a short read rather than parsing whatever
    arrived.
    """
    offset = start
    while offset + 8 <= end and offset + 8 <= len(buffer):
        size = struct.unpack(">I", buffer[offset:offset + 4])[0]
        name = buffer[offset + 4:offset + 8].decode("latin-1", "replace")
        body = offset + 8
        if size == 1:
            if offset + 16 > len(buffer):
                return
            size = struct.unpack(">Q", buffer[offset + 8:offset + 16])[0]
            body = offset + 16
        elif size == 0:
            size = end - offset
        if size < 8:
            return
        yield name, body, offset + size
        offset += size


def _version_duration(buffer: bytes, body: int) -> tuple[int, int]:
    """(timescale, duration) from an mvhd or mdhd box body."""
    version = buffer[body]
    if version == 1:
        timescale = struct.unpack(">I", buffer[body + 20:body + 24])[0]
        duration = struct.unpack(">Q", buffer[body + 24:body + 32])[0]
    else:
        timescale = struct.unpack(">I", buffer[body + 12:body + 16])[0]
        duration = struct.unpack(">I", buffer[body + 16:body + 20])[0]
    if timescale <= 0:
        raise Mp4ReadError("the movie header declared a zero timescale")
    return timescale, duration


def _codec(buffer: bytes, minf_start: int, minf_end: int) -> str | None:
    """The first sample entry's four-character code, e.g. avc1 or mp4a."""
    for name, body, end in _boxes(buffer, minf_start, minf_end):
        if name != "stbl":
            continue
        for inner, inner_body, inner_end in _boxes(buffer, body, end):
            if inner != "stsd":
                continue
            # stsd: version/flags (4) then entry_count (4), then entries.
            for entry, _entry_body, _entry_end in _boxes(
                    buffer, inner_body + 8, inner_end):
                return entry
    return None


def read_movie_header(buffer: bytes) -> MediaProbe:
    """
    Parse a faststart MP4 header.

    Raises rather than returning a partial answer: a duration that came from a
    truncated header is a plausible number with nothing behind it, and this
    value decides where a lecture gets cut.
    """
    duration_seconds = None
    timescale = None
    tracks: list[TrackInfo] = []
    saw_moov = False

    for name, body, end in _boxes(buffer, 0, len(buffer)):
        if name == "mdat" and not saw_moov:
            raise Mp4ReadError(
                "the movie header follows the media data; this file is not "
                "faststart and cannot be measured from its first bytes")
        if name != "moov":
            continue
        saw_moov = True
        if end > len(buffer):
            raise Mp4ReadError("the movie header was truncated")
        for inner, inner_body, inner_end in _boxes(buffer, body, end):
            if inner == "mvhd":
                timescale, raw = _version_duration(buffer, inner_body)
                duration_seconds = raw / timescale
            elif inner == "trak":
                handler, codec, track_duration = "", None, 0.0
                for box, box_body, box_end in _boxes(buffer, inner_body, inner_end):
                    if box != "mdia":
                        continue
                    for media, media_body, media_end in _boxes(
                            buffer, box_body, box_end):
                        if media == "mdhd":
                            scale, raw = _version_duration(buffer, media_body)
                            track_duration = raw / scale
                        elif media == "hdlr":
                            handler = buffer[media_body + 8:media_body + 12].decode(
                                "latin-1", "replace")
                        elif media == "minf":
                            codec = _codec(buffer, media_body, media_end)
                if handler:
                    tracks.append(TrackInfo(handler, codec, track_duration))

    if not saw_moov:
        raise Mp4ReadError("no movie header was found in the supplied bytes")
    if duration_seconds is None or not duration_seconds > 0:
        raise Mp4ReadError("the movie header did not declare a usable duration")

    return MediaProbe(duration_seconds=duration_seconds, timescale=timescale,
                      source=MOVIE_HEADER_SOURCE, tracks=tracks)
