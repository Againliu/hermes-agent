import { atom } from 'nanostores'

import { $sessionControlBySession } from '@/store/session-control'

/** `runtime.describe.drain`: an owner finishing its turns before `hermes update` replaces it. */
export interface GatewayUpdateDrain {
  reason: 'update'
  target?: null | string
  waiting_for?: Record<string, number>
}

type Requester = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

/** The update the gateway is draining for; null when it serves normally (or names no update). */
export const $gatewayUpdateDrain = atom<GatewayUpdateDrain | null>(null)

/** How long a known update may keep the gateway away before the chip falls back to "offline". */
export const UPDATE_WAIT_MS = 15 * 60_000

let requester: null | Requester = null
let inFlight = false

/** Ask the owner why it is refusing work. A closed socket keeps the last cause: the owner that
 *  drained is gone, but the update it drained for is not finished until a new one answers. */
export async function refreshGatewayUpdateDrain(): Promise<void> {
  if (!requester || inFlight) {
    return
  }

  inFlight = true

  try {
    const described = await requester<{ drain?: GatewayUpdateDrain | null }>('runtime.describe')
    $gatewayUpdateDrain.set(described?.drain?.reason === 'update' ? described.drain : null)
  } catch {
    // The owner went away mid-question: the reconnect decides what is true next.
  } finally {
    inFlight = false
  }
}

/** The drain's live work units ("finishing N turns"); every kind counts as a turn to finish. */
export function updateDrainTurns(drain: GatewayUpdateDrain): number {
  return Object.values(drain.waiting_for ?? {}).reduce((sum, n) => sum + (Number.isInteger(n) && n > 0 ? n : 0), 0)
}

/** Wire the describe requester and ask whenever a session control is refused as draining. */
export function bindGatewayUpdateDrain(request: Requester): () => void {
  requester = request

  const stop = $sessionControlBySession.listen(entries => {
    if (Object.values(entries).some(entry => entry?.error === 'runtime_draining')) {
      void refreshGatewayUpdateDrain()
    }
  })

  return () => {
    stop()

    if (requester === request) {
      requester = null
    }
  }
}
