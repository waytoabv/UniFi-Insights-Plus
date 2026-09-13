import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchLogs } from '../api'
import {
  appendOlder,
  nearBottom,
  prependNew,
  shouldPauseForScroll,
  windowBounds,
} from '../logWindow'

const POLL_MS = 5000
const PAGE_SIZE = 100

/**
 * Cursor-based log window.
 *
 * Replaces the page-number model, where every poll re-fetched page 1 and
 * replaced the visible rows — which is what made the list jump under the
 * reader, most noticeably on a filter with few matches, where the whole page
 * changed at once.
 *
 * Here each poll asks only for what is newer than the window's top id and
 * prepends the result. Scrolling to the bottom asks for what is older than the
 * bottom id and appends. Neither costs a COUNT(*) or an OFFSET on the server.
 *
 * Live tailing pauses while the reader is scrolled away from the top, since
 * that is exactly when prepending rows would shift what they are looking at.
 * New rows keep being counted while paused and are shown on resume.
 */
export function useLogWindow(filters, { enabled = true, scrollRef } = {}) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [pendingCount, setPendingCount] = useState(0)
  const [atTop, setAtTop] = useState(true)
  const [lastUpdate, setLastUpdate] = useState(null)
  const [error, setError] = useState(null)

  // Read inside callbacks that must not be re-created on every change.
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const filtersRef = useRef(filters)
  filtersRef.current = filters
  const atTopRef = useRef(atTop)
  atTopRef.current = atTop
  const loadingOlderRef = useRef(false)
  // Guards against an in-flight reload landing after a newer one.
  const generationRef = useRef(0)

  const query = useCallback((extra) => {
    const { page, ...rest } = filtersRef.current
    return fetchLogs({ ...rest, per_page: PAGE_SIZE, ...extra })
  }, [])

  /** Discard the window and load the newest page. Used on every filter change. */
  const reload = useCallback(async () => {
    const generation = ++generationRef.current
    setLoading(true)
    setError(null)
    try {
      const result = await query({ before_id: 0, since: 0 })
      if (generation !== generationRef.current) return
      setRows(result.data || [])
      setHasMore(Boolean(result.has_more))
      setPendingCount(0)
      setLastUpdate(new Date())
    } catch (err) {
      if (generation !== generationRef.current) return
      setError(err)
    } finally {
      if (generation === generationRef.current) setLoading(false)
    }
  }, [query])

  /** Fetch what arrived since the top of the window. */
  const poll = useCallback(async () => {
    const { newestId } = windowBounds(rowsRef.current)
    if (!newestId) return reload()

    const generation = generationRef.current
    try {
      const result = await query({ since: newestId })
      if (generation !== generationRef.current) return
      const incoming = result.data || []
      if (incoming.length === 0) return

      if (atTopRef.current) {
        setRows((current) => prependNew(current, incoming))
        setLastUpdate(new Date())
      } else {
        // Scrolled away: count them, but leave the view where the reader put it.
        setPendingCount((n) => n + incoming.length)
      }
    } catch {
      // A failed poll is not worth surfacing — the next one is five seconds away.
    }
  }, [query, reload])

  /** Fetch the page before the bottom of the window. */
  const loadOlder = useCallback(async () => {
    if (loadingOlderRef.current) return
    const { oldestId } = windowBounds(rowsRef.current)
    if (!oldestId) return

    loadingOlderRef.current = true
    setLoadingOlder(true)
    const generation = generationRef.current
    try {
      const result = await query({ before_id: oldestId })
      if (generation !== generationRef.current) return
      setRows((current) => appendOlder(current, result.data || []))
      setHasMore(Boolean(result.has_more))
    } catch {
      // Leave hasMore alone so scrolling can retry.
    } finally {
      loadingOlderRef.current = false
      setLoadingOlder(false)
    }
  }, [query])

  /** Jump back to live: show what accumulated while the reader was scrolled away. */
  const resume = useCallback(() => {
    scrollRef?.current?.scrollTo({ top: 0, behavior: 'smooth' })
    setPendingCount(0)
    setAtTop(true)
    // The ref is what poll() reads, and it is only synced during render — so
    // without this the poll below would still see "scrolled away" and count the
    // rows as pending instead of showing them, which is the opposite of resume.
    atTopRef.current = true
    return poll()
  }, [poll, scrollRef])

  // Filter change: the window describes a different query now.
  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), reload])

  // Poll while enabled. Pausing stops the fetch entirely rather than
  // fetching and discarding — the request is the expensive part.
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(poll, POLL_MS)
    return () => clearInterval(id)
  }, [enabled, poll])

  // Scroll: pause live tailing away from the top, page in older rows at the bottom.
  useEffect(() => {
    const el = scrollRef?.current
    if (!el) return
    const onScroll = () => {
      setAtTop(!shouldPauseForScroll(el))
      if (nearBottom(el)) loadOlder()
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [scrollRef, loadOlder])

  return {
    rows,
    loading,
    loadingOlder,
    hasMore,
    pendingCount,
    atTop,
    lastUpdate,
    error,
    reload,
    loadOlder,
    resume,
    setRows,
  }
}
