import type { PluginContext } from '@hermes/plugin-sdk'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import plugin from './plugin'

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
    expect(rest).toHaveBeenCalledWith('/board')
    expect(socket).toHaveBeenCalledWith('/events', expect.any(Function))
  })
})
