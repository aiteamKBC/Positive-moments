"""
Canonical identity of a Microsoft Teams transcript.

WHY THIS EXISTS
---------------
The platform used the raw Graph transcript `id` string as identity: as the
artifact key, as the selection primary, and - through the renderer - as the
legacy `qa_doctors_sessions.session_id`. On 2026-09-22 Graph began returning
the SAME transcripts under a second serialization of that id. The raw string
changed; the transcript did not. 94 transcripts were stored twice, and three
lectures were written to `qa_doctors_sessions` a second time under the new
string beside n8n's existing row. See
docs/audits/QA_CORE_F01_F02_F03_ROOT_CAUSE_2026-09-22.md.

WHAT THE ID ACTUALLY IS
-----------------------
It is not opaque. Base64url-decoded, it is MessagePack in the MessagePack-CSharp
LZ4 envelope:

    array(2) [ ext(type 98, data = uncompressed length as a msgpack int),
               bin(LZ4 block) ]

and the decompressed LZ4 block is itself MessagePack:

    array(4) [ 4, "<thread id>", "<meeting timestamp, ms>", "<transcript id>" ]

The second serialization appends a fifth element, `nil`:

    array(5) [ 4, "<thread id>", "<meeting timestamp, ms>", "<transcript id>", nil ]

Every byte that differed between the 94 production pairs is a length field
growing by one to hold that `nil`: the array header (0x94 -> 0x95), the LZ4
literal length, the envelope's uncompressed length and the bin length. The
trailing 0xC0 is the `nil` itself. Nothing about the transcript differs.

So identity is the decoded tuple with trailing nils removed - not "the string
minus its last byte", which would be correct for exactly one serializer
version and silently wrong for the next.

WHAT THIS REFUSES TO DO
-----------------------
It never guesses. An id that does not decode completely and exactly - wrong
envelope, LZ4 length mismatch, trailing bytes, unknown format version, missing
field, a non-nil extra element - keeps its RAW identity. A raw identity equals
only the identical string, so an id this module does not understand can never
be merged with another. The failure mode is "not deduplicated", never
"wrongly deduplicated".

The raw id is never discarded: Graph fetches transcript content by the raw id,
and the raw id remains the physical artifact key.
"""
from __future__ import annotations

import base64
import binascii
import re
import struct
from dataclasses import dataclass


IDENTITY_VERSION = "teams_transcript_identity_v1"

# MessagePack-CSharp's extension type for an LZ4 block-compressed payload.
LZ4_BLOCK_EXT_TYPE = 98
# The only payload format version observed. A different version is a format we
# have not verified, so it is refused rather than interpreted.
SUPPORTED_FORMAT_VERSION = 4

SOURCE_DECODED = "DECODED"
SOURCE_RAW = "RAW"

# Hard bounds: a transcript id is a few hundred bytes. Anything claiming to
# decompress to more is not one, and must not be allowed to allocate.
MAX_ENCODED_BYTES = 4096
MAX_DECODED_BYTES = 16384


class IdentityDecodeError(ValueError):
    """The id is not a transcript id this module can prove it understands."""


@dataclass(frozen=True)
class TranscriptIdentity:
    key: str
    source: str
    thread_id: str | None = None
    meeting_timestamp: str | None = None
    transcript_id: str | None = None

    @property
    def decoded(self) -> bool:
        return self.source == SOURCE_DECODED


def canonical_transcript_identity(raw_id) -> TranscriptIdentity:
    """
    The canonical identity of one provider transcript id. Total: never raises.

    Two ids have the same `key` if and only if they decode to the same
    (thread, meeting timestamp, transcript) triple, or they are the identical
    raw string.
    """
    text = "" if raw_id is None else str(raw_id).strip()
    if not text:
        return TranscriptIdentity(key="raw:", source=SOURCE_RAW)
    try:
        thread_id, meeting_timestamp, transcript_id = _decode(text)
    except IdentityDecodeError:
        return TranscriptIdentity(key=f"raw:{text}", source=SOURCE_RAW)
    return TranscriptIdentity(
        key=(f"teams:v{SUPPORTED_FORMAT_VERSION}:{thread_id}|"
             f"{meeting_timestamp}|{transcript_id}"),
        source=SOURCE_DECODED, thread_id=thread_id,
        meeting_timestamp=meeting_timestamp, transcript_id=transcript_id)


def canonical_key(raw_id) -> str:
    return canonical_transcript_identity(raw_id).key


def same_transcript(left, right) -> bool:
    """Whether two raw ids denote the same transcript."""
    return canonical_key(left) == canonical_key(right)


# A Teams transcript id names its call: `<call id>-<sequence>-TranscriptV2`.
_TRANSCRIPT_CALL_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-\d+-TranscriptV2",
    re.IGNORECASE)


def teams_call_id(raw_id) -> str | None:
    """
    The Teams call id a raw Graph transcript id belongs to, or None.

    Derived only from a completely decoded identity - the same decoder, and
    the same refusal to guess, as `canonical_key`. An id that keeps a RAW
    identity, or whose transcript field is not a `…-TranscriptV2` id, has no
    call id: callers must treat that as unknown, never as a match.
    """
    identity = canonical_transcript_identity(raw_id)
    if not identity.decoded:
        return None
    match = _TRANSCRIPT_CALL_ID.fullmatch(identity.transcript_id or "")
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------

def _decode(text: str) -> tuple[str, str, str]:
    data = _base64url(text)
    outer = _unpack_exactly(data)
    payload = _unwrap_envelope(outer)
    fields = _unpack_exactly(payload)
    return _fields(fields)


def _base64url(text: str) -> bytes:
    if len(text) > MAX_ENCODED_BYTES * 2:
        raise IdentityDecodeError("id is implausibly long")
    body = text.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        data = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IdentityDecodeError("not base64url") from exc
    if not data or len(data) > MAX_ENCODED_BYTES:
        raise IdentityDecodeError("decoded length out of range")
    return data


def _unwrap_envelope(outer) -> bytes:
    """
    Accept exactly the MessagePack-CSharp LZ4 envelope.

    `array(1 + n) [ext(98, <n uncompressed lengths>), bin_1 ... bin_n]`. One
    block is what Graph produced; more are accepted because that is the same
    documented format, and each block is still length-checked.
    """
    if not isinstance(outer, list) or len(outer) < 2:
        raise IdentityDecodeError("not an LZ4 envelope")
    header = outer[0]
    if not isinstance(header, _Ext) or header.type != LZ4_BLOCK_EXT_TYPE:
        raise IdentityDecodeError("missing LZ4 extension header")
    lengths = _unpack_sequence(header.data)
    blocks = outer[1:]
    if len(lengths) != len(blocks) or not all(isinstance(b, bytes) for b in blocks):
        raise IdentityDecodeError("LZ4 header does not match its blocks")
    if not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n
               for n in lengths) or sum(lengths) > MAX_DECODED_BYTES:
        raise IdentityDecodeError("invalid uncompressed length")
    return b"".join(lz4_block_decompress(block, size)
                    for block, size in zip(blocks, lengths))


def _fields(fields) -> tuple[str, str, str]:
    if not isinstance(fields, list):
        raise IdentityDecodeError("payload is not an array")
    # The ONE tolerated difference between serializations: trailing nils.
    # A trailing non-nil element is a field we do not understand, so it is
    # refused - collapsing it away could merge two different transcripts.
    trimmed = list(fields)
    while trimmed and trimmed[-1] is None:
        trimmed.pop()
    if len(trimmed) != 4:
        raise IdentityDecodeError(f"expected 4 fields, found {len(trimmed)}")
    version, thread_id, meeting_timestamp, transcript_id = trimmed
    if isinstance(version, bool) or version != SUPPORTED_FORMAT_VERSION:
        raise IdentityDecodeError(f"unsupported format version {version!r}")
    for value in (thread_id, meeting_timestamp, transcript_id):
        if not isinstance(value, str) or not value.strip():
            raise IdentityDecodeError("identity field missing")
    if "@thread" not in thread_id:
        raise IdentityDecodeError("thread id is not a Teams thread")
    if not meeting_timestamp.isdigit():
        raise IdentityDecodeError("meeting timestamp is not numeric")
    return thread_id, meeting_timestamp, transcript_id


# ---------------------------------------------------------------------------
# LZ4 block format (lz4.org/lz4_Block_format)
# ---------------------------------------------------------------------------

def lz4_block_decompress(src: bytes, expected_size: int) -> bytes:
    """
    Decompress one raw LZ4 block and require EXACTLY `expected_size` bytes.

    Implemented rather than imported: it is fifty lines of a frozen public
    format, and a new production dependency for a safety fix is a larger risk
    than the code. Every read is bounds-checked; a malformed block raises.
    """
    if expected_size > MAX_DECODED_BYTES:
        raise IdentityDecodeError("block too large")
    dst = bytearray()
    i, n = 0, len(src)
    while True:
        if i >= n:
            raise IdentityDecodeError("LZ4 block truncated before a token")
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            literal, i = _lz4_length(src, i, literal)
        if i + literal > n:
            raise IdentityDecodeError("LZ4 literal run past end of block")
        dst += src[i:i + literal]
        i += literal
        if len(dst) > expected_size:
            raise IdentityDecodeError("LZ4 output exceeds declared size")
        if i == n:
            break  # the last sequence carries literals only
        if i + 2 > n:
            raise IdentityDecodeError("LZ4 match offset truncated")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            raise IdentityDecodeError("LZ4 match offset out of range")
        match = token & 0x0F
        if match == 15:
            match, i = _lz4_length(src, i, match)
        match += 4
        if len(dst) + match > expected_size:
            raise IdentityDecodeError("LZ4 output exceeds declared size")
        start = len(dst) - offset
        for k in range(match):  # byte-wise: matches may overlap themselves
            dst.append(dst[start + k])
    if len(dst) != expected_size:
        raise IdentityDecodeError(
            f"LZ4 produced {len(dst)} bytes, header declared {expected_size}")
    return bytes(dst)


def _lz4_length(src: bytes, i: int, value: int) -> tuple[int, int]:
    while True:
        if i >= len(src):
            raise IdentityDecodeError("LZ4 length truncated")
        byte = src[i]
        i += 1
        value += byte
        if byte != 255:
            return value, i


# ---------------------------------------------------------------------------
# MessagePack (msgpack.org spec) - a strict reader, no writer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Ext:
    type: int
    data: bytes


def _unpack_exactly(data: bytes):
    value, end = _read(data, 0, depth=0)
    if end != len(data):
        raise IdentityDecodeError("trailing bytes after MessagePack value")
    return value


def _unpack_sequence(data: bytes) -> list:
    values, i = [], 0
    while i < len(data):
        value, i = _read(data, i, depth=0)
        values.append(value)
    return values


def _take(data: bytes, i: int, count: int) -> tuple[bytes, int]:
    if count < 0 or i + count > len(data):
        raise IdentityDecodeError("MessagePack value truncated")
    return data[i:i + count], i + count


def _uint(data: bytes, i: int, size: int) -> tuple[int, int]:
    raw, i = _take(data, i, size)
    return int.from_bytes(raw, "big", signed=False), i


def _read(data: bytes, i: int, *, depth: int):
    if depth > 16:
        raise IdentityDecodeError("MessagePack nesting too deep")
    raw, i = _take(data, i, 1)
    b = raw[0]
    if b <= 0x7F:
        return b, i
    if b >= 0xE0:
        return b - 0x100, i
    if 0xA0 <= b <= 0xBF:
        return _str(data, i, b & 0x1F)
    if 0x90 <= b <= 0x9F:
        return _array(data, i, b & 0x0F, depth)
    if 0x80 <= b <= 0x8F:
        return _map(data, i, b & 0x0F, depth)
    if b == 0xC0:
        return None, i
    if b == 0xC2:
        return False, i
    if b == 0xC3:
        return True, i
    if b in (0xC4, 0xC5, 0xC6):
        size, i = _uint(data, i, {0xC4: 1, 0xC5: 2, 0xC6: 4}[b])
        return _take(data, i, size)
    if b in (0xC7, 0xC8, 0xC9):
        size, i = _uint(data, i, {0xC7: 1, 0xC8: 2, 0xC9: 4}[b])
        return _ext(data, i, size)
    if b in (0xCA, 0xCB):
        raw, i = _take(data, i, 4 if b == 0xCA else 8)
        return struct.unpack(">f" if b == 0xCA else ">d", raw)[0], i
    if 0xCC <= b <= 0xCF:
        return _uint(data, i, 1 << (b - 0xCC))
    if 0xD0 <= b <= 0xD3:
        raw, i = _take(data, i, 1 << (b - 0xD0))
        return int.from_bytes(raw, "big", signed=True), i
    if 0xD4 <= b <= 0xD8:
        return _ext(data, i, 1 << (b - 0xD4))
    if b in (0xD9, 0xDA, 0xDB):
        size, i = _uint(data, i, {0xD9: 1, 0xDA: 2, 0xDB: 4}[b])
        return _str(data, i, size)
    if b in (0xDC, 0xDD):
        size, i = _uint(data, i, 2 if b == 0xDC else 4)
        return _array(data, i, size, depth)
    if b in (0xDE, 0xDF):
        size, i = _uint(data, i, 2 if b == 0xDE else 4)
        return _map(data, i, size, depth)
    raise IdentityDecodeError(f"reserved MessagePack byte 0x{b:02x}")


def _str(data: bytes, i: int, size: int):
    raw, i = _take(data, i, size)
    try:
        return raw.decode("utf-8"), i
    except UnicodeDecodeError as exc:
        raise IdentityDecodeError("invalid UTF-8 in string") from exc


def _ext(data: bytes, i: int, size: int):
    raw, i = _take(data, i, 1)
    ext_type = int.from_bytes(raw, "big", signed=True)
    payload, i = _take(data, i, size)
    return _Ext(ext_type, payload), i


def _array(data: bytes, i: int, size: int, depth: int):
    if size > len(data):
        raise IdentityDecodeError("array length exceeds input")
    items = []
    for _ in range(size):
        value, i = _read(data, i, depth=depth + 1)
        items.append(value)
    return items, i


def _map(data: bytes, i: int, size: int, depth: int):
    if size > len(data):
        raise IdentityDecodeError("map length exceeds input")
    pairs = []
    for _ in range(size):
        key, i = _read(data, i, depth=depth + 1)
        value, i = _read(data, i, depth=depth + 1)
        pairs.append((key, value))
    return pairs, i
