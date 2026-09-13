/**
 * Does the filter bar render at all?
 *
 * It once did not. A rewrite left an effect calling setters for inputs that had
 * been removed; the bundle built cleanly — Vite does not check that a local
 * identifier exists — and the app threw on first render, leaving a blank page.
 * Every other test in the suite passed, because none of them mounted this
 * component.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../api', () => ({
  fetchServices: vi.fn(() => Promise.resolve({ services: ['https', 'dns'] })),
  fetchProtocols: vi.fn(() => Promise.resolve({ protocols: ['tcp', 'udp'] })),
  fetchInterfaces: vi.fn(() => Promise.resolve({ interfaces: [] })),
}))

const { default: FilterBar } = await import('../components/FilterBar')

// The panel button, distinct from the mobile "Filters & search" toggle that
// expands the whole bar.
const PANEL_BUTTON = /^Filters(\s\(\d+\))?$/

const BASE = {
  time_range: '24h',
  log_type: null,
  rule_action: null,
  direction: null,
  ip: null, src_ip: null, dst_ip: null,
  rule_name: null, search: null, service: null,
  country: null, asn: null, dst_port: null, src_port: null,
  protocol: null, interface: null,
  time_from: null, time_to: null,
  page: 1, per_page: 50, sort: 'timestamp', order: 'desc',
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('mounting', () => {
  it('renders without throwing', () => {
    expect(() =>
      render(<FilterBar filters={BASE} onChange={vi.fn()} />)
    ).not.toThrow()
  })

  it('shows the search box and the filter button', () => {
    render(<FilterBar filters={BASE} onChange={vi.fn()} />)
    expect(screen.getByPlaceholderText(/Filter —/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: PANEL_BUTTON })).toBeInTheDocument()
  })

  it('says so when nothing is filtering', () => {
    render(<FilterBar filters={BASE} onChange={vi.fn()} />)
    expect(screen.getByText('No filters')).toBeInTheDocument()
  })

  it('renders with every filter set', () => {
    const filters = {
      ...BASE,
      src_ip: '10.0.0.1', dst_ip: '1.1.1.1', ip: '8.8.8.8',
      src_port: '1234', dst_port: '!443', rule_name: 'LAN',
      country: 'DE', asn: 'Cloudflare', service: 'https',
      interface: 'eth0', protocol: 'tcp', rule_action: 'block',
      direction: 'inbound', search: 'nas "allow new"',
    }
    expect(() =>
      render(<FilterBar filters={filters} onChange={vi.fn()} />)
    ).not.toThrow()
  })
})

describe('chips', () => {
  it('shows a panel filter with its field', () => {
    render(<FilterBar filters={{ ...BASE, src_ip: '10.0.0.1' }} onChange={vi.fn()} />)
    expect(screen.getByText('Src IP:')).toBeInTheDocument()
    expect(screen.getByText('10.0.0.1')).toBeInTheDocument()
  })

  it('shows a search term without one', () => {
    render(<FilterBar filters={{ ...BASE, search: '10.0.0.1' }} onChange={vi.fn()} />)
    expect(screen.queryByText('Src IP:')).not.toBeInTheDocument()
    expect(screen.getByText('10.0.0.1')).toBeInTheDocument()
  })

  it('removing a chip clears just that filter', () => {
    const onChange = vi.fn()
    render(
      <FilterBar
        filters={{ ...BASE, src_ip: '10.0.0.1', dst_ip: '1.1.1.1' }}
        onChange={onChange}
      />
    )
    fireEvent.click(screen.getByLabelText('Remove filter Src IP: 10.0.0.1'))
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ src_ip: null, dst_ip: '1.1.1.1' })
    )
  })

  it('removing one search term keeps the others', () => {
    const onChange = vi.fn()
    render(<FilterBar filters={{ ...BASE, search: 'nas 443' }} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Remove filter nas'))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ search: '443' }))
  })
})

describe('search box', () => {
  const box = () => screen.getByPlaceholderText(/Filter —/)

  it('filters as you type, before the term is kept', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const onChange = vi.fn()
    render(<FilterBar filters={BASE} onChange={onChange} />)

    fireEvent.change(box(), { target: { value: '443' } })
    await vi.advanceTimersByTimeAsync(250)

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ search: '443' }))
    vi.useRealTimers()
  })

  it('Enter keeps the term and clears the box', () => {
    const onChange = vi.fn()
    render(<FilterBar filters={BASE} onChange={onChange} />)

    fireEvent.change(box(), { target: { value: '443' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ search: '443' }))
    expect(box()).toHaveValue('')
  })

  it('the next term is added, not substituted', () => {
    const onChange = vi.fn()
    render(<FilterBar filters={{ ...BASE, search: '443' }} onChange={onChange} />)

    fireEvent.change(box(), { target: { value: 'tcp' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ search: '443 tcp' }))
  })

  it('Escape drops the typed term and keeps the kept ones', () => {
    const onChange = vi.fn()
    render(<FilterBar filters={{ ...BASE, search: '443' }} onChange={onChange} />)

    fireEvent.change(box(), { target: { value: 'tcp' } })
    fireEvent.keyDown(box(), { key: 'Escape' })

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ search: '443' }))
    expect(box()).toHaveValue('')
  })

  it('a term set from outside becomes a chip, leaving the box empty', () => {
    render(<FilterBar filters={{ ...BASE, search: '10.0.0.1' }} onChange={vi.fn()} />)
    expect(box()).toHaveValue('')
    expect(screen.getByText('10.0.0.1')).toBeInTheDocument()
  })
})

describe('filter panel', () => {
  it('opens and closes', async () => {
    render(<FilterBar filters={BASE} onChange={vi.fn()} />)
    const button = screen.getByRole('button', { name: PANEL_BUTTON })

    fireEvent.click(button)
    expect(await screen.findByText('Clear all')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Apply')).not.toBeInTheDocument())
  })

  it('applying writes the fields back', async () => {
    const onChange = vi.fn()
    render(<FilterBar filters={BASE} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: PANEL_BUTTON }))
    fireEvent.change(await screen.findByLabelText('Source IP'), {
      target: { value: '10.0.0.1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ src_ip: '10.0.0.1' })
    )
  })

  it('the not switch negates the value', async () => {
    const onChange = vi.fn()
    render(<FilterBar filters={BASE} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: PANEL_BUTTON }))
    fireEvent.change(await screen.findByLabelText('Dest port'), {
      target: { value: '443' },
    })
    fireEvent.click(screen.getByLabelText('Exclude Dest port'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ dst_port: '!443' })
    )
  })
})


describe('typing a term does not accumulate its own intermediate states', () => {
  /**
   * Typing 10.10.10.0/24 produced five chips — 10.10.10., 10.10.10.1,
   * 10.10.10.0, 10.10.10.0/ and finally 10.10.10.0/24 — because each debounce
   * tick wrote the draft into filters.search, and the next render read that
   * back as a term that had already been kept.
   */
  function TypeHarness({ onFilters }) {
    const [filters, setFilters] = React.useState(BASE)
    React.useEffect(() => { onFilters(filters) }, [filters, onFilters])
    return <FilterBar filters={filters} onChange={setFilters} />
  }

  it('leaves one term after typing an address in steps', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let latest = BASE
    render(<TypeHarness onFilters={(f) => { latest = f }} />)
    const box = screen.getByPlaceholderText(/Filter —/)

    for (const step of ['10.10.10.', '10.10.10.0', '10.10.10.0/', '10.10.10.0/24']) {
      fireEvent.change(box, { target: { value: step } })
      await vi.advanceTimersByTimeAsync(200)
    }

    expect(latest.search).toBe('10.10.10.0/24')
    vi.useRealTimers()
  })

  it('shows no chip until the term is kept', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<TypeHarness onFilters={() => {}} />)
    const box = screen.getByPlaceholderText(/Filter —/)

    fireEvent.change(box, { target: { value: '10.10.10.0/24' } })
    await vi.advanceTimersByTimeAsync(200)

    // The draft is visible in the box; a chip would be a second copy of it.
    expect(screen.queryByLabelText(/Remove filter/)).not.toBeInTheDocument()
    vi.useRealTimers()
  })

  it('Enter turns the draft into exactly one chip', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<TypeHarness onFilters={() => {}} />)
    const box = screen.getByPlaceholderText(/Filter —/)

    fireEvent.change(box, { target: { value: '10.10.10.0/24' } })
    await vi.advanceTimersByTimeAsync(200)
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(screen.getAllByLabelText(/Remove filter/)).toHaveLength(1)
    vi.useRealTimers()
  })

  it('a second term is added beside the first, not merged into it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let latest = BASE
    render(<TypeHarness onFilters={(f) => { latest = f }} />)
    const box = screen.getByPlaceholderText(/Filter —/)

    fireEvent.change(box, { target: { value: '10.10.10.0/24' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    for (const step of ['4', '44', '443']) {
      fireEvent.change(box, { target: { value: step } })
      await vi.advanceTimersByTimeAsync(200)
    }
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(latest.search).toBe('10.10.10.0/24 443')
    vi.useRealTimers()
  })
})
