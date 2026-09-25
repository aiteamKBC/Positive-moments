// Recording v9: derive each lecture's exact Teams call id, and decide whether it
// can be looked up at all.
//
// qa_doctors_sessions.session_id is the raw Graph transcript id. It is NOT
// plain base64 text: it is base64url(MessagePack-CSharp LZ4 envelope). v8
// decoded it as UTF-8 and regex-searched for a GUID, which fails whenever the
// LZ4 block uses a back-reference inside the GUID. This is a strict port of
// the QA Core RC4 decoder (app/transcripts/identity.py): an id that does not
// decode completely and exactly yields NO call id - never a guess.

const MAX_ENCODED_BYTES = 4096;
const MAX_DECODED_BYTES = 16384;
const LZ4_BLOCK_EXT_TYPE = 98;
const SUPPORTED_FORMAT_VERSION = 4;
const TRANSCRIPT_ID = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-\d+-TranscriptV2$/i;

class IdentityDecodeError extends Error {}
class MpExt { constructor(type, data) { this.type = type; this.data = data; } }
class MpFloat { constructor(value) { this.value = value; } }

function fail(message) { throw new IdentityDecodeError(message); }

function base64url(text) {
  if (text.length > MAX_ENCODED_BYTES * 2) fail('id is implausibly long');
  const body = text.replace(/-/g, '+').replace(/_/g, '/').replace(/=+$/, '');
  if (!/^[A-Za-z0-9+/]*$/.test(body) || body.length % 4 === 1) fail('not base64url');
  const data = Buffer.from(body + '='.repeat((4 - (body.length % 4)) % 4), 'base64');
  if (!data.length || data.length > MAX_ENCODED_BYTES) fail('decoded length out of range');
  return data;
}

function lz4Length(src, i, value) {
  for (;;) {
    if (i >= src.length) fail('LZ4 length truncated');
    const byte = src[i++];
    value += byte;
    if (byte !== 255) return [value, i];
  }
}

function lz4BlockDecompress(src, expectedSize) {
  if (expectedSize > MAX_DECODED_BYTES) fail('block too large');
  const dst = [];
  let i = 0;
  const n = src.length;
  for (;;) {
    if (i >= n) fail('LZ4 block truncated before a token');
    const token = src[i++];
    let literal = token >> 4;
    if (literal === 15) [literal, i] = lz4Length(src, i, literal);
    if (i + literal > n) fail('LZ4 literal run past end of block');
    for (let k = 0; k < literal; k++) dst.push(src[i + k]);
    i += literal;
    if (dst.length > expectedSize) fail('LZ4 output exceeds declared size');
    if (i === n) break;
    if (i + 2 > n) fail('LZ4 match offset truncated');
    const offset = src[i] | (src[i + 1] << 8);
    i += 2;
    if (offset === 0 || offset > dst.length) fail('LZ4 match offset out of range');
    let match = token & 0x0f;
    if (match === 15) [match, i] = lz4Length(src, i, match);
    match += 4;
    if (dst.length + match > expectedSize) fail('LZ4 output exceeds declared size');
    const start = dst.length - offset;
    for (let k = 0; k < match; k++) dst.push(dst[start + k]);
  }
  if (dst.length !== expectedSize) fail('LZ4 produced the wrong number of bytes');
  return Buffer.from(dst);
}

function take(data, i, count) {
  if (count < 0 || i + count > data.length) fail('MessagePack value truncated');
  return [data.subarray(i, i + count), i + count];
}

function uint(data, i, size) {
  const [raw, next] = take(data, i, size);
  let value = 0;
  for (const byte of raw) value = value * 256 + byte;
  return [value, next];
}

function str(data, i, size) {
  const [raw, next] = take(data, i, size);
  const text = raw.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(raw)) fail('invalid UTF-8 in string');
  return [text, next];
}

function ext(data, i, size) {
  const [raw, afterType] = take(data, i, 1);
  const type = raw[0] > 127 ? raw[0] - 256 : raw[0];
  const [payload, next] = take(data, afterType, size);
  return [new MpExt(type, payload), next];
}

function array(data, i, size, depth) {
  if (size > data.length) fail('array length exceeds input');
  const values = [];
  for (let k = 0; k < size; k++) {
    let value;
    [value, i] = read(data, i, depth + 1);
    values.push(value);
  }
  return [values, i];
}

function map(data, i, size, depth) {
  if (size > data.length) fail('map length exceeds input');
  const pairs = [];
  for (let k = 0; k < size; k++) {
    let key, value;
    [key, i] = read(data, i, depth + 1);
    [value, i] = read(data, i, depth + 1);
    pairs.push([key, value]);
  }
  return [pairs, i];
}

function read(data, i, depth) {
  if (depth > 16) fail('MessagePack nesting too deep');
  const [raw, next] = take(data, i, 1);
  const b = raw[0];
  i = next;
  if (b <= 0x7f) return [b, i];
  if (b >= 0xe0) return [b - 0x100, i];
  if (b >= 0xa0 && b <= 0xbf) return str(data, i, b & 0x1f);
  if (b >= 0x90 && b <= 0x9f) return array(data, i, b & 0x0f, depth);
  if (b >= 0x80 && b <= 0x8f) return map(data, i, b & 0x0f, depth);
  if (b === 0xc0) return [null, i];
  if (b === 0xc2) return [false, i];
  if (b === 0xc3) return [true, i];
  if (b >= 0xc4 && b <= 0xc6) {
    const [size, at] = uint(data, i, { 0xc4: 1, 0xc5: 2, 0xc6: 4 }[b]);
    return take(data, at, size);
  }
  if (b >= 0xc7 && b <= 0xc9) {
    const [size, at] = uint(data, i, { 0xc7: 1, 0xc8: 2, 0xc9: 4 }[b]);
    return ext(data, at, size);
  }
  if (b === 0xca || b === 0xcb) {
    const [bytes, at] = take(data, i, b === 0xca ? 4 : 8);
    return [new MpFloat(b === 0xca ? bytes.readFloatBE(0) : bytes.readDoubleBE(0)), at];
  }
  if (b >= 0xcc && b <= 0xcf) return uint(data, i, 1 << (b - 0xcc));
  if (b >= 0xd0 && b <= 0xd3) {
    const size = 1 << (b - 0xd0);
    const [bytes, at] = take(data, i, size);
    let value = 0n;
    for (const byte of bytes) value = (value << 8n) | BigInt(byte);
    if (bytes[0] & 0x80) value -= 1n << BigInt(size * 8);
    return [Number(value), at];
  }
  if (b >= 0xd4 && b <= 0xd8) return ext(data, i, 1 << (b - 0xd4));
  if (b >= 0xd9 && b <= 0xdb) {
    const [size, at] = uint(data, i, { 0xd9: 1, 0xda: 2, 0xdb: 4 }[b]);
    return str(data, at, size);
  }
  if (b === 0xdc || b === 0xdd) {
    const [size, at] = uint(data, i, b === 0xdc ? 2 : 4);
    return array(data, at, size, depth);
  }
  if (b === 0xde || b === 0xdf) {
    const [size, at] = uint(data, i, b === 0xde ? 2 : 4);
    return map(data, at, size, depth);
  }
  fail('reserved MessagePack byte');
}

function unpackExactly(data) {
  const [value, end] = read(data, 0, 0);
  if (end !== data.length) fail('trailing bytes after MessagePack value');
  return value;
}

function unpackSequence(data) {
  const values = [];
  let i = 0;
  while (i < data.length) {
    let value;
    [value, i] = read(data, i, 0);
    values.push(value);
  }
  return values;
}

const isInt = value => typeof value === 'number' && Number.isInteger(value);

function unwrapEnvelope(outer) {
  if (!Array.isArray(outer) || outer.length < 2) fail('not an LZ4 envelope');
  const header = outer[0];
  if (!(header instanceof MpExt) || header.type !== LZ4_BLOCK_EXT_TYPE) {
    fail('missing LZ4 extension header');
  }
  const lengths = unpackSequence(header.data);
  const blocks = outer.slice(1);
  if (lengths.length !== blocks.length || !blocks.every(Buffer.isBuffer)) {
    fail('LZ4 header does not match its blocks');
  }
  if (!lengths.every(n => isInt(n) && n >= 0) ||
      lengths.reduce((a, b) => a + b, 0) > MAX_DECODED_BYTES) {
    fail('invalid uncompressed length');
  }
  return Buffer.concat(blocks.map((block, k) => lz4BlockDecompress(block, lengths[k])));
}

function fields(values) {
  if (!Array.isArray(values)) fail('payload is not an array');
  const trimmed = values.slice();
  while (trimmed.length && trimmed[trimmed.length - 1] === null) trimmed.pop();
  if (trimmed.length !== 4) fail('expected 4 fields');
  const [version, threadId, meetingTimestamp, transcriptId] = trimmed;
  const numeric = version instanceof MpFloat ? version.value : version;
  if (typeof numeric !== 'number' || numeric !== SUPPORTED_FORMAT_VERSION) {
    fail('unsupported format version');
  }
  for (const value of [threadId, meetingTimestamp, transcriptId]) {
    if (typeof value !== 'string' || !value.trim()) fail('identity field missing');
  }
  if (!threadId.includes('@thread')) fail('thread id is not a Teams thread');
  if (!/^\p{Nd}+$/u.test(meetingTimestamp)) fail('meeting timestamp is not numeric');
  return { thread_id: threadId, meeting_timestamp: meetingTimestamp, transcript_id: transcriptId };
}

function transcriptIdentity(rawId) {
  const text = rawId === null || rawId === undefined ? '' : String(rawId).trim();
  if (!text) return { source: 'RAW' };
  try {
    return { source: 'DECODED', ...fields(unpackExactly(unwrapEnvelope(unpackExactly(base64url(text))))) };
  } catch (error) {
    if (error instanceof IdentityDecodeError) return { source: 'RAW', decode_error: error.message };
    throw error;
  }
}

const settings = $items('Validate Run Settings')[0].json;
const blank = value => !String(value ?? '').trim();

return items.map((item, index) => {
  const lecture = item.json || {};
  const identity = transcriptIdentity(lecture.session_id);
  const callMatch = identity.source === 'DECODED'
    ? TRANSCRIPT_ID.exec(identity.transcript_id) : null;
  const expectedCallId = callMatch ? callMatch[1].toLowerCase() : null;

  const organizerCount = Number(lecture.organizer_count || 0);
  const organizerId = String(lecture.meeting_lookup_user_id || '').trim() || null;
  const cancelled = String(lecture.cancelled_session ?? '').trim().toLowerCase() === 'true';

  // Order matters: the first failing precondition is the reported reason.
  let precheck = null;
  if (cancelled) precheck = 'NO_RECORDING_EXPECTED_CANCELLED';
  else if (blank(lecture.meeting_id) || blank(lecture.session_id)) precheck = 'SESSION_IDENTITY_MISSING';
  else if (organizerCount > 1) precheck = 'ORGANIZER_LOOKUP_ID_AMBIGUOUS';
  else if (!organizerId) precheck = 'ORGANIZER_LOOKUP_ID_MISSING';
  else if (!expectedCallId) precheck = 'SESSION_CALL_ID_MISSING';

  return {
    json: {
      ...lecture,
      expected_call_id: expectedCallId,
      call_id_source: expectedCallId ? 'rc4_transcript_identity_v1' : null,
      transcript_identity_source: identity.source,
      meeting_lookup_user_id: organizerId,
      organizer_count: organizerCount,
      cancelled,
      precheck_status: precheck,
      graph_lookup_url: precheck ? null
        : 'https://graph.microsoft.com/v1.0/users/' + encodeURIComponent(organizerId) +
          '/onlineMeetings/' + encodeURIComponent(String(lecture.meeting_id).trim()) +
          '/recordings?$top=100',
      dry_run: settings.dry_run !== false,
      allow_overwrite_existing: false
    },
    pairedItem: { item: index }
  };
});
