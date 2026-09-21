/**
 * Small display helpers that are not about time.
 *
 * Date and status formatting used to live here; both moved out during the
 * redesign - dates to `utils/datetime` (which is Cairo-aware and knows the
 * difference between a business date and an instant) and status wording to
 * `utils/labels`. Leaving the old versions here would have guaranteed that
 * some page kept rendering a UTC date.
 */
export function formatConfidence(value?: number | null) {
  if (typeof value !== 'number') return 'Not provided'
  return `${Math.round(value * 100)}%`
}

export function openSafely(url: string) {
  window.open(url, '_blank', 'noopener,noreferrer')
}

export function errorMessage(error: unknown, fallback: string) {
  if (
    typeof error === 'object' &&
    error !== null &&
    'response' in error
  ) {
    const response = (error as { response?: { data?: { detail?: string } } }).response
    if (response?.data?.detail) return response.data.detail
  }
  return fallback
}

