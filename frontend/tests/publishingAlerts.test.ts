/**
 * Open publisher alerts: counts per stored severity, warnings in full, the daily digest folded.
 *
 * The API module is mocked with the shapes the alerts endpoint returns (values are made up).
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'

import PublisherAlertsPanel from '../src/components/publishing/PublisherAlertsPanel.vue'
import { getPublisherAlerts } from '../src/services/publishing'
import type { PublisherAlert } from '../src/types/publishing'
import { alertCounts, alertCountText } from '../src/utils/publishing'

vi.mock('../src/services/publishing', () => ({ getPublisherAlerts: vi.fn() }))

function alert(overrides: Partial<PublisherAlert>): PublisherAlert {
  return {
    alert_id: 'a-1', severity: 'WARNING', kind: 'ORPHAN_BUNNY_VIDEO', summary: 'A video has no lecture.',
    occurrences: 4, first_seen_at: '2026-10-24T19:00:00Z', last_seen_at: '2026-10-26T19:00:00Z',
    notification_status: null, lecture: null, job_id: null, detail: {},
    ...overrides,
  }
}

const WARNINGS = [
  alert({ alert_id: 'w-1', detail: { token: 'rec-0a1b2c3d', bunny_video_id: '00000000-0000-0000-0000-000000000001' } }),
  alert({
    alert_id: 'w-2', kind: 'MISSING_LMS_MAPPING', summary: 'No LMS occurrence for this lecture.',
    lecture: { lecture_id: '11111111-1111-1111-1111-111111111111', subject: 'Business Law', session_date: '2026-10-25' },
    job_id: '22222222-2222-2222-2222-222222222222', detail: { status: 'NOT_FOUND' },
  }),
  alert({ alert_id: 'w-3', kind: 'LMS_ONLY_SESSION', detail: { lms_occurrence_id: 'OCC-1', meeting_type: 'ONLINE' } }),
]
const DIGEST = alert({
  alert_id: 'd-1', severity: 'DIGEST', kind: 'DAILY_DIGEST', summary: 'Publisher daily digest.',
  occurrences: 1, notification_status: 'SENT',
  detail: { discovered: 14, imported: 12, verified: 11, released: 7, failed: 0 },
})

let mounted: VueWrapper | null = null

/** Attached to the document, so isVisible() reflects the folded (display: none) digest details. */
async function render(alerts: PublisherAlert[], counts: Record<string, number>) {
  vi.mocked(getPublisherAlerts).mockResolvedValue({ alerts, counts })
  mounted = mount(PublisherAlertsPanel, { props: { refreshKey: 'r1' }, attachTo: document.body })
  await flushPromises()
  return mounted
}

beforeEach(() => vi.mocked(getPublisherAlerts).mockReset())
afterEach(() => { mounted?.unmount(); mounted = null })

describe('alert counts', () => {
  it('counts each stored severity separately, worst first and the digest last', () => {
    expect(alertCountText({ DIGEST: 1, WARNING: 3 })).toBe('3 Warnings · 1 Digest')
    expect(alertCountText({ WARNING: 1, CRITICAL: 2 })).toBe('2 Criticals · 1 Warning')
    expect(alertCounts({ DIGEST: 1, WARNING: 3 }).map(c => c.digest)).toEqual([false, true])
  })

  it('leaves out zero counts, shows an unknown severity as itself and says None when empty', () => {
    expect(alertCountText({ WARNING: 0, DIGEST: 1 })).toBe('1 Digest')
    expect(alertCountText({ NEW_LEVEL: 2, DIGEST: 1 })).toBe('2 New levels · 1 Digest')
    expect(alertCountText({})).toBe('None')
    expect(alertCountText(null)).toBe('None')
  })
})

describe('Open publisher alerts panel', () => {
  it('heads the panel with the separate counts, not one ambiguous number', async () => {
    const wrapper = await render([...WARNINGS, DIGEST], { WARNING: 3, DIGEST: 1 })
    expect(wrapper.find('h2').text()).toContain('3 Warnings · 1 Digest')
  })

  it('lists warnings in full: lecture, job, stored message, severity, details and times', async () => {
    const wrapper = await render([...WARNINGS, DIGEST], { WARNING: 3, DIGEST: 1 })
    const rows = wrapper.findAll('[data-test="alert-row"]')
    expect(rows).toHaveLength(3)
    const mapped = rows[1].text()
    expect(mapped).toContain('Warning')
    expect(mapped).toContain('Missing lms mapping')
    expect(mapped).toContain('No LMS occurrence for this lecture.')
    expect(mapped).toContain('Business Law')
    expect(mapped).toContain('job 22222222-2222-2222-2222-222222222222')
    expect(mapped).toContain('NOT_FOUND')
    expect(mapped).toContain('4×')
    expect(rows[0].text()).toContain('rec-0a1b2c3d')
    expect(rows[2].text()).toContain('Not linked to a lecture')
    // The digest is not one of the warning rows.
    expect(wrapper.find('[data-test="alerts-table"]').text()).not.toContain('Daily digest')
  })

  it('opens the linked lecture from a warning', async () => {
    const wrapper = await render(WARNINGS, { WARNING: 3 })
    await wrapper.findAll('[data-test="alert-row"]')[1].find('button').trigger('click')
    expect(wrapper.emitted('openLecture')).toEqual([['11111111-1111-1111-1111-111111111111']])
  })

  it('folds the digest counters by default and shows every one of them on View details', async () => {
    const wrapper = await render([...WARNINGS, DIGEST], { WARNING: 3, DIGEST: 1 })
    const row = wrapper.find('[data-test="digest-row"]')
    expect(row.text()).toContain('Publisher daily digest.')
    const toggle = row.find('[data-test="digest-toggle"]')
    const details = row.find('[data-test="digest-details"]')
    expect(toggle.text()).toBe('View details')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe(details.attributes('id'))
    expect(details.isVisible()).toBe(false)

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.text()).toBe('Hide details')
    expect(details.isVisible()).toBe(true)
    for (const key of ['Discovered', 'Imported', 'Verified', 'Released', 'Failed']) expect(details.text()).toContain(key)
    expect(details.text()).toContain('14')
    expect(details.text()).toContain('notification sent')

    await toggle.trigger('click')
    expect(details.isVisible()).toBe(false)
  })

  it('keeps an opened digest open across an auto-refresh', async () => {
    const wrapper = await render([DIGEST], { DIGEST: 1 })
    await wrapper.find('[data-test="digest-toggle"]').trigger('click')
    await wrapper.setProps({ refreshKey: 'r2' })
    await flushPromises()
    expect(getPublisherAlerts).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="digest-details"]').isVisible()).toBe(true)
  })

  it('with only a digest open, says no warning is open rather than showing an empty table', async () => {
    const wrapper = await render([DIGEST], { DIGEST: 1 })
    expect(wrapper.find('[data-test="no-alerts"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="alerts-table"]').exists()).toBe(false)
    expect(wrapper.find('h2').text()).toContain('1 Digest')
  })

  it('lists an unknown stored severity with the alerts, under its own name', async () => {
    const wrapper = await render([alert({ alert_id: 'n-1', severity: 'NEW_LEVEL' })], { NEW_LEVEL: 1 })
    expect(wrapper.findAll('[data-test="alert-row"]')).toHaveLength(1)
    expect(wrapper.find('[data-test="alert-row"]').text()).toContain('New level')
    expect(wrapper.find('[data-test="digest-section"]').exists()).toBe(false)
  })

  it('offers no action on an alert: the only buttons hide the panel, open a lecture or fold the digest', async () => {
    const wrapper = await render([...WARNINGS, DIGEST], { WARNING: 3, DIGEST: 1 })
    const words = wrapper.findAll('button').map(b => b.text())
    for (const word of words) expect(word).not.toMatch(/acknowledge|resolve|delete|retry|import|release/i)
  })
})
