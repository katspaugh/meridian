export function register(on) {
  on('session.start', async ($, event, next) => {
    await $.command.register({
      name: 'muxteroid-view',
      description: 'Show the conversation selected in the Muxteroid sidebar',
      immediate: true,
    })
    return next(event)
  })

  on('command.run', { command: 'muxteroid-view' }, async $ => {
    const directory = await $.env.get('MUXTEROID_VIEW_DIR')
    const session = await $.session.id()
    const request = JSON.parse(await $.fs.read(directory + '/request.json'))
    if (request.session !== session) return { text: 'This selection belongs to another Claude session.' }
    const result = await $.ui.view(request.agent ? { agentId: request.agent } : {})
    await $.fs.write(directory + '/response.json', JSON.stringify({ token: request.token, error: result.deny || '' }))
    return { text: result.deny || '' }
  })
}
