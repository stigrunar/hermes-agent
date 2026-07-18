import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { act, StrictMode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatRoutesSurface } from '@/app/contrib/surfaces'
import { routeSessionId } from '@/app/routes'
import { TITLEBAR_HEIGHT } from '@/app/shell/titlebar'
import { registry } from '@/contrib/registry'
import { setApiRequestProfile } from '@/hermes'
import { relativeTime } from '@/lib/time'

import { $dashboardPluginDiscovery, setDashboardPluginPendingPath } from './dashboard-discovery-state'
import { adaptDashboardManifest, type DashboardManifestAdapter } from './dashboard-manifest'
import {
  assertSubresourceIntegrity,
  DashboardPluginPage,
  refreshDashboardPlugins,
  resetDashboardPluginDiscoveryForTests
} from './dashboard-plugins'

vi.mock('@/app/chat', () => ({
  ChatView: () => <div data-testid="chat-view">chat</div>
}))

function pluginManifest(name: string): DashboardManifestAdapter {
  return adaptDashboardManifest(
    { entry: 'dist/index.js', label: `${name} plugin`, name, tab: { path: `/${name}` } },
    () => null
  )!
}

function makeActions() {
  const noop = () => undefined

  return {
    getGateway: () => null,
    onAddContextRef: noop,
    onAddUrl: noop,
    onAttachDroppedItems: noop,
    onAttachImageBlob: noop,
    onAttachFiles: noop,
    onAttachFolders: noop,
    onAttachImages: noop,
    onBranchInNewChat: noop,
    onCancel: noop,
    onDeleteSelectedSession: noop,
    onDismissError: noop,
    onEdit: noop,
    onLoadMoreMessaging: noop,
    onLoadMoreProfileSessions: noop,
    onLoadMoreSessions: noop,
    onManageCronJob: noop,
    onNavigate: noop,
    onNewSessionInWorkspace: noop,
    onNewSessionSplit: noop,
    onPickFiles: noop,
    onPickFolders: noop,
    onPickImages: noop,
    onRemoveAttachment: noop,
    onRetryResume: noop,
    onResumeSession: noop,
    onSteer: noop,
    onSubmit: noop,
    onThreadMessagesChange: noop,
    onToggleSelectedPin: noop,
    onTranscribeAudio: noop,
    onTrash: noop,
    onTriggerCronJob: noop,
    openAgents: noop,
    openCommandCenterSection: noop,
    requestGateway: () => Promise.resolve(),
    selectModel: noop,
    toggleCommandCenter: noop,
    onRestoreToMessage: noop
  } as any
}

function captureDashboardScripts() {
  const appendChild = window.document.body.appendChild
  const scripts: HTMLScriptElement[] = []

  const restore = () => {
    window.document.body.appendChild = appendChild
  }

  window.document.body.appendChild = <T extends Node>(node: T): T => {
    if (node instanceof HTMLScriptElement) {
      scripts.push(node)
    }

    return appendChild.call(window.document.body, node as Node) as T
  }

  return { scripts, restore }
}

async function flushMicrotasks() {
  await Promise.resolve()
}

describe('dashboard plugins', () => {
  let dashboardPluginAsset: ReturnType<typeof vi.fn>
  let objectUrlCounter = 0

  beforeEach(() => {
    objectUrlCounter = 0
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:dashboard-plugin-${++objectUrlCounter}`)
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    dashboardPluginAsset = vi.fn().mockImplementation(({ assetPath }) =>
      Promise.resolve({
        body: bytesFromText(assetPath.endsWith('.css') ? '.plugin { color: red; }' : 'window.__loaded = true'),
        headers: { 'content-type': assetPath.endsWith('.css') ? 'text/css' : 'text/javascript' },
        status: 200
      })
    )

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        dashboardPluginAsset,
        getGatewayWsUrl: vi.fn().mockResolvedValue('ws://127.0.0.1/ws?token=local'),
        getPluginWsUrl: vi.fn().mockResolvedValue('ws://127.0.0.1/api/plugins/kanban/events?token=local'),
        pluginRaw: vi.fn().mockResolvedValue({ body: new ArrayBuffer(0), headers: {}, status: 200 }),
        api: vi.fn().mockResolvedValue([])
      }
    })
  })

  afterEach(() => {
    cleanup()
    resetDashboardPluginDiscoveryForTests()
    setApiRequestProfile(null)
    setDashboardPluginPendingPath(null)
    delete window.__HERMES_PLUGIN_SDK__
    delete window.__HERMES_PLUGINS__
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.restoreAllMocks()
  })

  it('rerenders route surface when plugin discovery phase updates', async () => {
    const actions = makeActions()

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/plugin-x'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: '/plugin-x'
    })

    const result = render(
      <MemoryRouter initialEntries={['/plugin-x']}>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    expect(result.getByLabelText('Loading dashboard plugin')).toBeTruthy()

    act(() => {
      $dashboardPluginDiscovery.set({
        candidatePaths: ['/other-route'],
        generation: 1,
        phase: 'resolved',
        reservedPaths: ['/other-route'],
        pendingPath: null
      })
    })

    await flushMicrotasks()

    expect(result.container.querySelector('[data-testid="chat-view"]')).toBeTruthy()
    expect(result.queryByLabelText('Loading dashboard plugin')).toBeNull()
  })

  it('renders an explicit manifest root override instead of the new-chat index', () => {
    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        label: 'Plugin home',
        name: 'plugin-home',
        tab: { override: '/', path: '/plugin-home' }
      },
      () => <div data-testid="plugin-home">plugin home</div>
    )!
    let dispose: () => void = () => undefined

    act(() => {
      dispose = registry.registerMany(adapted.contributions)
    })

    try {
      const result = render(
        <MemoryRouter initialEntries={['/']}>
          <ChatRoutesSurface actions={makeActions()} />
        </MemoryRouter>
      )

      expect(result.getByTestId('plugin-home')).toBeTruthy()
      expect(result.queryByTestId('chat-view')).toBeNull()
    } finally {
      act(() => dispose())
    }
  })

  it('keeps a normal cold session deep-link as chat while plugin discovery is pending or failed', () => {
    const sessionId = 'session-123'

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/kanban'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: null
    })

    expect(routeSessionId(`/${sessionId}`)).toBe(sessionId)

    const pending = render(
      <MemoryRouter initialEntries={[`/${sessionId}`]}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    expect(pending.getByTestId('chat-view')).toBeTruthy()
    expect(pending.queryByLabelText('Loading dashboard plugin')).toBeNull()
    pending.unmount()

    act(() => {
      $dashboardPluginDiscovery.set({
        candidatePaths: ['/kanban'],
        generation: 1,
        phase: 'failed',
        reservedPaths: [],
        pendingPath: null
      })
    })

    expect(routeSessionId(`/${sessionId}`)).toBe(sessionId)

    const failed = render(
      <MemoryRouter initialEntries={[`/${sessionId}`]}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    expect(failed.getByTestId('chat-view')).toBeTruthy()
    expect(failed.queryByRole('alert')).toBeNull()
  })

  it('keeps a cold known plugin path out of sessions without waiting for pendingPath mirroring', () => {
    $dashboardPluginDiscovery.set({
      candidatePaths: ['/kanban'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: null
    })

    expect(routeSessionId('/kanban')).toBeNull()

    const pending = render(
      <MemoryRouter initialEntries={['/kanban']}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    expect(pending.getByLabelText('Loading dashboard plugin')).toBeTruthy()
    pending.unmount()

    act(() => {
      $dashboardPluginDiscovery.set({
        candidatePaths: ['/kanban'],
        generation: 1,
        phase: 'failed',
        reservedPaths: [],
        pendingPath: null
      })
    })

    expect(routeSessionId('/kanban')).toBeNull()

    const failed = render(
      <MemoryRouter initialEntries={['/kanban']}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    expect(failed.getByRole('alert').textContent).toContain('Could not discover dashboard plugin')
  })

  it('renders a manifest discovery failure for the captured cold plugin path and keeps it out of sessions', async () => {
    const api = vi.fn().mockRejectedValue(new Error('manifest service unavailable'))
    window.hermesDesktop.api = api

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/kanban'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: '/kanban'
    })

    await refreshDashboardPlugins()

    expect(routeSessionId('/kanban')).toBeNull()

    const result = render(
      <MemoryRouter initialEntries={['/kanban']}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    expect(result.getByRole('alert').textContent).toContain('Could not discover dashboard plugin')
    expect(result.getByRole('alert').textContent).toContain('/kanban')
    expect(result.queryByLabelText('Loading dashboard plugin')).toBeNull()
  })

  it('retrying manifest discovery invokes the refresh path and resolves the captured route', async () => {
    const api = vi
      .fn()
      .mockRejectedValueOnce(new Error('manifest service unavailable'))
      .mockResolvedValueOnce([{ entry: 'dist/index.js', label: 'Kanban', name: 'kanban', tab: { path: '/kanban' } }])

    window.hermesDesktop.api = api

    $dashboardPluginDiscovery.set({
      candidatePaths: ['/kanban'],
      generation: 1,
      phase: 'pending',
      reservedPaths: [],
      pendingPath: '/kanban'
    })

    await refreshDashboardPlugins()

    const result = render(
      <MemoryRouter initialEntries={['/kanban']}>
        <ChatRoutesSurface actions={makeActions()} />
      </MemoryRouter>
    )

    fireEvent.click(result.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect($dashboardPluginDiscovery.get()).toMatchObject({
        phase: 'resolved',
        reservedPaths: ['/kanban'],
        pendingPath: null
      })
    )

    expect(routeSessionId('/kanban')).toBeNull()
  })

  it('fetches CSS and JS assets through the desktop bridge and removes blob assets on unmount', async () => {
    const adapted = adaptDashboardManifest(
      {
        css: 'dist/style.css',
        entry: 'dist/index.js',
        label: 'Signed plugin',
        name: 'signed',
        tab: { path: '/signed' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    const stylesheet = window.document.head.querySelector<HTMLLinkElement>(
      'link[data-hermes-dashboard-plugin="signed"]'
    )

    expect(dashboardPluginAsset).toHaveBeenCalledWith(
      expect.objectContaining({ assetPath: 'dist/style.css', manifestId: 'signed' })
    )
    expect(dashboardPluginAsset).toHaveBeenCalledWith(
      expect.objectContaining({ assetPath: 'dist/index.js', manifestId: 'signed' })
    )
    expect(stylesheet?.href).toBe('blob:dashboard-plugin-1')
    expect(scripts).toHaveLength(1)
    expect(scripts[0].src).toBe('blob:dashboard-plugin-2')

    act(() => {
      window.__HERMES_PLUGINS__?.register('signed', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    result.unmount()

    await waitFor(() => expect(stylesheet?.isConnected).toBe(false))
    expect(scripts[0].isConnected).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-2')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-1')
    restore()
  })

  it('exposes the shared Desktop titlebar height to plugin CSS', async () => {
    const adapted = pluginManifest('clearance')
    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    act(() => {
      window.__HERMES_PLUGINS__?.register('clearance', () => <div data-testid="plugin-content" />)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    const page = result.getByTestId('plugin-content').closest<HTMLElement>('.dashboard-plugin-page')

    expect(page?.style.getPropertyValue('--dashboard-plugin-titlebar-height')).toBe(`${TITLEBAR_HEIGHT}px`)
    expect(page?.className).toContain('[contain:paint]')

    result.unmount()
    restore()
  })

  it('formats dashboard plugin timestamps with the Desktop relative-time helper', async () => {
    const adapted = pluginManifest('time-formatters')
    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    const sdk = window.__HERMES_PLUGIN_SDK__ as
      | {
          utils?: {
            isoTimeAgo?: (timestamp: string) => string
            timeAgo?: (timestampSeconds: number) => string
          }
        }
      | undefined
    const timestampSeconds = Math.floor(Date.now() / 1000) - 120
    const timestampIso = new Date(Date.now() - 3_600_000).toISOString()

    expect(sdk?.utils?.timeAgo?.(timestampSeconds)).toBe(relativeTime(timestampSeconds * 1000))
    expect(sdk?.utils?.isoTimeAgo?.(timestampIso)).toBe(relativeTime(Date.parse(timestampIso)))
    expect(sdk?.utils?.timeAgo?.(Number.NaN)).toBe('unknown')
    expect(sdk?.utils?.isoTimeAgo?.('not-a-date')).toBe('unknown')

    act(() => {
      window.__HERMES_PLUGINS__?.register('time-formatters', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    result.unmount()
    restore()
  })

  it('exposes Desktop dialog, textarea, and checkbox primitives to dashboard plugins', async () => {
    const adapted = pluginManifest('primitives')
    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    const sdk = window.__HERMES_PLUGIN_SDK__ as { components?: Record<string, unknown> } | undefined

    expect(Object.keys(sdk?.components ?? {})).toEqual(
      expect.arrayContaining([
        'Checkbox',
        'Dialog',
        'DialogContent',
        'DialogDescription',
        'DialogFooter',
        'DialogHeader',
        'DialogTitle',
        'Textarea'
      ])
    )

    act(() => {
      window.__HERMES_PLUGINS__?.register('primitives', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    result.unmount()
    restore()
  })

  it('passes the active Desktop profile through both WebSocket URL helpers', async () => {
    const adapted = pluginManifest('profile-ws')
    const getGatewayWsUrl = vi.fn().mockResolvedValue('ws://127.0.0.1/ws?ticket=secondary')
    const getPluginWsUrl = vi
      .fn()
      .mockResolvedValue('ws://127.0.0.1/api/plugins/profile-ws/events?ticket=secondary')
    window.hermesDesktop.getGatewayWsUrl = getGatewayWsUrl
    window.hermesDesktop.getPluginWsUrl = getPluginWsUrl
    setApiRequestProfile('secondary')

    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    const sdk = window.__HERMES_PLUGIN_SDK__ as
      | {
          buildWsAuthParam?: () => Promise<readonly [string, string]>
          buildWsUrl?: (path: string, params?: Record<string, string>) => Promise<string>
        }
      | undefined
    await expect(sdk?.buildWsAuthParam?.()).resolves.toEqual(['ticket', 'secondary'])
    expect(getGatewayWsUrl).toHaveBeenCalledWith('secondary')
    await expect(sdk?.buildWsUrl?.('/api/plugins/profile-ws/events', { since: '7' })).resolves.toContain('since=7')
    expect(getPluginWsUrl).toHaveBeenCalledWith('profile-ws', '/events', 'secondary')

    act(() => {
      window.__HERMES_PLUGINS__?.register('profile-ws', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    result.unmount()
    setApiRequestProfile(null)
    restore()
  })

  it('shares one asset execution for duplicate route and pane mounts of the same manifest', async () => {
    const adapted = adaptDashboardManifest(
      {
        css: 'dist/style.css',
        entry: 'dist/index.js',
        label: 'Shared plugin',
        name: 'shared',
        tab: { path: '/shared' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()
    const first = render(<DashboardPluginPage manifest={adapted.manifest} />)
    const second = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))
    expect(dashboardPluginAsset).toHaveBeenCalledTimes(2)

    const stylesheet = window.document.head.querySelector<HTMLLinkElement>(
      'link[data-hermes-dashboard-plugin="shared"]'
    )

    expect(stylesheet?.isConnected).toBe(true)
    expect(scripts[0].isConnected).toBe(true)

    act(() => {
      window.__HERMES_PLUGINS__?.register('shared', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    first.unmount()

    expect(stylesheet?.isConnected).toBe(true)
    expect(scripts[0].isConnected).toBe(true)
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    second.unmount()

    await waitFor(() => expect(stylesheet?.isConnected).toBe(false))
    expect(scripts[0].isConnected).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-1')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-2')
    restore()
  })

  it('does not execute or leak duplicate assets across a StrictMode route-to-pane handoff', async () => {
    const adapted = adaptDashboardManifest(
      {
        css: 'dist/style.css',
        entry: 'dist/index.js',
        label: 'Strict plugin',
        name: 'strict',
        tab: { path: '/strict' }
      },
      () => null
    )!
    const { scripts, restore } = captureDashboardScripts()
    const result = render(
      <StrictMode>
        <DashboardPluginPage manifest={adapted.manifest} />
      </StrictMode>
    )

    await waitFor(() => expect(scripts).toHaveLength(1))
    expect(dashboardPluginAsset).toHaveBeenCalledTimes(2)
    expect(document.querySelectorAll('script[data-hermes-dashboard-plugin="strict"]')).toHaveLength(1)
    expect(document.querySelectorAll('link[data-hermes-dashboard-plugin="strict"]')).toHaveLength(1)

    act(() => {
      window.__HERMES_PLUGINS__?.register('strict', () => null)
      scripts[0].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    result.unmount()
    await waitFor(() =>
      expect(document.querySelectorAll('script[data-hermes-dashboard-plugin="strict"]')).toHaveLength(0)
    )
    expect(document.querySelectorAll('link[data-hermes-dashboard-plugin="strict"]')).toHaveLength(0)
    restore()
  })

  it('verifies SRI before executing JS assets and sets anonymous CORS', async () => {
    const bytes = bytesFromText('window.__signed = true')
    const integrity = await sriFor(bytes, 'SHA-384')
    dashboardPluginAsset.mockResolvedValue({ body: bytes, headers: { 'content-type': 'text/javascript' }, status: 200 })

    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        integrity,
        label: 'Signed plugin',
        name: 'signed',
        tab: { path: '/signed' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()

    render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))
    expect(scripts[0].getAttribute('integrity')).toBe(integrity)
    expect(scripts[0].getAttribute('crossorigin')).toBe('anonymous')
    restore()
  })

  it('does not apply the entry-script SRI hash to separate CSS bytes', async () => {
    const scriptBytes = bytesFromText('window.__signed = true')
    const integrity = await sriFor(scriptBytes, 'SHA-384')

    dashboardPluginAsset.mockImplementation(({ assetPath }: { assetPath: string }) =>
      Promise.resolve({
        body: assetPath.endsWith('.css') ? bytesFromText('.board {}') : scriptBytes,
        headers: { 'content-type': assetPath.endsWith('.css') ? 'text/css' : 'text/javascript' },
        status: 200
      })
    )

    const adapted = adaptDashboardManifest(
      {
        css: 'dist/style.css',
        entry: 'dist/index.js',
        integrity,
        label: 'Signed plugin',
        name: 'signed',
        tab: { path: '/signed' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))
    expect(result.container.querySelector('[role="alert"]')).toBeNull()
    expect(scripts[0].getAttribute('integrity')).toBe(integrity)
    restore()
  })

  it('fails before JS execution when manifest integrity does not match fetched bytes', async () => {
    dashboardPluginAsset.mockResolvedValue({
      body: bytesFromText('window.__tampered = true'),
      headers: { 'content-type': 'text/javascript' },
      status: 200
    })

    const adapted = adaptDashboardManifest(
      {
        entry: 'dist/index.js',
        integrity: await sriFor(bytesFromText('expected bytes'), 'SHA-384'),
        label: 'Signed plugin',
        name: 'signed',
        tab: { path: '/signed' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() =>
      expect(result.container.querySelector('[role="alert"]')?.textContent).toContain(
        'Plugin assets could not be loaded.'
      )
    )
    expect(scripts).toHaveLength(0)
    restore()
  })

  it('removes appended plugin assets and revokes blob URLs immediately on script load failure', async () => {
    const adapted = adaptDashboardManifest(
      {
        css: 'dist/style.css',
        entry: 'dist/index.js',
        label: 'Signed plugin',
        name: 'signed',
        tab: { path: '/signed' }
      },
      () => null
    )!

    const { scripts, restore } = captureDashboardScripts()
    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    const stylesheet = window.document.head.querySelector<HTMLLinkElement>(
      'link[data-hermes-dashboard-plugin="signed"]'
    )

    expect(stylesheet?.isConnected).toBe(true)
    expect(scripts[0].isConnected).toBe(true)

    act(() => {
      scripts[0].onerror?.(new Event('error'))
    })

    await waitFor(() =>
      expect(result.container.querySelector('[role="alert"]')?.textContent).toContain(
        'Plugin assets could not be loaded.'
      )
    )

    expect(stylesheet?.isConnected).toBe(false)
    expect(scripts[0].isConnected).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-1')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dashboard-plugin-2')

    result.unmount()

    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
    restore()
  })

  it('accepts matching SRI bytes and rejects malformed SRI contracts', async () => {
    const bytes = bytesFromText('asset bytes')

    await expect(assertSubresourceIntegrity(bytes, await sriFor(bytes, 'SHA-256'))).resolves.toBeUndefined()
    await expect(assertSubresourceIntegrity(bytes, 'md5-example')).rejects.toThrow(/Invalid/)
    await expect(assertSubresourceIntegrity(bytes, await sriFor(bytesFromText('other'), 'SHA-256'))).rejects.toThrow(
      /integrity check failed/
    )
  })

  it('restores prior dashboard globals after plugin execution', async () => {
    const adapted = pluginManifest('owner')
    const { scripts, restore } = captureDashboardScripts()

    const previousSdk = { source: 'legacy-sdk' }
    const previousPlugins = { register: vi.fn() }

    window.__HERMES_PLUGIN_SDK__ = previousSdk as never
    window.__HERMES_PLUGINS__ = previousPlugins as never

    const result = render(<DashboardPluginPage manifest={adapted.manifest} />)

    await waitFor(() => expect(scripts).toHaveLength(1))

    expect(scripts).toHaveLength(1)

    act(() => {
      window.__HERMES_PLUGINS__?.register('owner', () => <div data-testid="plugin-component" />)
      scripts[0].onload?.(new Event('load'))
    })

    await flushMicrotasks()

    expect(result.container.querySelector('[data-testid="plugin-component"]')).toBeTruthy()
    expect(window.__HERMES_PLUGIN_SDK__).toBe(previousSdk)
    expect(window.__HERMES_PLUGINS__).toBe(previousPlugins)

    result.unmount()
    restore()
  })

  it('rejects registration for a non-owner manifest id during concurrent loads', async () => {
    const first = pluginManifest('alpha')
    const second = pluginManifest('beta')
    const { scripts, restore } = captureDashboardScripts()

    const pendingScripts = render(
      <>
        <DashboardPluginPage manifest={first.manifest} />
        <DashboardPluginPage manifest={second.manifest} />
      </>
    )

    await waitFor(() => expect(scripts).toHaveLength(1))
    expect(scripts).toHaveLength(1)
    expect(scripts[0].src).toBe('blob:dashboard-plugin-1')

    act(() => {
      window.__HERMES_PLUGINS__?.register('beta', () => <div data-testid="wrong-owner" />)
      scripts[0].onload?.(new Event('load'))
    })
    await waitFor(() => expect(scripts).toHaveLength(2))

    act(() => {
      scripts[1].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    expect(pendingScripts.container.querySelector('[data-testid="wrong-owner"]')).toBeNull()
    expect(pendingScripts.container.querySelector('[role="alert"]')).toBeTruthy()

    pendingScripts.unmount()
    restore()
  })

  it('restores globals and releases the execution queue when a pending load unmounts', async () => {
    const first = pluginManifest('first')
    const next = pluginManifest('next')
    const { scripts, restore } = captureDashboardScripts()
    const previousSdk = { source: 'desktop-runtime-sdk' }
    const previousPlugins = { register: vi.fn() }

    window.__HERMES_PLUGIN_SDK__ = previousSdk as never
    window.__HERMES_PLUGINS__ = previousPlugins as never

    const firstRender = render(<DashboardPluginPage manifest={first.manifest} />)
    await waitFor(() => expect(scripts).toHaveLength(1))

    firstRender.unmount()

    await waitFor(() => expect(window.__HERMES_PLUGIN_SDK__).toBe(previousSdk))
    expect(window.__HERMES_PLUGINS__).toBe(previousPlugins)

    const nextRender = render(<DashboardPluginPage manifest={next.manifest} />)
    await waitFor(() => expect(scripts).toHaveLength(2))
    expect(scripts).toHaveLength(2)

    act(() => {
      scripts[1].onload?.(new Event('load'))
    })
    await flushMicrotasks()

    nextRender.unmount()
    restore()
  })
})

function bytesFromText(text: string): ArrayBuffer {
  return new TextEncoder().encode(text).buffer
}

async function sriFor(bytes: ArrayBuffer, algorithm: AlgorithmIdentifier): Promise<string> {
  const digest = await crypto.subtle.digest(algorithm, bytes)
  const sriName = String(algorithm).toLowerCase().replace('sha-', 'sha')
  let binary = ''

  for (const byte of new Uint8Array(digest)) {
    binary += String.fromCharCode(byte)
  }

  return `${sriName}-${btoa(binary)}`
}
