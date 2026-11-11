/**
 * The Publishing monitor's selected day: one Africa/Cairo business date.
 *
 * Every value here is a "YYYY-MM-DD" business date and every step is calendar arithmetic on the
 * parts (shiftBusinessDate), never a local or UTC midnight, so the selected day cannot slip to the
 * previous or next day for a browser outside Cairo. The page builds the same `Navigation` shape the
 * Operations day report returns, so it reuses the Operations DateNav component unchanged.
 */
import type { Navigation } from '../types/operations'
import { CAIRO, shiftBusinessDate } from './datetime'

/** Today's date in Africa/Cairo, whatever the browser's timezone. */
export function cairoToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: CAIRO, year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}

/** A real calendar date in YYYY-MM-DD form ("2026-02-30" is not). */
export function isBusinessDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && shiftBusinessDate(value, 0) === value
}

type Query = Record<string, unknown>

/**
 * The day a Publishing URL names. `?date=` is the current form; links written before the daily
 * navigation (`?range=yesterday`, `?range=custom&from=…`) still open on the day they meant first.
 * Anything else, or nothing, is today.
 */
export function dayFromQuery(query: Query, today: string): string {
  if (isBusinessDate(query.date)) return query.date
  if (query.range === 'yesterday') return shiftBusinessDate(today, -1)
  if (query.range === 'custom' && isBusinessDate(query.from)) return query.from
  return today
}

/** The navigation the Operations DateNav renders, for one selected day. */
export function dayNavigation(date: string, today: string): Navigation {
  return {
    date,
    previous_date: shiftBusinessDate(date, -1),
    next_date: shiftBusinessDate(date, 1),
    today,
    is_today: date === today,
    is_future: date > today,
    timezone: CAIRO,
  }
}
