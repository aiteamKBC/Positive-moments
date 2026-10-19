/**
 * Edit AI Prompts and Reanalyse Lecture.
 *
 * The API module is mocked with the shapes Django sends. What matters: the
 * saved text is shown and sent back exactly, saving names the version it was
 * opened on and a 409 never loses the edits; reanalysis asks first, sends one
 * request, polls read-only, never calls an acknowledgement "completed", and
 * refreshes the opportunities only after a completion it saw happen.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import SocialMediaDetailView from '../src/views/SocialMediaDetailView.vue'
import SocialMediaView from '../src/views/SocialMediaView.vue'
import {
  getAnalysisRuns,
  getPrompt,
  getPromptVersion,
  getSocialLecture,
  getSocialLectures,
  getSocialSummary,
  requestReanalysis,
  restorePrompt,
  savePrompt,
} from '../src/services/social'
import type { AnalysisRun, PromptDetail, PromptKey } from '../src/types/social'

vi.mock('../src/services/social', () => ({
  getSocialClips: vi.fn(),
  getSocialLecture: vi.fn(),
  getSocialLectures: vi.fn(),
  getSocialSummary: vi.fn(),
  requestSocialClip: vi.fn(),
  getAnalysisRuns: vi.fn(),
  requestReanalysis: vi.fn(),
  getPrompt: vi.fn(),
  getPromptVersion: vi.fn(),
  savePrompt: vi.fn(),
  restorePrompt: vi.fn(),
}))

const COUNTS = { reel: 1, carousel: 0, quote_post: 0, educational_post: 0, other: 0 }
const LECTURE = {
  session_id: 'session-1', session_key: 'KEY-1', subject: 'AI In Marketing', trainer: 'Sharon Shields',
  date: '2026-10-13', recording_available: true, recording_url: 'https://tenant.sharepoint.com/r.mp4',
  opportunity_count: 53, format_counts: COUNTS, last_analyzed_at: '2026-10-18T11:05:42Z',
  meeting_id: null, recording_filename: 'r.mp4', recording_link_status: 'available', opportunities: [],
}
const DISCOVERY = 'You are the "Social Discovery" agent.\n\n\tRules:\n  - Return JSON {"items": []}\nUnicode: é — 🎬 مرحبا\n'
const VERIFICATION = 'Verify each candidate.\nKeep quotes exact.'

function promptDetail(key: PromptKey, content: string | null, version = 1, versions = version): PromptDetail {
  const meta = (v: number) => ({
    prompt_key: key, version: v, content_sha256: `sha-${v}`, restored_from_version: null,
    created_by: 'Mona Hassan', created_at: '2026-10-19T09:00:00Z',
  })
  return {
    prompt_key: key,
    active: content === null ? null : { ...meta(version), content },
    versions: content === null ? [] : Array.from({ length: versions }, (_, i) => meta(versions - i)),
  }
}

function run(overrides: Partial<AnalysisRun> = {}): AnalysisRun {
  return {
    run_id: 'run-1', session_id: 'session-1', trigger: 'on_demand', status: 'queued', requested_by: 'Mona Hassan',
    opportunities_reported: null, opportunities_verified: null, error_code: null, error_message: null,
    created_at: '2026-10-19T09:00:00Z', dispatched_at: '2026-10-19T09:00:01Z', started_at: null,
    finished_at: null, stale: false, ...overrides,
  }
}

function refusal(status: number, data: object) {
  return Object.assign(new Error('refused'), { response: { status, data } })
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/social-media', name: 'social-media', component: SocialMediaView },
      { path: '/social-media/:sessionKey', name: 'social-media-detail', component: SocialMediaDetailView },
    ],
  })
}

async function openPrompts(discovery: PromptDetail, verification: PromptDetail) {
  vi.mocked(getSocialSummary).mockResolvedValue({ lectures: 0, opportunity_count: 0, format_counts: COUNTS })
  vi.mocked(getSocialLectures).mockResolvedValue({
    count: 0, next: null, previous: null, page: 1, page_size: 20, total_pages: 1, results: [],
  })
  vi.mocked(getPrompt).mockImplementation(async (key) => (key === 'social_discovery' ? discovery : verification))
  const router = makeRouter()
  await router.push('/social-media')
  await router.isReady()
  const wrapper = mount(SocialMediaView, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  await wrapper.get('[data-test="edit-prompts"]').trigger('click')
  await flushPromises()
  return wrapper
}

const editor = (wrapper: ReturnType<typeof mount>, key: PromptKey) => wrapper.get(`[data-test="prompt-${key}"]`)

describe('Edit AI Prompts', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  it('1. shows both saved System Messages exactly, each with its active version', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY), promptDetail('social_verification', VERIFICATION, 3))
    expect(wrapper.find('[data-test="prompts-dialog"]').exists()).toBe(true)
    const discovery = editor(wrapper, 'social_discovery')
    expect((discovery.get('[data-test="prompt-text"]').element as HTMLTextAreaElement).value).toBe(DISCOVERY)
    expect(discovery.get('[data-test="active-version"]').text()).toBe('Active: version 1')
    const verification = editor(wrapper, 'social_verification')
    expect((verification.get('[data-test="prompt-text"]').element as HTMLTextAreaElement).value).toBe(VERIFICATION)
    expect(verification.get('[data-test="active-version"]').text()).toBe('Active: version 3')
    expect(verification.get('[data-test="active-meta"]').text()).toContain('Mona Hassan')
    wrapper.unmount()
  })

  it('2. saves one prompt with its exact text and the version it was opened on', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY), promptDetail('social_verification', VERIFICATION))
    const discovery = editor(wrapper, 'social_discovery')
    expect(discovery.get('[data-test="save"]').attributes('disabled')).toBeDefined()
    const edited = `${DISCOVERY}\n  New rule: "keep it short".\n`
    await discovery.get('[data-test="prompt-text"]').setValue(edited)
    vi.mocked(savePrompt).mockResolvedValue({ ...promptDetail('social_discovery', edited, 2).active! })
    vi.mocked(getPrompt).mockResolvedValueOnce(promptDetail('social_discovery', edited, 2))
    await discovery.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(savePrompt).toHaveBeenCalledExactlyOnceWith('social_discovery', edited, 1)
    expect(discovery.get('[data-test="feedback"]').text()).toContain('Saved as version 2')
    expect(discovery.get('[data-test="active-version"]').text()).toBe('Active: version 2')
    // The other prompt was not touched.
    expect(savePrompt).not.toHaveBeenCalledWith('social_verification', expect.anything(), expect.anything())
    wrapper.unmount()
  })

  it('3. a prompt never saved before is saved as version 1 (expected_version 0)', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', null), promptDetail('social_verification', VERIFICATION))
    const discovery = editor(wrapper, 'social_discovery')
    expect(discovery.get('[data-test="active-version"]').text()).toBe('Not saved yet')
    await discovery.get('[data-test="prompt-text"]').setValue('First text')
    vi.mocked(savePrompt).mockResolvedValue({ ...promptDetail('social_discovery', 'First text').active! })
    await discovery.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(savePrompt).toHaveBeenCalledWith('social_discovery', 'First text', 0)
    wrapper.unmount()
  })

  it('4. a 409 conflict keeps the edits and offers to load the latest version', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY), promptDetail('social_verification', VERIFICATION))
    const discovery = editor(wrapper, 'social_discovery')
    await discovery.get('[data-test="prompt-text"]').setValue('My edit')
    vi.mocked(savePrompt).mockRejectedValue(refusal(409, {
      code: 'version_conflict', detail: 'The prompt changed since you opened it.', active_version: 2,
    }))
    await discovery.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(discovery.get('[data-test="feedback"]').text()).toContain('Someone saved version 2')
    expect((discovery.get('[data-test="prompt-text"]').element as HTMLTextAreaElement).value).toBe('My edit')
    vi.mocked(getPrompt).mockResolvedValueOnce(promptDetail('social_discovery', 'Newer text', 2))
    await discovery.get('[data-test="load-latest"]').trigger('click')
    await flushPromises()
    expect((discovery.get('[data-test="prompt-text"]').element as HTMLTextAreaElement).value).toBe('Newer text')
    expect(discovery.get('[data-test="active-version"]').text()).toBe('Active: version 2')
    wrapper.unmount()
  })

  it('5. shows a server refusal as an error and saves nothing else', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY), promptDetail('social_verification', VERIFICATION))
    const discovery = editor(wrapper, 'social_discovery')
    await discovery.get('[data-test="prompt-text"]').setValue('Edit')
    vi.mocked(savePrompt).mockRejectedValue(refusal(400, { code: 'invalid_request', detail: 'content is too long.' }))
    await discovery.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(discovery.get('[data-test="feedback"]').text()).toContain('content is too long.')
    expect(discovery.get('[data-test="feedback"]').classes()).toContain('notice-error')
    wrapper.unmount()
  })

  it('6. views and restores an earlier version through the rollback API', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY, 2), promptDetail('social_verification', VERIFICATION))
    const discovery = editor(wrapper, 'social_discovery')
    vi.mocked(getPromptVersion).mockResolvedValue({ ...promptDetail('social_discovery', 'Old text', 1).active! })
    await discovery.get('[data-test="view-1"]').trigger('click')
    await flushPromises()
    expect(discovery.get('[data-test="preview"]').text()).toContain('Old text')

    vi.mocked(restorePrompt).mockResolvedValue({ ...promptDetail('social_discovery', 'Old text', 3).active! })
    vi.mocked(getPrompt).mockResolvedValueOnce(promptDetail('social_discovery', 'Old text', 3))
    await discovery.get('[data-test="restore-1"]').trigger('click')
    expect(restorePrompt).not.toHaveBeenCalled()
    await discovery.get('[data-test="confirm-restore-1"]').trigger('click')
    await flushPromises()
    expect(restorePrompt).toHaveBeenCalledExactlyOnceWith('social_discovery', 1, 2)
    expect(discovery.get('[data-test="feedback"]').text()).toContain('Version 1 restored as version 3')
    wrapper.unmount()
  })

  it('7. asks before closing with unsaved changes', async () => {
    const wrapper = await openPrompts(promptDetail('social_discovery', DISCOVERY), promptDetail('social_verification', VERIFICATION))
    await editor(wrapper, 'social_verification').get('[data-test="prompt-text"]').setValue('Unsaved')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    await wrapper.get('[data-test="close-prompts"]').trigger('click')
    expect(confirm).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-test="prompts-dialog"]').exists()).toBe(true)
    confirm.mockReturnValue(true)
    await wrapper.get('[data-test="close-prompts"]').trigger('click')
    expect(wrapper.find('[data-test="prompts-dialog"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

async function renderDetail(runs: AnalysisRun[] = []) {
  vi.mocked(getSocialLecture).mockResolvedValue(LECTURE as never)
  vi.mocked(getAnalysisRuns).mockResolvedValue(runs)
  const router = makeRouter()
  await router.push('/social-media/KEY-1')
  await router.isReady()
  const wrapper = mount(SocialMediaDetailView, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('Reanalyse Lecture', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it('8. asks for confirmation, explaining the opportunities are replaced', async () => {
    const wrapper = await renderDetail()
    expect(wrapper.get('[data-test="no-runs"]').text()).toContain('No reanalysis')
    await wrapper.get('[data-test="reanalyse"]').trigger('click')
    const dialog = wrapper.get('[data-test="confirm-reanalysis"]')
    expect(dialog.text()).toContain("replaces this lecture's 53 current Social Media opportunities")
    expect(requestReanalysis).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('9. sends one request and shows the acknowledgement as Queued, never Completed', async () => {
    const wrapper = await renderDetail()
    vi.mocked(requestReanalysis).mockResolvedValue(run())
    await wrapper.get('[data-test="reanalyse"]').trigger('click')
    await wrapper.get('[data-test="confirm"]').trigger('click')
    await flushPromises()
    expect(requestReanalysis).toHaveBeenCalledExactlyOnceWith('KEY-1')
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Queued')
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('Sent to the analysis service')
    expect(wrapper.get('[data-test="reanalyse"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="confirm-reanalysis"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('10. polls the run read-only and refreshes opportunities only after an observed completion', async () => {
    vi.useFakeTimers()
    const wrapper = await renderDetail([run()])
    expect(getSocialLecture).toHaveBeenCalledTimes(1)
    vi.mocked(getAnalysisRuns)
      .mockResolvedValueOnce([run({ status: 'running', started_at: '2026-10-19T09:01:00Z' })])
      .mockResolvedValueOnce([run({
        status: 'completed', started_at: '2026-10-19T09:01:00Z', finished_at: '2026-10-19T09:06:00Z',
        opportunities_reported: 41, opportunities_verified: 41,
      })])
    await vi.advanceTimersByTimeAsync(15_000)
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Running')
    expect(getSocialLecture).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Completed')
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('41 opportunities saved')
    expect(wrapper.get('[data-test="reanalysis-feedback"]').text()).toContain('refreshed')
    expect(getSocialLecture).toHaveBeenCalledTimes(2)
    // Finished: no more polling, and polling never sent a request.
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(getAnalysisRuns).toHaveBeenCalledTimes(3)
    expect(requestReanalysis).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('11. a completed run from before this visit does not trigger a refresh', async () => {
    vi.useFakeTimers()
    const wrapper = await renderDetail([run({ status: 'completed', finished_at: '2026-10-18T09:00:00Z', opportunities_verified: 53 })])
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Completed')
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(getAnalysisRuns).toHaveBeenCalledTimes(1)
    expect(getSocialLecture).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-test="reanalyse"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('12. shows a failed run with its reason and allows a new request', async () => {
    const wrapper = await renderDetail([run({ status: 'failed', error_code: 'dispatch_failed', finished_at: '2026-10-19T09:00:02Z' })])
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Failed')
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('did not accept the request')
    expect(wrapper.get('[data-test="reanalyse"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('13. a run with no result after the time allowed is shown as such and can be requested again', async () => {
    vi.useFakeTimers()
    const wrapper = await renderDetail([run({ stale: true })])
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('No result yet')
    expect(wrapper.get('[data-test="reanalyse"]').attributes('disabled')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(getAnalysisRuns).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('14. shows refusals: not configured, and already in progress', async () => {
    const wrapper = await renderDetail()
    vi.mocked(requestReanalysis).mockRejectedValueOnce(refusal(503, {
      code: 'reanalysis_not_configured', detail: 'Reanalysis is not available yet.',
    }))
    await wrapper.get('[data-test="reanalyse"]').trigger('click')
    await wrapper.get('[data-test="confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="reanalysis-feedback"]').text()).toContain('Reanalysis is not available yet.')

    vi.mocked(requestReanalysis).mockRejectedValueOnce(refusal(409, {
      code: 'analysis_in_progress', detail: 'This lecture is already being analysed.', run_id: 'run-1',
    }))
    vi.mocked(getAnalysisRuns).mockResolvedValue([run({ status: 'running' })])
    await wrapper.get('[data-test="reanalyse"]').trigger('click')
    await wrapper.get('[data-test="confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="reanalysis-feedback"]').text()).toContain('already being analysed')
    expect(wrapper.get('[data-test="run-badge"]').text()).toBe('Running')
    expect(requestReanalysis).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
