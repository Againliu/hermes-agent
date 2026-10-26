import { describe, expect, it } from 'vitest'

import { buildToolView, type ToolPart } from './fallback-model'
import { summarizeToolRun } from './run-summary'

// Real envelopes: terminal_tool._error_json for a refused approval (the
// gateway path adds user_summary from tools/approval.py::_denied), and
// code_execution_tool._error_result for a refused script.
const TIMED_OUT_TERMINAL = JSON.stringify({
  output: '',
  exit_code: -1,
  error:
    'BLOCKED: Command timed out without user response. The user has NOT consented to this action. Do NOT retry this command.',
  status: 'blocked',
  user_summary: 'No answer within 20 seconds — the command did not run.'
})

const HARDLINE_TERMINAL = JSON.stringify({
  output: '',
  exit_code: -1,
  error: 'BLOCKED (hardline): recursive delete of root. This command is never allowed.',
  status: 'blocked'
})

const DENIED_CODE = JSON.stringify({
  status: 'error',
  error: 'BLOCKED: User denied execute_code script execution (matched shutil.rmtree).',
  tool_calls_made: 0,
  duration_seconds: 0,
  user_summary: 'You denied this code — it did not run.'
})

const call = (toolName: string, args: Record<string, unknown>, result: string): ToolPart => ({
  args,
  isError: false,
  result,
  toolCallId: `${toolName}-1`,
  toolName,
  type: 'tool-call'
})

describe('a refused command never reads as run', () => {
  it('titles the row Blocked and leads its detail with the human sentence', () => {
    const timedOut = buildToolView(call('terminal', { command: 'rm -rf /srv/data' }, TIMED_OUT_TERMINAL), '')

    expect(timedOut.title).toBe('Blocked rm -rf /srv/data')
    expect(timedOut.detail.split('\n')[0]).toBe('No answer within 20 seconds — the command did not run.')

    const hardline = buildToolView(call('terminal', { command: 'rm -rf /' }, HARDLINE_TERMINAL), '')

    expect(hardline.title).toBe('Blocked rm -rf /')

    const code = buildToolView(call('execute_code', { code: 'import shutil; shutil.rmtree("/srv")' }, DENIED_CODE), '')

    expect(code.title.startsWith('Blocked ')).toBe(true)
    expect(code.detail.split('\n')[0]).toBe('You denied this code — it did not run.')
  })

  it('keeps a refused command out of the run summary’s Ran and failed counts', () => {
    const blocked = call('terminal', { command: 'rm -rf /srv/data' }, TIMED_OUT_TERMINAL)
    const ran = call('terminal', { command: 'ls' }, JSON.stringify({ output: 'a', exit_code: 0 }))

    expect(summarizeToolRun([blocked], false)).toBe('Blocked rm -rf /srv/data')
    expect(summarizeToolRun([ran, blocked], false)).toBe('Ran 1 command, blocked rm -rf /srv/data')
  })
})
