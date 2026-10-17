import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

export const register: Register = (on, options) => {
  const quiet = atom({ plugin: 'agent-pm', key: 'quiet' } as const, options.quiet_background !== false)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'quiet',
      description: 'Show or hide Monitor event and stream-ended notices in this session',
    })
    return next(e)
  })

  on('command.run', { command: 'quiet' }, async $ => {
    await update($, quiet, hidden => !hidden)
    return { text: (await read($, quiet)) ? 'Monitor notices hidden.' : 'Monitor notices shown.' }
  })

  // Monitor rows carry no task.type; the engine's summary text is what names them.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'task-notification' } } }, async ($, e, next) => {
    if (e.props.isExpanded || !e.props.text.startsWith('Monitor ') || !(await read($, quiet))) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
}
