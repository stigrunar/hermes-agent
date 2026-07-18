import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getDashboardPluginAsset,
  pluginFetchJSON,
  pluginRawFetch,
  pluginRest,
  pluginSocket,
  setApiRequestProfile
} from './hermes'

describe('Desktop plugin IPC transport', () => {
  let api: ReturnType<typeof vi.fn>
  let pluginRaw: ReturnType<typeof vi.fn>
  let dashboardPluginAsset: ReturnType<typeof vi.fn>

  beforeEach(() => {
    api = vi.fn().mockResolvedValue({ ok: true })
    pluginRaw = vi.fn().mockResolvedValue({ body: new ArrayBuffer(0), headers: {}, status: 200 })
    dashboardPluginAsset = vi.fn().mockResolvedValue({ body: new ArrayBuffer(0), headers: {}, status: 200 })
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api,
        pluginRaw,
        dashboardPluginAsset,
        getPluginWsUrl: vi
          .fn()
          .mockResolvedValueOnce('ws://127.0.0.1/api/plugins/kanban/events?ticket=one')
          .mockResolvedValueOnce('ws://127.0.0.1/api/plugins/kanban/events?ticket=two')
      }
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('scopes JSON plugin REST under the manifest id and rejects traversal', async () => {
    await pluginRest('kanban', '/board')
    expect(api).toHaveBeenCalledWith(expect.objectContaining({ path: '/api/plugins/kanban/board' }))
    await expect(pluginRest('kanban', '/../other')).rejects.toThrow(/Unsafe/)
    await expect(pluginRest('../kanban', '/board')).rejects.toThrow(/manifest id/)
  })

  it('adapts legacy dashboard fetchJSON bodies onto IPC JSON', async () => {
    await pluginFetchJSON('/api/plugins/kanban/tasks', {
      body: JSON.stringify({ title: 'A' }),
      method: 'POST'
    })

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ body: { title: 'A' }, method: 'POST', path: '/api/plugins/kanban/tasks' })
    )
  })

  it('passes the active profile through pluginRawFetch requests', async () => {
    setApiRequestProfile('kanban-profile')
    await pluginRawFetch('/api/plugins/kanban/tasks')

    expect(pluginRaw).toHaveBeenCalledWith(expect.objectContaining({ profile: 'kanban-profile' }))
    setApiRequestProfile(null)
  })

  it('passes the active profile through dashboard plugin asset requests', async () => {
    setApiRequestProfile('kanban-profile')
    await getDashboardPluginAsset('kanban', 'dist/index.js')

    expect(dashboardPluginAsset).toHaveBeenCalledWith(
      expect.objectContaining({ assetPath: 'dist/index.js', manifestId: 'kanban', profile: 'kanban-profile' })
    )
    setApiRequestProfile(null)
  })

  it('rejects unsafe dashboard plugin asset requests before IPC', async () => {
    await expect(getDashboardPluginAsset('kanban', '../dist/index.js')).rejects.toThrow(/Unsafe/)
    await expect(getDashboardPluginAsset('kanban', 'dist/index.js#frag')).rejects.toThrow(/Invalid/)
    expect(dashboardPluginAsset).not.toHaveBeenCalled()
  })

  it('rejects legacy SDK calls into another plugin namespace', async () => {
    await expect(pluginFetchJSON('/api/plugins/other/board', {}, 'kanban')).rejects.toThrow(/cannot access/)
    await expect(pluginRawFetch('/api/plugins/other/file', {}, 'kanban')).rejects.toThrow(/cannot access/)
  })

  it('adapts FormData uploads onto the authenticated IPC multipart envelope', async () => {
    const form = new FormData()
    form.append('file', new File(['hello'], 'note.txt', { type: 'text/plain' }))

    await pluginRawFetch('/api/plugins/kanban/tasks/t1/attachments', { body: form, method: 'POST' })

    expect(pluginRaw).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'POST',
        path: '/api/plugins/kanban/tasks/t1/attachments',
        upload: expect.objectContaining({ contentType: 'text/plain', filename: 'note.txt' })
      })
    )
  })

  it('mints a fresh plugin WebSocket URL for reconnects', () => {
    const sockets: Array<{
      close: () => void
      onclose?: () => void
      onmessage?: (event: MessageEvent) => void
      url: string
    }> = []

    const OriginalWebSocket = window.WebSocket
    let closeCallCount = 0

    vi.useFakeTimers()
    vi.stubGlobal(
      'WebSocket',
      class {
        close: () => void
        onclose: (() => void) | undefined
        onmessage: ((event: MessageEvent) => void) | undefined
        url: string

        constructor(url: string) {
          closeCallCount += 1
          this.url = url
          this.close = vi.fn()
          sockets.push(this)
        }
      }
    )

    const dispose = pluginSocket('kanban', '/events', vi.fn())

    return Promise.resolve()
      .then(async () => {
        await Promise.resolve()
        expect(sockets).toHaveLength(1)
        expect(sockets[0].url).toContain('ticket=one')
        sockets[0].onclose?.()
        expect(closeCallCount).toBe(1)
        await vi.runOnlyPendingTimersAsync()
        expect(sockets).toHaveLength(2)
        expect(sockets[1].url).toContain('ticket=two')
      })
      .finally(() => {
        dispose()
        vi.useRealTimers()
        vi.stubGlobal('WebSocket', OriginalWebSocket)
      })
  })
})
