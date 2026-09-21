// Central redaction helpers. Microsoft Graph temporary download URLs and
// resumable upload session URLs carry authentication material in their query
// string. They must never reach logs, the n8n fail endpoint, or the database.

const URL_PATTERN = /https?:\/\/[^\s"'<>()]+/gi;

export function redactUrl(value) {
  const raw = String(value || '');
  if (!raw) return '';
  try {
    const url = new URL(raw);
    const hasSecrets = url.search.length > 0;
    return `${url.protocol}//${url.host}${url.pathname}${hasSecrets ? '?<redacted>' : ''}`;
  } catch {
    return '<redacted-url>';
  }
}

// Scrubs any URL appearing anywhere inside free text (e.g. FFmpeg stderr).
export function redactText(value) {
  return String(value == null ? '' : value).replace(URL_PATTERN, match => redactUrl(match));
}
