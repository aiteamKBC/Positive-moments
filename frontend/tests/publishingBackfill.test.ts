/**
 * Historical Backfill in the Publishing Monitor: the inclusive range check, Run only after a current preview
 * and an explicit confirmation (operators only), and the run's progress read back from the server.
 *
 * The API module is mocked with the shapes the backfill endpoints return (values are made up).
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import HistoricalBackfillPanel from '../src/components/publishing/HistoricalBackfillPanel.vue'
import {
  cancelBackfill, getBackfillRun, getBackfillRuns, previewBackfill, retryBackfill, startBackfill,
} from '../src/services/publishing'
import type { BackfillPreview, BackfillRun, BackfillRunDetail, BackfillRuns } from '../src/types/backfill'
import { CATEGORY, canRun, duration, inclusiveDays, rangeProblem } from '../src/utils/backfill'

vi.mock('../src/services/publishing', () => ({
  getBackfillRuns: vi.fn(), previewBackfill: vi.fn(), startBackfill: vi.fn(), getBackfillRun: vi.fn(),
  cancelBackfill: vi.fn(), retryBackfill: vi.fn(),
}))
const replace = vi.fn()
const query: Record<string, string> = {}
vi.mock('vue-router', () => ({ useRoute: () => ({ query }), useRouter: () => ({ replace }) }))

const RUN_ID = 'cccccccc-0000-0000-0000-000000000001'

function runs(over: Partial<BackfillRuns> = {}): BackfillRuns {
  return { runs: [], active: null, operator: true, library_id: 786045, max_days: 31, today: '2026-10-29',
    timezone: 'Africa/Cairo', ...over }
}

function preview(over: Partial<BackfillPreview['summary']> = {}): BackfillPreview {
  return {
    date_from: '2026-09-01', date_to: '2026-09-10', days: 10, library_id: 786045, generated_at: '2026-10-29T10:00:00Z',
    data_source: 'DATABASE', fingerprint: 'fp', preview_token: 'signed-token', token_ttl_seconds: 1800, read_only: true,
    summary: {
      total: 3, eligible: 2, already_published: 0, already_processing: 0, blocked: 1, skipped: 0,
      categories: { READY_TO_QUEUE: 2, MISSING_LMS_MATCH: 1 }, estimated_imports: { known: 1, possible: 1, upper_bound: 2 },
      known_selected_duration_seconds: 7348.5, per_day: { '2026-09-01': { total: 1, eligible: 1 }, '2026-09-10': { total: 2, eligible: 1 } },
      ...over,
    },
    lectures: [{
      lecture_id: 'L1', subject: 'Marketing Impact and Planning', module: null, session_date: '2026-09-01',
      scheduled_start: '2026-09-01T09:00:00Z', scheduled_end: '2026-09-01T11:00:00Z', lms: { status: 'VERIFIED' },
      recording: { status: 'LOCATED', verified_count: 2, candidates: 2, selected_part: 2, selection_rule: 'longest_recording',
        selected_duration_seconds: 7348.5 },
      bunny: { status: 'NOT_IMPORTED', video: 'NOT_STARTED' }, release: 'NOT_RELEASED',
      playback: { available: false, reason: 'NO_VIDEO' }, monitor_state: 'PROCESSING', category: 'READY_TO_QUEUE',
      proposed_action: 'QUEUE', reason: 'LMS match verified and recording located.', data_source: 'DATABASE',
    }],
  }
}

function run(over: Partial<BackfillRun> = {}): BackfillRun {
  return {
    run_id: RUN_ID, date_from: '2026-09-01', date_to: '2026-09-10', library_id: 786045, status: 'RUNNING',
    requested_by: 'operator', reason: '', preview_summary: {}, lms_status: 'COMPLETED', last_tick_at: null, last_tick: {},
    cancel_requested_at: null, cancel_requested_by: null, created_at: '2026-10-29T10:00:00Z', started_at: '2026-10-29T10:01:00Z',
    finished_at: null, is_finished: false, cancel_requested: false,
    counts: { total: 3, pending: 1, processing: 1, completed: 0, skipped: 0, blocked: 1, failed: 0, cancelled: 0 }, ...over,
  }
}

function detail(over: Partial<BackfillRun> = {}): BackfillRunDetail {
  return {
    run: run(over), generated_at: '2026-10-29T10:05:00Z', operator: true,
    lectures: [{
      lecture_id: 'L1', session_date: '2026-09-01', status: 'PROCESSING', initial_category: 'READY_TO_QUEUE', reason: null,
      stage: 'ENCODING', retries: 0, started_at: null, finished_at: null, updated_at: null, subject: 'Marketing Impact and Planning',
      module: null, scheduled_start: '2026-09-01T09:00:00Z', scheduled_end: '2026-09-01T11:00:00Z', monitor_state: 'PROCESSING',
      monitor_reason: null, pipeline: [
        { key: 'teams', label: 'Teams', status: 'LOCATED', done: true, failed: false },
        { key: 'lms', label: 'LMS Match', status: 'VERIFIED', done: true, failed: false },
        { key: 'import', label: 'Bunny Import', status: 'UPLOADED', done: true, failed: false },
        { key: 'encoding', label: 'Encoding', status: 'IN_PROGRESS', done: false, failed: false },
        { key: 'captions', label: 'Captions', status: 'NOT_AVAILABLE', done: false, failed: false },
        { key: 'release', label: 'Release', status: 'NOT_RELEASED', done: false, failed: false },
        { key: 'playback', label: 'LMS Playback', status: 'NOT_RELEASED', done: false, failed: false },
      ],
    }],
  }
}

async function mounted(over: Partial<BackfillRuns> = {}) {
  vi.mocked(getBackfillRuns).mockResolvedValue(runs(over))
  const wrapper = mount(HistoricalBackfillPanel)
  await flushPromises()
  await wrapper.find('[data-test="bf-from"]').setValue('2026-09-01')
  await wrapper.find('[data-test="bf-to"]').setValue('2026-09-10')
  return wrapper
}

describe('Historical Backfill vocabulary and checks', () => {
  it('counts both boundary dates and refuses reversed, future and oversized ranges', () => {
    expect(inclusiveDays('2026-09-01', '2026-09-01')).toBe(1)
    expect(inclusiveDays('2026-09-01', '2026-09-10')).toBe(10)
    expect(inclusiveDays('2026-10-24', '2026-10-31')).toBe(8)                 // across the UK clock change
    expect(rangeProblem('2026-09-01', '2026-09-10', '2026-10-29', 31)).toBe('')
    expect(rangeProblem('2026-09-10', '2026-09-01', '2026-10-29', 31)).toMatch(/before/)
    expect(rangeProblem('2026-10-01', '2026-10-30', '2026-10-29', 31)).toMatch(/future/)
    expect(rangeProblem('2026-08-01', '2026-09-10', '2026-10-29', 31)).toMatch(/at most 31/)
    expect(rangeProblem('', '2026-09-10', '2026-10-29', 31)).toMatch(/both/)
  })

  it('offers Run only for a current preview of the same range, to an operator, with nothing else running', () => {
    const p = preview()
    expect(canRun(p, '2026-09-01', '2026-09-10', true, false)).toBe(true)
    expect(canRun(null, '2026-09-01', '2026-09-10', true, false)).toBe(false)
    expect(canRun(p, '2026-09-01', '2026-09-11', true, false)).toBe(false)
    expect(canRun(p, '2026-09-01', '2026-09-10', false, false)).toBe(false)
    expect(canRun(p, '2026-09-01', '2026-09-10', true, true)).toBe(false)
    expect(canRun(preview({ eligible: 0 }), '2026-09-01', '2026-09-10', true, false)).toBe(false)
  })

  it('writes durations for people and never shows a blocked category as success', () => {
    expect(duration(7348.5)).toBe('2 h 2 min')
    expect(duration(62)).toBe('1 min')
    expect(duration(null)).toBe('—')
    for (const key of ['MISSING_LMS_MATCH', 'MISSING_RECORDING', 'REVIEW_REQUIRED', 'ERROR']) {
      expect(CATEGORY[key].tone).not.toBe('ok')
    }
  })
})

describe('Historical Backfill panel', () => {
  beforeEach(() => {
    vi.mocked(getBackfillRun).mockResolvedValue(detail())
    vi.mocked(previewBackfill).mockResolvedValue(preview())
  })
  afterEach(() => {
    vi.clearAllMocks()
    for (const key of Object.keys(query)) delete query[key]
  })

  it('keeps Run disabled until a preview of the chosen range exists', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="bf-run"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-test="bf-preview"]').trigger('click')
    await flushPromises()
    expect(previewBackfill).toHaveBeenCalledWith('2026-09-01', '2026-09-10')
    expect(wrapper.find('[data-test="bf-preview-table"]').text()).toContain('Marketing Impact and Planning')
    expect(wrapper.find('[data-test="bf-preview-table"]').text()).toContain('2 h 2 min')
    expect(wrapper.find('[data-test="bf-run"]').attributes('disabled')).toBeUndefined()
    await wrapper.find('[data-test="bf-to"]').setValue('2026-09-09')               // the preview no longer applies
    expect(wrapper.find('[data-test="bf-run"]').attributes('disabled')).toBeDefined()
  })

  it('asks for confirmation showing range, eligible lectures, imports and the library before starting', async () => {
    vi.mocked(startBackfill).mockResolvedValue({ run: run({ status: 'QUEUED' }), detail: 'queued' })
    const wrapper = await mounted()
    await wrapper.find('[data-test="bf-preview"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="bf-run"]').trigger('click')
    expect(startBackfill).not.toHaveBeenCalled()
    const confirmation = wrapper.find('[data-test="bf-confirmation"]').text()
    expect(confirmation).toContain('10 days')
    expect(confirmation).toContain('786045')
    expect(confirmation).toContain('up to 2')
    await wrapper.find('[data-test="bf-confirm"]').trigger('click')
    await flushPromises()
    expect(startBackfill).toHaveBeenCalledTimes(1)
    expect(vi.mocked(startBackfill).mock.calls[0][0].preview_token).toBe('signed-token')
    expect(getBackfillRun).toHaveBeenCalledWith(RUN_ID)
  })

  it('lets a non-operator preview but never start', async () => {
    const wrapper = await mounted({ operator: false })
    await wrapper.find('[data-test="bf-preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="bf-run"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('backfill operator role')
  })

  it('shows a reversed range at once and does not ask the server', async () => {
    const wrapper = await mounted()
    await wrapper.find('[data-test="bf-from"]').setValue('2026-09-12')
    expect(wrapper.find('[data-test="bf-problem"]').text()).toMatch(/before/)
    expect(wrapper.find('[data-test="bf-preview"]').attributes('disabled')).toBeDefined()
  })

  it('reopens the active run with its counters and pipeline, and cancels or retries it', async () => {
    vi.mocked(cancelBackfill).mockResolvedValue({ run: run({ cancel_requested: true }), detail: 'cancellation requested' })
    vi.mocked(retryBackfill).mockResolvedValue({ run_id: RUN_ID, lectures_reopened: 1, jobs_reopened: 1, refused: [], lms_rematch: true })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = await mounted({ runs: [run()], active: run() })
    expect(getBackfillRun).toHaveBeenCalledWith(RUN_ID)
    const counters = wrapper.find('[data-test="bf-counters"]').text()
    expect(counters).toContain('Pending')
    expect(counters).toContain('Blocked')
    expect(wrapper.find('[data-test="bf-run-lectures"]').text()).toContain('LMS Playback')
    await wrapper.find('[data-test="bf-cancel"]').trigger('click')
    await flushPromises()
    expect(cancelBackfill).toHaveBeenCalledWith(RUN_ID)
    expect(wrapper.find('[data-test="bf-retry"]').attributes('disabled')).toBeDefined()   // a reason is required
    await wrapper.find('input[aria-label="Retry reason"]').setValue('LMS session created')
    await wrapper.find('[data-test="bf-retry"]').trigger('click')
    await flushPromises()
    expect(retryBackfill).toHaveBeenCalledWith(RUN_ID, 'LMS session created')
    expect(wrapper.text()).toContain('LMS match is checked again')
  })
})
