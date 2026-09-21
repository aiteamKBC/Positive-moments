/**
 * Phase 5A: the Operations API client.
 *
 * Every function here is one HTTP GET. There is no derivation, no counting and
 * no status logic - the platform already decided all of that, and a second
 * opinion computed in the browser is a second opinion that drifts.
 *
 * It reuses the existing axios instance, so it inherits the project's token
 * handling and its 401 redirect rather than introducing a second auth story.
 */
import { api } from './api'
import type {
  BackfillCreatedResponse,
  BackfillDetailResponse,
  BackfillListResponse,
  CalendarResponse,
  DayReport,
  DirectoryResponse,
  LectureMedia,
  MediaJobsResponse,
  LectureDetail,
  LectureListResponse,
  LectureListSlice,
  RunsResponse,
} from '../types/operations'

const BASE = '/operations'

/** Omitting the date means "today", decided in Africa/Cairo by the server. */
function dateParams(date?: string): Record<string, string> {
  return date ? { date } : {}
}

export async function getDay(date?: string) {
  return (await api.get<DayReport>(`${BASE}/day/`, { params: dateParams(date) })).data
}

export async function getLectures(date?: string, includeSuppressed = false) {
  const params: Record<string, string> = { ...dateParams(date) }
  if (includeSuppressed) params.include_suppressed = 'true'
  return (await api.get<LectureListResponse>(`${BASE}/lectures/`, { params })).data
}

/**
 * Which business days carry lectures. One cheap query - it resolves no stages,
 * so it is safe to call on every page load.
 */
export async function getCalendar(dateFrom: string, dateTo: string) {
  return (await api.get<CalendarResponse>(`${BASE}/calendar/`, {
    params: { date_from: dateFrom, date_to: dateTo },
  })).data
}

/** Trainer, module and scheduled window for a range. Descriptive only. */
export async function getDirectory(dateFrom: string, dateTo: string) {
  return (await api.get<DirectoryResponse>(`${BASE}/directory/`, {
    params: { date_from: dateFrom, date_to: dateTo },
  })).data
}

export async function getLecture(lectureId: string) {
  return (await api.get<LectureDetail>(`${BASE}/lectures/${lectureId}/`)).data
}

/**
 * Delivered media for one lecture. Clip and part statuses come from the worker
 * and the asset registry, never from the existence of a plan.
 */
export async function getLectureMedia(lectureId: string) {
  return (await api.get<LectureMedia>(`${BASE}/lectures/${lectureId}/media/`)).data
}

/** The media queue. `status` narrows to pending/processing/completed/failed. */
export async function getMediaJobs(status?: string, limit = 100) {
  const params: Record<string, string> = { limit: String(limit) }
  if (status) params.status = status
  return (await api.get<MediaJobsResponse>(`${BASE}/media-jobs/`, { params })).data
}

export async function getPendingAttendance(date?: string) {
  return (
    await api.get<LectureListSlice>(`${BASE}/pending-attendance/`, {
      params: dateParams(date),
    })
  ).data
}

export async function getReviewRequired(date?: string) {
  return (
    await api.get<LectureListSlice>(`${BASE}/review-required/`, {
      params: dateParams(date),
    })
  ).data
}

export async function getErrors(date?: string) {
  return (await api.get<LectureListSlice>(`${BASE}/errors/`, { params: dateParams(date) })).data
}

export async function getRuns(limit = 10) {
  return (await api.get<RunsResponse>(`${BASE}/runs/`, { params: { limit } })).data
}

export async function getRun(runId: string) {
  return (await api.get<Record<string, unknown>>(`${BASE}/runs/${runId}/`)).data
}

export async function getRetryPlan(lectureId: string) {
  return (await api.get<Record<string, unknown>>(`${BASE}/lectures/${lectureId}/retry/plan/`)).data
}

export async function retryLecture(lectureId: string, expectedAction?: string) {
  return (await api.post(`${BASE}/lectures/${lectureId}/retry/`, expectedAction ? { expected_action: expectedAction } : {})).data
}

export async function getRecoverAttendancePlan(lectureId: string) {
  return (await api.get<Record<string, unknown>>(`${BASE}/lectures/${lectureId}/recover-attendance/plan/`)).data
}

export async function recoverAttendance(lectureId: string) {
  return (await api.post(`${BASE}/lectures/${lectureId}/recover-attendance/`, {})).data
}

export async function reconcileDay(date: string) {
  return (await api.post(`${BASE}/reconcile/`, { date })).data
}

/**
 * The n8n safety answer. Read-only, GET only, against a remote n8n on a
 * separate VPS - and deliberately NOT called on page load, because a normal
 * page view should never depend on a third-party system being reachable.
 */
export async function getPrecheck() {
  return (await api.get<Record<string, unknown>>(`${BASE}/precheck/`)).data
}

/* --- QA Core RC2: Operations Backfill -------------------------------------
 *
 * Both creators return immediately with a queued run; the `backfill-runner`
 * service does the work. Nothing here waits on a month of Graph calls.
 */

export async function previewBackfill(from: string, to: string) {
  return (await api.post<BackfillCreatedResponse>(
    `${BASE}/backfills/preview/`, { from, to })).data
}

export async function startBackfill(from: string, to: string) {
  return (await api.post<BackfillCreatedResponse>(
    `${BASE}/backfills/start/`, { from, to })).data
}

export async function getBackfills(limit = 25) {
  return (await api.get<BackfillListResponse>(
    `${BASE}/backfills/`, { params: { limit } })).data
}

export async function getBackfill(runId: string) {
  return (await api.get<BackfillDetailResponse>(`${BASE}/backfills/${runId}/`)).data
}

export async function cancelBackfill(runId: string) {
  return (await api.post<BackfillCreatedResponse>(
    `${BASE}/backfills/${runId}/cancel/`, {})).data
}
