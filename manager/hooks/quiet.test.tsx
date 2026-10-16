import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { CommandRunInput, On, RenderPropsOf } from 'claude-code'

const NOTICE: RenderPropsOf['UserMessage'] = {
  text: 'Background command "Wait for checks" completed (exit code 0)',
  origin: { kind: 'task-notification' },
  isExpanded: false,
  task: { id: 'b1', status: 'completed' },
}
const PROMPT: RenderPropsOf['UserMessage'] = { text: 'hi', origin: { kind: 'composer' }, isExpanded: false }
const GROUP: RenderPropsOf['ToolGroup'] = {
  calls: [{ tool: 'Read', input: { file_path: '/a' }, isRunning: false, isErrored: false, isInterrupted: false }],
  isActive: false,
  isExpanded: false,
}

const QUIET: CommandRunInput = { command: 'quiet', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } }

function engine(on: On) {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
}

async function drawn<C extends 'UserMessage' | 'ToolGroup'>($: Engine, component: C, props: RenderPropsOf[C]) {
  const ui = await $.ui.mount({ plugin: 'agent-pm', surface: 'terminal', component, props })
  const tree = await ui.drawn()
  await ui.unmount()
  return tree
}

const HIDDEN = { type: 'Box', part: { plugin: 'agent-pm' } }
const ENGINE = { type: 'Text', children: ['engine'] }

test('hides background notices and folded tool lines', async ($, on) => {
  engine(on)
  expect(await drawn($, 'UserMessage', NOTICE)).toEqual(HIDDEN)
  expect(await drawn($, 'ToolGroup', GROUP)).toEqual(HIDDEN)
})

test('leaves the prompt, ctrl+o and a live group drawn', async ($, on) => {
  engine(on)
  expect(await drawn($, 'UserMessage', PROMPT)).toMatchObject(ENGINE)
  expect(await drawn($, 'UserMessage', { ...NOTICE, isExpanded: true })).toMatchObject(ENGINE)
  expect(await drawn($, 'ToolGroup', { ...GROUP, isExpanded: true })).toMatchObject(ENGINE)
  expect(await drawn($, 'ToolGroup', { ...GROUP, isActive: true })).toMatchObject(ENGINE)
})

test('/quiet toggles it for the session', async ($, on) => {
  engine(on)
  expect(await $.command.run(QUIET)).toMatchObject({ text: 'Background notices shown.' })
  expect(await drawn($, 'UserMessage', NOTICE)).toMatchObject(ENGINE)
  expect(await drawn($, 'ToolGroup', GROUP)).toMatchObject(ENGINE)
  expect(await $.command.run(QUIET)).toMatchObject({ text: 'Background notices hidden.' })
  expect(await drawn($, 'UserMessage', NOTICE)).toEqual(HIDDEN)
})

test('the quiet_background option off draws as the engine does', { options: { quiet_background: false } }, async ($, on) => {
  engine(on)
  expect(await drawn($, 'UserMessage', NOTICE)).toMatchObject(ENGINE)
  expect(await drawn($, 'ToolGroup', GROUP)).toMatchObject(ENGINE)
  expect(await $.command.run(QUIET)).toMatchObject({ text: 'Background notices hidden.' })
  expect(await drawn($, 'UserMessage', NOTICE)).toEqual(HIDDEN)
})
