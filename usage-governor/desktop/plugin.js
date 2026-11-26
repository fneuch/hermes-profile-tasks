// usage-governor status-bar item: mode and % used from GET /state (dashboard/plugin_api.py), read every 30 s; createElement, no JSX.

import { Codicon, STATUSBAR_AREAS, Tip, useQuery } from '@hermes/plugin-sdk'
import { createElement as h } from 'react'

const ID = 'usage-governor'
const POLL_MS = 30 * 1000
const QUERY_KEY = ['usage-governor', 'state']

let api = null

function pct(value) {
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)}%` : '--'
}

// Local day and time of an epoch in seconds, like the state's reset times (`Wed 12:30`).
function when(epoch) {
  const d = new Date(epoch * 1000)
  const day = d.toLocaleDateString('en-US', { weekday: 'short' })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${day} ${time}`
}

// The item's text, warning flag and tooltip lines from a GET /state body (null when the backend did not answer).
function view(body, error) {
  const state = body && body.state && typeof body.state === 'object' ? body.state : null
  const health = (body && body.health) || { ok: false, reasons: [] }
  // Reasons from the backend's health check get a `Health:` prefix; the item's own do not.
  const reasons = (Array.isArray(health.reasons) ? health.reasons : []).map(r => `Health: ${r}`)
  if (error) reasons.push(`backend unreachable: ${error}`)
  if (!state && !reasons.length) reasons.push('no state yet')
  const mode = (state && state.mode) || '--'
  const text = `${mode} · 5h ${pct(state && state.five_hour)} · wk ${pct(state && state.seven_day)}`
  const resets = (state && state.resets) || {}
  const tip = [`Usage governor: ${mode}`]
  tip.push(`5-hour window: ${pct(state && state.five_hour)}${resets.five_hour ? `, resets ${resets.five_hour}` : ''}`)
  tip.push(`Week: ${pct(state && state.seven_day)}${resets.seven_day ? `, resets ${resets.seven_day}` : ''}`)
  const ov = state && state.override
  if (ov && ov.active) tip.push(typeof ov.until === 'number' ? `Override active until ${when(ov.until)}` : 'Override active (end time unknown)')
  if (state && state.reason) tip.push(`Reading: ${state.reason}`)
  if (state && state.settings_error) tip.push(`Settings: ${state.settings_error}`)
  // A missing reading or a settings error leaves the % frozen: warn so the number is not taken as current.
  const warn = !state || !health.ok || !!error || state.reading === 'missing' || !!state.settings_error
  if (warn) tip.push(...reasons)
  return { text, warn, tip }
}

function UsageGovernorStatus() {
  const q = useQuery({ queryKey: QUERY_KEY, enabled: !!api, queryFn: () => api.rest('/state'), refetchInterval: POLL_MS })
  if (q.isPending) return null
  const v = view(q.data, q.isError ? String(q.error?.message || q.error) : null)
  return h(
    Tip,
    { label: h('div', { style: { whiteSpace: 'pre-line' } }, v.tip.join('\n')), side: 'top' },
    h(
      'span',
      {
        'data-usage-governor': true,
        'data-usage-governor-warn': v.warn,
        'aria-label': `Usage governor: ${v.text}`,
        style: {
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          padding: '0 6px',
          height: '100%',
          color: v.warn ? 'var(--ui-yellow)' : 'inherit'
        }
      },
      v.warn ? h(Codicon, { name: 'warning', size: 14 }) : null,
      v.text
    )
  )
}

export default {
  id: ID,
  name: 'Usage governor',
  register(ctx) {
    api = ctx
    ctx.onDispose(() => {
      api = null
    })
    ctx.register({ id: 'status', area: STATUSBAR_AREAS.right, order: 100, render: () => h(UsageGovernorStatus) })
  }
}
