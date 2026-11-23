// profile-tasks: a status-bar item for the background tasks of the conversation open on screen.
// The item shows a speech bubble and the number of profile_task tasks started from that conversation; it is hidden
// when there are none. Clicking it opens the list: target profile, title and state (running, waiting for an
// approval, past the wait limit, answered, late answer, failed). Each entry opens the task's session in a tab, as
// the @session:<profile>/<id> chip does.
// The list comes from this package's backend (dashboard/plugin_api.py, read-only). It is read again when the open
// conversation changes, on the backend's plugin.profile-tasks.tasks.changed event (sent on every task save), and
// every 60 s as a safety net.
// The file loads uncompiled, so UI is built with createElement, not JSX.

import {
  Codicon,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  atom,
  host,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { createElement as h, useState } from 'react'

const ID = 'profile-tasks'
const POLL_MS = 60 * 1000
const QUERY_KEY = ['profile-tasks', 'tasks']

const STATES = {
  approval: { label: 'waiting for an approval', color: 'var(--ui-yellow)' },
  running: { label: 'running', color: 'var(--ui-blue, inherit)' },
  past_wait_limit: { label: 'past the wait limit', color: 'var(--ui-yellow)' },
  wait_lost: { label: 'wait lost', color: 'var(--ui-red)' },
  answered: { label: 'answered', color: 'var(--ui-green)' },
  late_answer: { label: 'late answer', color: 'var(--ui-green)' },
  failed: { label: 'failed', color: 'var(--ui-red)' }
}

const muted = { color: 'var(--ui-text-tertiary)' }
// Stands in for a host.state readout an older desktop does not have.
const $none = atom(null)

let api = null

function refresh() {
  void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
}

// The conversation on screen and the profile that owns it (its tasks live in that profile's plugin state).
function useOpenConversation() {
  const sessionId = useValue(host.state?.focusedStoredSessionId || $none) || ''
  const owner = useValue(host.state?.focusedSessionOwner || $none)
  const fallback = useValue(host.state?.focusedSessionProfile || $none) || ''
  return { sessionId, profile: (owner && owner.profile) || fallback || 'default' }
}

function useTasks(open) {
  return useQuery({
    queryKey: [...QUERY_KEY, open.profile, open.sessionId],
    enabled: !!open.sessionId && !!api,
    queryFn: () =>
      api.rest(
        `/tasks?session_id=${encodeURIComponent(open.sessionId)}&session_profile=${encodeURIComponent(open.profile)}`
      ),
    refetchInterval: POLL_MS
  })
}

function openTask(task, close) {
  close()
  void host.openSession(task.session_id, { intent: 'tab', profile: task.profile })
}

function TaskRow({ task, close }) {
  const state = STATES[task.state] || { label: task.state, color: 'inherit' }
  const canOpen = !!task.session_id
  return h(
    'button',
    {
      type: 'button',
      disabled: !canOpen,
      'data-profile-task': task.task_id,
      title: canOpen ? `Open the ${task.profile} session` : 'No session yet',
      onClick: () => canOpen && openTask(task, close),
      style: {
        display: 'block',
        width: '100%',
        textAlign: 'left',
        padding: '6px 8px',
        borderRadius: 4,
        border: 'none',
        background: 'transparent',
        font: 'inherit',
        color: 'inherit',
        cursor: canOpen ? 'pointer' : 'default'
      }
    },
    h(
      'div',
      { style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
      h('span', { style: { fontWeight: 600 } }, task.profile),
      h('span', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, task.title),
      h('span', { style: { color: state.color, whiteSpace: 'nowrap', fontSize: '0.9em' } }, state.label)
    ),
    task.state === 'approval' && task.approval_command
      ? h('div', { style: { ...muted, fontSize: '0.85em', marginTop: 2 } }, `approval: ${task.approval_command}`)
      : null
  )
}

function TaskMenu({ q, close }) {
  if (q.isError) return h('div', { style: { color: 'var(--ui-red)' } }, String(q.error?.message || q.error))
  const tasks = (q.data && q.data.tasks) || []
  return h(
    'div',
    null,
    h('div', { style: { ...muted, fontSize: '0.85em', marginBottom: 6 } }, 'Background tasks of this conversation'),
    tasks.map(task => h(TaskRow, { key: task.task_id, task, close }))
  )
}

function ProfileTasksStatus() {
  const open = useOpenConversation()
  const q = useTasks(open)
  const [menu, setMenu] = useState(false)
  const tasks = (q.data && q.data.tasks) || []
  if (!open.sessionId || tasks.length === 0) return null
  const waiting = tasks.filter(t => t.state === 'approval').length
  const running = tasks.filter(t => t.state === 'running' || t.state === 'past_wait_limit').length
  const tip = `${tasks.length} background task${tasks.length === 1 ? '' : 's'}: ${running} running` +
    (waiting ? `, ${waiting} waiting for an approval` : '')
  return h(
    Popover,
    { open: menu, onOpenChange: setMenu },
    h(
      PopoverTrigger,
      { asChild: true },
      h(
        'button',
        {
          type: 'button',
          title: tip,
          'aria-label': `Profile tasks ${tasks.length}`,
          'data-profile-tasks-trigger': true,
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
            color: waiting ? 'var(--ui-yellow)' : 'inherit',
            fontWeight: waiting ? 600 : 'inherit',
            padding: '0 6px',
            height: '100%',
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            font: 'inherit'
          }
        },
        h(Codicon, { name: 'comment', size: 14 }),
        String(tasks.length)
      )
    ),
    h(
      PopoverContent,
      { side: 'top', align: 'end', sideOffset: 8, style: { width: 'min(420px, calc(100vw - 24px))', padding: 10 } },
      menu ? h(TaskMenu, { q, close: () => setMenu(false) }) : null
    )
  )
}

export default {
  id: ID,
  name: 'Profile tasks',
  register(ctx) {
    api = ctx
    ctx.onDispose(() => {
      api = null
    })
    ctx.onEvent(`plugin.${ID}.tasks.changed`, refresh)
    ctx.register({ id: 'status', area: STATUSBAR_AREAS.right, order: 105, render: () => h(ProfileTasksStatus) })
  }
}
