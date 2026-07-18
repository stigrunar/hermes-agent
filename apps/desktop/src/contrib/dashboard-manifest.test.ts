import { afterEach, describe, expect, it, vi } from 'vitest'

import { routeSessionId } from '@/app/routes'
import { registry } from '@/contrib/registry'

import {
  $dashboardPluginDiscovery,
  bundledDashboardCandidatePaths,
  defaultDashboardPluginCandidatePaths,
  setDashboardPluginPendingPath
} from './dashboard-discovery-state'
import { adaptDashboardManifest, manifestPaths } from './dashboard-manifest'

describe('dashboard manifest adaptation', () => {
  afterEach(() => {
    $dashboardPluginDiscovery.set({
      candidatePaths: defaultDashboardPluginCandidatePaths(),
      generation: 0,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: null
    })
  })

  it('maps a manifest to route and sidebar contributions without hardcoding Kanban', () => {
    const render = vi.fn()

    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        icon: 'LayoutDashboard',
        label: 'Board',
        name: 'board',
        tab: { path: '/board', position: 'after:skills' }
      },
      render
    )

    expect(adapted?.contributions).toEqual([
      expect.objectContaining({ area: 'routes', data: expect.objectContaining({ path: '/board' }), id: 'route', title: 'Board' }),
      expect.objectContaining({
        area: 'sidebar.nav',
        data: { codicon: 'layout-dashboard', label: 'Board', path: '/board', position: 'after:skills' },
        id: 'nav',
        order: 500
      })
    ])
  })

  it('derives cold-route candidates from bundled manifests without a host route list', () => {
    expect(
      bundledDashboardCandidatePaths([
        { name: 'board', tab: { path: '/board' } },
        { name: 'skin', tab: { override: '/', path: '/skin-home' } },
        { name: 'invalid', tab: { path: '/nested/path' } }
      ])
    ).toEqual(['/board', '/', '/skin-home'])
    expect(defaultDashboardPluginCandidatePaths()).toContain('/kanban')
  })

  it('keeps hidden manifests routable while omitting sidebar nav', () => {
    const adapted = adaptDashboardManifest(
      { entry: 'dist/index.js', label: 'Hidden', name: 'hidden', tab: { hidden: true, path: '/hidden' } },
      () => null
    )

    expect(adapted?.contributions.map(c => c.area)).toEqual(['routes'])
  })

  it('lets explicit manifest overrides replace built-in route and nav paths', () => {
    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        icon: 'Kanban',
        label: 'Kanban Skills',
        name: 'kanban-skills',
        tab: { override: '/skills', path: '/kanban-skills', position: 'before:messaging' }
      },
      () => null
    )

    expect(adapted?.manifest.tab.override).toBe('/skills')
    expect(adapted?.contributions).toEqual([
      expect.objectContaining({
        area: 'routes',
        data: { override: true, path: '/skills' },
        id: 'route',
        title: 'Kanban Skills'
      }),
      expect.objectContaining({
        area: 'sidebar.nav',
        data: {
          codicon: 'kanban',
          label: 'Kanban Skills',
          override: '/skills',
          path: '/skills',
          position: 'before:messaging'
        },
        id: 'nav',
        order: 100
      })
    ])
  })

  it('accepts an explicit root-route override from the backend manifest contract', () => {
    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        label: 'Plugin home',
        name: 'plugin-home',
        tab: { override: '/', path: '/plugin-home' }
      },
      () => null
    )

    expect(adapted?.manifest.tab.override).toBe('/')
    expect(adapted?.contributions[0]).toEqual(
      expect.objectContaining({ area: 'routes', data: { override: true, path: '/' } })
    )
  })

  it('rejects non-override collisions with built-in routes and bad override targets', () => {
    expect(
      adaptDashboardManifest(
        { entry: 'dist/index.js', label: 'Bad', name: 'bad', tab: { path: '/skills' } },
        () => null
      )
    ).toBeNull()
    expect(
      adaptDashboardManifest(
        { entry: 'dist/index.js', label: 'Bad', name: 'bad', tab: { override: '/not-built-in', path: '/bad' } },
        () => null
      )
    ).toBeNull()
  })

  it('rejects malformed ids, bad routes, and traversal assets', () => {
    expect(
      adaptDashboardManifest(
        { entry: 'dist/index.js', label: 'Bad', name: '../bad', tab: { path: '/bad' } },
        () => null
      )
    ).toBeNull()
    expect(
      adaptDashboardManifest(
        { entry: 'dist/index.js', label: 'Bad', name: 'bad', tab: { path: '/bad/child' } },
        () => null
      )
    ).toBeNull()
    expect(
      adaptDashboardManifest(
        { entry: '../dist/index.js', label: 'Bad', name: 'bad', tab: { path: '/bad' } },
        () => null
      )
    ).toBeNull()
  })

  it('reserves paths only for manifests that pass the complete adapter contract', () => {
    expect(
      manifestPaths([
        { entry: 'dist/index.js', label: 'Valid', name: 'valid', tab: { path: '/valid' } },
        { entry: '../dist/index.js', label: 'Traversal', name: 'traversal', tab: { path: '/blocked' } },
        { entry: 'dist/index.js', label: '', name: 'missing-label', tab: { path: '/missing-label' } },
        { entry: 'dist/index.js', label: 'Bad id', name: '../bad', tab: { path: '/bad-id' } }
      ])
    ).toEqual(['/valid'])
  })

  it('treats explicit pending plugin candidate routes as non-sessions', () => {
    setDashboardPluginPendingPath('/plugin-x')
    $dashboardPluginDiscovery.set({
      candidatePaths: ['/plugin-x'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: '/plugin-x'
    })

    expect(routeSessionId('/plugin-x')).toBeNull()
  })

  it('resolves manifest routes as plugin pages and allows non-manifest single segments as sessions', () => {
    $dashboardPluginDiscovery.set({
      candidatePaths: defaultDashboardPluginCandidatePaths(),
      generation: 1,
      phase: 'failed',
      reservedPaths: [],
      pendingPath: null
    })
    expect(routeSessionId('/other-route')).toBe('other-route')

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/plugin-x'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: '/plugin-x'
    })
    expect(routeSessionId('/plugin-x')).toBeNull()
    setDashboardPluginPendingPath('/plugin-x')

    $dashboardPluginDiscovery.set({ ...$dashboardPluginDiscovery.get(), phase: 'failed', pendingPath: '/plugin-x' })
    expect(routeSessionId('/plugin-x')).toBeNull()

    $dashboardPluginDiscovery.set({
      ...$dashboardPluginDiscovery.get(),
      phase: 'resolved',
      reservedPaths: ['/plugin-x'],
      pendingPath: null
    })
    expect(routeSessionId('/plugin-x')).toBeNull()

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/plugin-other'],
      generation: 2,
      phase: 'resolved',
      reservedPaths: ['/plugin-other'],
      pendingPath: null
    })
    expect(routeSessionId('/plugin-x')).toBe('plugin-x')
  })

  it('registered routes also reserve their path from session parsing', () => {
    $dashboardPluginDiscovery.set({
      candidatePaths: defaultDashboardPluginCandidatePaths(),
      generation: 1,
      phase: 'resolved',
      reservedPaths: [],
      pendingPath: null
    })

    const adapted = adaptDashboardManifest(
      { entry: 'dist/index.js', label: 'Extension', name: 'ext', tab: { path: '/extension' } },
      () => null
    )

    const dispose = registry.registerMany(adapted?.contributions ?? [])

    try {
      expect(routeSessionId('/extension')).toBeNull()
    } finally {
      dispose()
    }
  })
})
