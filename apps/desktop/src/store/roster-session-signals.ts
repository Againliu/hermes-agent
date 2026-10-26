/**
 * ROSTER SESSION SIGNALS — the read-only view a roster (Bot Mode's Bots pane)
 * needs to say what each agent is doing without a new RPC:
 *
 * - `dots`: the one resolved status per stored session ($sessionDotStateById),
 *   so a bot row keyed on its canonical chat agrees with every other surface.
 * - `prompts`: the blocking prompt itself, per stored session (every lineage
 *   alias), so a row can QUOTE what it is waiting on — the clarify question or
 *   the command an approval would run. Prompts are parked under the runtime
 *   id that raised them; the runtime→stored bridge is the same one the dot
 *   state uses.
 * - `routines`: owner scopes with a cron run the scheduler still owns
 *   (`scheduler_owned`, not the 300 s `is_active` window, which stays lit for a
 *   finished run and goes dark during a long tool call).
 *
 * Every input is reference-stable across stream deltas, so this recomputes on
 * prompt/status edges, not per token.
 */

import { computed } from 'nanostores'

import { stableArray, stableRecord } from '@/lib/stable-array'

import { $clarifyRequests } from './clarify'
import { $approvalRequests } from './prompts'
import { $cronSessions, $sessions, lineageAliases } from './session'
import { $sessionDotStateById, type SessionDotState } from './session-dot-state'
import { $sessionStates } from './session-states'

export interface SessionPromptQuote {
  kind: 'approval' | 'clarify'
  /** The question asked, or the command the approval would run. */
  text: string
}

export interface RosterSessionSignals {
  dots: Readonly<Record<string, SessionDotState>>
  prompts: Readonly<Record<string, SessionPromptQuote>>
  /** `${connection_id ?? ''}::${profile}` for every cron run still in flight. */
  routines: readonly string[]
}

const quoteCache = new Map<string, SessionPromptQuote>()

// Interned so an unchanged prompt keeps its reference and stableRecord holds.
function internQuote(kind: SessionPromptQuote['kind'], text: string): SessionPromptQuote {
  const key = `${kind}\u0000${text}`
  let quote = quoteCache.get(key)

  if (!quote) {
    quote = Object.freeze({ kind, text })
    quoteCache.set(key, quote)
  }

  return quote
}

export const routineScopeKey = (connectionId: null | string | undefined, profile: null | string | undefined) =>
  `${String(connectionId || '').trim()}::${String(profile || 'default').trim() || 'default'}`

let prevPrompts: Readonly<Record<string, SessionPromptQuote>> = {}
let prevRoutines: readonly string[] = []
let prevSignals: RosterSessionSignals | null = null

export const $rosterSessionSignals = computed(
  [$sessionDotStateById, $clarifyRequests, $approvalRequests, $sessionStates, $sessions, $cronSessions],
  (dots, clarifies, approvals, states, sessions, cron): RosterSessionSignals => {
    const prompts: Record<string, SessionPromptQuote> = {}

    const claim = (runtimeId: string, quote: SessionPromptQuote) => {
      if (!runtimeId) {
        return
      }

      for (const alias of lineageAliases(states[runtimeId]?.storedSessionId ?? runtimeId, sessions)) {
        prompts[alias] = quote
      }
    }

    for (const [runtimeId, request] of Object.entries(clarifies)) {
      const text = request.questions.map(q => q.question).find(Boolean)

      if (text) {
        claim(runtimeId, internQuote('clarify', text))
      }
    }

    // Approval wins over a clarify on the same session: it names the command
    // that will run the moment the user says yes.
    for (const [runtimeId, request] of Object.entries(approvals)) {
      const text = String(request.command || request.description || '').trim()

      if (text) {
        claim(runtimeId, internQuote('approval', text))
      }
    }

    const routines = cron
      .filter(row => row.scheduler_owned === true)
      .map(row => routineScopeKey(row.connection_id, row.profile))

    const nextPrompts = (prevPrompts = stableRecord(prevPrompts, prompts))
    const nextRoutines = (prevRoutines = stableArray(prevRoutines, [...new Set(routines)].sort()))

    if (
      prevSignals &&
      prevSignals.dots === dots &&
      prevSignals.prompts === nextPrompts &&
      prevSignals.routines === nextRoutines
    ) {
      return prevSignals
    }

    return (prevSignals = Object.freeze({ dots, prompts: nextPrompts, routines: nextRoutines }))
  }
)
