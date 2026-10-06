/**
 * "Clip" on each positive moment of a lecture.
 *
 * The API module is mocked: what matters here is which moment's identity the
 * page asks for, and what the button shows while and after it does.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import PositiveMomentDetailView from '../src/views/PositiveMomentDetailView.vue'
import { getLecture, requestClip } from '../src/services/api'

vi.mock('../src/services/api', () => ({
  getLecture: vi.fn(),
  getWatchUrl: vi.fn(),
  requestClip: vi.fn(),
}))

function moment(startCue: number, endCue: number, quote: string, clipAsset: object | null = null) {
  return {
    start: '00:05:50.000', end: '00:06:00.000', start_cue: startCue, end_cue: endCue,
    positive_quote: quote, quote, speaker: 'Learner', category: 'content', dialogue: [],
    positive_speakers: [], other_speakers: [], semantic_verification: {}, clip_asset: clipAsset,
  }
}

function lecture(clips: object[]) {
  return {
    session_id: 'abc123', session_key: 'KEY-abc123', subject: 'Lecture', trainer: 'Trainer',
    date: '2026-10-01', recording_available: true, recording_url: 'https://tenant.sharepoint.com/r.mp4',
    clips,
  }
}

async function render(clips: object[]) {
  vi.mocked(getLecture).mockResolvedValue(lecture(clips) as never)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/positive-moments', name: 'positive-moments', component: { template: '<div />' } },
      { path: '/positive-moments/:sessionKey', name: 'positive-moment-detail', component: PositiveMomentDetailView },
    ],
  })
  await router.push('/positive-moments/KEY-abc123')
  await router.isReady()
  const wrapper = mount(PositiveMomentDetailView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

/** What axios rejects with: an Error carrying the HTTP response. */
function httpError(response: object) {
  return Object.assign(new Error('Request failed'), { response })
}

const buttons = (wrapper: Awaited<ReturnType<typeof render>>) => wrapper.findAll('[data-test="clip-request"]')

describe('Clip button on each positive moment', () => {
  beforeEach(() => { vi.mocked(requestClip).mockReset() })
  afterEach(() => vi.clearAllMocks())

  it('1. renders a Clip button on every moment without a produced clip', async () => {
    const wrapper = await render([moment(350, 360, 'A'), moment(540, 547, 'B')])
    expect(buttons(wrapper)).toHaveLength(2)
    expect(buttons(wrapper).map((button) => button.text())).toEqual(['Clip', 'Clip'])
  })

  it("2. clicking clip A sends the lecture's key and clip A's cues", async () => {
    vi.mocked(requestClip).mockResolvedValue()
    const wrapper = await render([moment(350, 360, 'A'), moment(540, 547, 'B')])
    await buttons(wrapper)[0].trigger('click')
    expect(requestClip).toHaveBeenCalledExactlyOnceWith('KEY-abc123', 350, 360)
  })

  it("3. clicking clip B sends clip B's cues, not clip A's", async () => {
    vi.mocked(requestClip).mockResolvedValue()
    const wrapper = await render([moment(350, 360, 'A'), moment(540, 547, 'B')])
    await buttons(wrapper)[1].trigger('click')
    expect(requestClip).toHaveBeenCalledExactlyOnceWith('KEY-abc123', 540, 547)
  })

  it('4. is disabled and says Processing while submitting, then Clip requested', async () => {
    let accept: () => void = () => {}
    vi.mocked(requestClip).mockReturnValue(new Promise<void>((resolve) => { accept = resolve }))
    const wrapper = await render([moment(350, 360, 'A')])
    await buttons(wrapper)[0].trigger('click')
    expect(buttons(wrapper)[0].text()).toBe('Processing…')
    expect(buttons(wrapper)[0].attributes('disabled')).toBeDefined()
    await buttons(wrapper)[0].trigger('click')                 // double click: ignored
    expect(requestClip).toHaveBeenCalledTimes(1)
    accept()
    await flushPromises()
    expect(buttons(wrapper)[0].text()).toContain('Clip requested')
    expect(buttons(wrapper)[0].attributes('disabled')).toBeDefined()
  })

  it('5. an HTTP failure restores the Clip button and shows the error', async () => {
    vi.mocked(requestClip).mockImplementation(async () => { throw httpError({ status: 502, data: { detail: 'The clip service did not accept the request. Try again.' } }) })
    const wrapper = await render([moment(350, 360, 'A')])
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(buttons(wrapper)[0].text()).toBe('Clip')
    expect(buttons(wrapper)[0].attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[role="alert"]').text()).toContain('did not accept the request')
  })

  it('6. a moment whose clip is already produced shows View clip and never requests', async () => {
    const produced = { url: 'https://tenant.sharepoint.com/clip-a.mp4', duration_seconds: 12 }
    const wrapper = await render([moment(350, 360, 'A', produced), moment(540, 547, 'B')])
    expect(buttons(wrapper)).toHaveLength(1)                   // only B can be requested
    const view = wrapper.findAll('a').find((link) => link.text().includes('View clip'))
    expect(view?.attributes('href')).toBe('https://tenant.sharepoint.com/clip-a.mp4')
    expect(requestClip).not.toHaveBeenCalled()
  })

  it('6b. a clip produced meanwhile (409) turns into View clip instead of an error', async () => {
    vi.mocked(requestClip).mockImplementation(async () => { throw httpError({ status: 409, data: { code: 'clip_already_produced', clip_asset: { url: 'https://tenant.sharepoint.com/new.mp4' } } }) })
    const wrapper = await render([moment(350, 360, 'A')])
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(buttons(wrapper)).toHaveLength(0)
    expect(wrapper.find('a[href="https://tenant.sharepoint.com/new.mp4"]').exists()).toBe(true)
  })
})
