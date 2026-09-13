import React, { useState, useEffect, useRef, useCallback } from 'react'
import FilterPanel, { countActive, fromDraft } from './FilterPanel'
import { activeChips, chipText, clearChips, commitTerm, effectiveSearch, removeChip } from '../activeFilters'
import { fetchServices, fetchInterfaces, fetchProtocols } from '../api'
import { getInterfaceName, DIRECTION_ICONS, DIRECTION_COLORS, LOG_TYPE_STYLES, ACTION_STYLES, timeRangeToDays, filterVisibleRanges } from '../utils'
import DateRangePicker from './DateRangePicker'

const LOG_TYPES = ['firewall', 'dns', 'dhcp', 'wifi', 'system']
const TIME_RANGES = [
  { value: '1h', label: '1h' },
  { value: '6h', label: '6h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '60d', label: '60d' },
  { value: '90d', label: '90d' },
  { value: '180d', label: '180d' },
  { value: '365d', label: '365d' },
]
const ACTIONS = ['allow', 'block', 'redirect', 'unknown']
const ACTION_LABELS = { allow: 'ALLOW', block: 'BLOCK', redirect: 'REDIRECT', unknown: 'UNK' }
const ACTION_TOOLTIPS = { unknown: 'Unknown action type' }
const DIRECTIONS = ['inbound', 'outbound', 'inter_vlan', 'nat']

const RESET_FILTERS = {
  time_range: '24h', time_from: null, time_to: null,
  page: 1, per_page: 50,
  ip: null, rule_name: null, search: null, service: null,
  interface: null, protocol: null, dst_port: null, src_port: null,
  country: null, asn: null,
  log_type: null, rule_action: null, direction: null, vpn_only: null,
}

// Shown as the search box's tooltip. Terms are ANDed, so the box doubles as a
// way to stack filters without opening the panel.
const SEARCH_HELP = [
  'Every term must match. Filters as you type; Enter keeps the term.',
  '',
  '10.10.10.10      that address exactly',
  '10.10.30.0/24    that subnet  (10.10.30.* works too)',
  '443              that port, either end',
  'nas              anywhere it is displayed',
  '"allow new"      an exact phrase',
  '!tcp             exclude',
  '',
  'Scope a term:  src: dst: ip: port: sport: dport:',
  '               rule: host: iface: country: asn: proto: action: type:',
].join('\n')


export default function FilterBar({ filters, onChange, maxFilterDays, prefetchedInterfaces, hiddenLogTypes }) {
  const visibleLogTypes = hiddenLogTypes?.size
    ? LOG_TYPES.filter(t => !hiddenLogTypes.has(t))
    : LOG_TYPES
  // The box holds the term being typed; committed terms live in filters.search
  // and are shown as chips. Both apply while typing, so the list narrows before
  // the term is fixed in place — Enter is what fixes it.
  const [textSearch, setTextSearch] = useState('')
  const committedRef = useRef(filters.search || null)
  committedRef.current = filters.search || null
  const [showPanel, setShowPanel] = useState(false)
  // Option lists for the filter panel's protocol chips and the parent's
  // interface prefetch. The per-field inputs that used to filter these lists
  // are gone — the panel and the search box cover the same ground.
  const [services, setServices] = useState([])
  const [interfaces, setInterfaces] = useState([])
  const [protocols, setProtocols] = useState([])
  // Ref to avoid stale closures in debounce effects
  const filtersRef = useRef(filters)
  useEffect(() => { filtersRef.current = filters }, [filters])

  // Sync local input state when filters change externally (e.g. drill-to-logs).
  // Use a ref guard so our own debounced onChange calls don't trigger a sync loop.
  const isInternalChange = useRef(false)
  const wrappedOnChange = useCallback((f) => {
    isInternalChange.current = true
    onChange(f)
  }, [onChange])
  // A search set from outside — a drill-down from the dashboard — arrives as
  // committed terms, so the box itself stays empty and ready for the next one.
  useEffect(() => {
    if (isInternalChange.current) { isInternalChange.current = false; return }
    setTextSearch('')
  }, [filters.search])

  // Load services for autocomplete
  useEffect(() => {
    fetchServices()
      .then(data => setServices(data.services || []))
      .catch(err => { console.error('Failed to load services:', err); setServices([]) })
  }, [])

  // Load protocols for dropdown
  useEffect(() => {
    fetchProtocols()
      .then(data => setProtocols(data.protocols || []))
      .catch(err => { console.error('Failed to load protocols:', err); setProtocols([]) })
  }, [])

  // Load interfaces for filtering (skip fetch if parent already provided them)
  useEffect(() => {
    if (prefetchedInterfaces) { setInterfaces(prefetchedInterfaces); return }
    fetchInterfaces()
      .then(data => setInterfaces(data.interfaces || []))
      .catch(err => { console.error('Failed to load interfaces:', err); setInterfaces([]) })
  }, [prefetchedInterfaces])

  /** Fix the typed term in place as a chip and clear the box for the next. */
  const commitSearch = useCallback(() => {
    const committed = commitTerm(committedRef.current, textSearch)
    setTextSearch('')
    wrappedOnChange({ ...filtersRef.current, search: committed })
  }, [textSearch]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Drop the typed term without committing it. */
  const clearDraft = useCallback(() => {
    setTextSearch('')
    wrappedOnChange({ ...filtersRef.current, search: committedRef.current })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Filter as you type. The intermediate states of a structured term are not
  // wrong, just wider — typing 10.10.10.10 passes through 10.1 and 10.10.,
  // each a real subnet — so the result narrows with each keystroke. The term is
  // provisional until Enter: it applies, but it is not yet a chip.
  useEffect(() => {
    const next = effectiveSearch(committedRef.current, textSearch)
    if (next === (filtersRef.current.search || null)) return
    const t = setTimeout(() => {
      wrappedOnChange({ ...filtersRef.current, search: next })
    }, 180)
    return () => clearTimeout(t)
  }, [textSearch]) // eslint-disable-line react-hooks/exhaustive-deps






  // Auto-correct selected range if it exceeds visible ranges (respects ceiling)
  // Skip when in custom date mode (time_range is null, time_from/time_to are set)
  useEffect(() => {
    if (maxFilterDays == null || visibleRanges.length === 0) return
    if (!filters.time_range && filters.time_from) return
    if (visibleRanges.some(tr => tr.value === filters.time_range)) return
    const largest = visibleRanges.findLast(tr => timeRangeToDays(tr.value) >= 1) || visibleRanges[visibleRanges.length - 1]
    if (largest && largest.value !== filters.time_range) {
      wrappedOnChange({ ...filters, time_range: largest.value })
    }
  }, [maxFilterDays]) // eslint-disable-line react-hooks/exhaustive-deps

  // Strip hidden types from active filter on prop change
  useEffect(() => {
    if (!hiddenLogTypes?.size || !filters.log_type) return
    const current = filters.log_type.split(',')
    const cleaned = current.filter(t => !hiddenLogTypes.has(t))
    if (cleaned.length !== current.length) {
      wrappedOnChange({ ...filters, log_type: cleaned.length === visibleLogTypes.length ? null : cleaned.join(',') || null })
    }
  }, [hiddenLogTypes]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggleType = (type) => {
    const current = filters.log_type ? filters.log_type.split(',') : visibleLogTypes
    const updated = current.includes(type)
      ? current.filter(t => t !== type)
      : [...current, type]
    wrappedOnChange({ ...filters, log_type: updated.length === visibleLogTypes.length ? null : updated.join(',') })
  }

  const activeTypes = filters.log_type ? filters.log_type.split(',') : visibleLogTypes
  const activeActions = filters.rule_action ? filters.rule_action.split(',') : ACTIONS
  const activeDirections = filters.direction ? filters.direction.split(',') : DIRECTIONS

  const [filtersExpanded, setFiltersExpanded] = useState(false)

  const visibleRanges = filterVisibleRanges(TIME_RANGES, maxFilterDays, tr => tr.value)

  // Count active (non-default) filters for mobile badge
  // Counts only what the panel itself exposes, so its badge matches its contents.
  const panelFilterCount = countActive(filters)
  const chips = activeChips(filters)

  const activeFilterCount = [
    filters.log_type,              // types narrowed
    filters.rule_action,           // actions narrowed
    filters.direction,             // directions narrowed
    filters.vpn_only,              // VPN filter active
    (filters.time_from || filters.time_to) || (filters.time_range !== '24h' ? filters.time_range : null),
    filters.search,
    filters.ip, filters.src_ip, filters.dst_ip,
    filters.rule_name, filters.country, filters.asn,
    filters.dst_port, filters.src_port,
    filters.service, filters.interface, filters.protocol,
  ].filter(Boolean).length

  const toggleAction = (action) => {
    const current = filters.rule_action ? filters.rule_action.split(',') : ACTIONS
    const updated = current.includes(action)
      ? current.filter(a => a !== action)
      : [...current, action]
    wrappedOnChange({ ...filters, rule_action: updated.length === ACTIONS.length ? null : updated.join(',') })
  }

  const toggleDirection = (dir) => {
    const current = filters.direction ? filters.direction.split(',') : DIRECTIONS
    const updated = current.includes(dir)
      ? current.filter(d => d !== dir)
      : [...current, dir]
    wrappedOnChange({ ...filters, direction: updated.length === DIRECTIONS.length ? null : updated.join(',') })
  }

  return (
    <div className="space-y-3 lg:space-y-0">
      {/* Mobile filter toggle */}
      <button
        type="button"
        onClick={() => setFiltersExpanded(v => !v)}
        className="lg:hidden flex items-center gap-2 px-3 py-1.5 rounded text-xs font-medium border border-gray-600 text-gray-300 hover:bg-gray-700 transition-colors w-full justify-between"
        aria-expanded={filtersExpanded}
        aria-controls="log-filters-panel"
      >
        {/* Distinct from the panel button below, which opens the filter form —
            this one just expands the bar on a narrow screen. */}
        <span>Filters &amp; search{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}</span>
        <svg className={`w-3.5 h-3.5 transition-transform ${filtersExpanded ? 'rotate-180' : ''}`} viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" focusable="false">
          <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
        </svg>
      </button>

      {/* Filter content — always visible on desktop, collapsible on mobile */}
      <div id="log-filters-panel" className={`${filtersExpanded ? 'block' : 'hidden'} lg:block space-y-3`}>
      {/* Row 1: Log types + time range */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-1.5">
          {visibleLogTypes.map(type => (
            <button
              key={type}
              onClick={() => toggleType(type)}
              className={`px-2.5 py-[3px] rounded text-xs font-medium uppercase border transition-all ${
                activeTypes.includes(type)
                  ? LOG_TYPE_STYLES[type]
                  : 'border-transparent text-gray-500 hover:text-gray-400'
              }`}
            >
              {type}
            </button>
          ))}
        </div>

        <div className="h-5 w-px bg-gray-700" />

        <div className="flex items-center gap-1.5">
          {ACTIONS.map(action => (
            <button
              key={action}
              onClick={() => toggleAction(action)}
              title={ACTION_TOOLTIPS[action] || undefined}
              className={`px-2 py-[3px] rounded text-xs font-medium uppercase border transition-all ${
                activeActions.includes(action)
                  ? ACTION_STYLES[action]
                  : 'border-transparent text-gray-500 hover:text-gray-400'
              }`}
            >
              {ACTION_LABELS[action] || action}
            </button>
          ))}
        </div>

        <div className="h-5 w-px bg-gray-700" />

        <div className="flex items-center gap-1">
          {DIRECTIONS.map(dir => {
            const dirLocked = !!filters.vpn_only
            if (dir === 'nat') {
              return (
                <React.Fragment key={dir}>
                  <button
                    onClick={() => !dirLocked && toggleDirection(dir)}
                    className={`px-2 py-1 rounded text-xs font-medium uppercase transition-all ${
                      dirLocked
                        ? 'bg-black text-white border border-gray-600 opacity-40 cursor-not-allowed'
                        : activeDirections.includes(dir)
                          ? 'bg-black text-white border border-gray-600'
                          : 'text-gray-500 hover:text-gray-400'
                    }`}
                  >
                    <span className={activeDirections.includes(dir) ? DIRECTION_COLORS[dir] : ''}>{DIRECTION_ICONS[dir]}</span> {dir}
                  </button>
                  <button
                    onClick={() => wrappedOnChange({
                      ...filters,
                      vpn_only: filters.vpn_only ? null : true,
                      // When activating VPN, clear direction filter so all directions show
                      ...(!filters.vpn_only ? { direction: null } : {}),
                    })}
                    className={`px-2 py-1 rounded text-xs font-medium uppercase transition-all ${
                      filters.vpn_only
                        ? 'bg-black text-white border border-gray-600'
                        : 'text-gray-500 hover:text-gray-400'
                    }`}
                  >
                    <span className={filters.vpn_only ? 'text-teal-400' : ''}>⛨</span> vpn
                  </button>
                </React.Fragment>
              )
            }
            return (
              <button
                key={dir}
                onClick={() => !dirLocked && toggleDirection(dir)}
                className={`px-2 py-1 rounded text-xs font-medium uppercase transition-all ${
                  dirLocked
                    ? 'bg-black text-white border border-gray-600 opacity-40 cursor-not-allowed'
                    : activeDirections.includes(dir)
                      ? 'bg-black text-white border border-gray-600'
                      : 'text-gray-500 hover:text-gray-400'
                }`}
              >
                <span className={activeDirections.includes(dir) ? DIRECTION_COLORS[dir] : ''}>{DIRECTION_ICONS[dir]}</span> {dir === 'inter_vlan' ? 'vlan' : dir}
              </button>
            )
          })}
        </div>

        <div className="h-5 w-px bg-gray-700" />

        <div className="flex items-center gap-1">
          {visibleRanges.map(tr => (
            <button
              key={tr.value}
              onClick={() => wrappedOnChange({ ...filters, time_range: tr.value, time_from: null, time_to: null })}
              className={`px-2 py-1 rounded text-xs font-medium transition-all ${
                filters.time_range === tr.value
                  ? 'bg-black text-white border border-gray-600'
                  : 'text-gray-400 hover:text-gray-300'
              }`}
            >
              {tr.label}
            </button>
          ))}
          <DateRangePicker
            isActive={!filters.time_range && !!(filters.time_from || filters.time_to)}
            timeFrom={filters.time_from}
            timeTo={filters.time_to}
            maxFilterDays={maxFilterDays}
            onApply={({ time_from, time_to }) =>
              wrappedOnChange({ ...filters, time_range: null, time_from, time_to })
            }
            onClear={() =>
              wrappedOnChange({ ...filters, time_range: '24h', time_from: null, time_to: null })
            }
          />
        </div>
      </div>

      {/* Row 2: what is filtering, and the two ways to change it */}
      <div className="flex flex-col lg:flex-row lg:items-start gap-2">
        {/* Active filters, whichever way they were set */}
        <div className="flex-1 min-w-0 flex flex-wrap items-center gap-1.5">
          {chips.length === 0 ? (
            <span className="text-[11px] text-gray-600 py-1">No filters</span>
          ) : (
            <>
              {chips.map(chip => (
                <span
                  key={chip.id}
                  className={`filter-chip${chip.negated ? ' is-negated' : ''}`}
                >
                  {chip.negated && <span className="filter-chip-label">not</span>}
                  {chip.label && <span className="filter-chip-label">{chip.label}:</span>}
                  <span className="filter-chip-value">{chip.value}</span>
                  <button
                    type="button"
                    onClick={() => wrappedOnChange(removeChip(filtersRef.current, chip))}
                    aria-label={`Remove filter ${chipText(chip)}`}
                    className="filter-chip-remove"
                  >
                    ✕
                  </button>
                </span>
              ))}
              <button
                type="button"
                onClick={() => {
                  setTextSearch('')
                  wrappedOnChange(clearChips(filtersRef.current))
                }}
                className="text-[11px] text-gray-500 hover:text-gray-300 px-1.5 py-0.5"
              >
                Clear all
              </button>
            </>
          )}
        </div>

        {/* Search and the filter panel */}
        <div className="flex items-center gap-2 shrink-0">
          <div className="relative w-full sm:w-72">
            <input
              type="text"
              placeholder="Filter — Enter to keep"
              title={SEARCH_HELP}
              value={textSearch}
              onChange={e => setTextSearch(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') { e.preventDefault(); commitSearch() }
                if (e.key === 'Escape') { e.preventDefault(); clearDraft() }
              }}
              className="w-full bg-black border border-gray-700 rounded pl-7 pr-7 py-1.5 text-xs
                         text-gray-300 placeholder-gray-500 focus:outline-none focus:border-teal-500
                         focus:ring-2 focus:ring-teal-500/20"
            />
            <span className="absolute left-2.5 top-1.5 text-gray-600 text-xs">⌕</span>
            {textSearch && (
              <button
                type="button"
                onClick={clearDraft}
                aria-label="Clear search"
                className="absolute right-2 top-1.5 text-gray-500 hover:text-gray-300 text-xs"
              >✕</button>
            )}
          </div>

          <div className="relative">
            <button
              type="button"
              onClick={() => setShowPanel(v => !v)}
              className={`px-3 py-1.5 rounded border text-xs whitespace-nowrap transition-colors ${
                panelFilterCount > 0
                  ? 'border-teal-500/60 text-teal-300 bg-teal-500/10'
                  : 'border-gray-700 text-gray-400 hover:text-gray-200'
              }`}
              aria-expanded={showPanel}
            >
              Filters{panelFilterCount > 0 ? ` (${panelFilterCount})` : ''}
            </button>
            {showPanel && (
              <FilterPanel
                filters={filters}
                protocols={protocols}
                onApply={(draft) => wrappedOnChange(fromDraft(filtersRef.current, draft))}
                onClose={() => setShowPanel(false)}
              />
            )}
          </div>
        </div>
      </div>
      </div>{/* end collapsible wrapper */}
    </div>
  )
}
