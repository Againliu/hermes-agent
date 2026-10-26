import { afterEach, describe, expect, it } from 'vitest'

import { $gatewayUpdateDrain, bindGatewayUpdateDrain, refreshGatewayUpdateDrain, updateDrainTurns } from './gateway-update-drain'
import { $sessionControlBySession } from './session-control'

const drain = { reason: 'update' as const, target: 'v0.99.0', waiting_for: { chat: 1, cron: 2 } }

describe('gateway update drain', () => {
  let unbind = () => {}

  afterEach(() => {
    unbind()
    $gatewayUpdateDrain.set(null)
    $sessionControlBySession.set({})
  })

  it('asks the owner why when a session control is refused as draining, and counts what it finishes', async () => {
    const asked: string[] = []
    unbind = bindGatewayUpdateDrain(async <T,>(method: string) => {
      asked.push(method)

      return { drain } as T
    })
    $sessionControlBySession.set({ s1: { error: 'runtime_draining' } as never })
    await Promise.resolve()
    await Promise.resolve()
    expect(asked).toEqual(['runtime.describe'])
    expect($gatewayUpdateDrain.get()).toEqual(drain)
    expect(updateDrainTurns(drain)).toBe(3)
  })

  it('keeps the cause while the drained owner is gone and clears it when a new owner answers', async () => {
    let answer: () => Promise<unknown> = async () => ({ drain })
    unbind = bindGatewayUpdateDrain(<T,>() => answer() as Promise<T>)
    await refreshGatewayUpdateDrain()
    answer = async () => Promise.reject(new Error('Hermes gateway unavailable'))
    await refreshGatewayUpdateDrain()
    expect($gatewayUpdateDrain.get()).toEqual(drain)
    answer = async () => ({ drain: null })
    await refreshGatewayUpdateDrain()
    expect($gatewayUpdateDrain.get()).toBeNull()
  })
})
