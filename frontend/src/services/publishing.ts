/**
 * Lecture Publishing Monitor API client, with the operator's own session token (services/api.ts). No
 * publisher or LMS service token ever reaches the browser. Every call is a GET except the three alert
 * review calls, which change only the signed-in user's own read / dismiss state - never the alert.
 */
import { api } from './api'
import type {
  AlertReviewAnswer, PublisherAlerts, PublisherHealth, PublishingDetail, PublishingFilters, PublishingPage, PublishingSummary,
} from '../types/publishing'

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
