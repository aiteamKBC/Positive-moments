/**
 * Manual Transcript Recovery on the Operations dashboard.
 *
 * The API module is mocked (values are made up). What matters here: the
 * dashboard offers recovery for lectures on ANY date, which lectures take an
 * upload is the server's answer and never the page's, "Upload" opens the one
 * upload dialog for exactly that lecture, and the statuses are re-read after a
 * transcript changed. The dialog itself is covered by transcriptUpload.test.ts.
 */
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import OperationsDashboardView from '../src/views/OperationsDashboardView.vue'
import {
  getCalendar, getDay, getDirectory, getRuns, getTranscriptRecoveryQueue, getTranscriptUploadStatus,
} from '../src/services/operations'
import type {
  DayReport, LectureRow, TranscriptRecoveryLecture, TranscriptRecoveryQueue, TranscriptUploadStatus,
} from '../src/types/operations'

vi.mock('../src/services/operations', () => ({
  getCalendar: vi.fn(), getDay: vi.fn(), getDirectory: vi.fn(), getRuns: vi.fn(), reconcileDay: vi.fn(),
  getTranscriptRecoveryQueue: vi.fn(), getTranscriptUploadStatus: vi.fn(),
  previewTranscriptUpload: vi.fn(), storeTranscriptUpload: vi.fn(), processTranscriptUpload: vi.fn(),
}))

const DAY = '2026-11-13'
const MISSING = '4761883d-1035-5390-9fde-625ffbdd545b'     // on the selected day, transcript missing
const SETTLED = 'a8b354ab-370c-5907-813e-650ec5cc9c0c'      // on the selected day, needs attention otherwise
const OLDER = '59929792-c621-5790-8b7c-12067f27db8e'        // another date, transcript missing

function row(lectureId: string, subject: string): LectureRow {
  return {
    lecture_id: lectureId, subject, session_date: DAY, module: subject, scheduled_start: `${DAY}T09:00:00Z`,
    bucket: 'failed', next_action: 'ACQUIRE_TRANSCRIPT', next_executable_action: 'ACQUIRE_TRANSCRIPT',
    blocking_stage: 'TRANSCRIPT_ACQUISITION', attendance_coverage_status: null,
    attendance_source_authoritative: false, perfect_reason: null, reason_codes: {},
    stages: { QA_EVALUATION: 'BLOCKED', PERFECT_ELIGIBILITY: 'BLOCKED', RECORDING_LINK: 'BLOCKED' },
    is_suppressed_duplicate: false, duplicate_winner_lecture_id: null,
  } as unknown as LectureRow
}

function day(): DayReport {
  return {
    session_date: DAY, canonical_lecture_count: 2, business_lecture_count: 2, suppressed_duplicate_count: 0,
    complete_count: 0, waiting_count: 0, review_count: 0, failed_count: 2,
    lectures: [row(MISSING, 'Ray - MSP'), row(SETTLED, 'Data Analysis')],
    navigation: {
      date: DAY, previous_date: '2026-11-12', next_date: '2026-11-14', today: '2026-11-17',
      is_today: false, is_future: false, timezone: 'Africa/Cairo',
    },
  } as unknown as DayReport
}

function recovering(lectureId: string, sessionDate: string, subject: string): TranscriptRecoveryLecture {
  return {
    lecture_id: lectureId, subject, module: subject, session_date: sessionDate,
    scheduled_start: `${sessionDate}T09:00:00Z`, scheduled_end: `${sessionDate}T11:00:00Z`,
    part_count: 1, missing_part_count: 1, previous_upload_count: 0,
    missing_parts: [{
      part_index: 1, artifact_id: `artifact-${lectureId}`, teams_started_at: `${sessionDate}T09:00:00Z`,
      duration_ms: 7_200_000, artifact_status: 'CONTENT_FETCH_ERROR', graph_failure_reason: 'PROVIDER_ERROR',
    }],
  }
}

function queue(lectures: TranscriptRecoveryLecture[]): TranscriptRecoveryQueue {
  return { lectures, count: lectures.length, truncated: false, date_from: null, date_to: null, limit: 100 }
}

function uploadStatus(lectureId: string, allowed = true): TranscriptUploadStatus {
  return {
    lecture: { lecture_id: lectureId, subject: 'Ray - MSP', module: 'MSP', session_date: DAY,
      scheduled_start: null, scheduled_end: null },
    parts: [], upload_allowed: allowed,
    refusal: allowed ? null : { code: 'ALL_PARTS_HAVE_TRANSCRIPTS', message: 'Every transcript part already has content.' },
    missing_part_count: allowed ? 1 : 0, all_parts_stored: !allowed, uploads: [],
    limits: { max_bytes: 5 * 1024 * 1024, extension: '.vtt' },
    processing: { available: false, reason: 'NO_MANUAL_UPLOAD' },
  } as unknown as TranscriptUploadStatus
}

let page: VueWrapper | undefined

async function open() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/operations', name: 'operations', component: OperationsDashboardView },
      { path: '/lectures/:lectureId', name: 'lecture', component: { template: '<div />' } },
      { path: '/:rest(.*)*', name: 'other', component: { template: '<div />' } },
    ],
  })
  // Named links on the page resolve to the catch-all.
  for (const name of ['operations-media', 'operations-backfill', 'operations-runs', 'pending-attendance',
    'manual-review', 'lectures']) {
    router.addRoute({ path: `/${name}`, name, component: { template: '<div />' } })
  }
  await router.push(`/operations?date=${DAY}`)
  await router.isReady()
  page = mount(OperationsDashboardView, {
    global: {
      plugins: [router],
      stubs: { DateNav: true, RunsTable: true, StageChip: true, AttendanceCell: true, TranscriptUploadDialog: true },
    },
  })
  await flushPromises()
  return page
}

const dialog = () => page!.findComponent({ name: 'TranscriptUploadDialog' })

beforeEach(() => {
  vi.mocked(getDay).mockReset().mockResolvedValue(day())
  vi.mocked(getRuns).mockReset().mockResolvedValue({ runs: [] } as never)
  vi.mocked(getDirectory).mockReset().mockResolvedValue({ lectures: [] } as never)
  vi.mocked(getCalendar).mockReset().mockResolvedValue({ latest_lecture_date: DAY } as never)
  vi.mocked(getTranscriptRecoveryQueue).mockReset().mockImplementation(async (from?: string) => (from
    ? queue([recovering(MISSING, DAY, 'Ray - MSP')])
    : queue([recovering(MISSING, DAY, 'Ray - MSP'), recovering(OLDER, '2026-11-06', 'Ray - MSP 6 Nov')])))
  vi.mocked(getTranscriptUploadStatus).mockReset().mockImplementation(async (id: string) => uploadStatus(id))
})
afterEach(() => {
  page?.unmount()
  page = undefined
})

describe('Manual Transcript Recovery on the Operations dashboard', () => {
  it('is a visible dashboard action that lists missing transcripts across every date', async () => {
    await open()
    const button = page!.get('[data-test="open-transcript-recovery"]')
    expect(button.text()).toContain('Manual Transcript Recovery')
    expect(page!.find('[data-test="transcript-recovery-panel"]').exists()).toBe(false)

    await button.trigger('click')
    await flushPromises()
    // Unbounded: not the selected day.
    expect(vi.mocked(getTranscriptRecoveryQueue).mock.calls.at(-1)).toEqual([])
    const panel = page!.get('[data-test="transcript-recovery-panel"]')
    expect(panel.find(`[data-test="recovery-row-${OLDER}"]`).text()).toContain('Ray - MSP 6 Nov')
    expect(panel.find(`[data-test="recovery-row-${OLDER}"]`).text()).toContain('6 November 2026')
    expect(panel.find(`[data-test="recovery-row-${OLDER}"]`).text()).toContain('content missing')
    expect(panel.find(`[data-test="recovery-row-${OLDER}"]`).text()).toContain('Provider error')
    expect(panel.find(`[data-test="recovery-row-${MISSING}"]`).exists()).toBe(true)
  })

  it('opens the one upload dialog for exactly the lecture chosen', async () => {
    await open()
    await page!.get('[data-test="open-transcript-recovery"]').trigger('click')
    await flushPromises()
    await page!.get(`[data-test="recovery-upload-${OLDER}"]`).trigger('click')
    await flushPromises()
    expect(getTranscriptUploadStatus).toHaveBeenCalledWith(OLDER)
    expect(dialog().props()).toMatchObject({ open: true, lectureId: OLDER })
    expect(dialog().props('status').lecture.lecture_id).toBe(OLDER)
  })

  it('offers a direct upload only on the attention rows the server named for this day', async () => {
    await open()
    expect(getTranscriptRecoveryQueue).toHaveBeenCalledWith(DAY, DAY)
    expect(page!.find(`[data-test="attention-upload-${MISSING}"]`).exists()).toBe(true)
    expect(page!.find(`[data-test="attention-upload-${SETTLED}"]`).exists()).toBe(false)

    await page!.get(`[data-test="attention-upload-${MISSING}"]`).trigger('click')
    await flushPromises()
    expect(dialog().props()).toMatchObject({ open: true, lectureId: MISSING })
  })

  it('offers no upload when the server lists nothing, and the day still loads if the list fails', async () => {
    vi.mocked(getTranscriptRecoveryQueue).mockRejectedValue(new Error('down'))
    await open()
    expect(page!.text()).toContain('Lectures requiring attention')
    expect(page!.find('[data-test^="attention-upload-"]').exists()).toBe(false)
  })

  it('does not open the dialog for a lecture that no longer needs an upload, and says so', async () => {
    vi.mocked(getTranscriptUploadStatus).mockResolvedValue(uploadStatus(MISSING, false))
    await open()
    await page!.get(`[data-test="attention-upload-${MISSING}"]`).trigger('click')
    await flushPromises()
    expect(dialog().exists()).toBe(false)
    expect(page!.text()).toContain('Every transcript part already has content.')
  })

  it('re-reads the day and the recovery list after the dialog changed a transcript', async () => {
    await open()
    await page!.get('[data-test="open-transcript-recovery"]').trigger('click')
    await flushPromises()
    await page!.get(`[data-test="recovery-upload-${MISSING}"]`).trigger('click')
    await flushPromises()
    const dayReads = vi.mocked(getDay).mock.calls.length
    const listReads = vi.mocked(getTranscriptRecoveryQueue).mock.calls.length

    vi.mocked(getTranscriptRecoveryQueue).mockResolvedValue(queue([recovering(OLDER, '2026-11-06', 'Ray - MSP 6 Nov')]))
    dialog().vm.$emit('changed')
    await flushPromises()
    expect(vi.mocked(getDay).mock.calls.length).toBe(dayReads + 1)
    expect(vi.mocked(getTranscriptRecoveryQueue).mock.calls.length).toBeGreaterThanOrEqual(listReads + 2)
    expect(page!.find(`[data-test="recovery-row-${MISSING}"]`).exists()).toBe(false)
    expect(page!.find(`[data-test="attention-upload-${MISSING}"]`).exists()).toBe(false)
    // The dialog stays open over the refresh; the operator closes it.
    expect(dialog().props('open')).toBe(true)
  })
})
