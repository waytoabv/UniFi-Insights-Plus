/**
 * Tests for the cursor-window merge rules.
 *
 * These encode why the log stream stopped jumping: rows are prepended to a
 * window rather than replacing a page, and live tailing pauses while the user
 * is scrolled away from the top.
 */

import { describe, it, expect } from 'vitest'

import {
  MAX_WINDOW,
  appendOlder,
  capWindow,
  mergeDetail,
  nearBottom,
  prependNew,
  shouldPauseForScroll,
  windowBounds,
} from '../logWindow'

const rows = (...ids) => ids.map((id) => ({ id, src_ip: `10.0.0.${id % 255}` }))

describe('prependNew', () => {
  it('puts newer rows in front', () => {
    expect(prependNew(rows(3, 2, 1), rows(5, 4)).map((r) => r.id))
      .toEqual([5, 4, 3, 2, 1])
  })

  it('keeps the window newest-first even if the server did not', () => {
    expect(prependNew(rows(3), rows(4, 6, 5)).map((r) => r.id))
      .toEqual([6, 5, 4, 3])
  })

  it('drops rows already in the window', () => {
    expect(prependNew(rows(3, 2, 1), rows(4, 3, 2)).map((r) => r.id))
      .toEqual([4, 3, 2, 1])
  })

  it('returns the same window when nothing is new', () => {
    const before = rows(3, 2, 1)
    expect(prependNew(before, rows(3, 2))).toBe(before)
  })

  it('handles an empty response', () => {
    const before = rows(3, 2, 1)
    expect(prependNew(before, [])).toBe(before)
    expect(prependNew(before, null)).toBe(before)
  })

  it('fills an empty window', () => {
    expect(prependNew([], rows(2, 1)).map((r) => r.id)).toEqual([2, 1])
  })

  it('ignores rows without an id', () => {
    expect(prependNew([], [{ id: 2 }, { src_ip: 'x' }, { id: null }]).map((r) => r.id))
      .toEqual([2])
  })

  it('caps the window at the older end', () => {
    const full = Array.from({ length: MAX_WINDOW }, (_, i) => ({ id: MAX_WINDOW - i }))
    const merged = prependNew(full, [{ id: MAX_WINDOW + 1 }])
    expect(merged).toHaveLength(MAX_WINDOW)
    expect(merged[0].id).toBe(MAX_WINDOW + 1)
    expect(merged[merged.length - 1].id).toBe(2)
  })
})

describe('appendOlder', () => {
  it('adds older rows to the end', () => {
    expect(appendOlder(rows(5, 4), rows(3, 2)).map((r) => r.id))
      .toEqual([5, 4, 3, 2])
  })

  it('drops overlap from a stale cursor', () => {
    expect(appendOlder(rows(5, 4), rows(4, 3)).map((r) => r.id))
      .toEqual([5, 4, 3])
  })

  it('returns the same window when the page is exhausted', () => {
    const before = rows(5, 4)
    expect(appendOlder(before, [])).toBe(before)
  })

  it('does not cap — paging back is a deliberate request for more', () => {
    const full = Array.from({ length: MAX_WINDOW }, (_, i) => ({ id: MAX_WINDOW - i }))
    expect(appendOlder(full, [{ id: -1 }])).toHaveLength(MAX_WINDOW + 1)
  })
})

describe('capWindow', () => {
  it('leaves a short window alone', () => {
    const before = rows(3, 2, 1)
    expect(capWindow(before, 10)).toBe(before)
  })

  it('trims from the older end', () => {
    expect(capWindow(rows(5, 4, 3, 2, 1), 3).map((r) => r.id)).toEqual([5, 4, 3])
  })
})

describe('windowBounds', () => {
  it('reports both edges', () => {
    expect(windowBounds(rows(9, 5, 2))).toEqual({ newestId: 9, oldestId: 2 })
  })

  it('reports zero for an empty window so the first request is unbounded', () => {
    expect(windowBounds([])).toEqual({ newestId: 0, oldestId: 0 })
    expect(windowBounds(null)).toEqual({ newestId: 0, oldestId: 0 })
  })

  it('does not assume the window is sorted', () => {
    expect(windowBounds(rows(5, 9, 2))).toEqual({ newestId: 9, oldestId: 2 })
  })

  it('handles a single row', () => {
    expect(windowBounds(rows(7))).toEqual({ newestId: 7, oldestId: 7 })
  })
})

describe('mergeDetail', () => {
  it('merges enrichment into the matching row', () => {
    const merged = mergeDetail(rows(3, 2, 1), { id: 2, rdns: 'nas.lan' })
    expect(merged[1]).toMatchObject({ id: 2, rdns: 'nas.lan' })
  })

  it('keeps fields the detail response does not carry', () => {
    const merged = mergeDetail([{ id: 1, src_ip: '10.0.0.1' }], { id: 1, rdns: 'x' })
    expect(merged[0].src_ip).toBe('10.0.0.1')
  })

  it('leaves the window alone when the row scrolled out', () => {
    const before = rows(3, 2)
    expect(mergeDetail(before, { id: 99, rdns: 'x' })).toBe(before)
  })

  it('ignores a detail response without an id', () => {
    const before = rows(3, 2)
    expect(mergeDetail(before, null)).toBe(before)
    expect(mergeDetail(before, {})).toBe(before)
  })

  it('does not mutate the original window', () => {
    const before = [{ id: 1, src_ip: '10.0.0.1' }]
    mergeDetail(before, { id: 1, rdns: 'x' })
    expect(before[0].rdns).toBeUndefined()
  })
})

describe('nearBottom', () => {
  const el = (scrollHeight, scrollTop, clientHeight) => ({ scrollHeight, scrollTop, clientHeight })

  it('is true within the threshold', () => {
    expect(nearBottom(el(10000, 8600, 500))).toBe(true)
  })

  it('is false further up', () => {
    expect(nearBottom(el(10000, 1000, 500))).toBe(false)
  })

  it('is true at the very bottom', () => {
    expect(nearBottom(el(10000, 9500, 500))).toBe(true)
  })

  it('tolerates a missing element', () => {
    expect(nearBottom(null)).toBe(false)
  })
})

describe('shouldPauseForScroll', () => {
  it('does not pause at the top', () => {
    expect(shouldPauseForScroll({ scrollTop: 0 })).toBe(false)
  })

  it('does not pause for a few pixels of overscroll', () => {
    expect(shouldPauseForScroll({ scrollTop: 40 })).toBe(false)
  })

  it('pauses once the user has scrolled into the list', () => {
    expect(shouldPauseForScroll({ scrollTop: 400 })).toBe(true)
  })

  it('tolerates a missing element', () => {
    expect(shouldPauseForScroll(null)).toBe(false)
  })
})
