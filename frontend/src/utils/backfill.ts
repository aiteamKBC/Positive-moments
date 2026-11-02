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
  READY_TO_QUEUE: badge('Ready to process', 'info'),
  RESUME_REQUIRED: badge('Incomplete · resume', 'info'),
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
  RESUME: badge('Resume', 'brand'),
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
  INCOMPLETE: badge('Incomplete', 'error'),
  QUEUED: badge('Queued', 'waiting'),
  RUNNING: badge('Running', 'info'),
  COMPLETED: badge('Completed', 'ok'),
  CANCELLED: badge('Cancelled', 'neutral'),
  FAILED: badge('Failed', 'error'),
}

/** Where a lecture of the range was found. */
export const ORIGIN: Record<string, Badge> = {
  TEAMS_NEW: badge('New · Teams only', 'brand'),
  TEAMS_EXISTING: badge('Teams + existing', 'neutral'),
  STORED_ONLY: badge('Existing · not in Teams now', 'review'),
  DISCOVERED_AT_RUN: badge('Found at run', 'brand'),
  QA_ONLY: badge('QA database only', 'review'),
}

/** What the preview has verified, and what only the run verifies. */
export const VERIFICATION: Record<string, string> = {
  LIVE: 'checked live in Teams',
  LIVE_DRY_RUN: 'dry run, re-checked at run',
  STORED: 'from stored state',
  TO_BE_VERIFIED_AT_RUN: 'checked at run',
}

/** One day's source discovery. */
export const COVERAGE: Record<string, Badge> = {
  COMPLETE: badge('Discovered', 'ok'),
  PENDING: badge('Waiting', 'quiet'),
  FAILED: badge('Failed', 'error'),
  NOT_AVAILABLE: badge('Not available', 'error'),
  PARTIAL: badge('Partial', 'error'),
  NOT_CHECKED: badge('Not checked', 'quiet'),
}

/** Why a QA-only lecture is not processed (app/publisher/historical_sources.py). */
export const QA_ONLY_CODE: Record<string, string> = {
  QA_ONLY_NO_LMS_SESSION: 'No LMS session of its Teams meeting that day',
  QA_ONLY_SCHEDULE_UNKNOWN: 'Not in the Teams calendar: exact start unknown',
  QA_ONLY_LMS_UNCHECKED: 'LMS could not be checked',
  QA_ONLY_NO_MEETING: 'No Teams meeting on the QA record',
  QA_CANCELLED: 'Cancelled in QA',
}

/** "Teams + existing + QA", from the server's list of the sources a lecture was seen in. */
export function provenance(sources?: string[] | null): string {
  if (!sources?.length) return ''
  return sources.map((s) => (s === 'PUBLISHER' ? 'existing' : s === 'QA' ? 'QA' : 'Teams')).join(' + ')
}

/** A row key that is unique for QA-only lectures too (they have no lecture id). */
export function rowKey(row: { lecture_id: string | null; qa_ref?: string | null; session_date: string }): string {
  return row.lecture_id ?? `qa:${row.qa_ref ?? ''}:${row.session_date}`
}

export const backfillLabel = {
  category: (v?: string | null) => lookup(CATEGORY, v),
  action: (v?: string | null) => lookup(ACTION, v),
  item: (v?: string | null) => lookup(ITEM_STATUS, v),
  run: (v?: string | null) => lookup(RUN_STATUS, v),
  origin: (v?: string | null) => lookup(ORIGIN, v),
  coverage: (v?: string | null) => lookup(COVERAGE, v),
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

type PreviewLike = {
  preview: { date_from: string; date_to: string }
  summary: { eligible: number } | null
  confirmable: boolean
}

/**
 * Run Backfill is offered only for a COMPLETE, fresh preview (`confirmable`, decided by the server) of exactly
 * the chosen range with something to process. An incomplete preview can never be run.
 */
export function canRun(preview: PreviewLike | null, from: string, to: string, operator: boolean, activeRun: boolean): boolean {
  return Boolean(operator && !activeRun && preview && preview.confirmable && preview.preview.date_from === from
    && preview.preview.date_to === to && (preview.summary?.eligible ?? 0) > 0)
}
