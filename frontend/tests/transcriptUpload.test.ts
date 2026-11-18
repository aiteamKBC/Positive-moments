/**
 * Upload Transcript on the Operations lecture page.
 *
 * The API module is mocked. What matters here: the page sends the file to THIS
 * lecture, shows the server's verdict before anything is stored, confirms with
 * exactly what the server previewed, says what is happening while it stores and
 * processes, and offers to resume when processing does not finish.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TranscriptUploadDialog from '../src/components/operations/TranscriptUploadDialog.vue'
import {
  getTranscriptUploadStatus, previewTranscriptUpload, processTranscriptUpload, storeTranscriptUpload,
} from '../src/services/operations'
import type {
  ProcessingJob, TranscriptPart, TranscriptUploadPreview, TranscriptUploadStatus,
} from '../src/types/operations'

vi.mock('../src/services/operations', () => ({
  getTranscriptUploadStatus: vi.fn(),
  previewTranscriptUpload: vi.fn(),
  storeTranscriptUpload: vi.fn(),
  processTranscriptUpload: vi.fn(),
}))

const LECTURE = '59929792-c621-5790-8b7c-12067f27db8e'
const SHA = 'ab'.repeat(32)

function part(index: number, hasContent = false): TranscriptPart {
  return {
    part_index: index, artifact_id: `artifact-${index}`, is_primary: index === 1,
    teams_started_at: '2026-11-06T09:00:00Z', teams_ended_at: '2026-11-06T11:00:00Z',
    duration_ms: 7_200_000, has_content: hasContent, content_source: hasContent ? 'MICROSOFT_GRAPH' : null,
    artifact_status: 'CONTENT_FETCH_ERROR', graph_failure_reason: hasContent ? null : 'PROVIDER_ERROR',
    uploaded_at: null, uploaded_by: null,
  }
}

const lecture = {
  lecture_id: LECTURE, subject: 'Ray - MSP', module: 'MSP', session_date: '2026-11-06',
  scheduled_start: '2026-11-06T09:00:00Z', scheduled_end: '2026-11-06T11:00:00Z',
}

function status(parts: TranscriptPart[] = [part(1)], processing = { available: false, reason: 'NO_MANUAL_UPLOAD' }): TranscriptUploadStatus {
  return {
    lecture, parts, upload_allowed: true, refusal: null,
    missing_part_count: parts.filter((item) => !item.has_content).length, all_parts_stored: false,
    uploads: [], limits: { max_bytes: 5 * 1024 * 1024, extension: '.vtt' },
    processing: {
      available: processing.available, reason: processing.reason, reason_message: null,
      next_action: 'BUILD_CANONICAL_CUES', blocking_stage: 'CANONICAL_CUES', stages_preserved: [],
      cost: { buys_model_generation: true, affects_other_lectures_that_day: false },
    },
  }
}

function preview(overrides: Partial<TranscriptUploadPreview> = {}): TranscriptUploadPreview {
  return {
    ok: true, errors: [], warnings: [], lecture, part: part(1), parts: [part(1)], duplicate: false,
    file: {
      filename: 'Ray MSP.vtt', content_bytes: 76_656, content_sha256: SHA, cue_count: 336,
      spoken_cue_count: 336, first_cue_start_ms: 0, last_cue_end_ms: 7_100_000,
      speaker_label_count: 4, speaker_attribution: true,
    },
    ...overrides,
  }
}

function job(state: ProcessingJob['status'] = 'QUEUED'): ProcessingJob {
  return {
    job_id: 'job-1', lecture_id: LECTURE, reason: 'MANUAL_TRANSCRIPT_UPLOAD', status: state, requested_by: 'ops',
    attempt_count: state === 'QUEUED' ? 0 : 1, max_attempts: 3, next_attempt_after: null, pipeline_run_id: null,
    last_error_code: null, last_error_message: null, outcome: {}, created_at: '2026-11-18T10:00:00Z',
    started_at: null, finished_at: null, updated_at: '2026-11-18T10:00:00Z',
  }
}

function httpError(status: number, data: object) {
  return Object.assign(new Error('Request failed'), { response: { status, data } })
}

async function render(current = status()) {
  const wrapper = mount(TranscriptUploadDialog, { props: { open: true, lectureId: LECTURE, status: current } })
  await flushPromises()
  return wrapper
}

async function choose(wrapper: Awaited<ReturnType<typeof render>>, name = 'Ray MSP.vtt', size = 1000) {
  const file = new File(['x'.repeat(size)], name, { type: 'text/vtt' })
  const input = wrapper.find('[data-test="upload-file"]')
  Object.defineProperty(input.element, 'files', { value: [file] })
  await input.trigger('change')
  return file
}

describe('Upload Transcript dialog', () => {
  afterEach(() => vi.clearAllMocks())

  it('1. shows the target lecture before anything is chosen', async () => {
    const wrapper = await render()
    expect(wrapper.find('[data-test="upload-target-lecture"]').text()).toContain('Ray - MSP')
    expect(wrapper.find('[data-test="upload-check"]').attributes('disabled')).toBeDefined()
  })

  it('2. refuses a non-.vtt file in the browser and never sends it', async () => {
    const wrapper = await render()
    await choose(wrapper, 'notes.docx')
    expect(wrapper.find('[data-test="upload-client-error"]').text()).toContain('.vtt')
    expect(wrapper.find('[data-test="upload-check"]').attributes('disabled')).toBeDefined()
    expect(previewTranscriptUpload).not.toHaveBeenCalled()
  })

  it('3. previews the file against THIS lecture and shows the part and the file facts', async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview())
    const wrapper = await render()
    const file = await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    expect(previewTranscriptUpload).toHaveBeenCalledExactlyOnceWith(LECTURE, file, undefined)
    expect(wrapper.find('[data-test="upload-target-part"]').text()).toContain('Part 1 of 1')
    expect(wrapper.find('[data-test="upload-file-facts"]').text()).toContain('336')
    expect(wrapper.find('[data-test="upload-confirm"]').attributes('disabled')).toBeUndefined()
    expect(storeTranscriptUpload).not.toHaveBeenCalled()             // nothing stored by a preview
  })

  it("4. shows the server's validation errors and cannot be confirmed", async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview({
      ok: false,
      errors: [{ code: 'LONGER_THAN_TEAMS_TRANSCRIPT', message: 'The file runs longer than the Teams transcript of this lecture.' }],
      warnings: [{ code: 'NO_SPEAKER_LABELS', message: 'No cue names a speaker.' }],
    }))
    const wrapper = await render()
    await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="upload-errors"]').text()).toContain('runs longer than the Teams transcript')
    expect(wrapper.find('[data-test="upload-warnings"]').text()).toContain('No cue names a speaker')
    expect(wrapper.find('[data-test="upload-confirm"]').attributes('disabled')).toBeDefined()
  })

  it('5. a multi-part lecture asks which part the file is before checking', async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview())
    const wrapper = await render(status([part(1), part(2)]))
    const file = await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    expect(previewTranscriptUpload).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="upload-client-error"]').text()).toContain('part')
    await wrapper.find('[data-test="upload-part"]').setValue('artifact-2')
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    expect(previewTranscriptUpload).toHaveBeenCalledWith(LECTURE, file, 'artifact-2')
  })

  it('6. confirms with exactly what was previewed and the store queues processing; nothing waits on QA', async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview())
    let finish: (value: never) => void = () => {}
    vi.mocked(storeTranscriptUpload).mockReturnValue(new Promise((resolve) => { finish = resolve as never }))
    const wrapper = await render()
    const file = await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="upload-confirm"]').trigger('click')
    await flushPromises()
    expect(storeTranscriptUpload).toHaveBeenCalledExactlyOnceWith(LECTURE, file, 'artifact-1', SHA)
    expect(wrapper.find('[data-test="upload-progress"]').text()).toContain('Storing the transcript')
    finish({ ...preview(), status: 'STORED', stored: true, selection: {},
      queue: { queued: true, created: true, reason: null, reason_message: null, job: job() } } as never)
    await flushPromises()
    const outcome = wrapper.find('[data-test="upload-outcome"]').text()
    expect(outcome).toContain('processing queued')
    expect(outcome).toContain('You can close this window')
    // The store queued it: the dialog never runs (or waits for) processing itself.
    expect(processTranscriptUpload).not.toHaveBeenCalled()
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('6b. a lecture still missing other parts is stored and says what it waits for', async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview())
    vi.mocked(storeTranscriptUpload).mockResolvedValue({ ...preview(), status: 'STORED', stored: true, selection: {},
      queue: { queued: false, created: false, reason: 'WAITING_FOR_TRANSCRIPT_PARTS',
        reason_message: 'Other transcript parts of this lecture are still missing. Upload them first.', job: null } })
    const wrapper = await render()
    await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="upload-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="upload-outcome"]').text()).toContain('still missing')
  })

  it('7. a store refused by the server shows its reason and processes nothing', async () => {
    vi.mocked(previewTranscriptUpload).mockResolvedValue(preview())
    vi.mocked(storeTranscriptUpload).mockRejectedValue(httpError(409, {
      code: 'TRANSCRIPT_ALREADY_STORED',
      problems: [{ code: 'TRANSCRIPT_ALREADY_STORED', message: 'This transcript part already holds a different stored transcript.' }],
    }))
    const wrapper = await render()
    await choose(wrapper)
    await wrapper.find('[data-test="upload-check"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="upload-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="upload-server-errors"]').text()).toContain('already holds a different stored transcript')
    expect(processTranscriptUpload).not.toHaveBeenCalled()
  })

  it('7b. processing an already stored upload queues it and returns at once', async () => {
    vi.mocked(processTranscriptUpload).mockResolvedValue(
      { queued: true, created: true, reason: null, reason_message: null, job: job() })
    const wrapper = mount(TranscriptUploadDialog, { props: {
      open: true, lectureId: LECTURE, processOnly: true, status: status([part(1, true)], { available: true, reason: '' }) } })
    await flushPromises()
    expect(wrapper.find('[data-test="upload-process"]').text()).toContain('Queue processing')
    await wrapper.find('[data-test="upload-process"]').trigger('click')
    await flushPromises()
    expect(processTranscriptUpload).toHaveBeenCalledExactlyOnceWith(LECTURE)
    expect(wrapper.find('[data-test="upload-outcome"]').text()).toContain('processing queued')
    expect(wrapper.find('[data-test="upload-resume"]').exists()).toBe(false)   // nothing failed
  })

  it('8. a refused queueing keeps the transcript and offers to try again', async () => {
    vi.mocked(processTranscriptUpload)
      .mockRejectedValueOnce(httpError(409, { code: 'NOTHING_TO_PROCESS', detail: 'Every stage this lecture can take is already complete.' }))
      .mockResolvedValueOnce({ queued: true, created: false, reason: 'ALREADY_QUEUED',
        reason_message: 'Processing is already queued or running for this lecture.', job: job('RUNNING') })
    const wrapper = mount(TranscriptUploadDialog, { props: {
      open: true, lectureId: LECTURE, processOnly: true,
      status: status([part(1, true)], { available: true, reason: '' }) } })
    await flushPromises()
    await wrapper.find('[data-test="upload-process"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="upload-outcome"]').text()).toContain('already complete')
    expect(wrapper.find('[data-test="upload-outcome"]').text()).toContain('stays stored')
    await wrapper.find('[data-test="upload-resume"]').trigger('click')
    await flushPromises()
    expect(processTranscriptUpload).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="upload-outcome"]').text()).toContain('already queued')
  })
})
