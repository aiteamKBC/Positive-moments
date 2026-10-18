import { api } from './api'
import type { PaginatedSocialLectures, SocialClip, SocialLectureDetail, SocialSummary } from '../types/social'

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
