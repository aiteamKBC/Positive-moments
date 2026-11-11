/**
 * Positive Moments Media API client.
 *
 * Every call is short: reads are persisted state, and every action only
 * queues work for the background media runner. Nothing here waits on an AI
 * analysis, a render or a file transfer, so the 20 s client timeout is never
 * the limit.
 *
 * The two `open*` calls return one durable SharePoint link for the operator
 * to open, and are only made on click.
 */
import { api } from './api'
import type { LectureMediaDetail, MediaDashboard, MediaRun, RunMode } from '../types/media'

const BASE = '/operations'

export async function getMediaDashboard(dateFrom: string, dateTo: string) {
  return (await api.get<MediaDashboard>(`${BASE}/media/dashboard/`, {
    params: { date_from: dateFrom, date_to: dateTo },
  })).data
}

export async function queueMediaRun(mode: RunMode, from: string, to: string, lectureId?: string) {
  const body: Record<string, string> = { mode, from, to }
  if (lectureId) body.lecture_id = lectureId
  return (await api.post<{ run: MediaRun; detail: string }>(`${BASE}/media/runs/`, body)).data
}

export async function getMediaRun(runId: string) {
  return (await api.get<{ run: MediaRun }>(`${BASE}/media/runs/${runId}/`)).data.run
}

export async function getLecturePositiveMoments(lectureId: string) {
  return (await api.get<LectureMediaDetail>(
    `${BASE}/lectures/${lectureId}/positive-moments/`)).data
}

export async function renderMoment(momentId: string) {
  return (await api.post(`${BASE}/media/moments/${momentId}/render/`, {})).data
}

export async function retryMoment(momentId: string) {
  return (await api.post(`${BASE}/media/moments/${momentId}/retry/`, {})).data
}

export async function openRecording(lectureId: string) {
  return (await api.get<{ url: string }>(`${BASE}/lectures/${lectureId}/recording/open/`)).data.url
}

export async function openAsset(assetId: string) {
  return (await api.get<{ url: string }>(`${BASE}/media/assets/${assetId}/open/`)).data.url
}
