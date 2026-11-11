/**
 * Card headings when an opportunity has no saved content angle.
 *
 * The heading is display only: the saved content angle when there is one,
 * otherwise words cut from the opportunity's own saved text, otherwise its
 * format. Nothing is generated and the saved values are never changed.
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SocialOpportunityCard from '../src/components/SocialOpportunityCard.vue'
import { opportunityHeading } from '../src/utils/social'

// why_selected texts as the pipeline saves them for evidence-derived opportunities.
const MINI_STORY = 'Clear mini-story with specific constraint and consequences; works as a case-style reel, '
  + 'and the constraint/workflow points can be mapped to slides for a carousel.'
const FORMAT_FIRST = 'Already structured as challenge → solution → impact with specific figures, making it '
  + 'naturally carousel-friendly; also works as a narrated case-study reel.'
const HEDGED = 'Could work as a conservative quote_post because it is a complete idea about needing brand '
  + 'guidelines for AI content; however it is hedged, which slightly weakens standalone impact.'
const LEAD_IN = 'The clip is a coherent mini-case (print vs digital tracking gap) with a clear practical question; '
  + 'works as a reel and can be a short carousel.'
const LONG_QUOTE = 'So the challenge faced was a company called Example Group, a UK headquartered global B2B '
  + 'distributor, and it needed a stronger customer data foundation.'

describe('opportunityHeading', () => {
  it('uses the saved content angle when present, trimmed', () => {
    expect(opportunityHeading({ content_angle: '  Why data veracity matters ', why_selected: MINI_STORY }))
      .toEqual({ text: 'Why data veracity matters', source: 'content_angle' })
  })

  it('takes the first descriptive clause of why_selected, never all of it', () => {
    const heading = opportunityHeading({ content_angle: null, why_selected: MINI_STORY, primary_format: 'reel' })
    expect(heading).toEqual({ text: 'Clear mini-story with specific constraint and consequences', source: 'why_selected' })
    expect(heading.text).not.toBe(MINI_STORY)
    expect(MINI_STORY.startsWith(heading.text)).toBe(true)
  })

  it('drops a lead-in about the clip itself', () => {
    expect(opportunityHeading({ content_angle: null, why_selected: LEAD_IN }).text)
      .toBe('Coherent mini-case (print vs digital tracking gap) with a clear practical question')
  })

  it('refuses a clause about formats or a hedged judgement', () => {
    expect(opportunityHeading({ content_angle: null, why_selected: FORMAT_FIRST, primary_format: 'carousel' }))
      .toEqual({ text: 'Carousel opportunity', source: 'format' })
    expect(opportunityHeading({ content_angle: null, why_selected: HEDGED, primary_format: 'quote_post' }))
      .toEqual({ text: 'Quote post opportunity', source: 'format' })
  })

  it('uses a short, complete quote sentence only as a quotation', () => {
    const heading = opportunityHeading({
      content_angle: null, why_selected: FORMAT_FIRST,
      quote: 'Yeah, so we always do a spot check on the AI output. Then I report it.', primary_format: 'reel',
    })
    expect(heading).toEqual({ text: '“We always do a spot check on the AI output”', source: 'quote' })
  })

  it('never uses filler or a long quote sentence', () => {
    for (const quote of ['Right, okay, okay.', LONG_QUOTE]) {
      expect(opportunityHeading({ content_angle: null, why_selected: null, quote, primary_format: 'reel' }))
        .toEqual({ text: 'Reel opportunity', source: 'format' })
    }
  })

  it('has a neutral heading for every format, and when the format is unknown', () => {
    expect(opportunityHeading({ primary_format: 'educational_post' }).text).toBe('Educational post opportunity')
    expect(opportunityHeading({ primary_format: null }).text).toBe('Social media opportunity')
  })
})

function opportunity(overrides: object = {}) {
  return {
    opportunity_id: 3, opportunity_key: '128:130', start_cue: 128, end_cue: 130,
    start_time: '00:28:56.248', end_time: '00:29:23.768', duration_seconds: 27.52, speaker: 'Banks, Nicholas',
    quote: 'Right, okay, okay. So what do you use Copilot for?', content_angle: 'Copilot at work',
    why_selected: MINI_STORY, primary_format: 'reel', formats: ['reel', 'carousel'],
    verification_status: 'accepted', verification_reason: 'Coherent.', discovery_source: 'existing_evidence',
    evidence_type: 'OFSTED_CASE_STUDY', analyzed_at: '2026-10-18T11:05:42Z', updated_at: '2026-10-18T11:05:42Z',
    opportunity_data: { content_angle: null }, reel_segments: [], reel_edit_mode: 'contiguous',
    reel_estimated_duration_seconds: null, carousel_slide_outline: [], also_in_evidence: true, watch_url: null,
    clip: null, evidence_clip: null,
    ...overrides,
  }
}

const render = (overrides: object = {}, open = false) =>
  mount(SocialOpportunityCard, { props: { opportunity: opportunity(overrides) as never, open, sessionKey: 'KEY' } })

describe('Card heading', () => {
  it('shows a populated content angle as saved', () => {
    const card = render()
    const heading = card.find('[data-test="content-angle"]')
    expect(heading.text()).toBe('Copilot at work')
    expect(heading.attributes('data-heading-source')).toBe('content_angle')
    expect(card.find('[data-test="toggle-details"]').attributes('aria-label')).toBe('View details: Copilot at work')
  })

  for (const [name, value] of [['missing', null], ['empty', ''], ['whitespace-only', '   ']] as const) {
    it(`derives a heading when the content angle is ${name}`, () => {
      const card = render({ content_angle: value }, true)
      const heading = card.find('[data-test="content-angle"]')
      expect(heading.text()).toBe('Clear mini-story with specific constraint and consequences')
      expect(heading.attributes('data-heading-source')).toBe('why_selected')
      expect(card.text()).not.toContain('No content angle stored')
      expect(card.find('[data-test="heading-note"]').text()).toContain('taken from its saved selection note')
      // The full selection note is still shown, unchanged, in the details.
      expect(card.find('[data-test="why-selected"]').text()).toContain(MINI_STORY)
    })
  }

  it('falls back to the format when nothing reliable can be derived', () => {
    const card = render({ content_angle: null, why_selected: FORMAT_FIRST, primary_format: 'carousel' }, true)
    expect(card.find('[data-test="content-angle"]').text()).toBe('Carousel opportunity')
    expect(card.find('[data-test="heading-note"]').text()).toContain('The heading is its format.')
  })

  it('never shows the heading note for a saved content angle', () => {
    expect(render({}, true).find('[data-test="heading-note"]').exists()).toBe(false)
  })

  it('leaves the opportunity it was given unchanged', () => {
    const given = opportunity({ content_angle: null })
    const before = JSON.stringify(given)
    mount(SocialOpportunityCard, { props: { opportunity: given as never, open: true, sessionKey: 'KEY' } })
    expect(JSON.stringify(given)).toBe(before)
  })
})
