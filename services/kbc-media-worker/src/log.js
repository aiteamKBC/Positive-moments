import { redactText } from './redact.js';

// Every string that reaches the log is scrubbed of URL query strings, so a
// Graph download/upload URL can never be leaked by an accidental log call.
function scrub(value) {
  if (typeof value === 'string') return redactText(value);
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, scrub(v)]));
  }
  return value;
}

function emit(level, event, data = {}) {
  const payload = {
    ts: new Date().toISOString(),
    level,
    event,
    ...scrub(data)
  };
  console.log(JSON.stringify(payload));
}

export const log = {
  info: (event, data) => emit('info', event, data),
  warn: (event, data) => emit('warn', event, data),
  error: (event, data) => emit('error', event, data)
};
