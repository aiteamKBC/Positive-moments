/**
 * "API docs": a signed-in Dashboard user reaches the LMS API documentation without signing in again.
 *
 * The token travels only as the Authorization header of one POST; the browser is then sent to
 * /api/docs/ with nothing about the token in the address. The page never bounces for ever.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ApiDocumentationView from '../src/views/ApiDocumentationView.vue'
import { api, openApiDocumentation } from '../src/services/api'

describe('openApiDocumentation', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('opens the grant with the token header, then navigates to a URL without the token', async () => {
    localStorage.setItem('positive_mentions_token', 'dashboard-token-123')
    const post = vi.spyOn(api, 'post').mockImplementation(async (url, data, config) => {
      // The request interceptor adds the header; what is posted carries nothing of the token.
      expect(url).toBe('/docs/session/')
      expect(JSON.stringify([data, config])).not.toContain('dashboard-token-123')
      return { data: null }
    })
    const navigate = vi.fn()
    await openApiDocumentation(navigate)
    expect(post).toHaveBeenCalledOnce()
    expect(navigate).toHaveBeenCalledWith('/api/docs/')
    expect(String(navigate.mock.calls[0][0])).not.toContain('dashboard-token-123')
  })

  it('sends the token as the Authorization header of that one request', async () => {
    localStorage.setItem('positive_mentions_token', 'dashboard-token-123')
    let header: unknown
    const adapter = vi.fn(async (config) => {
      header = config.headers.Authorization
      return { data: null, status: 204, statusText: 'No Content', headers: {}, config }
    })
    const original = api.defaults.adapter
    api.defaults.adapter = adapter as never
    const navigate = vi.fn()
    try {
      await openApiDocumentation(navigate)
    } finally {
      api.defaults.adapter = original
    }
    expect(header).toBe('Token dashboard-token-123')
    expect(navigate).toHaveBeenCalledWith('/api/docs/')
  })
})

vi.mock('../src/services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/services/api')>()
  return { ...actual, openApiDocumentation: vi.fn(actual.openApiDocumentation) }
})

describe('ApiDocumentationView', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.mocked(openApiDocumentation).mockReset()
  })

  it('opens the documentation straight away', async () => {
    vi.mocked(openApiDocumentation).mockResolvedValue()
    const page = mount(ApiDocumentationView)
    await flushPromises()
    expect(openApiDocumentation).toHaveBeenCalledOnce()
    expect(page.text()).toContain('Opening the API documentation')
  })

  it('stops instead of bouncing when the browser comes straight back', async () => {
    sessionStorage.setItem('api_docs_opened_at', String(Date.now()))
    const page = mount(ApiDocumentationView)
    await flushPromises()
    expect(openApiDocumentation).not.toHaveBeenCalled()
    expect(page.text()).toContain('accepts cookies')
    vi.mocked(openApiDocumentation).mockResolvedValue()
    await page.get('button').trigger('click')
    expect(openApiDocumentation).toHaveBeenCalledOnce()
  })

  it('says when the documentation is switched off', async () => {
    vi.mocked(openApiDocumentation).mockRejectedValue(
      Object.assign(new Error('not found'), { isAxiosError: true, response: { status: 404 } }),
    )
    const page = mount(ApiDocumentationView)
    await flushPromises()
    expect(page.text()).toContain('not switched on')
  })
})
