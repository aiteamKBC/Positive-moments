import axios from 'axios'

import type { LectureDetail, PaginatedLectures, Summary } from '../types'

const TOKEN_KEY = 'positive_mentions_token'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
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
      if (window.location.pathname !== '/login') window.location.assign('/login')
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

export async function getWatchUrl(sessionKey: string, clipIndex: number) {
  return (await api.get<{ url: string }>(
    `/positive-mentions/lectures/${encodeURIComponent(sessionKey)}/clips/${clipIndex}/watch/`,
  )).data.url
}
