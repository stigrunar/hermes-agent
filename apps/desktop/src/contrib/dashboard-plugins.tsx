import * as React from 'react'
import { type CSSProperties, useSyncExternalStore } from 'react'

import { TITLEBAR_HEIGHT } from '@/app/shell/titlebar'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { ErrorState } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import { Loader } from '@/components/ui/loader'
import { SearchField } from '@/components/ui/search-field'
import { Textarea } from '@/components/ui/textarea'
import { getApiRequestProfile, getDashboardPluginAsset, getDashboardPluginManifests, pluginFetchJSON, pluginRawFetch } from '@/hermes'
import { useI18n } from '@/i18n'
import { relativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { $activeGatewayProfile, $profileScope, ALL_PROFILES, normalizeProfileKey } from '@/store/profile'

import { $dashboardPluginDiscovery, defaultDashboardPluginCandidatePaths } from './dashboard-discovery-state'
import { adaptDashboardManifest, type DashboardPluginManifest, manifestPaths } from './dashboard-manifest'
import { createPluginContext } from './plugin'

type PluginComponent = React.ComponentType<Record<string, never>>
type Listener = () => void

type PluginManifestLoadState = 'aborted' | 'loaded' | 'error'
type DashboardAssetKind = 'css' | 'js'

const components = new Map<string, PluginComponent>()
const errors = new Map<string, string>()
const listeners = new Set<Listener>()
const loadingManifests = new Set<string>()
let generation = 0
let activeDisposers: Array<() => void> = []
let manifestExecutionQueue = Promise.resolve()

const DASHBOARD_PLUGIN_PAGE_STYLE = {
  '--dashboard-plugin-titlebar-height': `${TITLEBAR_HEIGHT}px`
} as CSSProperties

function dashboardTimeAgo(timestampSeconds: number): string {
  return Number.isFinite(timestampSeconds) ? relativeTime(timestampSeconds * 1000) : 'unknown'
}

function dashboardIsoTimeAgo(timestamp: string): string {
  const timestampMs = Date.parse(timestamp)

  return Number.isFinite(timestampMs) ? relativeTime(timestampMs) : 'unknown'
}

interface DashboardPluginAssetEntry {
  finalize: (state: PluginManifestLoadState) => void
  refs: number
  releaseDom: () => void
  releaseTimer: number | null
}

const assetEntries = new Map<string, DashboardPluginAssetEntry>()

function queueDashboardPluginExecution(task: () => Promise<void> | void): void {
  const next = manifestExecutionQueue.then(() => task())

  manifestExecutionQueue = next.then(
    () => undefined,
    () => undefined
  )
}

function notify(): void {
  listeners.forEach(listener => listener())
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener)

  return () => listeners.delete(listener)
}

function registerDashboardComponent(ownerId: string, name: string, component: PluginComponent): void {
  if (ownerId !== name) {
    console.warn(`[dashboard-plugins] rejected cross-manifest registration for ${name}`)

    return
  }

  if (!loadingManifests.has(ownerId)) {
    console.warn(`[dashboard-plugins] rejected stale registration for ${name}`)

    return
  }

  errors.delete(name)
  components.set(name, component)
  notify()
}

function setDashboardPluginError(name: string, error: string): void {
  errors.set(name, error)
  notify()
}

function disposeActive(): void {
  const disposers = activeDisposers
  activeDisposers = []
  disposers.reverse().forEach(dispose => {
    try {
      dispose()
    } catch {
      // Keep unloading the rest of the generation.
    }
  })
  components.clear()
  errors.clear()

  for (const [key, entry] of assetEntries) {
    if (entry.releaseTimer !== null) {
      window.clearTimeout(entry.releaseTimer)
    }

    entry.finalize('aborted')
    entry.releaseDom()
    assetEntries.delete(key)
  }

  notify()
}

export async function refreshDashboardPlugins(): Promise<void> {
  const ownGeneration = ++generation
  const previousDiscovery = $dashboardPluginDiscovery.get()

  disposeActive()
  $dashboardPluginDiscovery.set({
    ...previousDiscovery,
    generation: ownGeneration,
    phase: 'pending'
  })

  try {
    const response = await getDashboardPluginManifests<unknown>()

    if (ownGeneration !== generation) {
      return
    }

    const manifests = Array.isArray(response) ? response : []
    const paths = manifestPaths(manifests)
    const candidatePaths = Array.from(new Set([...defaultDashboardPluginCandidatePaths(), ...paths]))

    const previousPendingPath =
      previousDiscovery.phase === 'failed' || previousDiscovery.phase === 'pending'
        ? previousDiscovery.pendingPath
        : null

    const pendingPath = previousPendingPath && paths.includes(previousPendingPath) ? previousPendingPath : null

    $dashboardPluginDiscovery.set({
      ...$dashboardPluginDiscovery.get(),
      candidatePaths,
      phase: 'pending',
      reservedPaths: paths,
      pendingPath
    })

    const nextDisposers: Array<() => void> = []

    for (const candidate of manifests) {
      let resolvedManifest: DashboardPluginManifest | null = null

      const adapted = adaptDashboardManifest(candidate, () =>
        resolvedManifest ? <DashboardPluginPage manifest={resolvedManifest} /> : null
      )

      if (!adapted) {
        continue
      }

      resolvedManifest = adapted.manifest
      createPluginContext(adapted.manifest.name, dispose => nextDisposers.push(dispose)).registerMany(
        adapted.contributions
      )
    }

    if (ownGeneration !== generation) {
      nextDisposers.reverse().forEach(dispose => dispose())

      return
    }

    activeDisposers = nextDisposers
    const resolvedDiscovery = $dashboardPluginDiscovery.get()
    $dashboardPluginDiscovery.set({
      ...resolvedDiscovery,
      candidatePaths,
      phase: 'resolved',
      pendingPath: null,
      reservedPaths: paths
    })
  } catch (error) {
    if (ownGeneration === generation) {
      const currentDiscovery = $dashboardPluginDiscovery.get()

      const failedPath =
        currentDiscovery.phase === 'failed' || currentDiscovery.phase === 'pending'
          ? currentDiscovery.pendingPath
          : previousDiscovery.phase === 'failed' || previousDiscovery.phase === 'pending'
            ? previousDiscovery.pendingPath
            : null

      $dashboardPluginDiscovery.set({
        ...currentDiscovery,
        candidatePaths: Array.from(
          new Set([...defaultDashboardPluginCandidatePaths(), ...currentDiscovery.candidatePaths, ...previousDiscovery.reservedPaths])
        ),
        phase: 'failed',
        pendingPath: failedPath,
        reservedPaths: previousDiscovery.reservedPaths
      })
      console.warn('[dashboard-plugins] discovery failed', error)
    }
  }
}

export function resetDashboardPluginDiscoveryForTests(): void {
  generation += 1
  disposeActive()
  $dashboardPluginDiscovery.set({
    candidatePaths: defaultDashboardPluginCandidatePaths(),
    generation,
    phase: 'pending',
    reservedPaths: [],
    pendingPath: null
  })
}

export function DashboardPluginPage({ manifest }: { manifest: DashboardPluginManifest }) {
  useDashboardPluginAssets(manifest)

  const Component = useSyncExternalStore(
    subscribe,
    () => components.get(manifest.name) ?? null,
    () => null
  )

  const error = useSyncExternalStore(
    subscribe,
    () => errors.get(manifest.name) ?? null,
    () => null
  )

  if (Component) {
    return (
      <div
        className="dashboard-plugin-page relative isolate h-full min-h-0 overflow-auto [container-type:inline-size] [contain:paint]"
        style={DASHBOARD_PLUGIN_PAGE_STYLE}
      >
        <Component />
      </div>
    )
  }

  if (error) {
    return (
      <div className="grid h-full place-items-center p-8" role="alert">
        <ErrorState description={error} title={`${manifest.label} failed to load`} />
      </div>
    )
  }

  return <DashboardPluginPendingPage />
}

export function DashboardPluginPendingPage() {
  return (
    <div className="grid h-full place-items-center p-8">
      <Loader aria-label="Loading dashboard plugin" className="size-6 text-(--ui-text-secondary)" />
    </div>
  )
}

export function DashboardPluginDiscoveryFailurePage({ path }: { path: string }) {
  const [retrying, setRetrying] = React.useState(false)

  const retry = React.useCallback(() => {
    setRetrying(true)
    void refreshDashboardPlugins().finally(() => setRetrying(false))
  }, [])

  return (
    <div className="grid h-full place-items-center p-8" role="alert">
      <ErrorState
        description={
          <>
            Hermes could not load plugin manifests for <span className="font-mono">{path}</span>.
          </>
        }
        title="Could not discover dashboard plugin"
      >
        <Button disabled={retrying} onClick={retry} size="sm" type="button">
          {retrying ? 'Retrying...' : 'Retry'}
        </Button>
      </ErrorState>
    </div>
  )
}

function useDashboardPluginAssets(manifest: DashboardPluginManifest): void {
  React.useEffect(() => {
    const manifestName = manifest.name
    const key = `${generation}:${manifestName}`
    const existing = assetEntries.get(key)

    if (existing) {
      if (existing.releaseTimer !== null) {
        window.clearTimeout(existing.releaseTimer)
        existing.releaseTimer = null
      }

      existing.refs += 1

      return () => releaseDashboardPluginAssets(key, existing)
    }

    const cleanup: Array<() => void> = []
    let finalize: (state: PluginManifestLoadState) => void = () => undefined
    let cleanupDrained = false

    const drainCleanup = () => {
      if (cleanupDrained) {
        return
      }

      cleanupDrained = true
      cleanup.reverse().forEach(fn => fn())
    }

    const assetEntry: DashboardPluginAssetEntry = {
      finalize: state => finalize(state),
      refs: 1,
      releaseDom: drainCleanup,
      releaseTimer: null
    }
    const isCurrentEntry = () => assetEntries.get(key) === assetEntry

    assetEntries.set(key, assetEntry)

    const load = () =>
      new Promise<void>(resolve => {
        let done = false
        loadingManifests.add(manifestName)

        const restoreGlobals = installDashboardPluginSdk(manifestName)

        const finish = (state: PluginManifestLoadState) => {
          if (done) {
            return
          }

          done = true
          loadingManifests.delete(manifestName)
          restoreGlobals()

          if (isCurrentEntry() && state === 'error') {
            setDashboardPluginError(manifestName, 'Plugin assets could not be loaded.')
            drainCleanup()
          }

          if (isCurrentEntry() && state === 'loaded' && !components.has(manifestName)) {
            setDashboardPluginError(manifestName, 'Plugin did not register a page.')
          }

          resolve()
        }

        finalize = finish

        if (!isCurrentEntry()) {
          finish('aborted')

          return
        }
        void (async () => {
          try {
            if (manifest.css) {
              const stylesheet = await createDashboardPluginAssetUrl(manifest, manifest.css, 'css')

              if (!isCurrentEntry()) {
                stylesheet.dispose()

                return
              }

              const link = document.createElement('link')
              link.dataset.hermesDashboardPlugin = manifestName
              link.href = stylesheet.url
              link.rel = 'stylesheet'

              if (manifest.integrity) {
                link.setAttribute('crossorigin', 'anonymous')
              }

              document.head.appendChild(link)
              cleanup.push(() => {
                link.remove()
                stylesheet.dispose()
              })
            }

            const entry = await createDashboardPluginAssetUrl(manifest, manifest.entry, 'js')

            if (!isCurrentEntry()) {
              entry.dispose()

              return
            }

            const script = document.createElement('script')
            script.async = true
            script.dataset.hermesDashboardPlugin = manifestName
            script.src = entry.url

            if (manifest.integrity) {
              script.setAttribute('integrity', manifest.integrity)
              script.setAttribute('crossorigin', 'anonymous')
            }

            script.onerror = () => finish('error')
            script.onload = () => finish('loaded')

            document.body.appendChild(script)
            cleanup.push(() => {
              if (script.parentElement) {
                script.onerror = null
                script.onload = null
                script.remove()
              }

              entry.dispose()
            })
          } catch (error) {
            console.warn(`[dashboard-plugins] failed to load ${manifestName}`, error)
            finish('error')
          }
        })()
      })

    queueDashboardPluginExecution(() => load())

    return () => releaseDashboardPluginAssets(key, assetEntry)
  }, [manifest])
}

function releaseDashboardPluginAssets(key: string, expectedEntry: DashboardPluginAssetEntry): void {
  const entry = assetEntries.get(key)

  if (!entry || entry !== expectedEntry) {
    return
  }

  entry.refs -= 1

  if (entry.refs > 0) {
    return
  }

  // Route -> pane handoff and React StrictMode both briefly unmount then remount
  // the same manifest. Keep the entry alive through the current task so the
  // next mount can reclaim it instead of executing the plugin (and opening its
  // WebSocket) a second time.
  entry.releaseTimer = window.setTimeout(() => {
    entry.releaseTimer = null

    if (assetEntries.get(key) !== entry || entry.refs > 0) {
      return
    }

    assetEntries.delete(key)
    entry.finalize('aborted')
    entry.releaseDom()
    const manifestName = key.slice(key.indexOf(':') + 1)
    components.delete(manifestName)
    errors.delete(manifestName)
    notify()
  }, 0)
}

async function createDashboardPluginAssetUrl(
  manifest: DashboardPluginManifest,
  assetPath: string,
  kind: DashboardAssetKind
): Promise<{ dispose: () => void; url: string }> {
  const response = await getDashboardPluginAsset(manifest.name, assetPath)

  if (!response.ok) {
    throw new Error(`dashboard plugin asset returned ${response.status}`)
  }

  const bytes = await response.arrayBuffer()

  // The manifest's single `integrity` field is the entry-script SRI contract
  // (matching the browser dashboard loader), not a hash shared by unrelated
  // CSS bytes. Verify only the executable entry before creating its Blob URL.
  if (kind === 'js' && manifest.integrity) {
    await assertSubresourceIntegrity(bytes, manifest.integrity)
  }

  const type = response.headers.get('content-type') || (kind === 'css' ? 'text/css' : 'text/javascript')
  const url = URL.createObjectURL(new Blob([bytes], { type }))
  let revoked = false

  return {
    dispose: () => {
      if (!revoked) {
        revoked = true
        URL.revokeObjectURL(url)
      }
    },
    url
  }
}

export async function assertSubresourceIntegrity(bytes: ArrayBuffer, integrity: string): Promise<void> {
  const candidates = integrity
    .trim()
    .split(/\s+/)
    .map(parseIntegrityCandidate)
    .filter((candidate): candidate is { algorithm: AlgorithmIdentifier; expected: string } => Boolean(candidate))

  if (!candidates.length) {
    throw new Error('Invalid dashboard plugin asset integrity')
  }

  for (const candidate of candidates) {
    const digest = await crypto.subtle.digest(candidate.algorithm, bytes)

    if (arrayBufferToBase64(digest) === candidate.expected) {
      return
    }
  }

  throw new Error('Dashboard plugin asset integrity check failed')
}

function parseIntegrityCandidate(value: string): null | { algorithm: AlgorithmIdentifier; expected: string } {
  const separator = value.indexOf('-')

  if (separator <= 0) {
    return null
  }

  const algorithm = value.slice(0, separator).toLowerCase()
  const expected = value.slice(separator + 1)

  if (!expected) {
    return null
  }

  if (algorithm === 'sha256') {
    return { algorithm: 'SHA-256', expected }
  }

  if (algorithm === 'sha384') {
    return { algorithm: 'SHA-384', expected }
  }

  if (algorithm === 'sha512') {
    return { algorithm: 'SHA-512', expected }
  }

  return null
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''

  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }

  return btoa(binary)
}

function installDashboardPluginSdk(ownerId: string): () => void {
  const previousPlugins = window.__HERMES_PLUGINS__
  const previousSdk = window.__HERMES_PLUGIN_SDK__

  window.__HERMES_PLUGINS__ = {
    register: (name: string, component: PluginComponent) => registerDashboardComponent(ownerId, name, component),
    registerSlot: () => undefined
  }

  window.__HERMES_PLUGIN_SDK__ = {
    React,
    api: {},
    authedFetch: (url: string, init?: RequestInit) => pluginRawFetch(url, init, ownerId),
    buildWsAuthParam: async () => {
      const url = await window.hermesDesktop.getGatewayWsUrl(getApiRequestProfile())
      const parsed = new URL(url)
      const ticket = parsed.searchParams.get('ticket')
      const token = parsed.searchParams.get('token')

      return ticket ? ['ticket', ticket] : ['token', token ?? '']
    },
    buildWsUrl: async (path: string, params?: Record<string, string>) => {
      const { pluginId, suffix } = pluginEndpointParts(path)

      if (pluginId !== ownerId) {
        throw new Error(`Plugin ${ownerId} cannot access plugin namespace ${pluginId}`)
      }

      const url = await window.hermesDesktop.getPluginWsUrl(pluginId, suffix, getApiRequestProfile())
      const parsed = new URL(url)

      for (const [key, value] of Object.entries(params ?? {})) {
        parsed.searchParams.set(key, value)
      }

      return parsed.toString()
    },
    components: {
      Badge: ({
        children,
        className,
        variant: _variant,
        ...props
      }: React.ComponentProps<'span'> & { variant?: string }) => (
        <span className={cn('inline-flex items-center', className)} {...props}>
          {children}
        </span>
      ),
      Button,
      Card: ({ className, ...props }: React.ComponentProps<'div'>) => (
        <div className={cn('rounded-md border border-(--ui-stroke-tertiary)', className)} {...props} />
      ),
      CardContent: ({ className, ...props }: React.ComponentProps<'div'>) => (
        <div className={cn('p-3', className)} {...props} />
      ),
      CardHeader: ({ className, ...props }: React.ComponentProps<'div'>) => (
        <div className={cn('p-3', className)} {...props} />
      ),
      CardTitle: ({ className, ...props }: React.ComponentProps<'div'>) => (
        <div className={cn('text-sm font-medium', className)} {...props} />
      ),
      Checkbox,
      Dialog,
      DialogContent,
      DialogDescription,
      DialogFooter,
      DialogHeader,
      DialogTitle,
      Input,
      Label: (props: React.ComponentProps<'label'>) => <label {...props} />,
      SearchField,
      Loader,
      ErrorState,
      Select: ({
        children,
        className,
        onChange,
        onValueChange,
        ...props
      }: React.ComponentProps<'select'> & { onValueChange?: (value: string) => void }) => (
        <select
          className={cn('rounded border border-(--ui-stroke-tertiary) bg-transparent px-2 py-1 text-xs', className)}
          onChange={event => {
            onChange?.(event)
            onValueChange?.(event.target.value)
          }}
          {...props}
        >
          {children}
        </select>
      ),
      SelectOption: (props: React.ComponentProps<'option'>) => <option {...props} />,
      Separator: (props: React.ComponentProps<'hr'>) => <hr {...props} />,
      Tabs: (props: React.ComponentProps<'div'>) => <div {...props} />,
      TabsList: (props: React.ComponentProps<'div'>) => <div {...props} />,
      TabsTrigger: (props: React.ComponentProps<'button'>) => <button type="button" {...props} />,
      Textarea
    },
    fetchJSON: <T,>(url: string, init?: RequestInit) => pluginFetchJSON<T>(url, init, ownerId),
    hooks: {
      createContext: React.createContext,
      useCallback: React.useCallback,
      useContext: React.useContext,
      useEffect: React.useEffect,
      useMemo: React.useMemo,
      useRef: React.useRef,
      useSyncExternalStore: React.useSyncExternalStore,
      useState: React.useState
    },
    sdkVersion: '1.1.0-desktop',
    useDesktopProfile: () => {
      const snapshot = React.useSyncExternalStore(
        listener => {
          const unprofile = $activeGatewayProfile.subscribe(listener)
          const unscope = $profileScope.subscribe(listener)

          return () => {
            unprofile()
            unscope()
          }
        },
        () => {
          const profileScope = $profileScope.get()
          const activeProfile = normalizeProfileKey($activeGatewayProfile.get())

          return `${activeProfile}\n${profileScope}`
        },
        () => 'default\ndefault'
      )

      const [activeProfile, profileScope] = snapshot.split('\n')

      return React.useMemo(
        () => ({
          activeProfile,
          allProfiles: profileScope === ALL_PROFILES,
          profileScope
        }),
        [activeProfile, profileScope]
      )
    },
    useI18n,
    utils: { cn, isoTimeAgo: dashboardIsoTimeAgo, timeAgo: dashboardTimeAgo }
  }

  return () => {
    if (typeof previousPlugins === 'undefined') {
      delete window.__HERMES_PLUGINS__
    } else {
      window.__HERMES_PLUGINS__ = previousPlugins
    }

    if (typeof previousSdk === 'undefined') {
      delete window.__HERMES_PLUGIN_SDK__
    } else {
      window.__HERMES_PLUGIN_SDK__ = previousSdk
    }
  }
}

function pluginEndpointParts(path: string): { pluginId: string; suffix: string } {
  const normalized = path.startsWith('/') ? path : `/${path}`
  const match = normalized.match(/^\/api\/plugins\/([^/?#]+)(\/.*)?$/)

  if (!match) {
    throw new Error(`Plugin WebSocket path must target /api/plugins/<id>: ${path}`)
  }

  return { pluginId: decodeURIComponent(match[1]), suffix: match[2] || '/' }
}
