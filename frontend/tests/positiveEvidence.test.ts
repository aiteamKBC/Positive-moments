/**
 * Verified evidence and social-media proposals on the Positive Moments
 * lecture page.
 *
 * The API module is mocked with the fields the backend derives. What matters
 * here is that the page shows them as given, filters on them independently
 * and never invents a quotation.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import PositiveMomentDetailView from '../src/views/PositiveMomentDetailView.vue'
import { getLecture, getWatchUrl } from '../src/services/api'

vi.mock('../src/services/api', () => ({
  getLecture: vi.fn(),
  getWatchUrl: vi.fn(),
  requestClip: vi.fn(),
}))

/** A legacy clip as the API now returns it: new fields present but unknown. */
function legacy(overrides: object = {}) {
  return {
    start: '00:10:00.000', end: '00:10:20.000', start_cue: 100, end_cue: 104, duration_seconds: 20,
    positive_quote: 'I finally understand it.', quote: 'I finally understand it.', speaker: 'Learner A',
    category: 'content', reason: 'POSITIVE_MOMENT: the learner says it makes sense.', confidence: 0.9,
    dialogue: [], positive_speakers: [], other_speakers: [], semantic_verification: {}, clip_asset: null,
    evidence_type: null, final_evidence_type: 'POSITIVE_MOMENT', evidence_type_source: 'reason_prefix',
    selector_evidence_type: null, evidence_type_reclassified: null, selector_reason: null,
    semantic_verdict: null, semantic_status: 'not_verified', semantic_confidence: null,
    semantic_reason: null, semantic_advisory_pass: null, feedback_scope: null,
    conversation_type: null, trainer_included: null,
    social_media_primary_format: null, social_media_formats: null, social_formats: [],
    is_social_candidate: false, social_media_ready: null, social_media_rationale: null,
    social_verification_status: null, social_verifier_confidence: null, social_verifier_reason: null,
    reel_edit_mode: null, reel_segments: null, reel_estimated_duration_seconds: null,
    carousel_slide_outline: null,
    ...overrides,
  }
}

/** An OFSTED case study the verifier reclassified, proposed as a reel and a carousel. */
function caseStudy(overrides: object = {}) {
  return legacy({
    start: '00:20:00.000', end: '00:21:05.000', start_cue: 200, end_cue: 210, duration_seconds: 65,
    speaker: 'Trainer', positive_quote: 'Here is how the case unfolded.',
    reason: 'OFSTED_CASE_STUDY: the tutor explains a workplace case.',
    evidence_type: 'OFSTED_CASE_STUDY', final_evidence_type: 'OFSTED_CASE_STUDY', evidence_type_source: 'verified',
    selector_evidence_type: 'POSITIVE_MOMENT', evidence_type_reclassified: true,
    semantic_verdict: 'accept', semantic_status: 'accepted', semantic_confidence: 0.91,
    semantic_reason: 'A worked case study.',
    social_media_primary_format: 'reel', social_media_formats: ['reel', 'carousel'],
    social_formats: ['reel', 'carousel'], is_social_candidate: true, social_media_ready: true,
    social_verification_status: 'verified', reel_edit_mode: 'contiguous', reel_estimated_duration_seconds: 48,
    reel_segments: [
      { start_cue: 200, end_cue: 205, start: '00:20:00.000', end: '00:20:36.000' },
      { start_cue: 207, end_cue: 210, start: null, end: null },
    ],
    carousel_slide_outline: ['The problem', 'What the team tried', 'The result'],
    ...overrides,
  })
}

const rejected = () => legacy({
  start: '00:30:00.000', start_cue: 300, end_cue: 302, semantic_verdict: 'reject',
  semantic_status: 'rejected', semantic_reason: 'Not about the training.',
})

const SUMMARY = {
  total: 3, ai_accepted: 1, ai_rejected: 1, not_verified: 1, positive_moments: 2, ofsted_case_studies: 1,
  rejected_as_evidence: 0, unclassified: 0, social_candidates: 1, reel: 1, carousel: 1, quote_post: 0,
}

async function render(clips: object[], extra: object = {}) {
  vi.mocked(getLecture).mockResolvedValue({
    session_id: 's', session_key: 'KEY-s', subject: 'Lecture', trainer: 'Trainer', date: '2026-10-01',
    recording_available: true, recording_url: 'https://tenant.sharepoint.com/r.mp4',
    clips, evidence_summary: SUMMARY, ...extra,
  } as never)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/positive-moments', name: 'positive-moments', component: { template: '<div />' } },
      { path: '/positive-moments/:sessionKey', name: 'positive-moment-detail', component: PositiveMomentDetailView },
    ],
  })
  await router.push('/positive-moments/KEY-s')
  await router.isReady()
  const wrapper = mount(PositiveMomentDetailView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

type Wrapper = Awaited<ReturnType<typeof render>>
const cards = (wrapper: Wrapper) => wrapper.findAll('[data-test="evidence-card"]')
const types = (wrapper: Wrapper) => wrapper.findAll('[data-test="evidence-type"]').map((badge) => badge.text())

describe('Verified evidence on the lecture page', () => {
  afterEach(() => vi.clearAllMocks())

  it('1. shows a legacy clip with its type and as not verified, never as accepted', async () => {
    const wrapper = await render([legacy()])
    expect(types(wrapper)).toEqual(['Positive Moment'])
    expect(wrapper.find('[data-test="semantic"]').text()).toContain('Not verified')
    expect(wrapper.find('[data-test="social"]').exists()).toBe(false)
  })

  it('2. shows the verified type and says the Selector was overruled', async () => {
    const wrapper = await render([caseStudy()])
    expect(types(wrapper)).toEqual(['OFSTED Case Study'])
    expect(wrapper.find('[data-test="reclassified"]').text())
      .toContain('The Selector proposed Positive Moment; the verifier classified it as OFSTED Case Study')
  })

  it('3. shows accepted evidence by default and rejected candidates only on request', async () => {
    const wrapper = await render([legacy(), caseStudy(), rejected()])
    expect(cards(wrapper)).toHaveLength(2)
    expect(types(wrapper)).toEqual(['Positive Moment', 'OFSTED Case Study'])
    expect(wrapper.text()).not.toContain('Not about the training.')
    const toggle = wrapper.find('[data-test="evidence-filter-rejected"]')
    expect(toggle.text()).toBe('Show rejected candidates (1)')
    await toggle.trigger('click')
    expect(cards(wrapper)).toHaveLength(1)
    expect(types(wrapper)).toEqual(['Rejected as evidence'])
    expect(wrapper.find('[data-test="rejected-notice"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="rejection-reason"]').text()).toContain('Not about the training.')
    await wrapper.find('[data-test="evidence-filter-rejected"]').trigger('click')
    expect(cards(wrapper)).toHaveLength(2)
  })

  it('3b. a REJECT evidence type is rejected even when the verdict was not recorded', async () => {
    const wrapper = await render([legacy(), legacy({ start_cue: 500, end_cue: 501, final_evidence_type: 'REJECT' })])
    expect(cards(wrapper)).toHaveLength(1)
    expect(wrapper.find('[data-test="evidence-filter-rejected"]').text()).toContain('(1)')
  })

  it('4. an OFSTED case study shows both its reel and its carousel', async () => {
    const wrapper = await render([caseStudy()])
    expect(wrapper.findAll('[data-test="social-format"]').map((chip) => chip.text().replace(/\s+/g, ' ')))
      .toEqual(['Reel· primary', 'Carousel'])
    expect(wrapper.find('[data-test="reel"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="carousel"]').exists()).toBe(true)
  })

  it('5. renders reel segments with cues, and stored times only where they exist', async () => {
    const wrapper = await render([caseStudy()])
    const segments = wrapper.findAll('[data-test="reel-segment"]').map((item) => item.text())
    expect(segments).toEqual(['1. cues 200–205 · 00:20:00 – 00:20:36', '2. cues 207–210'])
    expect(wrapper.find('[data-test="reel"]').text()).toContain('Edit: Contiguous')
    expect(wrapper.find('[data-test="reel"]').text()).toContain('about 48s')
  })

  it('6. renders the carousel outline in its stored order', async () => {
    const wrapper = await render([caseStudy()])
    expect(wrapper.findAll('[data-test="carousel-slide"]').map((slide) => slide.text()))
      .toEqual(['The problem', 'What the team tried', 'The result'])
  })

  it('7. a quote post shows the speaker\'s exact stored quotation', async () => {
    const wrapper = await render([legacy({ social_formats: ['quote_post'], is_social_candidate: true })])
    const post = wrapper.find('[data-test="quote-post"]').text()
    expect(post).toContain('“I finally understand it.”')
    expect(post).toContain('— Learner A')
  })

  it('8. missing optional fields show nothing rather than an invented quotation', async () => {
    const wrapper = await render([legacy({ positive_quote: null, quote: null, social_formats: ['quote_post'], is_social_candidate: true })])
    expect(wrapper.find('[data-test="quotation"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="no-quotation"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Positive learner feedback')
    expect(wrapper.find('[data-test="quote-post"]').text()).toContain('No exact quotation stored.')
  })

  it('8b. an API without the new fields still renders the page', async () => {
    const old = { start: '00:01:00.000', end: '00:01:10.000', positive_quote: 'Old', dialogue: [],
      positive_speakers: [], other_speakers: [], semantic_verification: {}, clip_asset: null }
    const wrapper = await render([old], { evidence_summary: undefined })
    expect(cards(wrapper)).toHaveLength(1)
    expect(wrapper.find('[data-test="evidence-filter-rejected"]').exists()).toBe(false)
  })

  it('9. an OFSTED explanation is labelled as a case study, not a learner testimonial', async () => {
    const wrapper = await render([caseStudy()])
    expect(wrapper.find('[data-test="quote-label"]').text()).toContain('Case study explanation')
    expect(wrapper.text()).not.toContain('Positive learner feedback')
  })

  it('10. "AI marked ready" is never shown as approved', async () => {
    const wrapper = await render([caseStudy()])
    expect(wrapper.find('[data-test="readiness"]').text()).toContain('not approval to publish')
    expect(wrapper.text()).not.toMatch(/approved/i)
  })

  it('11. the evidence filter separates Positive Moments from OFSTED Case Studies', async () => {
    const wrapper = await render([legacy(), caseStudy(), rejected()])
    await wrapper.find('[data-test="evidence-filter-POSITIVE_MOMENT"]').trigger('click')
    expect(types(wrapper)).toEqual(['Positive Moment'])
    await wrapper.find('[data-test="evidence-filter-OFSTED_CASE_STUDY"]').trigger('click')
    expect(types(wrapper)).toEqual(['OFSTED Case Study'])
    await wrapper.find('[data-test="evidence-filter-all"]').trigger('click')
    expect(cards(wrapper)).toHaveLength(2)
    expect(wrapper.find('[data-test="evidence-filter-POSITIVE_MOMENT"]').text()).toBe('Positive Moments1')
  })

  it('11b. the main card is evidence-first; AI and social details are collapsed', async () => {
    const wrapper = await render([caseStudy()])
    const card = cards(wrapper)[0]
    expect(card.find('[data-test="quotation"]').text()).toContain('Here is how the case unfolded.')
    expect(card.find('[data-test="speaker"]').text()).toBe('Trainer')
    expect(card.find('[data-test="timestamps"]').text()).toBe('00:20:00 – 00:21:05')
    // The workflow's "OFSTED_CASE_STUDY:" prefix is not repeated to the reader.
    expect(card.find('[data-test="rationale"]').text()).toBe('Why it matters: the tutor explains a workplace case.')
    const details = card.find('[data-test="ai-details"]')
    expect(details.element.tagName).toBe('DETAILS')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.find('[data-test="semantic"]').exists()).toBe(true)
    expect(details.find('[data-test="social"]').exists()).toBe(true)
    // Nothing social or diagnostic sits outside the collapsed section.
    for (const name of ['semantic', 'social', 'reel', 'carousel']) {
      expect(card.findAll(`[data-test="${name}"]`).every((node) => details.element.contains(node.element))).toBe(true)
    }
    expect(wrapper.find('[data-test="evidence-summary"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="verdict-filter-all"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="social-filter-all"]').exists()).toBe(false)
  })

  it('11c. there are no review or approval controls', async () => {
    const wrapper = await render([legacy(), caseStudy()])
    expect(wrapper.find('[data-test="review"]').exists()).toBe(false)
    expect(wrapper.findAll('button').map((button) => button.text()))
      .not.toContain('Approve')
  })

  it('12. the header counts accepted evidence and its produced clips', async () => {
    const produced = { url: 'https://tenant.sharepoint.com/clip.mp4', duration_seconds: 65 }
    const wrapper = await render([legacy(), caseStudy({ clip_asset: produced }), rejected()])
    const badges = wrapper.find('[data-test="header-badges"]').text()
    expect(badges).toContain('2 accepted moments')
    expect(badges).toContain('1 of 2 clips produced')
  })

  it('13. the recording, produced clip and Create Clip are still there', async () => {
    const produced = { url: 'https://tenant.sharepoint.com/clip.mp4', duration_seconds: 65 }
    const wrapper = await render([caseStudy({ clip_asset: produced }), legacy()])
    const [withClip, withoutClip] = cards(wrapper)
    expect(withClip.findAll('a[href="https://tenant.sharepoint.com/clip.mp4"]')).toHaveLength(1)
    expect(withClip.find('[data-test="clip-request"]').exists()).toBe(false)
    expect(withoutClip.find('[data-test="clip-request"]').exists()).toBe(true)
    expect(withoutClip.find('a[href^="https://tenant.sharepoint.com"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Watch full lecture')
    expect(wrapper.text()).toContain('Watch in full lecture')
  })

  it('14. Watch in full lecture and Create Clip still use the original moment index and cues', async () => {
    vi.mocked(getWatchUrl).mockResolvedValue('https://tenant.sharepoint.com/r.mp4?nav=x')
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = await render([rejected(), legacy()])
    await cards(wrapper)[0].find('button').trigger('click')
    await flushPromises()
    // The rejected candidate is hidden, but the accepted one keeps its index 1.
    expect(getWatchUrl).toHaveBeenCalledWith('KEY-s', 1)
    open.mockRestore()
  })
})
