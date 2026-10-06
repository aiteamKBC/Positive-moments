import axios from 'axios'

import type { LectureDetail, PaginatedLectures, Summary } from '../types'

const TOKEN_KEY = 'positive_mentions_token'
const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api'

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 20000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Token ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (window.location.pathname !== '/login') {
        startMicrosoftSignIn(window.location.pathname + window.location.search)
      }
    }
    return Promise.reject(error)
  },
)

export function hasToken() {
  return Boolean(localStorage.getItem(TOKEN_KEY))
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export async function login(username: string, password: string) {
  const { data } = await api.post<{ token: string }>('/auth/login/', { username, password })
  setToken(data.token)
}

/**
 * Sign in with Microsoft, the way the Communication Centre does. Someone
 * already signed in there comes straight back without typing anything.
 */
export function startMicrosoftSignIn(returnTo = '/positive-moments') {
  window.location.assign(`${API_BASE}/auth/sso/start?${new URLSearchParams({ return_to: returnTo })}`)
}

/** Trade the one-time sign-in code for the API token. Returns where to go. */
export async function completeMicrosoftSignIn(code: string) {
  const { data } = await api.post<{ token: string; return_to: string }>('/auth/sso/exchange/', { code })
  setToken(data.token)
  return data.return_to
}

export async function logout() {
  await api.post('/auth/logout/')
}

export async function getSummary(params: Record<string, string | number> = {}) {
  return (await api.get<Summary>('/positive-mentions/summary/', { params })).data
}

export async function getLectures(params: Record<string, string | number>) {
  return (await api.get<PaginatedLectures>('/positive-mentions/lectures/', { params })).data
}

export async function getLecture(sessionKey: string) {
  return (await api.get<LectureDetail>(
    `/positive-mentions/lectures/${encodeURIComponent(sessionKey)}/`,
  )).data
}

/**
 * Ask for one moment to be cut into a clip. Accepted means "requested": the
 * clip itself appears later, once it has been produced.
 */
export async function requestClip(sessionKey: string, startCue: number, endCue: number) {
  await api.post(
    `/positive-mentions/lectures/${encodeURIComponent(sessionKey)}/clip-requests/`,
    { start_cue: startCue, end_cue: endCue },
  )
}

export async function getWatchUrl(sessionKey: string, clipIndex: number) {
  return (await api.get<{ url: string }>(
    `/positive-mentions/lectures/${encodeURIComponent(sessionKey)}/clips/${clipIndex}/watch/`,
  )).data.url
}
