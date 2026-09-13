/**
 * Tests for the active-filter chips.
 *
 * A chip has to remove exactly what it displays. For a search term that means
 * splitting the query the same way the backend does, or removing one chip would
 * quietly change another term.
 */

import { describe, it, expect } from 'vitest'

import {
  activeChips,
  chipText,
  clearChips,
  commitTerm,
  effectiveSearch,
  joinTerms,
  removeChip,
  splitTerms,
} from '../activeFilters'

describe('splitTerms', () => {
  it('splits on whitespace', () => {
    expect(splitTerms('10.10.10.10 443')).toEqual(['10.10.10.10', '443'])
  })

  it('keeps a quoted phrase together', () => {
    expect(splitTerms('"allow established" tcp')).toEqual(['allow established', 'tcp'])
  })

  it('keeps a quoted phrase inside a scoped term', () => {
    expect(splitTerms('rule:"allow new"')).toEqual(['rule:allow new'])
  })

  it('collapses extra whitespace', () => {
    expect(splitTerms('  443   tcp  ')).toEqual(['443', 'tcp'])
  })

  it('handles an empty query', () => {
    expect(splitTerms('')).toEqual([])
    expect(splitTerms(null)).toEqual([])
  })

  it('tolerates an unbalanced quote mid-typing', () => {
    expect(splitTerms('rule:"allow')).toEqual(['rule:allow'])
  })
})

describe('joinTerms', () => {
  it('joins with spaces', () => {
    expect(joinTerms(['443', 'tcp'])).toBe('443 tcp')
  })

  it('re-quotes a phrase', () => {
    expect(joinTerms(['allow established'])).toBe('"allow established"')
  })

  it('round-trips', () => {
    const query = '10.10.10.10 "allow established" !tcp'
    expect(splitTerms(joinTerms(splitTerms(query)))).toEqual(splitTerms(query))
  })
})

describe('activeChips', () => {
  it('is empty for no filters', () => {
    expect(activeChips({})).toEqual([])
  })

  it('labels a panel filter with its field', () => {
    const [chip] = activeChips({ src_ip: '10.10.10.10' })
    expect(chipText(chip)).toBe('Src IP: 10.10.10.10')
  })

  it('leaves a search term bare', () => {
    const [chip] = activeChips({ search: '10.10.10.10' })
    expect(chipText(chip)).toBe('10.10.10.10')
  })

  it('keeps a scoped search term as typed', () => {
    const [chip] = activeChips({ search: 'src:10.0.0.1' })
    expect(chipText(chip)).toBe('src:10.0.0.1')
  })

  it('marks a negated filter', () => {
    const [chip] = activeChips({ dst_port: '!443' })
    expect(chip.negated).toBe(true)
    expect(chipText(chip)).toBe('not Dst port: 443')
  })

  it('marks a negated search term', () => {
    const [chip] = activeChips({ search: '!tcp' })
    expect(chip.negated).toBe(true)
    expect(chipText(chip)).toBe('not tcp')
  })

  it('makes one chip per search term', () => {
    expect(activeChips({ search: '10.10.10.10 443 tcp' })).toHaveLength(3)
  })

  it('combines both sources', () => {
    const chips = activeChips({ src_ip: '10.0.0.1', search: '443' })
    expect(chips.map(chipText)).toEqual(['Src IP: 10.0.0.1', '443'])
  })

  it('ignores filters that are not chips', () => {
    expect(activeChips({ time_range: '7d', per_page: 50, page: 2 })).toEqual([])
  })

  it('ignores an empty value', () => {
    expect(activeChips({ src_ip: '', search: '   ' })).toEqual([])
  })

  it('gives every chip a distinct id', () => {
    const chips = activeChips({ src_ip: '10.0.0.1', dst_ip: '1.1.1.1', search: 'a b' })
    expect(new Set(chips.map((c) => c.id)).size).toBe(chips.length)
  })
})

describe('removeChip', () => {
  it('clears a panel filter', () => {
    const chips = activeChips({ src_ip: '10.0.0.1' })
    expect(removeChip({ src_ip: '10.0.0.1' }, chips[0]).src_ip).toBeNull()
  })

  it('removes one search term and keeps the others', () => {
    const filters = { search: '10.10.10.10 443 tcp' }
    const chips = activeChips(filters)
    expect(removeChip(filters, chips[1]).search).toBe('10.10.10.10 tcp')
  })

  it('clears the search when its last term goes', () => {
    const filters = { search: '443' }
    expect(removeChip(filters, activeChips(filters)[0]).search).toBeNull()
  })

  it('re-quotes a surviving phrase', () => {
    const filters = { search: '"allow established" tcp' }
    const chips = activeChips(filters)
    expect(removeChip(filters, chips[1]).search).toBe('"allow established"')
  })

  it('removes the right term when two look alike', () => {
    const filters = { search: 'tcp 443 tcp' }
    const chips = activeChips(filters)
    expect(removeChip(filters, chips[2]).search).toBe('tcp 443')
  })

  it('leaves other filters alone', () => {
    const filters = { src_ip: '10.0.0.1', dst_ip: '1.1.1.1', time_range: '7d' }
    const next = removeChip(filters, activeChips(filters)[0])
    expect(next.dst_ip).toBe('1.1.1.1')
    expect(next.time_range).toBe('7d')
  })
})

describe('clearChips', () => {
  it('removes every chip', () => {
    const filters = { src_ip: '10.0.0.1', dst_port: '!443', search: 'nas tcp' }
    expect(activeChips(clearChips(filters))).toEqual([])
  })

  it('keeps the time range and log types', () => {
    const next = clearChips({ src_ip: '10.0.0.1', time_range: '7d', log_type: 'firewall' })
    expect(next.time_range).toBe('7d')
    expect(next.log_type).toBe('firewall')
  })
})


describe('commitTerm', () => {
  it('adds the draft to an empty search', () => {
    expect(commitTerm(null, '443')).toBe('443')
  })

  it('appends to what is already committed', () => {
    expect(commitTerm('nas', '443')).toBe('nas 443')
  })

  it('quotes a phrase', () => {
    expect(commitTerm('nas', 'allow new')).toBe('nas "allow new"')
  })

  it('ignores an empty draft', () => {
    expect(commitTerm('nas', '   ')).toBe('nas')
    expect(commitTerm(null, '')).toBeNull()
  })

  it('does not add a term twice', () => {
    expect(commitTerm('443', '443')).toBe('443')
  })

  it('keeps distinct terms that share a prefix', () => {
    expect(commitTerm('10.10.10.1', '10.10.10.10')).toBe('10.10.10.1 10.10.10.10')
  })
})

describe('effectiveSearch', () => {
  it('is the committed terms plus the draft', () => {
    expect(effectiveSearch('nas', '443')).toBe('nas 443')
  })

  it('is the draft alone when nothing is committed', () => {
    expect(effectiveSearch(null, '443')).toBe('443')
  })

  it('is the committed terms alone when the draft is empty', () => {
    expect(effectiveSearch('nas', '')).toBe('nas')
  })

  it('is null when both are empty', () => {
    expect(effectiveSearch(null, '')).toBeNull()
  })

  it('does not duplicate a draft that is already committed', () => {
    expect(effectiveSearch('443', '443')).toBe('443')
  })
})
