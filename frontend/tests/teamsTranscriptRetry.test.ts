/**
 * Retry Teams transcript: how the lecture's background job and the confirmation dialog read.
 *
 * The job row is the server's; these tests pin the words an operator sees for each state, that a
 * Teams retry is named as such (a manual upload's job keeps its own name), and that the dialog's
 * optional summary and button words appear only when given.
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ActionPlanDialog from '../src/components/ActionPlanDialog.vue'
import ProcessingJobStatus from '../src/components/operations/ProcessingJobStatus.vue'
import type { ProcessingJob } from '../src/types/operations'

function job(overrides: Partial<ProcessingJob> = {}): ProcessingJob {
  return {
    job_id: 'J1', lecture_id: '94008498-1fa8-51a2-a5c6-c9cd4077e8ab', reason: 'TEAMS_TRANSCRIPT_RETRY',
    status: 'QUEUED', requested_by: 'ops.user', attempt_count: 0, max_attempts: 3,
    next_attempt_after: '2026-11-20T09:00:00Z', pipeline_run_id: null, last_error_code: null,
    last_error_message: null, outcome: {}, created_at: '2026-11-20T09:00:00Z', started_at: null,
    finished_at: null, updated_at: '2026-11-20T09:00:00Z', ...overrides,
  }
}

describe('ProcessingJobStatus for a Teams transcript retry', () => {
  it('names the request and says Teams is being asked while it runs', () => {
    const text = mount(ProcessingJobStatus, { props: { job: job({ status: 'RUNNING', attempt_count: 1,
      started_at: '2026-11-20T09:01:00Z' }) } }).text()
    expect(text).toContain('Teams transcript retry:')
    expect(text).toContain('Asking Microsoft Teams for the transcript, then processing')
  })

  it('says the transcript came from Teams when it completed', () => {
    const text = mount(ProcessingJobStatus, { props: { job: job({ status: 'COMPLETED',
      finished_at: '2026-11-20T09:05:00Z',
      outcome: { final_action: 'NOTHING_TO_DO', teams: { kind: 'READY', reason: 'TRANSCRIPT_STORED', graph_calls: 2 } },
    }) } }).text()
    expect(text).toContain('The transcript was retrieved from Teams.')
  })

  it('shows the failure message once, without a doubled full stop', () => {
    const text = mount(ProcessingJobStatus, { props: { job: job({ status: 'FAILED', attempt_count: 1,
      finished_at: '2026-11-20T09:05:00Z', last_error_code: 'TEAMS_TRANSCRIPT_UNAVAILABLE',
      last_error_message: 'Teams only has a transcript too short or empty to use. No processing was run.',
    }) } }).text()
    expect(text).toContain('No processing was run.')
    expect(text).not.toContain('..')
  })

  const failedAtAttendance = {
    status: 'FAILED' as const, attempt_count: 1, finished_at: '2026-11-20T12:00:00Z',
    last_error_code: 'STOPPED_AT', last_error_message: 'stopped at ATTENDANCE: RESOLVE_ATTENDANCE',
  }

  it('shows a failed attempt as Resolved once its lecture has completed, keeping the history', () => {
    const wrapper = mount(ProcessingJobStatus, { props: { job: job({ ...failedAtAttendance,
      lecture_outcome: { bucket: 'complete', resolved: true, next_action: 'NOTHING_TO_DO' } }) } })
    const text = wrapper.text()
    expect(text).toContain('Resolved')
    expect(text).not.toContain('Failed')
    expect(text).toContain('The lecture has since completed (next: Nothing to do).')
    expect(text).toContain('This attempt stopped')
    expect(text).toContain('stopped at ATTENDANCE: RESOLVE_ATTENDANCE.')
    // The server's own status stays reachable on the badge.
    expect(wrapper.find('[title="FAILED"]').exists()).toBe(true)
  })

  it('keeps a failed attempt Failed while its lecture is still unresolved', () => {
    for (const lecture_outcome of [undefined, { bucket: 'failed', resolved: false, next_action: 'ACQUIRE_TRANSCRIPT' }]) {
      const text = mount(ProcessingJobStatus, { props: { job: job({ ...failedAtAttendance, lecture_outcome }) } }).text()
      expect(text).toContain('Failed')
      expect(text).not.toContain('Resolved')
      expect(text).toContain('Stopped ')
      expect(text).not.toContain('since completed')
    }
  })

  it("keeps a manual upload's job named as before", () => {
    const text = mount(ProcessingJobStatus, { props: { job: job({ reason: 'MANUAL_TRANSCRIPT_UPLOAD' }) } }).text()
    expect(text).toContain('Uploaded transcript processing:')
    expect(text).not.toContain('Teams transcript retry')
  })
})

describe('ActionPlanDialog optional words', () => {
  const plan = { action: 'ACQUIRE_TRANSCRIPT', cost: { calls_microsoft_graph: true } }

  it('shows the summary and the given button words', () => {
    const text = mount(ActionPlanDialog, { props: { open: true, title: 'Retry Teams transcript', plan,
      summary: 'Microsoft Teams is asked again.', confirmLabel: 'Queue the retry' } }).text()
    expect(text).toContain('Microsoft Teams is asked again.')
    expect(text).toContain('Queue the retry')
  })

  it('is unchanged for the existing actions', () => {
    const wrapper = mount(ActionPlanDialog, { props: { open: true, title: 'Retry', plan } })
    expect(wrapper.find('[data-test=plan-summary]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Confirm retry')
  })
})
