import type { ModelOptionsResult } from '@hermes/shared'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { modelOptionsQueryKey, type ModelOptionsRequest, requestModelOptions } from '@/lib/model-options'

interface LocalServerScan extends Pick<ModelOptionsRequest, 'gateway' | 'profile' | 'request' | 'sessionId'> {
  enabled: boolean
  ownerConnectionId?: null | string
}

/** An opened model picker scans this machine for local model servers (Ollama,
 *  LM Studio, vLLM). With nothing cached the picker's own catalog query asks
 *  for the scan (`detectLocal`). The composer pill subscribes to the same key
 *  WITHOUT scanning, though, so when it already cached the catalog the picker
 *  would just reuse it: this one-shot read on open scans and writes the answer,
 *  `Detected:` rows included, into that shared query. */
export function useLocalServerScan({
  enabled,
  gateway,
  ownerConnectionId,
  profile,
  request,
  sessionId
}: LocalServerScan): void {
  const queryClient = useQueryClient()
  // Callers may pass a fresh dispatcher every render; one scan per open, not per render.
  const route = useRef({ gateway, request })
  route.current = { gateway, request }

  useEffect(() => {
    if (!enabled) {
      return
    }

    const queryKey = modelOptionsQueryKey(profile, sessionId, ownerConnectionId)

    if (queryClient.getQueryData(queryKey) === undefined) {
      return
    }

    let live = true

    requestModelOptions({ detectLocal: true, ...route.current, profile, sessionId })
      .then(next => {
        if (live) {
          queryClient.setQueryData<ModelOptionsResult>(queryKey, next)
        }
      })
      // A failed scan leaves the catalog the picker already shows.
      .catch(() => undefined)

    return () => {
      live = false
    }
  }, [enabled, ownerConnectionId, profile, queryClient, sessionId])
}
