import { useEffect, useRef, useState } from 'react'

/**
 * Filter panel: one row per column, a switch for "not", no syntax to remember.
 *
 * The search box understands prefixes like `src:` and `!`, which is fast once
 * you know them and opaque until you do. This is the same set of filters laid
 * out so they can be read off the screen instead of recalled.
 *
 * Negation is stored the way the API already expects it — a leading '!' on the
 * value — so the switch is purely a matter of presentation and nothing
 * downstream changes.
 */

// Every filter the panel exposes, in the order it is displayed.
// `hint` is shown as placeholder text and is the only documentation most of
// these need.
const FIELDS = [
  { key: 'src_ip', label: 'Source IP', hint: '10.10.30.5 or 10.10.30.0/24' },
  { key: 'dst_ip', label: 'Dest IP', hint: '1.1.1.1 or 1.1.1.*' },
  { key: 'ip', label: 'Either IP', hint: 'matches source or dest' },
  { key: 'src_port', label: 'Source port', hint: '51234' },
  { key: 'dst_port', label: 'Dest port', hint: '443' },
  { key: 'rule_name', label: 'Rule', hint: 'LAN-to-WAN' },
  { key: 'country', label: 'Country', hint: 'DE, US, CN' },
  { key: 'asn', label: 'ASN', hint: 'Cloudflare' },
  { key: 'search', label: 'Anything', hint: 'searched across every column' },
]

const ACTIONS = [
  { value: null, label: 'Any' },
  { value: 'allow', label: 'Allowed' },
  { value: 'block', label: 'Blocked' },
]

const DIRECTIONS = [
  { value: null, label: 'Any' },
  { value: 'inbound', label: 'Inbound' },
  { value: 'outbound', label: 'Outbound' },
  { value: 'inter_vlan', label: 'Inter-VLAN' },
]

/** Split an API filter value into its negated flag and bare value. */
export function splitValue(raw) {
  const value = raw ?? ''
  return value.startsWith('!')
    ? { negated: true, value: value.slice(1) }
    : { negated: false, value }
}

/** Rejoin them into what the API expects. */
export function joinValue(negated, value) {
  const trimmed = (value ?? '').trim()
  if (!trimmed) return null
  return negated ? `!${trimmed}` : trimmed
}

/** Read the panel's fields out of a filter object. */
export function toDraft(filters) {
  const draft = {}
  for (const field of FIELDS) draft[field.key] = splitValue(filters[field.key])
  draft.rule_action = filters.rule_action || null
  draft.direction = filters.direction || null
  draft.protocol = filters.protocol ? filters.protocol.split(',').filter(Boolean) : []
  return draft
}

/** Fold the panel's fields back into a filter object. */
export function fromDraft(filters, draft) {
  const next = { ...filters }
  for (const field of FIELDS) {
    next[field.key] = joinValue(draft[field.key].negated, draft[field.key].value)
  }
  next.rule_action = draft.rule_action
  next.direction = draft.direction
  next.protocol = draft.protocol.length ? draft.protocol.join(',') : null
  return next
}

/** How many of the panel's filters are set — drives the badge on the button. */
export function countActive(filters) {
  const draft = toDraft(filters)
  let n = FIELDS.filter((f) => draft[f.key].value).length
  if (draft.rule_action) n += 1
  if (draft.direction) n += 1
  if (draft.protocol.length) n += 1
  return n
}

function Choice({ options, value, onChange }) {
  return (
    <div className="flex gap-1">
      {options.map((option) => {
        const active = value === option.value
        return (
          <button
            key={option.label}
            type="button"
            onClick={() => onChange(option.value)}
            className={`px-2.5 py-1 rounded text-[11px] border transition-colors ${
              active
                ? 'bg-teal-600 border-teal-600 text-white'
                : 'bg-black border-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

function NotSwitch({ on, onChange, label }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      aria-pressed={on}
      aria-label={`${on ? 'Include' : 'Exclude'} ${label}`}
      title={on ? 'Excluding — click to include' : 'Click to exclude instead'}
      className={`w-7 shrink-0 rounded text-[11px] font-medium border transition-colors ${
        on
          ? 'bg-amber-500/20 border-amber-500/60 text-amber-300'
          : 'bg-black border-gray-700 text-gray-600 hover:text-gray-400'
      }`}
    >
      {on ? 'is not' : 'is'}
    </button>
  )
}

export default function FilterPanel({ filters, protocols = [], onApply, onClose }) {
  const [draft, setDraft] = useState(() => toDraft(filters))
  const ref = useRef(null)

  useEffect(() => {
    const onClickOutside = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onClose()
    }
    // Deferred: the click that opened the panel is still propagating.
    const timer = setTimeout(() => document.addEventListener('mousedown', onClickOutside), 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('mousedown', onClickOutside)
    }
  }, [onClose])

  const setField = (key, patch) =>
    setDraft((d) => ({ ...d, [key]: { ...d[key], ...patch } }))

  const toggleProtocol = (name) =>
    setDraft((d) => ({
      ...d,
      protocol: d.protocol.includes(name)
        ? d.protocol.filter((p) => p !== name)
        : [...d.protocol, name],
    }))

  const clearAll = () => setDraft(toDraft({}))
  const apply = () => { onApply(draft); onClose() }

  const hasAny =
    FIELDS.some((f) => draft[f.key].value) ||
    draft.rule_action || draft.direction || draft.protocol.length > 0

  return (
    <div
      ref={ref}
      onKeyDown={(e) => {
        if (e.key === 'Enter') apply()
        if (e.key === 'Escape') onClose()
      }}
      className="absolute right-0 top-full mt-2 z-30 w-[26rem] max-w-[calc(100vw-2rem)]
                 bg-gray-950 border border-gray-700 rounded-lg shadow-2xl p-4"
    >
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-gray-200">Filters</span>
        <span className="text-[10px] text-gray-500">* matches any characters</span>
      </div>

      <div className="space-y-2.5 max-h-[60vh] overflow-y-auto pr-1">
        <div className="flex items-center gap-3">
          <span className="w-20 shrink-0 text-[11px] text-gray-500">Action</span>
          <Choice
            options={ACTIONS}
            value={draft.rule_action}
            onChange={(v) => setDraft((d) => ({ ...d, rule_action: v }))}
          />
        </div>

        <div className="flex items-center gap-3">
          <span className="w-20 shrink-0 text-[11px] text-gray-500">Direction</span>
          <Choice
            options={DIRECTIONS}
            value={draft.direction}
            onChange={(v) => setDraft((d) => ({ ...d, direction: v }))}
          />
        </div>

        {protocols.length > 0 && (
          <div className="flex items-center gap-3">
            <span className="w-20 shrink-0 text-[11px] text-gray-500">Protocol</span>
            <div className="flex gap-1 flex-wrap">
              {protocols.map((name) => {
                const active = draft.protocol.includes(name)
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() => toggleProtocol(name)}
                    className={`px-2.5 py-1 rounded text-[11px] border uppercase transition-colors ${
                      active
                        ? 'bg-teal-600 border-teal-600 text-white'
                        : 'bg-black border-gray-700 text-gray-400 hover:text-gray-200'
                    }`}
                  >
                    {name}
                  </button>
                )
              })}
            </div>
          </div>
        )}

        <div className="border-t border-gray-800 pt-2.5 space-y-2">
          {FIELDS.map((field) => (
            <div key={field.key} className="flex items-center gap-2">
              <label
                htmlFor={`filter-${field.key}`}
                className="w-20 shrink-0 text-[11px] text-gray-500"
              >
                {field.label}
              </label>
              <NotSwitch
                on={draft[field.key].negated}
                label={field.label}
                onChange={(negated) => setField(field.key, { negated })}
              />
              <input
                id={`filter-${field.key}`}
                type="text"
                value={draft[field.key].value}
                placeholder={field.hint}
                onChange={(e) => setField(field.key, { value: e.target.value })}
                className={`flex-1 min-w-0 bg-black border rounded px-2 py-1 text-xs
                            text-gray-300 placeholder-gray-600 focus:outline-none
                            focus:border-teal-500 focus:ring-1 focus:ring-teal-500/30 ${
                              draft[field.key].negated
                                ? 'border-amber-500/40'
                                : 'border-gray-700'
                            }`}
              />
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-between mt-3 pt-3 border-t border-gray-800">
        <button
          type="button"
          onClick={clearAll}
          disabled={!hasAny}
          className="text-[11px] text-gray-500 hover:text-gray-300 disabled:opacity-40
                     disabled:cursor-not-allowed"
        >
          Clear all
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 rounded border border-gray-700 text-[11px]
                       text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={apply}
            className="px-3 py-1.5 rounded bg-teal-600 text-[11px] text-white
                       hover:bg-teal-500"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  )
}
