/**
 * Publisher alerts: the user's own New / Read / Dismissed state, the LMS session an alert names, and
 * the informational digest folded away.
 *
 * The API module is mocked with the shapes the alerts endpoint returns (values are made up).
 */
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PublisherAlertsPanel from '../src/components/publishing/PublisherAlertsPanel.vue'
import { dismissAlert, markAlertRead, restoreAlert } from '../src/services/publishing'
import type { AlertReview, PublisherAlert, PublisherAlerts } from '../src/types/publishing'
import { alertCountText, inAlertView, newAlertCounts } from '../src/utils/publishing'

vi.mock('../src/services/publishing', () => ({ markAlertRead: vi.fn(), dismissAlert: vi.fn(), restoreAlert: vi.fn() }))

const review = (state: AlertReview['state'], over: Partial<AlertReview> = {}): AlertReview => ({
  state, read_at: null, dismissed_at: null, dismiss_reason: null, dismiss_note: null, ...over,
})

function alert(id: string, state: AlertReview['state'], overrides: Partial<PublisherAlert> = {}): PublisherAlert {
  return {
    alert_id: id, severity: 'WARNING', kind: 'ORPHAN_BUNNY_VIDEO', summary: 'Bunny video with no publisher row',
    status: state === 'RESOLVED' ? 'RESOLVED' : 'OPEN', occurrences: 4,
    first_seen_at: '2026-10-23T08:53:00Z', last_seen_at: '2026-10-26T02:00:00Z',
    resolved_at: state === 'RESOLVED' ? '2026-10-27T02:00:00Z' : null, notification_status: 'NOT_SENT',
    lecture: null, job_id: null, detail: { token: 'rec-0a1b2c3d', bunny_video_id: 'be785a53-0000' },
    lms: null, match: null, review: review(state), ...overrides,
  }
}

const LMS_ONLY = alert('a-lms', 'NEW', {
  kind: 'LMS_ONLY_SESSION', summary: 'LMS live-session session on 2026-10-23 has no Lecture Intelligence lecture',
  detail: { lms_occurrence_id: 'OCC-C35', meeting_type: 'live-session' },
  lms: {
    occurrence_id: 'OCC-C35', series_id: 'LIVE-197', module_catalogue_id: 'MOD-1', programme: 'Marketing Executive Level 4',
    meeting_type: 'live-session', occurrence_status: 'scheduled', series_status: 'active',
    scheduled_start: '2026-10-23T08:00:00+00:00', scheduled_end: '2026-10-23T10:00:00+00:00', has_teams_meeting: true,
    scope_status: 'IN_SCOPE', coverage_status: 'LMS_SESSION_NOT_DISCOVERED',
    coverage_meaning: 'Ended past the grace period and no Lecture Intelligence lecture was matched to its Teams meeting and start time.',
    matched_lecture_id: null, evidence: { hours_since_end: 70.2 }, first_seen_at: null, status_changed_at: null,
    source: {
      status: 'OK', title: 'MarTech Foundations — TEST', session_number: 3, series_sessions: 11,
      occurrence_status: 'scheduled', actual_start: null, participant_count: 0, attendance_report: false,
      programme: 'Marketing Executive Level 4', group: null, cohort: null,
    },
  },
})
const MAPPING = alert('a-map', 'READ', {
  kind: 'MISSING_LMS_MAPPING', summary: '2026-10-23: LMS occurrence NO_LMS_SESSION', detail: { status: 'NO_LMS_SESSION' },
  lecture: { lecture_id: '11111111-1111-1111-1111-111111111111', subject: 'AI in Project Control 2026', session_date: '2026-10-23' },
  match: { status: 'NO_LMS_SESSION', rule_version: 'v2', reason: 'meeting not in the LMS', candidates: 0, evaluated_at: null },
  review: review('READ', { read_at: '2026-10-26T09:00:00Z' }),
})
const ORPHAN = alert('a-orphan', 'NEW')
const DISMISSED = alert('a-dismissed', 'DISMISSED', {
  kind: 'DUPLICATE_BLOCKED',
  review: review('DISMISSED', { read_at: '2026-10-26T09:00:00Z', dismissed_at: '2026-10-26T09:01:00Z', dismiss_reason: 'KNOWN_TEST_UPLOAD' }),
})
const RESOLVED = alert('a-resolved', 'RESOLVED', { kind: 'VIDEO_MISSING_IN_BUNNY' })
const DIGEST = alert('a-digest', 'INFO', {
  severity: 'DIGEST', kind: 'DAILY_DIGEST', summary: 'daily digest 2026-10-26', occurrences: 1,
  detail: { discovered: 14, imported: 12, released: 7, failed: 0 },
})

function payload(open: PublisherAlert[], history: PublisherAlert[] = []): PublisherAlerts {
  const newOnes = open.filter(a => a.review.state === 'NEW')
  return {
    alerts: open, history, history_days: 30, counts: {},
    review_counts: {
      new: newOnes.length, new_by_severity: newOnes.length ? { WARNING: newOnes.length } : {},
      read: open.filter(a => a.review.state === 'READ').length,
      dismissed: open.filter(a => a.review.state === 'DISMISSED').length, resolved: history.length,
    },
    dismiss_reasons: [
      { code: 'TEST_LMS_SESSION', label: 'Test LMS session' },
      { code: 'NO_LMS_SESSION_INTENDED', label: 'Intentionally no LMS session' },
      { code: 'KNOWN_TEST_UPLOAD', label: 'Known test upload' }, { code: 'OTHER', label: 'Other' },
    ],
  }
}

let mounted: VueWrapper | null = null

/** Attached to the document, so isVisible() reflects folded (display: none) details. */
async function render(data: PublisherAlerts) {
  mounted = mount(PublisherAlertsPanel, { props: { data }, attachTo: document.body })
  await flushPromises()
  return mounted
}

const rows = (wrapper: VueWrapper) => wrapper.findAll('[data-test="alert-row"]')
const rowOf = (wrapper: VueWrapper, text: string) => rows(wrapper).find(r => r.text().includes(text))!

beforeEach(() => {
  vi.mocked(markAlertRead).mockReset().mockImplementation(async id => ({ alert_id: id, review: review('READ', { read_at: '2026-10-26T10:00:00Z' }) }))
  vi.mocked(dismissAlert).mockReset().mockImplementation(async (id, reason, note) => ({
    alert_id: id, review: review('DISMISSED', { read_at: 'x', dismissed_at: '2026-10-26T10:00:00Z', dismiss_reason: reason, dismiss_note: note || null }),
  }))
  vi.mocked(restoreAlert).mockReset().mockImplementation(async id => ({ alert_id: id, review: review('READ', { read_at: 'x' }) }))
})
afterEach(() => { mounted?.unmount(); mounted = null })

describe('alert counts and views', () => {
  it('counts new alerts per stored severity, worst first, and keeps the stored-severity summary', () => {
    expect(newAlertCounts({ WARNING: 2, CRITICAL: 1 }).map(c => c.text)).toEqual(['1 new Critical', '2 new Warnings'])
    expect(newAlertCounts({})).toEqual([])
    expect(alertCountText({ DIGEST: 1, WARNING: 3 })).toBe('3 Warnings · 1 Digest')
  })

  it('never lists the digest as new, read or history, and keeps dismissed and resolved alerts in history', () => {
    for (const view of ['new', 'read', 'history', 'all'] as const) expect(inAlertView('INFO', view)).toBe(false)
    expect(inAlertView('NEW', 'new') && inAlertView('READ', 'read')).toBe(true)
    expect(inAlertView('DISMISSED', 'new') || inAlertView('DISMISSED', 'read')).toBe(false)
    expect(inAlertView('DISMISSED', 'history') && inAlertView('RESOLVED', 'history')).toBe(true)
    expect(['NEW', 'READ', 'DISMISSED', 'RESOLVED'].every(s => inAlertView(s, 'all'))).toBe(true)
  })
})

describe('Publisher alerts panel', () => {
  it('opens on the new alerts, with each view counted', async () => {
    const wrapper = await render(payload([LMS_ONLY, MAPPING, ORPHAN, DISMISSED, DIGEST], [RESOLVED]))
    expect(wrapper.find('h2').text()).toContain('2 new')
    expect(wrapper.find('[data-test="view-new"]').attributes('aria-pressed')).toBe('true')
    expect(rows(wrapper)).toHaveLength(2)
    expect(rows(wrapper).every(r => r.find('[data-test="alert-state"]').text() === 'New')).toBe(true)
    expect(wrapper.find('[data-test="view-new"]').text()).toContain('2')
    expect(wrapper.find('[data-test="view-read"]').text()).toContain('1')
    expect(wrapper.find('[data-test="view-history"]').text()).toContain('2')        // dismissed + resolved
    expect(wrapper.find('[data-test="view-all"]').text()).toContain('5')            // never the digest
  })

  it('opens on the read alerts when nothing is new', async () => {
    const wrapper = await render(payload([MAPPING, DIGEST]))
    expect(wrapper.find('[data-test="view-read"]').attributes('aria-pressed')).toBe('true')
    expect(rows(wrapper)).toHaveLength(1)
  })

  it('marks a new alert read when it is opened, keeps it listed, and asks for a refresh', async () => {
    const wrapper = await render(payload([LMS_ONLY, ORPHAN]))
    const row = rowOf(wrapper, 'Lms only session')
    await row.find('[data-test="alert-open"]').trigger('click')
    await flushPromises()
    expect(markAlertRead).toHaveBeenCalledExactlyOnceWith('a-lms')
    expect(row.find('[data-test="alert-open"]').attributes('aria-expanded')).toBe('true')
    expect(rowOf(wrapper, 'Lms only session').find('[data-test="alert-state"]').exists()).toBe(false) // now read
    expect(wrapper.find('h2').text()).toContain('1 new')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    // Closing and opening it again does not read it twice.
    await row.find('[data-test="alert-open"]').trigger('click')
    await row.find('[data-test="alert-open"]').trigger('click')
    expect(markAlertRead).toHaveBeenCalledTimes(1)
  })

  it('does not mark an already-read alert again', async () => {
    const wrapper = await render(payload([MAPPING]))
    await rows(wrapper)[0].find('[data-test="alert-open"]').trigger('click')
    expect(markAlertRead).not.toHaveBeenCalled()
  })

  it('shows the real LMS session: title, programme, occurrence, times, and why nothing matched', async () => {
    const wrapper = await render(payload([LMS_ONLY]))
    const row = rows(wrapper)[0]
    expect(row.find('[data-test="alert-open"]').text()).toContain('MarTech Foundations — TEST')
    await row.find('[data-test="alert-open"]').trigger('click')
    const lms = row.find('[data-test="alert-lms"]')
    expect(lms.find('[data-test="lms-title"]').text()).toContain('MarTech Foundations — TEST')
    expect(lms.find('[data-test="lms-title"]').text()).toContain('session 3 of 11')
    for (const text of ['Marketing Executive Level 4', 'OCC-C35', 'LIVE-197', 'Scheduled']) expect(lms.text()).toContain(text)
    expect(lms.find('[data-test="lms-took-place"]').text()).toContain('No actual start recorded in the LMS')
    expect(lms.find('[data-test="lms-took-place"]').text()).toContain('0 participants')
    expect(lms.find('[data-test="lms-coverage"]').text()).toContain('Lms session not discovered')
    expect(lms.find('[data-test="lms-coverage"]').text()).toContain('no Lecture Intelligence lecture was matched')
  })

  it('never invents an LMS title: without the LMS record it says why', async () => {
    const lms = { ...LMS_ONLY.lms!, source: { status: 'NOT_CONFIGURED' } }
    const wrapper = await render(payload([{ ...LMS_ONLY, lms }]))
    const row = rows(wrapper)[0]
    await row.find('[data-test="alert-open"]').trigger('click')
    expect(row.find('[data-test="lms-title"]').text()).toBe('Title not available')
    expect(row.find('[data-test="lms-source-missing"]').text()).toContain('not configured')
    expect(row.find('[data-test="lms-took-place"]').exists()).toBe(false)
    expect(row.text()).toContain('Marketing Executive Level 4')                  // the publisher's coverage row
  })

  it('shows the lecture and its LMS match evaluation for a missing mapping', async () => {
    const wrapper = await render(payload([MAPPING]))
    const row = rows(wrapper)[0]
    await row.find('[data-test="alert-open"]').trigger('click')
    expect(row.find('[data-test="alert-match"]').text()).toContain('No lms session — meeting not in the LMS · 0 LMS candidates')
    expect(row.find('[data-test="alert-lecture"]').text()).toContain('AI in Project Control 2026')
    await row.find('[data-test="alert-lecture"]').trigger('click')
    expect(wrapper.emitted('openLecture')).toEqual([['11111111-1111-1111-1111-111111111111']])
  })

  it('dismisses with a preset reason, which moves the alert to history', async () => {
    const wrapper = await render(payload([LMS_ONLY, ORPHAN]))
    const row = rowOf(wrapper, 'Lms only session')
    await row.find('[data-test="alert-open"]').trigger('click')
    await flushPromises()
    await row.find('[data-test="alert-dismiss"]').trigger('click')
    expect(row.find('[data-test="dismiss-confirm"]').attributes('disabled')).toBeDefined()      // a reason is required
    await row.find('[data-test="reason-TEST_LMS_SESSION"]').setValue(true)
    expect(row.find('[data-test="dismiss-confirm"]').attributes('disabled')).toBeUndefined()
    await row.find('[data-test="dismiss-form"]').trigger('submit')
    await flushPromises()
    expect(dismissAlert).toHaveBeenCalledExactlyOnceWith('a-lms', 'TEST_LMS_SESSION', '')
    expect(row.find('[data-test="alert-review"]').text()).toContain('Test LMS session')
    expect(row.find('[data-test="alert-restore"]').exists()).toBe(true)
    await wrapper.find('[data-test="view-history"]').trigger('click')
    expect(rows(wrapper).some(r => r.text().includes('Lms only session'))).toBe(true)
  })

  it('needs a note for Other', async () => {
    const wrapper = await render(payload([ORPHAN]))
    const row = rows(wrapper)[0]
    await row.find('[data-test="alert-open"]').trigger('click')
    await flushPromises()
    await row.find('[data-test="alert-dismiss"]').trigger('click')
    await row.find('[data-test="reason-OTHER"]').setValue(true)
    expect(row.find('[data-test="dismiss-confirm"]').attributes('disabled')).toBeDefined()
    await row.find('[data-test="dismiss-note"]').setValue('Uploaded by hand before the publisher ran')
    await row.find('[data-test="dismiss-form"]').trigger('submit')
    await flushPromises()
    expect(dismissAlert).toHaveBeenCalledExactlyOnceWith('a-orphan', 'OTHER', 'Uploaded by hand before the publisher ran')
  })

  it('restores a dismissed alert from history; a resolved alert cannot be dismissed', async () => {
    const wrapper = await render(payload([DISMISSED], [RESOLVED]))
    await wrapper.find('[data-test="view-history"]').trigger('click')
    const dismissed = rowOf(wrapper, 'Duplicate blocked')
    await dismissed.find('[data-test="alert-open"]').trigger('click')
    await dismissed.find('[data-test="alert-restore"]').trigger('click')
    await flushPromises()
    expect(restoreAlert).toHaveBeenCalledExactlyOnceWith('a-dismissed')
    const resolved = rowOf(wrapper, 'Video missing in bunny')
    await resolved.find('[data-test="alert-open"]').trigger('click')
    expect(resolved.find('[data-test="alert-dismiss"]').exists()).toBe(false)
    expect(resolved.text()).toContain('resolved by the publisher')
    expect(markAlertRead).not.toHaveBeenCalled()
  })

  it('keeps the digest informational and folded, never read or counted', async () => {
    const wrapper = await render(payload([ORPHAN, DIGEST]))
    const digest = wrapper.find('[data-test="digest-row"]')
    const toggle = digest.find('[data-test="digest-toggle"]')
    const details = digest.find('[data-test="digest-details"]')
    expect([toggle.text(), toggle.attributes('aria-expanded'), details.isVisible()]).toEqual(['View details', 'false', false])
    expect(toggle.attributes('aria-controls')).toBe(details.attributes('id'))
    await toggle.trigger('click')
    expect([toggle.text(), details.isVisible()]).toEqual(['Hide details', true])
    for (const key of ['Discovered', 'Imported', 'Released', 'Failed']) expect(details.text()).toContain(key)
    expect(markAlertRead).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="view-all"]').text()).toContain('1')
  })

  it('an opened digest stays open when the data refreshes', async () => {
    const wrapper = await render(payload([DIGEST]))
    await wrapper.find('[data-test="digest-toggle"]').trigger('click')
    await wrapper.setProps({ data: payload([DIGEST]) })
    expect(wrapper.find('[data-test="digest-details"]').isVisible()).toBe(true)
  })

  it('offers no action on the issue itself: only open, dismiss for me, restore, and a lecture link', async () => {
    const wrapper = await render(payload([LMS_ONLY, MAPPING, ORPHAN, DISMISSED, DIGEST], [RESOLVED]))
    await wrapper.find('[data-test="view-all"]').trigger('click')
    for (const row of rows(wrapper)) await row.find('[data-test="alert-open"]').trigger('click')
    await flushPromises()
    // Every button other than a row's own open/close header (which shows the stored kind and state).
    const actions = wrapper.findAll('button').filter(b => b.attributes('data-test') !== 'alert-open').map(b => b.text())
    expect(actions).toContain('Dismiss…')
    expect(actions).toContain('Restore')
    for (const word of actions) expect(word).not.toMatch(/acknowledge|resolve|delete|retry|import|release|withdraw/i)
  })
})
