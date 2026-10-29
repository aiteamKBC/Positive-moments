/**
 * Historical Backfill vocabulary and the small client-side checks.
 *
 * Presentation only: every category, status and stage is decided by the server
 * (app/publisher/backfill.py). The range check below mirrors the server's so the
 * operator sees a mistake at once; the server checks again and is the authority.
 */
import { humanise, type Tone } from './labels'

type Badge = { label: string; tone: Tone }
const badge = (label: string, tone: Tone): Badge => ({ label, tone })

function lookup(table: Record<string, Badge>, value?: string | null): Badge {
  if (!value) return badge('—', 'quiet')
  return table[value] ?? badge(humanise(value), 'neutral')
}

/** Preview categories (what a run would do with a lecture). */
export const CATEGORY: Record<string, Badge> = {
  READY_TO_QUEUE: badge('Ready to queue', 'info'),
  ALREADY_PUBLISHED: badge('Already published', 'ok'),
  ALREADY_PROCESSING: badge('Already processing', 'waiting'),
  MISSING_LMS_MATCH: badge('Missing LMS match', 'review'),
  MISSING_RECORDING: badge('Missing recording', 'review'),
  REVIEW_REQUIRED: badge('Review required', 'review'),
  SKIPPED: badge('Skipped', 'quiet'),
  ERROR: badge('Error', 'error'),
}

export const ACTION: Record<string, Badge> = {
  QUEUE: badge('Queue', 'brand'),
  TRACK: badge('Track only', 'neutral'),
  NONE: badge('None', 'quiet'),
}

/** A run's lecture. */
export const ITEM_STATUS: Record<string, Badge> = {
  PENDING: badge('Pending', 'quiet'),
  PROCESSING: badge('Processing', 'info'),
  COMPLETED: badge('Completed', 'ok'),
  SKIPPED: badge('Skipped', 'quiet'),
  BLOCKED: badge('Blocked', 'review'),
  FAILED: badge('Failed', 'error'),
  CANCELLED: badge('Cancelled', 'neutral'),
}

/** A run. */
export const RUN_STATUS: Record<string, Badge> = {
  QUEUED: badge('Queued', 'waiting'),
  RUNNING: badge('Running', 'info'),
  COMPLETED: badge('Completed', 'ok'),
  CANCELLED: badge('Cancelled', 'neutral'),
  FAILED: badge('Failed', 'error'),
}

export const backfillLabel = {
  category: (v?: string | null) => lookup(CATEGORY, v),
  action: (v?: string | null) => lookup(ACTION, v),
  item: (v?: string | null) => lookup(ITEM_STATUS, v),
  run: (v?: string | null) => lookup(RUN_STATUS, v),
}

/** The progress counters, in the order the run view shows them. */
export const COUNTERS = [
  { key: 'total', label: 'Total' },
  { key: 'pending', label: 'Pending' },
  { key: 'processing', label: 'Processing' },
  { key: 'completed', label: 'Completed' },
  { key: 'skipped', label: 'Skipped' },
  { key: 'blocked', label: 'Blocked' },
  { key: 'failed', label: 'Failed' },
] as const

/** "1 h 42 min", "3 min", "—". */
export function duration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

/** Inclusive days between two YYYY-MM-DD business dates (calendar arithmetic, never clock time). */
export function inclusiveDays(from: string, to: string): number {
  const [a, b] = [from, to].map((d) => {
    const [y, m, day] = d.split('-').map(Number)
    return Date.UTC(y, m - 1, day)
  })
  return Math.round((b - a) / 86_400_000) + 1
}

/** The operator's mistake in a range, or '' when the server should be asked. */
export function rangeProblem(from: string, to: string, today: string, maxDays: number): string {
  if (!from || !to) return 'Choose both dates.'
  if (!ISO_DATE.test(from) || !ISO_DATE.test(to)) return 'Dates must be YYYY-MM-DD.'
  if (to < from) return 'To Date is before From Date.'
  if (to > today) return `To Date is in the future (today is ${today} in Cairo).`
  const days = inclusiveDays(from, to)
  if (days > maxDays) return `Choose at most ${maxDays} days (this range has ${days}).`
  return ''
}

/** Run Backfill is offered only for a current preview of exactly the chosen range with something to queue. */
export function canRun(preview: { date_from: string; date_to: string; summary: { eligible: number } } | null,
  from: string, to: string, operator: boolean, activeRun: boolean): boolean {
  return Boolean(operator && !activeRun && preview && preview.date_from === from && preview.date_to === to
    && preview.summary.eligible > 0)
}
