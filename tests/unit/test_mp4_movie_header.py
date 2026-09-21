"""
Phase 6A: the MP4 movie-header reader.

This reader decides how long a recording is, and that number decides where a
lecture gets cut. So the tests are mostly about what it REFUSES: a truncated
header, a file whose header sits after the media, a zero timescale. Every one
of those could otherwise produce a confident number with nothing behind it.

The durations it produces were checked against FFprobe on five real recordings
in Phase 6A and agreed to within one millisecond; these tests keep the parsing
honest without needing the network.
"""
import struct

import pytest

from app.media.mp4 import MOVIE_HEADER_SOURCE, Mp4ReadError, read_movie_header


def box(name: str, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + name.encode("latin-1") + payload


def mvhd(timescale: int, duration: int, version: int = 0) -> bytes:
    if version == 1:
        payload = (bytes([1]) + b"\x00\x00\x00"
                   + b"\x00" * 16                      # creation + modification
                   + struct.pack(">I", timescale)
                   + struct.pack(">Q", duration))
    else:
        payload = (b"\x00" * 4                          # version + flags
                   + b"\x00" * 8                        # creation + modification
                   + struct.pack(">I", timescale)
                   + struct.pack(">I", duration))
    return box("mvhd", payload + b"\x00" * 80)


def mdhd(timescale: int, duration: int) -> bytes:
    payload = (b"\x00" * 4 + b"\x00" * 8
               + struct.pack(">I", timescale) + struct.pack(">I", duration)
               + b"\x00" * 4)
    return box("mdhd", payload)


def hdlr(handler: str) -> bytes:
    return box("hdlr", b"\x00" * 8 + handler.encode("latin-1") + b"\x00" * 12)


def minf(codec: str) -> bytes:
    entry = box(codec, b"\x00" * 8)
    stsd = box("stsd", b"\x00" * 4 + struct.pack(">I", 1) + entry)
    return box("minf", box("stbl", stsd))


def trak(handler: str, codec: str, timescale: int, duration: int) -> bytes:
    return box("trak", box("mdia", mdhd(timescale, duration) + hdlr(handler)
                           + minf(codec)))


def faststart(movie: bytes, media: bytes = b"\x00" * 32) -> bytes:
    return box("ftyp", b"isom" + b"\x00" * 12) + box("moov", movie) + box("mdat", media)


# --- the happy path ---------------------------------------------------------

def test_it_reads_the_movie_duration():
    probe = read_movie_header(faststart(mvhd(1000, 14400064)))
    assert probe.duration_seconds == pytest.approx(14400.064)
    assert probe.timescale == 1000
    assert probe.source == MOVIE_HEADER_SOURCE


def test_it_reads_a_64_bit_movie_header():
    """Long recordings are written with version 1 boxes."""
    probe = read_movie_header(faststart(mvhd(10000, 126198410, version=1)))
    assert probe.duration_seconds == pytest.approx(12619.841)


def test_it_reports_the_tracks_and_their_codecs():
    movie = (mvhd(1000, 2870336)
             + trak("vide", "avc1", 10000, 28703360)
             + trak("soun", "mp4a", 16000, 45925376))
    probe = read_movie_header(faststart(movie))
    assert probe.video_codec == "avc1"
    assert probe.audio_codec == "mp4a"
    assert len(probe.tracks) == 2
    video = next(track for track in probe.tracks if track.handler == "vide")
    assert video.duration_seconds == pytest.approx(2870.336)


def test_a_file_with_no_tracks_still_yields_a_duration():
    probe = read_movie_header(faststart(mvhd(1000, 924544)))
    assert probe.duration_seconds == pytest.approx(924.544)
    assert probe.video_codec is None
    assert probe.audio_codec is None


# --- refusals ---------------------------------------------------------------

def test_a_file_whose_header_follows_the_media_is_refused():
    """
    Not faststart. The duration is knowable, but not from the first few
    megabytes, and quietly reading fewer bytes than needed is how a truncated
    header becomes a wrong number.
    """
    data = (box("ftyp", b"isom" + b"\x00" * 12)
            + box("mdat", b"\x00" * 64)
            + box("moov", mvhd(1000, 1000)))
    with pytest.raises(Mp4ReadError, match="faststart"):
        read_movie_header(data)


def test_a_truncated_movie_header_is_refused():
    complete = faststart(mvhd(1000, 14400064)
                         + trak("vide", "avc1", 10000, 144000640))
    with pytest.raises(Mp4ReadError):
        read_movie_header(complete[:len(complete) // 2])


def test_bytes_with_no_movie_header_are_refused():
    with pytest.raises(Mp4ReadError, match="no movie header"):
        read_movie_header(box("ftyp", b"isom" + b"\x00" * 12))


def test_a_zero_timescale_is_refused_rather_than_divided_by():
    with pytest.raises(Mp4ReadError, match="timescale"):
        read_movie_header(faststart(mvhd(0, 1000)))


def test_a_zero_duration_is_refused():
    """An unmeasured recording looks exactly like this."""
    with pytest.raises(Mp4ReadError, match="usable duration"):
        read_movie_header(faststart(mvhd(1000, 0)))


def test_empty_input_is_refused():
    with pytest.raises(Mp4ReadError):
        read_movie_header(b"")
