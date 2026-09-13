// Active filters as removable chips.
//
// Two sources feed the same list. A value set through the filter panel is shown
// with the field it belongs to — `Src IP: 10.10.10.10` — because that is how it
// was entered. A term typed into the search box is shown bare — `10.10.10.10` —
// for the same reason. Either way it can be removed on its own.
//
// The tokenizer here is for display only. The backend's parser in
// search_query.py is authoritative for what a term actually means; this one
// just needs to split the string the same way so a chip removes the right term.

/** Filters shown as chips, with the label the chip carries. */
export const CHIP_FIELDS = [
  { key: 'src_ip', label: 'Src IP' },
  { key: 'dst_ip', label: 'Dst IP' },
  { key: 'ip', label: 'IP' },
  { key: 'src_port', label: 'Src port' },
  { key: 'dst_port', label: 'Dst port' },
  { key: 'rule_name', label: 'Rule' },
  { key: 'country', label: 'Country' },
  { key: 'asn', label: 'ASN' },
  { key: 'service', label: 'Service' },
  { key: 'interface', label: 'Interface' },
  { key: 'rule_action', label: 'Action' },
  { key: 'direction', label: 'Direction' },
  { key: 'protocol', label: 'Protocol' },
]

const FIELD_LABELS = Object.fromEntries(CHIP_FIELDS.map((f) => [f.key, f.label]))

/**
 * Split a search string into its terms, keeping quoted phrases together.
 *
 * Mirrors shlex.split on the backend closely enough for display: the only
 * difference that matters is an unbalanced quote mid-typing, which falls back
 * to splitting on whitespace rather than throwing.
 */
export function splitTerms(query) {
  if (!query) return []
  const terms = []
  let current = ''
  let quote = null

  for (const char of query) {
    if (quote) {
      if (char === quote) { quote = null } else { current += char }
      continue
    }
    if (char === '"' || char === "'") { quote = char; continue }
    if (/\s/.test(char)) {
      if (current) { terms.push(current); current = '' }
      continue
    }
    current += char
  }
  if (current) terms.push(current)
  return terms
}

/** Rejoin terms, re-quoting any that contain whitespace. */
export function joinTerms(terms) {
  return terms
    .map((t) => (/\s/.test(t) ? `"${t}"` : t))
    .join(' ')
}

/**
 * The chips to display for a filter set.
 *
 * Each carries enough to remove itself: which filter it came from, and for a
 * search term, its position in the query.
 */
export function activeChips(filters) {
  const chips = []

  for (const { key, label } of CHIP_FIELDS) {
    const raw = filters[key]
    if (!raw) continue
    const negated = typeof raw === 'string' && raw.startsWith('!')
    const value = negated ? raw.slice(1) : raw
    if (!value) continue
    chips.push({
      id: `field:${key}`,
      source: 'field',
      field: key,
      label,
      value,
      negated,
    })
  }

  splitTerms(filters.search).forEach((term, index) => {
    const negated = term.startsWith('!') || term.startsWith('-')
    chips.push({
      id: `search:${index}`,
      source: 'search',
      index,
      // A term typed as `src:10.0.0.1` keeps its prefix as the label, so the
      // chip reads the way it was typed.
      label: null,
      value: negated ? term.slice(1) : term,
      negated,
    })
  })

  return chips
}

/** Remove one chip, returning the filters without it. */
export function removeChip(filters, chip) {
  if (chip.source === 'field') {
    return { ...filters, [chip.field]: null }
  }
  const terms = splitTerms(filters.search)
  terms.splice(chip.index, 1)
  return { ...filters, search: terms.length ? joinTerms(terms) : null }
}

/** Remove every chip. Filters not shown as chips — time range, log type — stay. */
export function clearChips(filters) {
  const next = { ...filters, search: null }
  for (const { key } of CHIP_FIELDS) next[key] = null
  return next
}

/** Text for a chip, used by the UI and by tests as the readable summary. */
export function chipText(chip) {
  const body = chip.label ? `${chip.label}: ${chip.value}` : chip.value
  return chip.negated ? `not ${body}` : body
}

export { FIELD_LABELS }
