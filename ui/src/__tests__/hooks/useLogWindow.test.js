/**
 * Tests for the cursor-window hook.
 *
 * The behaviour under test is the reason for the change: polling must not
 * replace what the reader is looking at, and it must not pay for a total it
 * does not use.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'

const fetchLogs = vi.fn()
vi.mock('../../api', () => ({ fetchLogs: (...args) => fetchLogs(...args) }))

const { useLogWindow } = await import('../../hooks/useLogWindow')

const page = (ids, has_more = false) => ({
  data: ids.map((id) => ({ id })),
  has_more,
})

/** A scroll container whose position the test can move. */
function scrollEl(scrollTop = 0) {
  const listeners = []
  return {
    current: {
      scrollTop,
      scrollHeight: 10000,
      clientHeight: 500,
      scrollTo: vi.fn(),
      addEventListener: (_ev, fn) => listeners.push(fn),
      removeEventListener: () => {},
      _scrollTo(top) {
        this.scrollTop = top
        listeners.forEach((fn) => fn())
      },
    },
  }
}

beforeEach(() => {
  fetchLogs.mockReset()
  fetchLogs.mockResolvedValue(page([]))
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('initial load', () => {
  it('fetches without a cursor', async () => {
    fetchLogs.mockResolvedValue(page([3, 2, 1]))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))

    await waitFor(() => expect(result.current.rows).toHaveLength(3))
    expect(fetchLogs).toHaveBeenCalledWith(
      expect.objectContaining({ since: 0, before_id: 0 })
    )
  })

  it('never sends a page number', async () => {
    renderHook(() => useLogWindow({ time_range: '24h', page: 7 }))
    await waitFor(() => expect(fetchLogs).toHaveBeenCalled())
    expect(fetchLogs.mock.calls[0][0]).not.toHaveProperty('page')
  })
})

describe('polling', () => {
  it('asks only for what is newer than the top of the window', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1]))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockResolvedValueOnce(page([5, 4]))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })

    expect(fetchLogs).toHaveBeenLastCalledWith(expect.objectContaining({ since: 3 }))
  })

  it('prepends rather than replacing', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1]))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockResolvedValueOnce(page([5, 4]))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })

    await waitFor(() =>
      expect(result.current.rows.map((r) => r.id)).toEqual([5, 4, 3, 2, 1])
    )
  })

  it('does not poll while disabled', async () => {
    fetchLogs.mockResolvedValueOnce(page([1]))
    renderHook(() => useLogWindow({ time_range: '24h' }, { enabled: false }))
    await waitFor(() => expect(fetchLogs).toHaveBeenCalledTimes(1))

    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(fetchLogs).toHaveBeenCalledTimes(1)
  })
})

describe('scrolled away from the top', () => {
  it('counts new rows instead of inserting them', async () => {
    const ref = scrollEl()
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1]))
    const { result } = renderHook(() =>
      useLogWindow({ time_range: '24h' }, { scrollRef: ref })
    )
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    act(() => { ref.current._scrollTo(800) })
    await waitFor(() => expect(result.current.atTop).toBe(false))

    fetchLogs.mockResolvedValue(page([5, 4]))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })

    await waitFor(() => expect(result.current.pendingCount).toBe(2))
    expect(result.current.rows.map((r) => r.id)).toEqual([3, 2, 1])
  })

  it('resumes at the top and shows what accumulated', async () => {
    const ref = scrollEl()
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1]))
    const { result } = renderHook(() =>
      useLogWindow({ time_range: '24h' }, { scrollRef: ref })
    )
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    act(() => { ref.current._scrollTo(800) })
    await waitFor(() => expect(result.current.atTop).toBe(false))

    fetchLogs.mockResolvedValue(page([5, 4]))
    await act(async () => { await result.current.resume() })

    expect(ref.current.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' })
    await waitFor(() => expect(result.current.pendingCount).toBe(0))
    await waitFor(() =>
      expect(result.current.rows.map((r) => r.id)).toEqual([5, 4, 3, 2, 1])
    )
  })
})

describe('paging older', () => {
  it('asks for rows before the bottom of the window', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1], true))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockResolvedValueOnce(page([0, -1]))
    await act(async () => { await result.current.loadOlder() })

    expect(fetchLogs).toHaveBeenLastCalledWith(expect.objectContaining({ before_id: 1 }))
  })

  it('appends to the end', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2], true))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(2))

    fetchLogs.mockResolvedValueOnce(page([1, 0]))
    await act(async () => { await result.current.loadOlder() })

    await waitFor(() =>
      expect(result.current.rows.map((r) => r.id)).toEqual([3, 2, 1, 0])
    )
  })

  it('does not fire twice while one is in flight', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1], true))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockClear()
    fetchLogs.mockImplementation(() => new Promise(() => {}))  // never settles
    await act(async () => {
      result.current.loadOlder()
      result.current.loadOlder()
    })
    expect(fetchLogs).toHaveBeenCalledTimes(1)
  })

  it('reports has_more from the server', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1], true))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.hasMore).toBe(true))
  })

  it('scrolling to the bottom pages in more', async () => {
    const ref = scrollEl()
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1], true))
    const { result } = renderHook(() =>
      useLogWindow({ time_range: '24h' }, { scrollRef: ref })
    )
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockResolvedValueOnce(page([0]))
    await act(async () => { ref.current._scrollTo(9000) })

    await waitFor(() =>
      expect(fetchLogs).toHaveBeenLastCalledWith(expect.objectContaining({ before_id: 1 }))
    )
  })
})

describe('filter changes', () => {
  it('discards the window and reloads without a cursor', async () => {
    fetchLogs.mockResolvedValue(page([3, 2, 1]))
    const { result, rerender } = renderHook(
      ({ f }) => useLogWindow(f),
      { initialProps: { f: { time_range: '24h' } } }
    )
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockClear()
    fetchLogs.mockResolvedValue(page([9]))
    rerender({ f: { time_range: '7d' } })

    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual([9]))
    expect(fetchLogs).toHaveBeenCalledWith(
      expect.objectContaining({ time_range: '7d', since: 0, before_id: 0 })
    )
  })

  it('ignores a slow response from a superseded filter', async () => {
    let resolveFirst
    fetchLogs.mockReturnValueOnce(new Promise((r) => { resolveFirst = r }))
    const { result, rerender } = renderHook(
      ({ f }) => useLogWindow(f),
      { initialProps: { f: { time_range: '24h' } } }
    )

    fetchLogs.mockResolvedValue(page([9]))
    rerender({ f: { time_range: '7d' } })
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual([9]))

    // The first request lands late with rows for the old filter.
    await act(async () => { resolveFirst(page([3, 2, 1])) })
    expect(result.current.rows.map((r) => r.id)).toEqual([9])
  })
})

describe('errors', () => {
  it('surfaces a failed reload', async () => {
    fetchLogs.mockRejectedValueOnce(new Error('boom'))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(result.current.loading).toBe(false)
  })

  it('keeps the window when a poll fails', async () => {
    fetchLogs.mockResolvedValueOnce(page([3, 2, 1]))
    const { result } = renderHook(() => useLogWindow({ time_range: '24h' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    fetchLogs.mockRejectedValue(new Error('network'))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })

    expect(result.current.rows).toHaveLength(3)
  })
})
