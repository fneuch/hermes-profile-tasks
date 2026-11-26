// Renders the status-bar item of desktop/plugin.js offline with SDK and React stubs; prints {text, tip, warn, icon}.
// Usage: node tests/status_render.mjs <payload.json>; payload is {"body": <GET /state>} or {"error": "<message>"}.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const payload = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const plugin = join(dirname(fileURLToPath(import.meta.url)), '..', 'desktop', 'plugin.js')

const queries = []
globalThis.__usageGovernorStub = {
  h: (type, props, ...children) => ({ type, props: props || {}, children: children.flat(Infinity) }),
  useQuery: q => {
    queries.push({ queryKey: q.queryKey, refetchInterval: q.refetchInterval })
    return payload.error
      ? { data: undefined, isError: true, isPending: false, error: new Error(payload.error) }
      : { data: payload.body, isError: false, isPending: false }
  }
}
const stub = name => props => ({ type: name, props, children: [props.children].flat(Infinity) })
const src = readFileSync(plugin, 'utf8')
  .replace(
    /import \{[^}]*\} from '@hermes\/plugin-sdk'/,
    [
      `const Codicon = (${stub.toString()})('Codicon'), Tip = (${stub.toString()})('Tip')`,
      "const STATUSBAR_AREAS = { right: 'right' }",
      'const useQuery = globalThis.__usageGovernorStub.useQuery'
    ].join('\n')
  )
  .replace(/import \{[^}]*\} from 'react'/, 'const h = globalThis.__usageGovernorStub.h')

const dir = mkdtempSync(join(tmpdir(), 'usage-governor-bar-'))
let mod
try {
  writeFileSync(join(dir, 'plugin.mjs'), src)
  mod = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)
} finally {
  rmSync(dir, { recursive: true, force: true })
}

// Expands function components until only host elements and strings remain.
function expand(node) {
  if (node === null || node === undefined || node === false) return null
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (typeof node.type === 'function') return expand(node.type({ ...node.props, children: node.children }))
  return { type: node.type, props: node.props, children: node.children.map(expand).filter(c => c !== null) }
}
const textOf = n => (typeof n === 'string' ? n : n.children.map(textOf).join(''))
const findAll = (n, pred) => (typeof n === 'string' ? [] : [...(pred(n) ? [n] : []), ...n.children.flatMap(c => findAll(c, pred))])

const registered = []
const ctx = { onDispose: () => {}, rest: () => Promise.resolve(null), register: c => registered.push(c) }
mod.default.register(ctx)
const item = registered[0]
const tree = expand(item.render())
const tip = tree.type === 'Tip' ? textOf(expand(tree.props.label)) : null
const [span] = findAll(tree, n => n.props['data-usage-governor'])
process.stdout.write(JSON.stringify({
  register: { id: item.id, area: item.area, order: item.order },
  query: queries[0],
  text: textOf(span),
  warn: span.props['data-usage-governor-warn'],
  icon: findAll(span, n => n.type === 'Codicon').map(n => n.props.name),
  tip
}))
