import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type ActiveWork, NO_ACTIVE_WORK } from './quit-guard'
import { mergeUpdateReadiness, UpdateWhenIdle } from './update-readiness'
import { applyUpdatesWhenIdle, busyApplyResult, createUpdateReadinessGate } from './update-readiness-gate'

interface FakeBackend {
  baseUrl: string
  idle: unknown
  fence: 'grant' | 'refuse' | 'missing'
  calls: string[]
}

function backend(idle: unknown, fence: FakeBackend['fence'] = 'grant', baseUrl = 'http://127.0.0.1:1'): FakeBackend {
  return { baseUrl, idle, fence, calls: [] }
}

function gateFor(backends: FakeBackend[], work: () => ActiveWork = () => NO_ACTIVE_WORK) {
  return createUpdateReadinessGate<FakeBackend>({
    activeWork: work,
    localBackends: async () => backends,
    log: () => undefined,
    request: async (b, path, opts) => {
      const body = opts.body as { action?: string; token?: string } | undefined
      b.calls.push(body?.action ? `${path}:${body.action}${body.token ? `:${body.token}` : ''}` : path)

      if (path === '/api/health/idle') {
        if (b.idle instanceof Error) {
          throw b.idle
        }

        return b.idle
      }

      if (b.fence === 'missing') {
        throw Object.assign(new Error('404: Not Found'), { statusCode: 404 })
      }

      if (body?.action === 'prepare') {
        return b.fence === 'grant' ? { ok: true, idle: true, token: `tok-${b.baseUrl}` } : { ok: false, idle: false }
      }

      return { ok: true }
    }
  })
}

const SESSION_TURN = { ok: true, idle: false, reason: 'turn_in_flight', detail: 'session:abc' }
const IDLE = { ok: true, idle: true, reason: null }

test('a running chat refuses the hand-off, names it, and takes no admission fence', async () => {
  const b = backend(SESSION_TURN)
  const freeze = await gateFor([b], () => ({ count: 1, titles: ['Refactor auth'] })).freeze()

  assert.equal(freeze.ok, false)
  assert.deepEqual(freeze.reasons, [{ kind: 'chats', count: 1, titles: ['Refactor auth'] }])
  assert.deepEqual(b.calls, ['/api/health/idle'])
})

test('a backend that cannot prove idle counts as busy (never assumed idle)', async () => {
  for (const idle of [{ ok: true, idle: null, reason: 'turn_probe_unavailable' }, new Error('ECONNRESET')]) {
    const freeze = await gateFor([backend(idle)]).freeze()

    assert.deepEqual(freeze.reasons, [{ kind: 'unknown' }])
  }
})

test('cron runs and delegations are named from the backend ledger', () => {
  const readiness = mergeUpdateReadiness(NO_ACTIVE_WORK, [
    { idle: false, reason: 'turn_in_flight', detail: 'cron:nightly-digest' },
    { idle: false, reason: 'turn_in_flight', detail: 'delegation' },
    { idle: false, reason: 'awaiting_human_input' }
  ])

  assert.equal(readiness.ready, false)
  assert.deepEqual(readiness.reasons, [
    { kind: 'cron', jobs: ['nightly-digest'] },
    { kind: 'delegation' },
    { kind: 'input' }
  ])
})

test('an idle machine freezes every backend, and release reopens exactly those fences', async () => {
  const a = backend(IDLE, 'grant', 'http://a')
  const old = backend(IDLE, 'missing', 'http://old')
  const freeze = await gateFor([a, old]).freeze()

  assert.equal(freeze.ok, true)
  assert.ok(a.calls.includes('/api/health/retirement:prepare'))

  await freeze.release()

  assert.ok(a.calls.includes('/api/health/retirement:cancel:tok-http://a'))
  assert.ok(!old.calls.some(call => call.includes('cancel')))
})

test('work that starts between the idle read and the fence releases fences already taken', async () => {
  const first = backend(IDLE, 'grant', 'http://first')
  const racing = backend(IDLE, 'refuse', 'http://racing')
  const freeze = await gateFor([first, racing]).freeze()

  assert.equal(freeze.ok, false)
  assert.ok(first.calls.includes('/api/health/retirement:cancel:tok-http://first'))
})

function clock() {
  let now = 0

  return { now: () => now, sleep: async (ms: number) => void (now += ms) }
}

test('update-when-idle fires only after the turn ends and readiness holds twice', async () => {
  const reads = [false, false, true, false, true, true] // busy, busy, a 1-poll gap, busy, idle, idle
  const fired: number[] = []
  const published: unknown[] = []
  const time = clock()

  const result = await applyUpdatesWhenIdle({
    scheduler: new UpdateWhenIdle(),
    read: async () =>
      reads.shift()
        ? { ready: true, reasons: [] }
        : { ready: false, reasons: [{ kind: 'chats', count: 1, titles: [] }] },
    applyGated: async () => {
      fired.push(time.now())

      return { ok: true, handedOff: true }
    },
    emitWaiting: reasons => published.push(reasons),
    log: () => undefined,
    pollMs: 1000,
    maxWaitMs: 60_000,
    ...time
  })

  assert.deepEqual(result, { ok: true, handedOff: true })
  assert.equal(fired.length, 1)
  assert.equal(reads.length, 0)
  assert.ok(published.length > 0)
})

test('a busy answer from the final gate keeps waiting instead of handing off', async () => {
  const gate = [busyApplyResult([{ kind: 'cron', jobs: ['j1'] }]), { ok: true, handedOff: true }]
  const time = clock()

  const result = await applyUpdatesWhenIdle({
    scheduler: new UpdateWhenIdle(),
    read: async () => ({ ready: true, reasons: [] }),
    applyGated: async () => gate.shift()!,
    emitWaiting: () => undefined,
    log: () => undefined,
    pollMs: 1000,
    maxWaitMs: 60_000,
    ...time
  })

  assert.deepEqual(result, { ok: true, handedOff: true })
  assert.equal(gate.length, 0)
})

test('cancel stops the wait without ever handing off', async () => {
  const scheduler = new UpdateWhenIdle<{ ok: boolean }>()
  let fired = false

  const pending = applyUpdatesWhenIdle({
    scheduler,
    read: async () => ({ ready: false, reasons: [{ kind: 'delegation' }] }),
    applyGated: async () => {
      fired = true

      return { ok: true }
    },
    emitWaiting: () => undefined,
    log: () => undefined,
    pollMs: 5,
    maxWaitMs: 60_000
  })

  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(scheduler.cancel(), true)
  assert.deepEqual(await pending, { ok: false, error: 'cancelled' })
  assert.equal(fired, false)
})

test('the wait is bounded: still busy at the deadline gives up and names what held it', async () => {
  const time = clock()
  let fired = false

  const result = await applyUpdatesWhenIdle({
    scheduler: new UpdateWhenIdle(),
    read: async () => ({ ready: false, reasons: [{ kind: 'cron', jobs: ['backup'] }] }),
    applyGated: async () => {
      fired = true

      return { ok: true }
    },
    emitWaiting: () => undefined,
    log: () => undefined,
    pollMs: 1000,
    maxWaitMs: 10_000,
    ...time
  })

  assert.equal(fired, false)
  assert.equal((result as { error?: string }).error, 'idle-timeout')
  assert.deepEqual((result as { waitingFor?: unknown }).waitingFor, [{ kind: 'cron', jobs: ['backup'] }])
})
