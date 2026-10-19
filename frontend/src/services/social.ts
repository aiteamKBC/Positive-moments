import { api } from './api'
import type {
  AnalysisRun,
  PaginatedSocialLectures,
  PromptDetail,
  PromptKey,
  PromptVersion,
  SocialClip,
  SocialLectureDetail,
  SocialSummary,
} from '../types/social'

const BASE = '/social-media'

export async function getSocialSummary(params: Record<string, string | number> = {}) {
  return (await api.get<SocialSummary>(`${BASE}/summary/`, { params })).data
}

export async function getSocialLectures(params: Record<string, string | number>) {
  return (await api.get<PaginatedSocialLectures>(`${BASE}/lectures/`, { params })).data
}

export async function getSocialLecture(sessionKey: string) {
  return (await api.get<SocialLectureDetail>(`${BASE}/lectures/${encodeURIComponent(sessionKey)}/`)).data
}

/**
 * Create Clip on one reel. Django validates it and forwards it to the same
 * clip-on-demand webhook Positive Moments uses. Accepted means "requested".
 */
export async function requestSocialClip(sessionKey: string, opportunityId: number) {
  return (await api.post<{ status: 'requested'; clip: SocialClip }>(
    `${BASE}/lectures/${encodeURIComponent(sessionKey)}/opportunities/${opportunityId}/clip-requests/`,
  )).data.clip
}

/** Every reel's clip state in one lecture, keyed by opportunity id. */
export async function getSocialClips(sessionKey: string) {
  return (await api.get<{ clips: Record<string, SocialClip> }>(
    `${BASE}/lectures/${encodeURIComponent(sessionKey)}/clips/`,
  )).data.clips
}

// --- AI prompts ------------------------------------------------------------------

export async function getPrompt(key: PromptKey) {
  return (await api.get<PromptDetail>(`${BASE}/prompts/${key}/`)).data
}

/** The full text of an earlier version. */
export async function getPromptVersion(key: PromptKey, version: number) {
  return (await api.get<PromptVersion & { content: string }>(`${BASE}/prompts/${key}/versions/${version}/`)).data
}

/**
 * Save a new version. `expectedVersion` is the version the editor was opened
 * on (0 when none existed); the server answers 409 if someone saved since.
 */
export async function savePrompt(key: PromptKey, content: string, expectedVersion: number) {
  return (await api.put<{ active: PromptVersion & { content: string } }>(
    `${BASE}/prompts/${key}/`, { content, expected_version: expectedVersion },
  )).data.active
}

/** Restore an earlier version: the server saves it again as a new version. */
export async function restorePrompt(key: PromptKey, version: number, expectedVersion: number) {
  return (await api.post<{ active: PromptVersion & { content: string } }>(
    `${BASE}/prompts/${key}/rollback/`, { version, expected_version: expectedVersion },
  )).data.active
}

// --- reanalysis ---------------------------------------------------------------------

/** Queue a reanalysis and send it to n8n. 202 means "received", never "done". */
export async function requestReanalysis(sessionKey: string) {
  return (await api.post<{ run: AnalysisRun }>(
    `${BASE}/lectures/${encodeURIComponent(sessionKey)}/reanalysis/`,
  )).data.run
}

/** This lecture's analysis runs, newest first. Read only: never re-sends anything. */
export async function getAnalysisRuns(sessionKey: string) {
  return (await api.get<{ runs: AnalysisRun[] }>(
    `${BASE}/lectures/${encodeURIComponent(sessionKey)}/analysis-runs/`,
  )).data.runs
}
