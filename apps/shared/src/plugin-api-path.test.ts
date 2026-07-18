import { describe, expect, it } from 'vitest'

import {
  buildDashboardPluginAssetPath,
  buildPluginApiPath,
  isValidPluginManifestId,
  normalizeDashboardPluginAssetPath,
  normalizePluginRelativePath
} from './plugin-api-path'

describe('plugin API namespace paths', () => {
  it('builds paths under the owning plugin namespace', () => {
    expect(buildPluginApiPath('kanban', '/board?archived=1')).toBe('/api/plugins/kanban/board?archived=1')
    expect(buildPluginApiPath('kanban', 'tasks')).toBe('/api/plugins/kanban/tasks')
  })

  it('rejects malformed manifest ids', () => {
    expect(isValidPluginManifestId('kanban')).toBe(true)
    expect(isValidPluginManifestId('../kanban')).toBe(false)
    expect(isValidPluginManifestId('kanban/other')).toBe(false)
    expect(() => buildPluginApiPath('kanban/other', '/board')).toThrow(/manifest id/)
  })

  it('rejects traversal, encoded traversal, schemes, and fragments', () => {
    expect(() => normalizePluginRelativePath('/../other')).toThrow(/Unsafe/)
    expect(() => normalizePluginRelativePath('/%2e%2e/other')).toThrow(/Unsafe/)
    expect(() => normalizePluginRelativePath('/%252e%252e/other')).toThrow(/Unsafe/)
    expect(() => normalizePluginRelativePath('https://example.test/x')).toThrow(/Unsafe/)
    expect(() => normalizePluginRelativePath('/board#secret')).toThrow(/Invalid/)
  })

  it('builds dashboard asset paths under the owning manifest', () => {
    expect(buildDashboardPluginAssetPath('kanban', 'dist/index.js')).toBe('/dashboard-plugins/kanban/dist/index.js')
  })

  it('rejects unsafe dashboard asset paths', () => {
    expect(() => normalizeDashboardPluginAssetPath('/dist/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('https://example.test/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('../index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/%2e%2e/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/%252e%252e/index.js')).toThrow(/Unsafe/)
    expect(() => normalizeDashboardPluginAssetPath('dist/index.js?x=1')).toThrow(/Invalid/)
    expect(() => normalizeDashboardPluginAssetPath('dist/index.js#x')).toThrow(/Invalid/)
    expect(() => buildDashboardPluginAssetPath('kanban/other', 'dist/index.js')).toThrow(/manifest id/)
  })
})
