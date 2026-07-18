import { isValidPluginManifestId, normalizeDashboardPluginAssetPath } from '@hermes/shared'
import type { ReactNode } from 'react'

import { isReservedAppPath } from '@/app/routes'

import type { PluginContribution } from './plugin'

const ROUTE_RE = /^\/(?:[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?$/i

export interface DashboardPluginManifest {
  name: string
  label: string
  description?: string
  icon?: string
  version?: string
  tab: {
    path: string
    position?: string
    override?: string
    hidden?: boolean
  }
  entry: string
  css?: null | string
  has_api?: boolean
  integrity?: string
  source?: string
}

export interface DashboardManifestAdapter {
  contributions: PluginContribution[]
  manifest: DashboardPluginManifest
}

export function adaptDashboardManifest(value: unknown, render: () => ReactNode): DashboardManifestAdapter | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const candidate = value as Partial<DashboardPluginManifest>
  const tab = candidate.tab

  if (
    typeof candidate.name !== 'string' ||
    !isValidPluginManifestId(candidate.name) ||
    typeof candidate.label !== 'string' ||
    !candidate.label.trim() ||
    !tab ||
    typeof tab.path !== 'string' ||
    !ROUTE_RE.test(tab.path) ||
    typeof candidate.entry !== 'string' ||
    !candidate.entry.trim() ||
    unsafeAssetPath(candidate.entry) ||
    unsafeAssetPath(candidate.css)
  ) {
    return null
  }

  const overridePath =
    typeof tab.override === 'string' && tab.override.trim() && ROUTE_RE.test(tab.override.trim())
      ? tab.override.trim()
      : undefined
  const effectivePath = overridePath ?? tab.path

  if (overridePath && !isReservedAppPath(overridePath)) {
    return null
  }

  if (!overridePath && isReservedAppPath(tab.path)) {
    return null
  }

  const manifest: DashboardPluginManifest = {
    description: typeof candidate.description === 'string' ? candidate.description : undefined,
    entry: candidate.entry.trim(),
    has_api: Boolean(candidate.has_api),
    icon: typeof candidate.icon === 'string' ? candidate.icon : undefined,
    integrity:
      typeof candidate.integrity === 'string' && candidate.integrity.trim() ? candidate.integrity.trim() : undefined,
    label: candidate.label.trim(),
    name: candidate.name,
    source: typeof candidate.source === 'string' ? candidate.source : undefined,
    tab: {
      hidden: tab.hidden === true,
      path: tab.path,
      ...(overridePath ? { override: overridePath } : {}),
      ...(typeof tab.position === 'string' && tab.position.trim() ? { position: tab.position.trim() } : {})
    },
    version: typeof candidate.version === 'string' ? candidate.version : undefined,
    css: typeof candidate.css === 'string' && candidate.css.trim() ? candidate.css.trim() : null
  }

  const contributions: PluginContribution[] = [
    {
      area: 'routes',
      data: { override: Boolean(overridePath), path: effectivePath },
      id: 'route',
      render,
      title: manifest.label
    }
  ]

  if (manifest.tab.hidden !== true) {
    contributions.push({
      area: 'sidebar.nav',
      data: {
        codicon: normalizeCodicon(manifest.icon),
        label: manifest.label,
        override: overridePath,
        path: effectivePath,
        position: manifest.tab.position
      },
      id: 'nav',
      order: orderForPosition(manifest.tab.position)
    })
  }

  return { contributions, manifest }
}

function orderForPosition(position: string | undefined): number {
  if (!position || position === 'end') {
    return 1_000
  }

  if (position.startsWith('before:')) {
    return 100
  }

  if (position.startsWith('after:')) {
    return 500
  }

  return 1_000
}

export function manifestPaths(values: readonly unknown[]): string[] {
  return values.flatMap(value => {
    // Route reservation must use the same complete contract as registration.
    // Otherwise a malformed manifest can reserve a path that never receives a
    // route contribution, leaving the pathname in a non-session limbo.
    const manifest = adaptDashboardManifest(value, () => null)?.manifest

    return manifest ? [manifest.tab.override ?? manifest.tab.path] : []
  })
}

function unsafeAssetPath(value: unknown): boolean {
  if (value == null || value === '') {
    return false
  }

  if (typeof value !== 'string') {
    return true
  }

  try {
    normalizeDashboardPluginAssetPath(value)

    return false
  } catch {
    return true
  }
}

function normalizeCodicon(icon: unknown): string {
  if (typeof icon !== 'string' || !icon.trim()) {
    return 'plug'
  }

  return icon
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[^a-z0-9-]/gi, '-')
    .replace(/-+/g, '-')
    .toLowerCase()
}
