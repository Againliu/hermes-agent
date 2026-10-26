import { describe, expect, it } from 'vitest'

import { applyDraftCapabilities, composeDraftSoul, mergeSoulSections } from './profile-draft'

const saved = [
  '# Scout',
  '',
  '## Role',
  '',
  'Old role.',
  '',
  '## Messaging other agents',
  '',
  'Keep me exactly.',
  ''
].join('\n')

describe('mergeSoulSections', () => {
  it('replaces an accepted section in place and keeps everything else byte-for-byte', () => {
    const out = mergeSoulSections(saved, [{ key: 'role', title: 'Role', text: 'New role.' }])

    expect(out).toBe(saved.replace('Old role.', 'New role.'))
  })

  it('appends a section the saved SOUL does not have', () => {
    const out = mergeSoulSections(saved, [{ key: 'voice', title: 'Voice', text: 'Terse.' }])

    expect(out.startsWith(saved.trimEnd())).toBe(true)
    expect(out.endsWith('## Voice\n\nTerse.\n')).toBe(true)
  })
})

describe('composeDraftSoul', () => {
  it('skips empty sections', () => {
    expect(
      composeDraftSoul('Mail', [
        { key: 'role', title: 'Role', text: 'R' },
        { key: 'voice', title: 'Voice', text: ' ' }
      ])
    ).toBe('# Mail\n\n## Role\n\nR\n')
  })
})

describe('applyDraftCapabilities', () => {
  it('turns picks on without turning anything else off', async () => {
    const calls: [string, Record<string, unknown> | undefined][] = []

    const request = async <T,>(method: string, params?: Record<string, unknown>): Promise<T> => {
      calls.push([method, params])

      return {
        skills: [
          { name: 'himalaya', enabled: false },
          { name: 'arxiv', enabled: false }
        ],
        toolsets: [
          { name: 'terminal', enabled: true },
          { name: 'web', enabled: false }
        ],
        mcp_servers: [{ name: 'gmail', enabled: false }]
      } as T
    }

    await applyDraftCapabilities(request, 'mailbot', {
      skills: ['email/himalaya'],
      toolsets: ['web'],
      mcp_servers: ['gmail']
    })

    expect(calls[1]).toEqual([
      'profiles.configure',
      { name: 'mailbot', disabled_skills: ['arxiv'], enabled_toolsets: ['terminal', 'web'], enabled_mcp_servers: ['gmail'] }
    ])
  })
})
