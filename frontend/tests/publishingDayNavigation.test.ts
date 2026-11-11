/**
 * Publishing monitor: one Africa/Cairo business day at a time, moved with the Operations day navigation.
 *
 * The publishing API module is mocked (values are made up); the page runs with a real in-memory router so
 * the URL, Back and refresh behave as in the browser. Only Date is faked, so "now" can sit on either side of
 * Cairo midnight.
 */
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import PublishingMonitorView from '../src/views/PublishingMonitorView.vue'
import {
  getPublisherAlerts, getPublisherHealth, getPublishingLectures, getPublishingSummary,
} from '../src/services/publishing'
import type { PublishingPage, PublishingSummary } from '../src/types/publishing'
import { cairoToday, dayFromQuery, dayNavigation, isBusinessDate } from '../src/utils/publishingDay'

vi.mock('../src/services/publishing', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/services/publishing')>()),
  getPublisherAlerts: vi.fn(), getPublisherHealth: vi.fn(), getPublishingLectures: vi.fn(), getPublishingSummary: vi.fn(),
}))

/** 22:30 UTC on 10 Nov is 00:30 on Wednesday 11 Nov in Cairo (UTC+2): the UTC date is the wrong day. */
const NOW = new Date('2026-11-10T22:30:00Z')

function summary(total = 1): PublishingSummary {
  return {
    total, generated_at: '2026-11-10T22:30:00Z', playable_without_captions: 0, states: {},
    kpis: [
      { key: 'discovered', label: 'Discovered', count: total, definition: 'Every lecture on the day.' },
      { key: 'playable', label: 'Playable in LMS', count: 0, definition: 'Playable now.' },
      { key: 'missing', label: 'Missing recording', count: 0, definition: 'No recording.' },
    ],
  } as unknown as PublishingSummary
}
const EMPTY_PAGE = { total: 0, results: [] } as unknown as PublishingPage

/** The day each summary / lectures request asked for, in call order. */
const summaryDays = () => vi.mocked(getPublishingSummary).mock.calls.map(([from, to]) => `${from}..${to}`)
const lastLecturesCall = () => vi.mocked(getPublishingLectures).mock.calls.at(-1)!

let router: Router
let page: VueWrapper | undefined

async function open(url = '/publishing') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/publishing', name: 'publishing', component: PublishingMonitorView }],
  })
  await router.push(url)
  await router.isReady()
  page = mount(PublishingMonitorView, {
    global: {
      plugins: [router],
      stubs: { HistoricalBackfillPanel: true, PublisherAlertsPanel: true, PublishingLectureDrawer: true },
    },
  })
  await flushPromises()
  return page
}

const heading = () => page!.find('section[aria-label="Business day"] p').text()
const button = (name: string) => page!.get(`button[aria-label="${name}"]`)
const todayButton = () => page!.findAll('section[aria-label="Business day"] button').find(b => b.text() === 'Today')!
async function click(name: string) {
  await button(name).trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  vi.mocked(getPublishingSummary).mockReset().mockResolvedValue(summary())
  vi.mocked(getPublishingLectures).mockReset().mockResolvedValue(EMPTY_PAGE)
  vi.mocked(getPublisherHealth).mockReset().mockRejectedValue(new Error('not under test'))
  vi.mocked(getPublisherAlerts).mockReset().mockRejectedValue(new Error('not under test'))
})
afterEach(() => {
  page?.unmount()
  page = undefined
  vi.useRealTimers()
})

describe('the selected day (pure calendar arithmetic)', () => {
  it('reads today in Africa/Cairo, not in UTC or the browser zone', () => {
    expect(cairoToday(NOW)).toBe('2026-11-11')
    expect(cairoToday(new Date('2026-11-10T21:59:00Z'))).toBe('2026-11-10')
    // Cairo summer time (UTC+3): 21:30 UTC on 30 Jun is already 1 Jul.
    expect(cairoToday(new Date('2026-06-30T21:30:00Z'))).toBe('2026-07-01')
  })

  it('steps across month, year and leap-day boundaries', () => {
    expect(dayNavigation('2026-11-01', '2026-11-11').previous_date).toBe('2026-10-31')
    expect(dayNavigation('2026-12-31', '2026-11-11').next_date).toBe('2027-01-01')
    expect(dayNavigation('2027-01-01', '2026-11-11').previous_date).toBe('2026-12-31')
    expect(dayNavigation('2028-03-01', '2026-11-11').previous_date).toBe('2028-02-29')
  })

  it('marks today and future days the way the Operations navigation does', () => {
    expect(dayNavigation('2026-11-11', '2026-11-11')).toMatchObject({ is_today: true, is_future: false, timezone: 'Africa/Cairo' })
    expect(dayNavigation('2026-11-12', '2026-11-11')).toMatchObject({ is_today: false, is_future: true })
    expect(dayNavigation('2020-01-01', '2026-11-11')).toMatchObject({ is_today: false, is_future: false })
  })

  it('accepts only real dates from the URL and still honours links written for the old presets', () => {
    expect(isBusinessDate('2026-02-28')).toBe(true)
    expect(isBusinessDate('2026-02-30')).toBe(false)
    expect(isBusinessDate('11/11/2026')).toBe(false)
    expect(dayFromQuery({ date: '2026-02-30' }, '2026-11-11')).toBe('2026-11-11')
    expect(dayFromQuery({ date: '2026-10-05' }, '2026-11-11')).toBe('2026-10-05')
    expect(dayFromQuery({ range: 'yesterday' }, '2026-11-01')).toBe('2026-10-31')
    expect(dayFromQuery({ range: 'custom', from: '2026-09-18', to: '2026-09-20' }, '2026-11-11')).toBe('2026-09-18')
    expect(dayFromQuery({ range: 'last7' }, '2026-11-11')).toBe('2026-11-11')
  })
})

describe('Publishing day navigation', () => {
  it('opens on the Cairo day and asks the API for that single day', async () => {
    await open()
    expect(heading()).toContain('Wednesday, 11 November 2026')
    expect(summaryDays()).toEqual(['2026-11-11..2026-11-11'])
    expect(lastLecturesCall().slice(0, 2)).toEqual(['2026-11-11', '2026-11-11'])
    expect(router.currentRoute.value.query.date).toBeUndefined()
    // The old presets are gone.
    for (const old of ['Yesterday', 'Last 7 days', 'Custom']) {
      expect(page!.findAll('button').some(b => b.text() === old)).toBe(false)
    }
  })

  it('moves back one day per click, as many days as needed, reloading each day', async () => {
    await open()
    await click('Previous day')
    expect(heading()).toContain('Tuesday, 10 November 2026')
    await click('Previous day')
    expect(heading()).toContain('Monday, 9 November 2026')
    for (let i = 0; i < 9; i += 1) await click('Previous day')
    expect(heading()).toContain('Saturday, 31 October 2026')
    expect(router.currentRoute.value.query.date).toBe('2026-10-31')
    expect(summaryDays()).toHaveLength(12)
    expect(summaryDays().at(-1)).toBe('2026-10-31..2026-10-31')
    expect(lastLecturesCall().slice(0, 2)).toEqual(['2026-10-31', '2026-10-31'])
  })

  it('moves forward one day, across the year boundary', async () => {
    await open('/publishing?date=2026-12-31')
    await click('Next day')
    expect(heading()).toContain('Friday, 1 January 2027')
    expect(summaryDays().at(-1)).toBe('2027-01-01..2027-01-01')
    await click('Previous day')
    await click('Previous day')
    expect(heading()).toContain('Wednesday, 30 December 2026')
  })

  it('jumps to any date chosen in the picker', async () => {
    await open()
    const picker = page!.get('#day-picker')
    await picker.setValue('2026-09-18')
    await flushPromises()
    expect(heading()).toContain('Friday, 18 September 2026')
    expect(summaryDays().at(-1)).toBe('2026-09-18..2026-09-18')
    expect(router.currentRoute.value.query.date).toBe('2026-09-18')
  })

  it('returns to today with the Today button, which is disabled on today', async () => {
    await open('/publishing?date=2026-10-01')
    expect(todayButton().attributes('disabled')).toBeUndefined()
    await todayButton().trigger('click')
    await flushPromises()
    expect(heading()).toContain('Wednesday, 11 November 2026')
    expect(summaryDays().at(-1)).toBe('2026-11-11..2026-11-11')
    expect(router.currentRoute.value.query.date).toBeUndefined()
    expect(todayButton().attributes('disabled')).toBeDefined()
  })

  it('reopens the day in the URL on refresh, with its filters', async () => {
    await open('/publishing?date=2026-10-05&state=FAILED')
    expect(heading()).toContain('Monday, 5 October 2026')
    const [from, to, filters] = lastLecturesCall()
    expect([from, to, filters.state]).toEqual(['2026-10-05', '2026-10-05', 'FAILED'])
  })

  it('keeps filters when the day changes and starts again at page one', async () => {
    vi.mocked(getPublishingLectures).mockResolvedValue({ total: 80, results: [] } as unknown as PublishingPage)
    await open('/publishing?date=2026-10-05&state=FAILED&page=3')
    await click('Previous day')
    const [from, , filters, pageNumber] = lastLecturesCall()
    expect([from, filters.state, pageNumber]).toEqual(['2026-10-04', 'FAILED', 1])
    expect(router.currentRoute.value.query).toMatchObject({ date: '2026-10-04', state: 'FAILED' })
    expect(router.currentRoute.value.query.page).toBeUndefined()
  })

  it('goes back to the previous day with the browser Back button', async () => {
    await open()
    await click('Previous day')
    await click('Previous day')
    expect(heading()).toContain('Monday, 9 November 2026')
    router.back()
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 0))
    await flushPromises()
    expect(heading()).toContain('Tuesday, 10 November 2026')
    expect(summaryDays().at(-1)).toBe('2026-11-10..2026-11-10')
  })

  it('never lets a slow answer for an earlier day replace the day now selected', async () => {
    await open()
    let late: (value: PublishingSummary) => void = () => {}
    vi.mocked(getPublishingSummary).mockImplementationOnce(() => new Promise(resolve => { late = resolve }))
    await button('Previous day').trigger('click')             // 10 Nov, still in flight
    await router.push('/publishing?date=2026-09-18')          // a link or Back while it loads
    await flushPromises()
    late(summary(99))
    await flushPromises()
    expect(heading()).toContain('Friday, 18 September 2026')
    expect(page!.text()).not.toContain('of 99 lectures')
  })

  it('shows an empty day as empty, not as an error', async () => {
    vi.mocked(getPublishingSummary).mockResolvedValue(summary(0))
    await open('/publishing?date=2026-11-08')
    expect(page!.text()).toContain('No lectures on this day')
  })

  it('shows a failed load as an error with a retry', async () => {
    vi.mocked(getPublishingSummary).mockRejectedValue(new Error('network down'))
    await open()
    expect(page!.text()).toContain('The publishing monitor could not be loaded.')
    vi.mocked(getPublishingSummary).mockResolvedValue(summary(0))
    await page!.findAll('button').find(b => b.text().includes('Try again') || b.text().includes('Retry'))!.trigger('click')
    await flushPromises()
    expect(page!.text()).toContain('No lectures on this day')
  })

  it('still filters by KPI card for the selected day', async () => {
    await open('/publishing?date=2026-10-05')
    await page!.findAll('section[aria-label="Publishing KPIs"] button').find(b => b.text().includes('Missing recording'))!.trigger('click')
    await flushPromises()
    const [from, to, filters] = lastLecturesCall()
    expect([from, to, filters.kpi]).toEqual(['2026-10-05', '2026-10-05', 'missing'])
    expect(router.currentRoute.value.query).toMatchObject({ date: '2026-10-05', kpi: 'missing' })
  })
})
