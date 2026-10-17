import type { Register } from 'claude-code'

export const register: Register = on => {
  on('ui.render', { component: 'ToolUse', props: { tool: 'Monitor' } }, async ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  // Monitor rows carry no task.type; the engine's summary text is what names them.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'task-notification' } } }, async ($, e, next) => {
    if (e.props.isExpanded || !e.props.text.startsWith('Monitor ')) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
}
