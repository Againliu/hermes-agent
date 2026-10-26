import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { useI18n } from '@/i18n'
import type { Translations } from '@/i18n/types'
import type { GatewayHealthPillCopy } from '@/lib/gateway-health-pill'
import {
  $gatewayUpdateDrain,
  bindGatewayUpdateDrain,
  refreshGatewayUpdateDrain,
  UPDATE_WAIT_MS,
  updateDrainTurns
} from '@/store/gateway-update-drain'

/** Re-read while draining, so "finishing N turns" counts down as turns end. */
const DRAIN_POLL_MS = 5_000

type Requester = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

/** "Updating to v0.23.1 · finishing 1 turn" while the owner drains for an update, else null.
 *  Owns the watch: asks on every open (a new owner answers null and clears it), re-reads while
 *  draining, and forgets a cause whose gateway stays away past UPDATE_WAIT_MS. */
export function useGatewayUpdateLabel(gatewayState: string, request: Requester): null | string {
  const { t } = useI18n()
  const drain = useStore($gatewayUpdateDrain)
  const open = gatewayState === 'open'
  const draining = drain !== null

  useEffect(() => bindGatewayUpdateDrain(request), [request])

  useEffect(() => {
    if (!open) {
      if (!draining) {
        return
      }

      const timer = window.setTimeout(() => $gatewayUpdateDrain.set(null), UPDATE_WAIT_MS)

      return () => window.clearTimeout(timer)
    }

    void refreshGatewayUpdateDrain()

    if (!draining) {
      return
    }

    const timer = window.setInterval(() => void refreshGatewayUpdateDrain(), DRAIN_POLL_MS)

    return () => window.clearInterval(timer)
  }, [draining, open])

  // Once the drained owner is gone its turns are done; only the target is still true.
  return drain ? t.gatewayUpdate.updating(drain.target ?? null, open ? updateDrainTurns(drain) : 0) : null
}

/** The status-bar gateway chip's copy; every connection detail names the update while one drains. */
export function useGatewayHealthCopy(
  copy: Translations['shell']['statusbar'],
  gatewayState: string,
  request: Requester
): GatewayHealthPillCopy {
  const updating = useGatewayUpdateLabel(gatewayState, request)

  return {
    backend: copy.backend,
    checking: updating ?? copy.gatewayChecking,
    connecting: updating ?? copy.gatewayConnecting,
    messagingDegraded: copy.messagingDegraded,
    messagingStopped: copy.messagingStopped,
    needsSetup: updating ?? copy.gatewayNeedsSetup,
    offline: updating ?? copy.gatewayOffline,
    ready: updating ?? copy.gatewayReady,
    restarting: copy.gatewayRestarting,
    unavailable: updating ?? copy.gatewayUnavailable
  }
}
