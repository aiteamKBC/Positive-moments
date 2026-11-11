/**
 * The Social Media pages.
 *
 * The API module is mocked with the shapes the backend sends. What matters:
 * the list and lecture page show the same counts, the format filter selects
 * on the saved primary format, every card says it is not approved, and the
 * only link is the stored recording - never an invented clip.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import SocialMediaDetailView from '../src/views/SocialMediaDetailView.vue'
import SocialMediaView from '../src/views/SocialMediaView.vue'
import {
  getSocialClips,
  getSocialLecture,
  getSocialLectures,
  getSocialSummary,
  requestSocialClip,
} from '../src/services/social'

vi.mock('../src/services/social', () => ({
  getSocialClips: vi.fn(),
  getSocialLecture: vi.fn(),
  getSocialLectures: vi.fn(),
  getSocialSummary: vi.fn(),
  requestSocialClip: vi.fn(),
  // The reanalysis panel on the lecture page: no runs yet (tested in socialOperations.test.ts).
  getAnalysisRuns: vi.fn(() => Promise.resolve([])),
  requestReanalysis: vi.fn(),
  getPrompt: vi.fn(),
  getPromptVersion: vi.fn(),
  savePrompt: vi.fn(),
  restorePrompt: vi.fn(),
}))

const COUNTS = { reel: 24, carousel: 19, quote_post: 7, educational_post: 3, other: 0 }
const WATCH = 'https://tenant.sharepoint.com/sites/kbc/_layouts/15/stream.aspx?id=x&nav=abc'

const LECTURE = {
  session_id: 's', session_key: 'KEY-s', subject: 'AI In Marketing', trainer: 'Sharon Shields',
  date: '2026-10-13', recording_available: true, recording_url: 'https://tenant.sharepoint.com/r.mp4',
  opportunity_count: 53, format_counts: COUNTS, last_analyzed_at: '2026-10-18T11:05:42Z',
}

function opportunity(id: number, primary: string, overrides: object = {}) {
  return {
    opportunity_id: id, opportunity_key: `${id}:${id + 1}`, start_cue: id, end_cue: id + 1,
    start_time: '00:21:25.288', end_time: '00:22:05.128', duration_seconds: 39.84,
    speaker: 'Sharon Shields', quote: 'Poor data can turn AI into a confident generator of poor decisions.',
    content_angle: 'Why data veracity matters', why_selected: 'A hooky line with a checklist.',
    primary_format: primary, formats: [primary, 'educational_post'], verification_status: 'accepted',
    verification_reason: 'Self-contained.', discovery_source: 'independent_social', evidence_type: null,
    analyzed_at: '2026-10-18T11:05:42Z', updated_at: '2026-10-18T11:05:42Z',
    opportunity_data: { source_key: `${id}:${id + 1}`, merged_ranges: ['1:2'] },
    reel_segments: [], reel_edit_mode: 'not_applicable', reel_estimated_duration_seconds: null,
    carousel_slide_outline: [], also_in_evidence: false, watch_url: WATCH,
    clip: primary === 'reel' ? {
      state: 'available', can_request: true, reason: null, clip_url: null, clip_filename: null,
      error: null, requested_at: null, updated_at: null,
    } : null,
    evidence_clip: null,
    ...overrides,
  }
}

/** 53 opportunities in the real lecture's split. */
function opportunities() {
  let id = 0
  return Object.entries(COUNTS).flatMap(([primary, total]) =>
    Array.from({ length: total }, () => opportunity(id += 10, primary)))
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/social-media', name: 'social-media', component: SocialMediaView },
      { path: '/social-media/:sessionKey', name: 'social-media-detail', component: SocialMediaDetailView },
    ],
  })
}

async function renderList() {
  vi.mocked(getSocialSummary).mockResolvedValue({ lectures: 1, opportunity_count: 53, format_counts: COUNTS })
  vi.mocked(getSocialLectures).mockResolvedValue({
    count: 1, next: null, previous: null, page: 1, page_size: 20, total_pages: 1, results: [LECTURE],
  })
  const router = makeRouter()
  await router.push('/social-media')
  await router.isReady()
  const wrapper = mount(SocialMediaView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

async function renderDetail(items: object[] = opportunities(), extra: object = {}) {
  vi.mocked(getSocialLecture).mockResolvedValue({
    ...LECTURE, meeting_id: null, recording_filename: 'r.mp4', recording_link_status: 'available',
    opportunities: items, ...extra,
  } as never)
  const router = makeRouter()
  await router.push('/social-media/KEY-s')
  await router.isReady()
  const wrapper = mount(SocialMediaDetailView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

type Wrapper = Awaited<ReturnType<typeof renderDetail>>
const cards = (wrapper: Wrapper) => wrapper.findAll('[data-test="social-card"]')
const chipCount = (wrapper: Wrapper, value: string) =>
  wrapper.find(`[data-test="format-filter-${value}"]`).text().replace(/\D+/g, '')

describe('Social Media lecture list', () => {
  afterEach(() => vi.clearAllMocks())

  it('1. lists the lecture with its date, trainer, total and counts by format', async () => {
    const wrapper = await renderList()
    const row = wrapper.find('[data-test="social-row"]')
    expect(row.text()).toContain('AI In Marketing')
    expect(row.text()).toContain('Sharon Shields')
    expect(row.find('[data-test="opportunity-count"]').text()).toBe('53')
    expect(['reel', 'carousel', 'quote_post', 'educational_post']
      .map((name) => row.find(`[data-test="count-${name}"]`).text())).toEqual(['24', '19', '7', '3'])
  })

  it('2. asks the server for a format when one is chosen, from the first page', async () => {
    const wrapper = await renderList()
    await wrapper.find('[data-test="format-filter-quote_post"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(getSocialLectures).mock.lastCall?.[0]).toMatchObject({ content_format: 'quote_post', page: 1 })
    expect(vi.mocked(getSocialSummary).mock.lastCall?.[0]).toEqual({ content_format: 'quote_post' })
  })

  it('3. says the opportunities are not approved', async () => {
    const wrapper = await renderList()
    expect(wrapper.find('[data-test="not-approved-notice"]').text())
      .toContain('AI Suggested — Not Approved for Publishing')
  })
})

describe('Social Media lecture page', () => {
  afterEach(() => vi.clearAllMocks())

  it('4. shows every opportunity, with the same counts as the list', async () => {
    const wrapper = await renderDetail()
    expect(cards(wrapper)).toHaveLength(53)
    expect(['all', 'reel', 'carousel', 'quote_post', 'educational_post'].map((value) => chipCount(wrapper, value)))
      .toEqual(['53', '24', '19', '7', '3'])
  })

  it('5. filters on the primary format: All, Reels, Carousels, Quotes, Educational', async () => {
    const wrapper = await renderDetail()
    for (const [value, expected] of [['reel', 24], ['carousel', 19], ['quote_post', 7], ['educational_post', 3], ['all', 53]] as const) {
      await wrapper.find(`[data-test="format-filter-${value}"]`).trigger('click')
      expect(cards(wrapper)).toHaveLength(expected)
    }
  })

  it('6. a card is compact: angle, formats, times, speaker and actions, details collapsed', async () => {
    const wrapper = await renderDetail([opportunity(1, 'carousel', {
      carousel_slide_outline: ['Slide 1: Accurate', 'Slide 2: Complete'],
    })])
    const card = cards(wrapper)[0]
    expect(card.attributes('data-format')).toBe('carousel')
    expect(card.find('[data-test="content-angle"]').text()).toBe('Why data veracity matters')
    expect(card.find('[data-test="primary-format"]').text()).toBe('Carousel')
    expect(card.find('[data-test="alternative-formats"]').text()).toBe('Also: Educational post')
    expect(card.findAll('[data-test="format"]').map((chip) => chip.text())).toEqual(['Educational post'])
    expect(card.find('[data-test="speaker"]').text()).toBe('Sharon Shields')
    expect(card.find('[data-test="timestamps"]').text()).toBe('00:21:25 – 00:22:05')
    // Collapsed: no quote, reasoning, outline or raw data until asked.
    for (const hidden of ['details', 'quote', 'why-selected', 'carousel-slide', 'payload']) {
      expect(card.find(`[data-test="${hidden}"]`).exists()).toBe(false)
    }
    // The disclaimer is once, at page level, not on every card.
    expect(card.text()).not.toContain('Not Approved for Publishing')
    expect(wrapper.find('[data-test="not-approved-notice"]').text()).toContain('AI Suggested — Not Approved for Publishing')
  })

  it('7. View details opens one card accessibly, and Hide details closes it', async () => {
    const wrapper = await renderDetail([
      opportunity(1, 'carousel', { carousel_slide_outline: ['Slide 1: Accurate', 'Slide 2: Complete'] }),
      opportunity(2, 'reel'),
    ])
    const toggle = cards(wrapper)[0].find('[data-test="toggle-details"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.text()).toContain('View details')
    await toggle.trigger('click')
    const card = cards(wrapper)[0]
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.text()).toContain('Hide details')
    expect(card.find(`#${toggle.attributes('aria-controls')}`).exists()).toBe(true)
    expect(card.find('[data-test="quote"]').text()).toContain('confident generator of poor decisions')
    expect(card.find('[data-test="why-selected"]').text()).toContain('A hooky line with a checklist.')
    expect(card.find('[data-test="verification"]').text()).toContain('Self-contained.')
    expect(card.findAll('[data-test="carousel-slide"]').map((slide) => slide.text()))
      .toEqual(['Slide 1: Accurate', 'Slide 2: Complete'])
    expect(JSON.parse(card.find('[data-test="payload"]').text())).toEqual({ source_key: '1:2', merged_ranges: ['1:2'] })
    expect(cards(wrapper)[1].find('[data-test="details"]').exists()).toBe(false)
    await toggle.trigger('click')
    expect(cards(wrapper)[0].find('[data-test="details"]').exists()).toBe(false)
  })

  it('8. Expand all and Collapse all act on the filtered opportunities', async () => {
    const wrapper = await renderDetail()
    await wrapper.find('[data-test="format-filter-quote_post"]').trigger('click')
    await wrapper.find('[data-test="toggle-all"]').trigger('click')
    expect(wrapper.findAll('[data-test="details"]')).toHaveLength(7)
    expect(wrapper.find('[data-test="toggle-all"]').text()).toBe('Collapse all')
    await wrapper.find('[data-test="toggle-all"]').trigger('click')
    expect(wrapper.findAll('[data-test="details"]')).toHaveLength(0)
  })

  it('9. shows reel segments with stored times only where they exist', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', {
      reel_edit_mode: 'contiguous', reel_estimated_duration_seconds: 27.5,
      reel_segments: [
        { start_cue: 128, end_cue: 130, start: '00:28:56.248', end: null },
        { start_cue: 140, end_cue: 141, start: null, end: null },
      ],
    })])
    await wrapper.find('[data-test="toggle-details"]').trigger('click')
    expect(wrapper.findAll('[data-test="reel-segment"]').map((item) => item.text()))
      .toEqual(['1. cues 128–130 · 00:28:56 – —', '2. cues 140–141'])
    expect(wrapper.find('[data-test="reel"]').text()).toContain('Edit: Contiguous')
  })

  it('10. Open Recording opens only the stored recording at the saved time', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = await renderDetail([opportunity(1, 'reel'), opportunity(2, 'quote_post', { watch_url: null })])
    const [first, second] = cards(wrapper)
    expect(first.find('[data-test="watch"]').text()).toBe('Open Recording')
    await first.find('[data-test="watch"]').trigger('click')
    expect(open).toHaveBeenCalledWith(WATCH, '_blank', 'noopener,noreferrer')
    expect(second.find('[data-test="watch"]').exists()).toBe(false)
    expect(wrapper.html()).not.toMatch(/download|trimmed clip/i)
    open.mockRestore()
  })

  it('11. offers no approval, publishing or scheduling action', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel')])
    await wrapper.find('[data-test="toggle-details"]').trigger('click')
    const actions = wrapper.findAll('button').map((button) => button.text().toLowerCase())
    expect(actions.some((text) => /approve|publish|schedule|reject/.test(text))).toBe(false)
  })
})

const CLIP_URL = 'https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/EabcSocialClip'
const clip = (overrides: object = {}) => ({
  state: 'available', can_request: true, reason: null, clip_url: null, clip_filename: null, error: null,
  requested_at: null, updated_at: null, ...overrides,
})

describe('Create Clip on Social Media reels', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('12. reels offer Create Clip; non-video opportunities have no clip control', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel'), opportunity(2, 'carousel'), opportunity(3, 'quote_post')])
    const [reel, carousel, quote] = cards(wrapper)
    expect(reel.find('[data-test="create-clip"]').text()).toBe('Create Clip')
    expect(reel.find('[data-test="watch"]').text()).toBe('Open Recording')
    for (const card of [carousel, quote]) {
      expect(card.html()).not.toMatch(/create clip|view clip|clip requested|clip unavailable/i)
    }
  })

  it('13. one click asks Django once, for this lecture and opportunity, and shows it requested', async () => {
    let finish: (value: unknown) => void = () => {}
    vi.mocked(requestSocialClip).mockImplementation(() => new Promise((resolve) => { finish = resolve }) as never)
    const wrapper = await renderDetail([opportunity(13, 'reel')])
    const button = wrapper.find('[data-test="create-clip"]')
    await button.trigger('click')
    await button.trigger('click')
    expect(button.text()).toBe('Requesting…')
    finish(clip({ state: 'requested', can_request: false, requested_at: '2026-10-18T12:00:00Z' }))
    await flushPromises()
    expect(requestSocialClip).toHaveBeenCalledTimes(1)
    expect(requestSocialClip).toHaveBeenCalledWith('KEY-s', 13)
    expect(wrapper.find('[data-test="clip-requested"]').text()).toBe('Clip requested · processing')
    expect(wrapper.find('[data-test="create-clip"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="view-clip"]').exists()).toBe(false)
  })

  it('14. View Clip appears only for a completed saved SharePoint link', async () => {
    const wrapper = await renderDetail([
      opportunity(1, 'reel', { clip: clip({ state: 'ready', can_request: false, clip_url: CLIP_URL, clip_filename: 'a_social-1.mp4' }) }),
      opportunity(2, 'reel', { clip: clip({ state: 'requested', can_request: false }) }),
    ])
    const [ready, requested] = cards(wrapper)
    expect(ready.find('[data-test="view-clip"]').attributes()).toMatchObject({
      href: CLIP_URL, target: '_blank', rel: 'noopener noreferrer',
    })
    expect(ready.find('[data-test="create-clip"]').exists()).toBe(false)
    expect(requested.find('[data-test="view-clip"]').exists()).toBe(false)
  })

  it('15. a failed render or a request with no result can be tried again', async () => {
    const wrapper = await renderDetail([
      opportunity(1, 'reel', { clip: clip({ state: 'failed', error: 'Creatomate render failed: timeout' }) }),
      opportunity(2, 'reel', { clip: clip({ state: 'stale', reason: 'No clip result arrived for the last request.' }) }),
    ])
    const [failed, stale] = cards(wrapper)
    expect(failed.find('[data-test="clip-failed"]').text()).toBe('Clip failed: Creatomate render failed: timeout')
    expect(failed.find('[data-test="create-clip"]').text()).toBe('Try again')
    expect(stale.find('[data-test="clip-stale"]').text()).toBe('No clip result arrived for the last request.')
    expect(stale.find('[data-test="create-clip"]').text()).toBe('Try again')
  })

  it('16. a refusal carrying the current state shows that state, not an error', async () => {
    vi.mocked(requestSocialClip).mockRejectedValue({ response: { status: 409, data: {
      code: 'clip_already_produced', clip: clip({ state: 'ready', can_request: false, clip_url: CLIP_URL }),
    } } })
    const wrapper = await renderDetail([opportunity(1, 'reel')])
    await wrapper.find('[data-test="create-clip"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="view-clip"]').attributes('href')).toBe(CLIP_URL)
    expect(wrapper.find('[data-test="clip-error"]').exists()).toBe(false)
  })

  it('17. a failed webhook shows its error and the button comes back', async () => {
    vi.mocked(requestSocialClip).mockRejectedValue({ response: { status: 502, data: {
      code: 'clip_request_failed', detail: 'The clip service did not accept the request. Try again.',
    } } })
    const wrapper = await renderDetail([opportunity(1, 'reel')])
    await wrapper.find('[data-test="create-clip"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="clip-error"]').text()).toBe('The clip service did not accept the request. Try again.')
    expect(wrapper.find('[data-test="create-clip"]').text()).toBe('Create Clip')
    expect(wrapper.find('[data-test="clip-requested"]').exists()).toBe(false)
  })

  it('18. an unusable reel says why, and cannot be requested', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', { clip: clip({
      state: 'unavailable', can_request: false, reason: 'Clips must be 5-180 seconds long.' }) })])
    expect(wrapper.find('[data-test="clip-unavailable"]').text()).toBe('Clip unavailable')
    expect(wrapper.find('[data-test="create-clip"]').exists()).toBe(false)
    await wrapper.find('[data-test="toggle-details"]').trigger('click')
    expect(wrapper.find('[data-test="clip-note"]').text()).toContain('5-180 seconds')
  })

  it('19. a processing clip is re-read until it is ready, never re-requested', async () => {
    vi.useFakeTimers()
    vi.mocked(getSocialClips)
      .mockResolvedValueOnce({ 1: clip({ state: 'requested', can_request: false }) } as never)
      .mockResolvedValueOnce({ 1: clip({ state: 'ready', can_request: false, clip_url: CLIP_URL }) } as never)
    const wrapper = await renderDetail([opportunity(1, 'reel', { clip: clip({ state: 'requested', can_request: false }) })])
    await vi.advanceTimersByTimeAsync(20_000)
    expect(wrapper.find('[data-test="clip-requested"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(wrapper.find('[data-test="view-clip"]').attributes('href')).toBe(CLIP_URL)
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(getSocialClips).toHaveBeenCalledTimes(2)
    expect(requestSocialClip).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('20. nothing in flight means no background request at all', async () => {
    vi.useFakeTimers()
    const wrapper = await renderDetail([opportunity(1, 'reel'), opportunity(2, 'carousel')])
    await vi.advanceTimersByTimeAsync(30 * 60_000)
    expect(getSocialClips).not.toHaveBeenCalled()
    expect(requestSocialClip).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

const EVIDENCE_URL = 'https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/EabcEvidenceClip'
const evidenceClip = (overrides: object = {}) => ({
  url: EVIDENCE_URL, filename: '20261013_AI-In-Marketing_x_clip-03_cue-128-130.mp4', duration_seconds: 147.52,
  context_before_seconds: 60, context_after_seconds: 60, evidence_type: 'OFSTED_CASE_STUDY', ...overrides,
})

describe('Which video a Social Media card offers', () => {
  afterEach(() => vi.clearAllMocks())

  const actions = (wrapper: Wrapper, index = 0) => cards(wrapper)[index].find('[data-test="video-actions"]')
  const ownReady = () => clip({ state: 'ready', can_request: false, clip_url: CLIP_URL, clip_filename: 'x_social-1.mp4' })

  it('21. own exact clip only: View Clip', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', { clip: ownReady() })])
    expect(actions(wrapper).find('[data-test="view-clip"]').text()).toBe('View Clip')
    expect(actions(wrapper).find('[data-test="view-clip"]').attributes('href')).toBe(CLIP_URL)
    expect(actions(wrapper).find('[data-test="view-existing-clip"]').exists()).toBe(false)
  })

  it('22. existing evidence clip only: View Existing Clip, labelled padded, plus Create Exact Reel', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', { evidence_clip: evidenceClip() })])
    const row = actions(wrapper)
    expect(row.find('[data-test="view-existing-clip"]').text()).toBe('View Existing Clip')
    expect(row.find('[data-test="view-existing-clip"]').attributes('href')).toBe(EVIDENCE_URL)
    expect(row.find('[data-test="view-existing-clip"]').attributes('title')).toContain('OFSTED Case Study clip')
    expect(row.find('[data-test="existing-clip-context"]').text()).toBe('±60 s context')
    expect(row.find('[data-test="create-clip"]').text()).toBe('Create Exact Reel')
    expect(row.find('[data-test="view-clip"]').exists()).toBe(false)
  })

  it('23. both clips: View Exact Reel first, the evidence clip in the details', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', { clip: ownReady(), evidence_clip: evidenceClip() })])
    const row = actions(wrapper)
    expect(row.find('[data-test="view-clip"]').text()).toBe('View Exact Reel')
    expect(row.find('[data-test="view-existing-clip"]').exists()).toBe(false)
    await wrapper.find('[data-test="toggle-details"]').trigger('click')
    const videos = wrapper.find('[data-test="videos"]')
    expect(videos.find('[data-test="video-exact"] a').attributes('href')).toBe(CLIP_URL)
    expect(videos.find('[data-test="video-exact"]').text()).toContain('no added context')
    expect(videos.find('[data-test="video-existing"] a').attributes('href')).toBe(EVIDENCE_URL)
    expect(videos.find('[data-test="video-existing"]').text()).toContain('±60 s context')
  })

  it('24. no clip at all: Create Clip on reels only', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel'), opportunity(2, 'carousel')])
    expect(actions(wrapper, 0).find('[data-test="create-clip"]').text()).toBe('Create Clip')
    expect(actions(wrapper, 1).html()).not.toMatch(/create|view clip|view existing|exact reel/i)
    await wrapper.findAll('[data-test="toggle-details"]')[0].trigger('click')
    expect(wrapper.find('[data-test="videos"]').exists()).toBe(false)
  })

  it('25. a non-video opportunity may show its existing clip but never offers to create one', async () => {
    const wrapper = await renderDetail([opportunity(1, 'quote_post', { evidence_clip: evidenceClip({ evidence_type: 'POSITIVE_MOMENT' }) })])
    const row = actions(wrapper)
    expect(row.find('[data-test="view-existing-clip"]').attributes('title')).toContain('Positive Moments clip')
    expect(row.find('[data-test="create-clip"]').exists()).toBe(false)
  })

  it('26. an exact reel requested next to an existing clip says "exact reel"', async () => {
    const wrapper = await renderDetail([opportunity(1, 'reel', {
      clip: clip({ state: 'requested', can_request: false }), evidence_clip: evidenceClip() })])
    expect(actions(wrapper).find('[data-test="clip-requested"]').text()).toBe('Exact reel requested · processing')
    expect(actions(wrapper).find('[data-test="view-existing-clip"]').exists()).toBe(true)
  })

  it('27. every card links each video once, and never one clip under the other\'s name', async () => {
    const wrapper = await renderDetail([
      opportunity(1, 'reel', { clip: ownReady(), evidence_clip: evidenceClip() }),
      opportunity(2, 'reel', { evidence_clip: evidenceClip({ url: 'https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/Other' }) }),
    ])
    const hrefs = (index: number) => actions(wrapper, index).findAll('a').map((link) => link.attributes('href'))
    expect(hrefs(0)).toEqual([CLIP_URL])
    expect(hrefs(1)).toEqual(['https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/Other'])
  })

  it('28. creating an exact reel still calls the unchanged Create Clip API', async () => {
    vi.mocked(requestSocialClip).mockResolvedValue(clip({ state: 'requested', can_request: false }) as never)
    const wrapper = await renderDetail([opportunity(7, 'reel', { evidence_clip: evidenceClip() })])
    await wrapper.find('[data-test="create-clip"]').trigger('click')
    await flushPromises()
    expect(requestSocialClip).toHaveBeenCalledWith('KEY-s', 7)
  })
})
