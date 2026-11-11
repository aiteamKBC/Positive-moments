// Recording v9: validate the operator's run settings. Fail closed.
//
// dry_run is disarmed ONLY by the literal boolean false. A missing value, the
// string "false", 0, null - anything else - keeps the run a dry run.
const raw = items[0]?.json || {};

const DATE = /^\d{4}-\d{2}-\d{2}$/;
function isoDate(name) {
  const value = String(raw[name] ?? '').trim();
  if (!DATE.test(value)) {
    throw new Error(`Run Settings: ${name} must be YYYY-MM-DD (got "${value}")`);
  }
  const parsed = new Date(value + 'T00:00:00Z');
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error(`Run Settings: ${name} is not a real calendar date (${value})`);
  }
  return value;
}

const dateFrom = isoDate('date_from');
const dateTo = isoDate('date_to');
if (dateFrom > dateTo) {
  throw new Error(`Run Settings: date_from ${dateFrom} is after date_to ${dateTo}`);
}
const spanDays = (Date.parse(dateTo) - Date.parse(dateFrom)) / 86400000;
if (spanDays > 366) {
  throw new Error(`Run Settings: date range of ${spanDays} days exceeds 366`);
}

const maxLectures = raw.max_lectures === undefined || raw.max_lectures === null ||
  raw.max_lectures === '' ? 100 : Number(raw.max_lectures);
if (!Number.isInteger(maxLectures) || maxLectures < 1 || maxLectures > 500) {
  throw new Error(`Run Settings: max_lectures must be an integer 1..500 (got "${raw.max_lectures}")`);
}

return [{
  json: {
    settings_version: 'recording_links_v9',
    dry_run: raw.dry_run !== false,
    date_from: dateFrom,
    date_to: dateTo,
    max_lectures: maxLectures
  }
}];
