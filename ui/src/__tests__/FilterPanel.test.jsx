/**
 * Tests for the graphical filter panel.
 *
 * The negation switch is presentation only: it writes the same leading '!' the
 * API already understands, so nothing downstream has to know the panel exists.
 */

import { describe, it, expect } from 'vitest'

import {
  countActive,
  fromDraft,
  joinValue,
  splitValue,
  toDraft,
} from '../components/FilterPanel'

describe('splitValue', () => {
  it('reads a plain value', () => {
    expect(splitValue('10.0.0.1')).toEqual({ negated: false, value: '10.0.0.1' })
  })

  it('reads a negated value', () => {
    expect(splitValue('!10.0.0.1')).toEqual({ negated: true, value: '10.0.0.1' })
  })

  it('treats absent as empty', () => {
    expect(splitValue(null)).toEqual({ negated: false, value: '' })
    expect(splitValue(undefined)).toEqual({ negated: false, value: '' })
  })

  it('keeps a bare bang as an empty negated value', () => {
    expect(splitValue('!')).toEqual({ negated: true, value: '' })
  })
})

describe('joinValue', () => {
  it('writes a plain value', () => {
    expect(joinValue(false, '10.0.0.1')).toBe('10.0.0.1')
  })

  it('writes a negated value', () => {
    expect(joinValue(true, '10.0.0.1')).toBe('!10.0.0.1')
  })

  it('drops an empty value', () => {
    expect(joinValue(false, '')).toBeNull()
    expect(joinValue(false, '   ')).toBeNull()
  })

  it('does not negate nothing', () => {
    expect(joinValue(true, '')).toBeNull()
  })

  it('trims whitespace', () => {
    expect(joinValue(false, '  10.0.0.1  ')).toBe('10.0.0.1')
  })
})

describe('round trip', () => {
  it('preserves values through the panel', () => {
    const filters = { src_ip: '10.0.0.1', dst_port: '!443', country: 'DE' }
    expect(fromDraft(filters, toDraft(filters))).toMatchObject(filters)
  })

  it('preserves the negation switch', () => {
    const draft = toDraft({ dst_port: '!443' })
    expect(draft.dst_port).toEqual({ negated: true, value: '443' })
    expect(fromDraft({}, draft).dst_port).toBe('!443')
  })

  it('leaves filters the panel does not show alone', () => {
    const filters = { time_range: '7d', per_page: 50, src_ip: '10.0.0.1' }
    expect(fromDraft(filters, toDraft(filters))).toMatchObject({
      time_range: '7d',
      per_page: 50,
    })
  })

  it('clears a field that was emptied', () => {
    const draft = toDraft({ src_ip: '10.0.0.1' })
    draft.src_ip.value = ''
    expect(fromDraft({ src_ip: '10.0.0.1' }, draft).src_ip).toBeNull()
  })

  it('carries protocols as a comma list', () => {
    const draft = toDraft({ protocol: 'tcp,udp' })
    expect(draft.protocol).toEqual(['tcp', 'udp'])
    expect(fromDraft({}, draft).protocol).toBe('tcp,udp')
  })

  it('clears protocols when none are selected', () => {
    const draft = toDraft({ protocol: 'tcp' })
    draft.protocol = []
    expect(fromDraft({}, draft).protocol).toBeNull()
  })
})

describe('countActive', () => {
  it('counts nothing for empty filters', () => {
    expect(countActive({})).toBe(0)
  })

  it('counts each set field', () => {
    expect(countActive({ src_ip: '10.0.0.1', dst_port: '443' })).toBe(2)
  })

  it('counts a negated field', () => {
    expect(countActive({ dst_port: '!443' })).toBe(1)
  })

  it('counts action, direction and protocol', () => {
    expect(countActive({ rule_action: 'block', direction: 'inbound', protocol: 'tcp' })).toBe(3)
  })

  it('ignores filters the panel does not show', () => {
    expect(countActive({ time_range: '7d', per_page: 50, page: 3 })).toBe(0)
  })
})
