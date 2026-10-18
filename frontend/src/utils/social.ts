/**
 * Social Media vocabulary. Presentation only: every value arrives from the API.
 */
import type { EvidenceClip, SocialFormat } from '../types/social'
import type { Tone } from './labels'

/** The four primary formats, in the order the filters show them. */
export const SOCIAL_FORMAT_FILTERS: { value: '' | SocialFormat; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'reel', label: 'Reels' },
  { value: 'carousel', label: 'Carousels' },
  { value: 'quote_post', label: 'Quotes' },
  { value: 'educational_post', label: 'Educational' },
]

export const SOCIAL_FORMAT_LABELS: Record<string, string> = {
  reel: 'Reel',
  carousel: 'Carousel',
  quote_post: 'Quote post',
  educational_post: 'Educational post',
}

export const DISCOVERY_SOURCES: Record<string, { label: string; tone: Tone }> = {
  independent_social: { label: 'Found by the social search', tone: 'brand' },
  existing_evidence: { label: 'From Positive Moments / OFSTED evidence', tone: 'info' },
  both: { label: 'Social search and existing evidence', tone: 'info' },
}

/** The one label every opportunity carries. */
export const NOT_APPROVED = 'AI Suggested — Not Approved for Publishing'

/** Transcript offsets are cue timestamps, not wall-clock times. */
export function offset(value?: string | null) {
  return value ? value.replace(/\.\d+$/, '') : '—'
}

export function seconds(value?: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  if (value < 60) return `${Math.round(value)}s`
  return `${Math.floor(value / 60)}m ${String(Math.round(value % 60)).padStart(2, '0')}s`
}

/** How each primary format reads on a card: one accent and one icon. */
export const FORMAT_STYLE: Record<string, { accent: string; icon: string }> = {
  reel: { accent: 'border-l-brand-600', icon: 'video' },
  carousel: { accent: 'border-l-gold-400', icon: 'layers' },
  quote_post: { accent: 'border-l-slate-400', icon: 'quote' },
  educational_post: { accent: 'border-l-sky-500', icon: 'lectures' },
}

// --- headings --------------------------------------------------------------

export interface OpportunityHeading {
  text: string
  /** Where the words came from. Anything but `content_angle` is a display fallback. */
  source: 'content_angle' | 'why_selected' | 'quote' | 'format'
}

type HeadingInput = {
  content_angle?: string | null
  why_selected?: string | null
  quote?: string | null
  primary_format?: string | null
}

const MAX_HEADING_WORDS = 14
const MAX_HEADING_CHARS = 90
const MIN_HEADING_CHARS = 12
// A clause about the post format describes the packaging, not the content.
const FORMAT_WORDS = /\b(reels?|carousels?|quote[_ ]posts?|educational[_ ]posts?|posts?|slides?|captions?|graphics?|formats?)\b/i
// Opening words that describe the clip rather than what it is about.
const LEAD_IN = /^(?:the\s+(?:clip|dialogue|exchange|moment|quote|segment|excerpt)\s+(?:is|has|offers|gives|shows)\s+(?:a\s+|an\s+)?|very\s+short\s+but\s+)/i
// Hedged or evaluative openings are judgements, never headings.
const HEDGED = /^(?:could|may|might|would|works?|best|suitable|good|strong|already|perhaps|possibly)\b/i
// Spoken filler at the start of a transcript sentence.
const FILLER = /^(?:(?:so|right|okay|ok|yeah|yes|um|uh|erm|well|and|but|like|i\s+think|you\s+know)[\s,.]+)+/i

const clean = (value?: string | null) => (value ?? '').replace(/\s+/g, ' ').trim()
const words = (value: string) => value.split(' ').filter(Boolean).length
const capitalise = (value: string) => value.charAt(0).toUpperCase() + value.slice(1)
const fits = (value: string) => value.length >= MIN_HEADING_CHARS && value.length <= MAX_HEADING_CHARS
  && words(value) <= MAX_HEADING_WORDS

/** The first descriptive clause of `why_selected`, when it is one. */
function fromWhySelected(value: string) {
  const clause = clean(value.split(/;|:|\s[—–-]\s|\.\s|\.$/)[0]).replace(LEAD_IN, '')
  if (!clause || HEDGED.test(clause) || FORMAT_WORDS.test(clause) || !fits(clause)) return null
  return capitalise(clause.replace(/[.,]+$/, ''))
}

/** A short, complete first sentence of the quote, shown as a quotation. */
function fromQuote(value: string) {
  const sentence = clean(value).match(/^[^.?!]+[.?!]/)?.[0] ?? ''
  const spoken = clean(sentence.replace(FILLER, ''))
  if (words(spoken) < 5 || !fits(spoken)) return null
  return `“${capitalise(spoken.replace(/[.,]+$/, ''))}”`
}

/**
 * The card heading. The saved content angle when there is one; otherwise a
 * heading cut, word for word, from the opportunity's own saved text - never
 * generated, never the whole `why_selected` - or, failing that, the format.
 */
export function opportunityHeading(opportunity: HeadingInput): OpportunityHeading {
  const angle = clean(opportunity.content_angle)
  if (angle) return { text: angle, source: 'content_angle' }
  const why = fromWhySelected(clean(opportunity.why_selected))
  if (why) return { text: why, source: 'why_selected' }
  const quoted = fromQuote(clean(opportunity.quote))
  if (quoted) return { text: quoted, source: 'quote' }
  const format = SOCIAL_FORMAT_LABELS[opportunity.primary_format ?? '']
  return { text: format ? `${format} opportunity` : 'Social media opportunity', source: 'format' }
}

/** How much context an existing evidence clip adds around the moment, from its saved trim. */
export function contextLabel(clip: Pick<EvidenceClip, 'context_before_seconds' | 'context_after_seconds'>) {
  const before = Math.round(Math.max(0, clip.context_before_seconds ?? 0))
  const after = Math.round(Math.max(0, clip.context_after_seconds ?? 0))
  if (!before && !after) return 'No added context'
  if (before === after) return `±${before} s context`
  return `+${before} s before, +${after} s after`
}
