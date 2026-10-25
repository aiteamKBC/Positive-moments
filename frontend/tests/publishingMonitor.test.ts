import { describe, expect, it } from 'vitest'

import { age, CAPTIONS, HEALTH, label, MONITOR_STATE, TRANSCRIPT } from '../src/utils/publishing'

describe('Lecture Publishing Monitor vocabulary', () => {
  it('never shows a caption or transcript status as an error: captions are optional', () => {
    for (const badge of [...Object.values(CAPTIONS), ...Object.values(TRANSCRIPT)]) {
      expect(badge.tone).not.toBe('error')
    }
    expect(label.state('PLAYABLE_NO_CAPTIONS').tone).toBe('ok')
  })

  it('never shows an unknown or stale publisher as healthy', () => {
    expect(HEALTH.UNKNOWN.tone).not.toBe('ok')
    expect(HEALTH.STALE.tone).toBe('error')
    expect(HEALTH.STOPPED.tone).toBe('error')
    expect(label.health(undefined).tone).toBe('quiet')
  })

  it('shows an unmapped backend state as itself rather than hiding it', () => {
    expect(label.state('SOMETHING_NEW')).toEqual({ label: 'Something new', tone: 'neutral' })
    expect(Object.keys(MONITOR_STATE)).toContain('WAITING_FOR_RECORDING')
    expect(MONITOR_STATE.WAITING_FOR_RECORDING.tone).toBe('waiting')
  })

  it('writes heartbeat ages for people', () => {
    expect(age(12)).toBe('12 s')
    expect(age(240)).toBe('4 min')
    expect(age(7500)).toBe('2 h 5 min')
    expect(age(null)).toBe('—')
  })
})
