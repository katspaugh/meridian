export function register(on) {
  on('session.start', async ($, event, next) => {
    const commands = ['meridian-view', await $.env.get('MERIDIAN_VIEW_ALIAS')].filter(Boolean)
    for (const name of commands) await $.command.register({
      name,
      description: 'Show the conversation selected in the Meridian sidebar',
      immediate: true,
    })
    return next(event)
  })

  on('session.measure', async ($, event, next) => {
    if (event.changed.includes('rateLimits')) {
      const directory = await $.env.get('MERIDIAN_VIEW_DIR')
      if (directory) await $.fs.write(directory + '/usage.json', JSON.stringify(event.rateLimits))
    }
    return next(event)
  })

  for (const command of ['meridian-view', 'muxteroid-view']) on('command.run', { command }, async $ => {
    const directory = await $.env.get('MERIDIAN_VIEW_DIR')
    const session = await $.session.id()
    const request = JSON.parse(await $.fs.read(directory + '/request.json'))
    if (request.session !== session) return { text: 'This selection belongs to another Claude session.' }
    const result = await $.ui.view(request.agent ? { agentId: request.agent } : {})
    await $.fs.write(directory + '/response.json', JSON.stringify({ token: request.token, error: result.deny || '' }))
    return { text: result.deny || '' }
  })
}
