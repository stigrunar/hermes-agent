import { describe, expect, it } from 'vitest'

import { dashboardPluginAssetBackendPath, normalizeDashboardPluginAssetPath } from './dashboard-plugin-assets'

describe('dashboard plugin asset IPC paths', () => {
  it('builds backend paths under the manifest asset namespace', () => {
    expect(dashboardPluginAssetBackendPath('kanban', 'dist/index.js')).toBe('/dashboard-plugins/kanban/dist/index.js')
  })

  it('rejects schemes, traversal, encoded traversal, fragments, and cross-manifest ids', () => {
    expect(() => normalizeDashboardPluginAssetPath('https://example.test/dist/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('/dashboard-plugins/kanban/dist/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('../other/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/%2e%2e/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/%252e%252e/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/index.js#frag')).toThrow(/Invalid/)
    expect(() => normalizeDashboardPluginAssetPath('dist/index.js?cache=1')).toThrow(/Invalid/)
    expect(() => dashboardPluginAssetBackendPath('kanban/other', 'dist/index.js')).toThrow(/manifest id/)
  })
})
