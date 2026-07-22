import { host, type PluginContext } from '@hermes/plugin-sdk'
import { act, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import plugin from './plugin'

afterEach(() => {
  ;(host.state.profile as unknown as { set(value: string): void }).set('default')
})

describe('bundled Kanban plugin', () => {
  it('uses the canonical HermesPlugin registration contract', () => {
    const registrations: Array<Record<string, unknown>> = []

    const context = {
      register: vi.fn((contribution: Record<string, unknown>) => {
        registrations.push(contribution)

        return vi.fn()
      }),
      registerMany: vi.fn(),
      rest: vi.fn(),
      socket: vi.fn(() => vi.fn()),
      source: 'plugin:kanban',
      storage: { get: vi.fn(), remove: vi.fn(), set: vi.fn() }
    } as unknown as PluginContext

    plugin.register(context)

    expect(registrations).toEqual([
      expect.objectContaining({ area: 'routes', data: { path: '/kanban' }, id: 'route' }),
      expect.objectContaining({ area: 'sidebar.nav', data: expect.objectContaining({ path: '/kanban' }), id: 'nav' })
    ])
    expect(registrations[0]?.render).toEqual(expect.any(Function))
  })

  it('loads board data through the scoped REST and socket doors', async () => {
    const registrations: Array<Record<string, unknown>> = []

    const rest = vi.fn().mockResolvedValue({
      columns: [{ name: 'todo', tasks: [{ assignee: 'dollycode', id: 't_1', title: 'Repair plugin host' }] }],
      latest_event_id: 42
    })

    const socket = vi.fn(() => vi.fn())

    const context = {
      register: vi.fn((contribution: Record<string, unknown>) => {
        registrations.push(contribution)

        return vi.fn()
      }),
      registerMany: vi.fn(),
      rest,
      socket,
      source: 'plugin:kanban',
      storage: { get: vi.fn(), remove: vi.fn(), set: vi.fn() }
    } as unknown as PluginContext

    plugin.register(context)
    const routeRender = registrations[0]?.render

    if (typeof routeRender !== 'function') {
      throw new Error('Kanban route contribution did not register a renderer')
    }

    render(routeRender())

    expect(await screen.findByText('Repair plugin host')).toBeTruthy()
    expect(rest).toHaveBeenCalledWith('/board', { timeoutMs: 60_000 })
    expect(socket).toHaveBeenCalledWith('/events', expect.any(Function))
  })

  it('dedupes event bursts and defers automatic refreshes while a read is active', async () => {
    const registrations: Array<Record<string, unknown>> = []
    let resolveFirst: ((value: unknown) => void) | undefined
    let onSocketMessage: ((data: unknown) => void) | undefined
    const disposeSocket = vi.fn()

    const rest = vi.fn().mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveFirst = resolve
        })
    )

    const context = {
      register: vi.fn((contribution: Record<string, unknown>) => {
        registrations.push(contribution)

        return vi.fn()
      }),
      registerMany: vi.fn(),
      rest,
      socket: vi.fn((_path: string, onMessage: (data: unknown) => void) => {
        onSocketMessage = onMessage

        return disposeSocket
      }),
      source: 'plugin:kanban',
      storage: { get: vi.fn(), remove: vi.fn(), set: vi.fn() }
    } as unknown as PluginContext

    plugin.register(context)
    const routeRender = registrations[0]?.render

    if (typeof routeRender !== 'function') {
      throw new Error('Kanban route contribution did not register a renderer')
    }

    const { unmount } = render(routeRender())

    await waitFor(() => expect(rest).toHaveBeenCalledTimes(1))

    act(() => {
      onSocketMessage?.({ id: 1 })
      onSocketMessage?.({ id: 2 })
      onSocketMessage?.({ id: 3 })
    })
    expect(rest).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolveFirst?.({ columns: [], latest_event_id: 3 })
    })
    await waitFor(() => expect(screen.getByText('Kanban')).toBeTruthy())

    act(() => {
      onSocketMessage?.({ id: 4 })
      onSocketMessage?.({ id: 5 })
    })
    expect(rest).toHaveBeenCalledTimes(1)

    unmount()
    expect(disposeSocket).toHaveBeenCalledOnce()
  })

  it('shares in-flight reads across remounts and ignores results from an old profile', async () => {
    const registrations: Array<Record<string, unknown>> = []
    const resolvers: Array<(value: unknown) => void> = []

    const rest = vi.fn(
      () =>
        new Promise(resolve => {
          resolvers.push(resolve)
        })
    )

    const context = {
      register: vi.fn((contribution: Record<string, unknown>) => {
        registrations.push(contribution)

        return vi.fn()
      }),
      registerMany: vi.fn(),
      rest,
      socket: vi.fn(() => vi.fn()),
      source: 'plugin:kanban',
      storage: { get: vi.fn(), remove: vi.fn(), set: vi.fn() }
    } as unknown as PluginContext

    plugin.register(context)
    const routeRender = registrations[0]?.render

    if (typeof routeRender !== 'function') {
      throw new Error('Kanban route contribution did not register a renderer')
    }

    const strictMount = render(<StrictMode>{routeRender()}</StrictMode>)

    await waitFor(() => expect(rest).toHaveBeenCalledTimes(1))
    strictMount.unmount()

    const remount = render(routeRender())

    await waitFor(() => expect(rest).toHaveBeenCalledTimes(1))

    act(() => {
      ;(host.state.profile as unknown as { set(value: string): void }).set('dollycode')
    })
    await waitFor(() => expect(rest).toHaveBeenCalledTimes(2))

    await act(async () => {
      resolvers[0]?.({
        columns: [{ name: 'todo', tasks: [{ id: 'old', title: 'Old profile board' }] }]
      })
    })
    expect(screen.queryByText('Old profile board')).toBeNull()

    await act(async () => {
      resolvers[1]?.({
        columns: [{ name: 'todo', tasks: [{ id: 'new', title: 'New profile board' }] }]
      })
    })
    expect(await screen.findByText('New profile board')).toBeTruthy()

    remount.unmount()
  })
})
