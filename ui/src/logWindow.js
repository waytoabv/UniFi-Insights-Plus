// Merge rules for the cursor-based log window.
//
// The stream keeps a window of rows the client has loaded: new entries are
// prepended as they arrive, older pages are appended as you scroll. Both edges
// are tracked by id rather than page number, because a page number means
// something different every time a row is inserted — which at this ingest rate
// is roughly every 16 milliseconds.
//
// Kept as pure functions so the ordering and dedupe rules can be tested without
// a DOM, a timer, or a fetch.

// Upper bound on rows held in memory. Roughly 8 MB of parsed JSON at the
// observed row size; beyond that the table's own rendering is the bottleneck,
// not the data.
export const MAX_WINDOW = 5000

/** Newest-first ordering. Ids are monotonic, so they order rows exactly. */
function byIdDesc(a, b) {
  return b.id - a.id
}

/**
 * Add newly arrived rows to the front of the window.
 *
 * Duplicates are dropped rather than replaced: a row already in the window was
 * rendered from the same query, and swapping the object would re-key it in
 * React for no visible change. The one exception is a row the user has
 * expanded — see mergeDetail.
 */
export function prependNew(window, incoming) {
  if (!incoming || incoming.length === 0) return window
  const known = new Set(window.map((r) => r.id))
  const fresh = incoming.filter((r) => r && r.id != null && !known.has(r.id))
  if (fresh.length === 0) return window
  return capWindow([...fresh.sort(byIdDesc), ...window])
}

/**
 * Add an older page to the end of the window.
 *
 * The server returns rows older than a cursor, so overlap is only possible if
 * the cursor was stale; filtering here keeps that from producing duplicate
 * React keys.
 */
export function appendOlder(window, incoming) {
  if (!incoming || incoming.length === 0) return window
  const known = new Set(window.map((r) => r.id))
  const older = incoming.filter((r) => r && r.id != null && !known.has(r.id))
  if (older.length === 0) return window
  return [...window, ...older.sort(byIdDesc)]
}

/**
 * Trim the window from the older end.
 *
 * Only the top is load-bearing: it is what the user sees while live-tailing.
 * Rows trimmed from the bottom are re-fetched if they scroll back to them.
 */
export function capWindow(window, max = MAX_WINDOW) {
  return window.length <= max ? window : window.slice(0, max)
}

/** The cursor bounds of a window: what to ask for next, in either direction. */
export function windowBounds(window) {
  if (!window || window.length === 0) return { newestId: 0, oldestId: 0 }
  let newestId = window[0].id
  let oldestId = window[0].id
  for (const row of window) {
    if (row.id > newestId) newestId = row.id
    if (row.id < oldestId) oldestId = row.id
  }
  return { newestId, oldestId }
}

/**
 * Replace one row with its detailed version.
 *
 * Expanding a row fetches enrichment the list query does not carry. Merging it
 * in place keeps the detail visible when new rows arrive above it.
 */
export function mergeDetail(window, detail) {
  if (!detail || detail.id == null) return window
  const index = window.findIndex((r) => r.id === detail.id)
  if (index === -1) return window
  const next = window.slice()
  next[index] = { ...next[index], ...detail }
  return next
}

/**
 * Whether the scroll container is close enough to the bottom to page in more.
 *
 * The threshold is generous on purpose: fetching a page takes longer than the
 * few hundred milliseconds of scrolling that remain once the bottom is visible.
 */
export function nearBottom(el, threshold = 1500) {
  if (!el) return false
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

/**
 * Whether live tailing should pause because the user scrolled away from the top.
 *
 * Prepending rows above the viewport shifts everything the user is reading
 * downward. Pausing while they are not at the top is what stops the list from
 * moving under them — the single most visible difference from replacing the
 * page wholesale every few seconds.
 */
export function shouldPauseForScroll(el, threshold = 60) {
  if (!el) return false
  return el.scrollTop > threshold
}
