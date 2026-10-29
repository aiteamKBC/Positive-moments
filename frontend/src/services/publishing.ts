/**
 * Lecture Publishing Monitor API client, with the operator's own session token (services/api.ts). No
 * publisher or LMS service token ever reaches the browser. Every call is a GET except the three alert
 * review calls, which change only the signed-in user's own read / dismiss state - never the alert, and the
 * Historical Backfill calls below.
 */
import { api } from './api'
import type {
  AlertReviewAnswer, PublisherAlerts, PublisherHealth, PublishingDetail, PublishingFilters, PublishingPage, PublishingSummary,
} from '../types/publishing'
import type { BackfillPreview, BackfillRetryResult, BackfillRun, BackfillRunDetail, BackfillRuns } from '../types/backfill'

const BASE = '/operations/publishing-monitor'
/** A wide range is resolved lecture by lecture on the server (cached for 20 s afterwards). */
const RANGE_TIMEOUT_MS = 60_000

export async function getPublisherHealth() {
  return (await api.get<PublisherHealth>(`${BASE}/health/`)).data
}

export async function getPublisherAlerts() {
  return (await api.get<PublisherAlerts>(`${BASE}/alerts/`)).data
}

export async function markAlertRead(alertId: string) {
  return (await api.post<AlertReviewAnswer>(`${BASE}/alerts/${alertId}/read/`, {})).data
}

export async function dismissAlert(alertId: string, reason: string, note: string) {
  return (await api.post<AlertReviewAnswer>(`${BASE}/alerts/${alertId}/dismiss/`, { reason, note })).data
}

export async function restoreAlert(alertId: string) {
  return (await api.post<AlertReviewAnswer>(`${BASE}/alerts/${alertId}/restore/`, {})).data
}

export async function getPublishingSummary(dateFrom: string, dateTo: string) {
  return (await api.get<PublishingSummary>(`${BASE}/summary/`, {
    params: { date_from: dateFrom, date_to: dateTo }, timeout: RANGE_TIMEOUT_MS,
  })).data
}

export async function getPublishingLectures(
  dateFrom: string, dateTo: string, filters: PublishingFilters, page: number, pageSize: number,
) {
  const params: Record<string, string | number> = { date_from: dateFrom, date_to: dateTo, page, page_size: pageSize }
  for (const [key, value] of Object.entries(filters)) {
    if (value === true) params[key] = '1'
    else if (typeof value === 'string' && value) params[key] = value
  }
  return (await api.get<PublishingPage>(`${BASE}/lectures/`, { params, timeout: RANGE_TIMEOUT_MS })).data
}

export async function getPublishingLecture(lectureId: string) {
  return (await api.get<PublishingDetail>(`${BASE}/lectures/${lectureId}/`)).data
}

// --------------------------------------------------------------------------- Historical Backfill
// Preview is read-only on the server (a READ ONLY transaction; no Graph, LMS or Bunny call). Start, cancel
// and retry only record the operator's decision; the publisher service does the work in the background.
const BACKFILL = `${BASE}/backfill`

export async function getBackfillRuns() {
  return (await api.get<BackfillRuns>(`${BACKFILL}/runs/`)).data
}

export async function previewBackfill(dateFrom: string, dateTo: string) {
  return (await api.post<BackfillPreview>(`${BACKFILL}/preview/`, { date_from: dateFrom, date_to: dateTo },
    { timeout: RANGE_TIMEOUT_MS })).data
}

export async function startBackfill(preview: BackfillPreview, reason: string) {
  return (await api.post<{ run: BackfillRun; detail: string }>(`${BACKFILL}/runs/start/`, {
    date_from: preview.date_from, date_to: preview.date_to, preview_token: preview.preview_token, reason,
    confirm: { library_id: preview.library_id, eligible: preview.summary.eligible },
  }, { timeout: RANGE_TIMEOUT_MS })).data
}

export async function getBackfillRun(runId: string) {
  return (await api.get<BackfillRunDetail>(`${BACKFILL}/runs/${runId}/`, { timeout: RANGE_TIMEOUT_MS })).data
}

export async function cancelBackfill(runId: string) {
  return (await api.post<{ run: BackfillRun; detail: string }>(`${BACKFILL}/runs/${runId}/cancel/`, {})).data
}

export async function retryBackfill(runId: string, reason: string, lectureIds: string[] = []) {
  return (await api.post<BackfillRetryResult>(`${BACKFILL}/runs/${runId}/retry/`, { reason, lecture_ids: lectureIds })).data
}
