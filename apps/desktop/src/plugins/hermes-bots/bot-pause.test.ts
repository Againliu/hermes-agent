import { describe, expect, it } from 'vitest'

import { botForSession } from './bot-pause'
import type { RosterRow } from './types'

const roster: RosterRow[] = [
  { name: 'default', canonical_session: { id: 'chat-local' } },
  { name: 'default', connectionId: 'cloud', canonical_session: { id: 'chat-cloud' } },
  { name: 'scribe', canonical_session: { id: 'chat-scribe', resolved_id: 'chat-scribe-tip' } }
]

describe('botForSession', () => {
  it('two same-named bots on different connections never answer for each other', () => {
    expect(botForSession(roster, { owner: { connectionId: 'cloud', profile: 'default' }, storedSessionId: 'x' }, 'local'))
      .toBe(roster[1])
    expect(botForSession(roster, { owner: { connectionId: null, profile: 'default' }, storedSessionId: 'x' }, 'local'))
      .toBe(roster[0])
    // An owner on a connection this roster has no row for is nobody's.
    expect(botForSession(roster, { owner: { connectionId: 'laptop', profile: 'default' }, storedSessionId: 'x' }, 'local'))
      .toBeUndefined()
  })

  it("falls back to the bot whose canonical Bot Chat the session is (durable id or lineage tip)", () => {
    expect(botForSession(roster, { owner: null, storedSessionId: 'chat-scribe-tip' }, 'local')).toBe(roster[2])
    expect(botForSession(roster, { owner: null, storedSessionId: 'unrelated' }, 'local')).toBeUndefined()
  })
})
