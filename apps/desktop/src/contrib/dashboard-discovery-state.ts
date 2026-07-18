import { atom } from 'nanostores'

export type DashboardPluginDiscoveryPhase = 'failed' | 'pending' | 'resolved'

export interface DashboardPluginDiscoveryState {
  candidatePaths: readonly string[]
  generation: number
  phase: DashboardPluginDiscoveryPhase
  reservedPaths: readonly string[]
  pendingPath: string | null
}

// Seed cold-route candidates from bundled dashboard manifests themselves. This
// keeps bundled plugin routes out of chat-session parsing before backend
// discovery resolves without hardcoding a plugin name or route in Desktop.
const bundledDashboardManifests = import.meta.glob<{ default: unknown }>(
  '../../../../plugins/*/dashboard/manifest.json',
  { eager: true }
)
const DEFAULT_DASHBOARD_PLUGIN_CANDIDATE_PATHS = bundledDashboardCandidatePaths(
  Object.values(bundledDashboardManifests).map(module => module.default)
)

export const $dashboardPluginDiscovery = atom<DashboardPluginDiscoveryState>({
  candidatePaths: DEFAULT_DASHBOARD_PLUGIN_CANDIDATE_PATHS,
  generation: 0,
  phase: 'pending',
  pendingPath: null,
  reservedPaths: []
})

export function dashboardPluginDiscoveryPending(): boolean {
  return $dashboardPluginDiscovery.get().phase === 'pending'
}

export function isDashboardPluginPathReserved(pathname: string): boolean {
  return $dashboardPluginDiscovery.get().reservedPaths.includes(pathname)
}

export function isDashboardPluginPathCandidate(pathname: string): boolean {
  const discovery = $dashboardPluginDiscovery.get()

  return (
    discovery.candidatePaths.includes(pathname) ||
    discovery.reservedPaths.includes(pathname) ||
    discovery.pendingPath === pathname
  )
}

export function dashboardPluginPendingPath(): string | null {
  return $dashboardPluginDiscovery.get().pendingPath
}

export function defaultDashboardPluginCandidatePaths(): readonly string[] {
  return DEFAULT_DASHBOARD_PLUGIN_CANDIDATE_PATHS
}

export function bundledDashboardCandidatePaths(manifests: readonly unknown[]): string[] {
  const paths = manifests.flatMap(value => {
    if (!value || typeof value !== 'object') {
      return []
    }

    const tab = (value as { tab?: unknown }).tab

    if (!tab || typeof tab !== 'object') {
      return []
    }

    const { override, path } = tab as { override?: unknown; path?: unknown }

    return [override, path].filter(
      (candidate): candidate is string =>
        typeof candidate === 'string' && /^\/(?:[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?$/i.test(candidate)
    )
  })

  return Array.from(new Set(paths))
}

export function setDashboardPluginPendingPath(pathname: string | null): void {
  const current = $dashboardPluginDiscovery.get()
  const next = pathname && pathname.length > 0 ? pathname : null

  if (current.pendingPath === next) {
    return
  }

  $dashboardPluginDiscovery.set({ ...current, pendingPath: next })
}
