/**
 * Time, as an operator reads it.
 *
 * TWO KINDS OF VALUE, TWO KINDS OF FORMATTING
 * -------------------------------------------
 * A `session_date` is a BUSINESS DATE - the Africa/Cairo day a lecture belongs
 * to. It is not an instant, so it must never be pushed through a timezone
 * conversion: doing that turns "2026-09-18" into the 17th for anyone west of
 * Cairo. Those values are formatted by `businessDate`, which parses the parts
 * and never constructs a UTC midnight.
 *
 * An `started_at` or `scheduled_start` IS an instant, and every one of them is
 * displayed in Africa/Cairo, because that is the timezone the college, the
 * scheduler and the business date all run in. A raw ISO string never reaches
 * the interface.
 */
export const CAIRO = 'Africa/Cairo'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function parse(value?: string | null): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function parts(date: Date) {
  const formatted = new Intl.DateTimeFormat('en-GB', {
    timeZone: CAIRO,
    year: 'numeric', month: 'short', day: '2-digit',
    hour: 'numeric', minute: '2-digit', hour12: true,
    weekday: 'long',
  }).formatToParts(date)
  const pick = (type: string) => formatted.find((part) => part.type === type)?.value ?? ''
  return {
    weekday: pick('weekday'),
    day: pick('day'),
    // en-GB abbreviates September as "Sept"; every other formatter in the
    // product uses three letters, and two spellings of one month in one table
    // reads as a bug.
    month: pick('month').replace('Sept', 'Sep'),
    year: pick('year'),
    hour: pick('hour'),
    minute: pick('minute'),
    period: (pick('dayPeriod') || '').toUpperCase(),
  }
}

/** "20 Sep 2026 · 1:34 PM" - the default for any stored instant. */
export function cairoDateTime(value?: string | null, fallback = '—') {
  const date = parse(value)
  if (!date) return fallback
  const p = parts(date)
  return `${p.day} ${p.month} ${p.year} · ${p.hour}:${p.minute} ${p.period}`
}

/** "20 Sep 2026" */
export function cairoDate(value?: string | null, fallback = '—') {
  const date = parse(value)
  if (!date) return fallback
  const p = parts(date)
  return `${p.day} ${p.month} ${p.year}`
}

/** "1:34 PM" */
export function cairoTime(value?: string | null, fallback = '—') {
  const date = parse(value)
  if (!date) return fallback
  const p = parts(date)
  return `${p.hour}:${p.minute} ${p.period}`
}

/** "11:00 AM – 1:00 PM", or just the start when there is no end. */
export function cairoTimeRange(start?: string | null, end?: string | null) {
  const from = cairoTime(start, '')
  if (!from) return ''
  const to = cairoTime(end, '')
  return to ? `${from} – ${to}` : from
}

/**
 * A business date string, formatted without ever touching a timezone.
 * `businessDate('2026-09-18')` is "18 Sep 2026" everywhere on earth.
 */
export function businessDate(value?: string | null, fallback = '—') {
  if (!value) return fallback
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (!match) return cairoDate(value, fallback)
  const [, year, month, day] = match
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`
}

/** "Friday, 18 September 2026" - the dashboard's heading for one day. */
export function businessDateLong(value?: string | null, fallback = '') {
  if (!value) return fallback
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (!match) return fallback
  const [, year, month, day] = match
  // Noon UTC keeps the weekday correct for every timezone offset.
  const at = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), 12))
  const weekday = new Intl.DateTimeFormat('en-GB', { weekday: 'long', timeZone: 'UTC' }).format(at)
  const monthName = new Intl.DateTimeFormat('en-GB', { month: 'long', timeZone: 'UTC' }).format(at)
  return `${weekday}, ${Number(day)} ${monthName} ${year}`
}

/** "just now" / "14 min ago" / "3 days ago". Relative, never a raw stamp. */
export function sinceNow(value?: string | null, fallback = '') {
  const date = parse(value)
  if (!date) return fallback
  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (Math.abs(seconds) < 60) return 'just now'
  const units: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, 'minute'], [3600, 'hour'], [86400, 'day'], [604800, 'week'],
  ]
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  let chosen: [number, Intl.RelativeTimeFormatUnit] = [86400, 'day']
  for (const unit of units) if (Math.abs(seconds) >= unit[0]) chosen = unit
  return formatter.format(-Math.round(seconds / chosen[0]), chosen[1])
}

/** How long a run took, from its two stamps. "1.4s", "2m 05s", or ''. */
export function elapsed(start?: string | null, end?: string | null) {
  const from = parse(start)
  const to = parse(end)
  if (!from || !to) return ''
  const seconds = (to.getTime() - from.getTime()) / 1000
  if (seconds < 0) return ''
  if (seconds < 1) return 'under a second'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, '0')}s`
}

/** Shift a business date string by N days without leaving date arithmetic. */
export function shiftBusinessDate(value: string, days: number) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (!match) return value
  const [, year, month, day] = match
  const at = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)))
  at.setUTCDate(at.getUTCDate() + days)
  return at.toISOString().slice(0, 10)
}

