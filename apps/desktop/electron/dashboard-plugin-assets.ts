const PLUGIN_MANIFEST_ID_RE = /^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/
const URI_SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i

function dashboardPluginAssetBackendPath(manifestId, assetPath) {
  if (typeof manifestId !== 'string' || !PLUGIN_MANIFEST_ID_RE.test(manifestId)) {
    throw new Error('Invalid plugin manifest id')
  }

  const normalized = normalizeDashboardPluginAssetPath(assetPath)

  const encodedAsset = normalized
    .split('/')
    .map(segment => encodeURIComponent(segment))
    .join('/')

  return `/dashboard-plugins/${manifestId}/${encodedAsset}`
}

function normalizeDashboardPluginAssetPath(assetPath) {
  if (
    typeof assetPath !== 'string' ||
    !assetPath.trim() ||
    assetPath.includes('?') ||
    assetPath.includes('#')
  ) {
    throw new Error('Invalid dashboard plugin asset path')
  }

  const trimmed = assetPath.trim()

  if (URI_SCHEME_RE.test(trimmed) || trimmed.startsWith('/') || trimmed.startsWith('//')) {
    throw new Error('Unsafe dashboard plugin asset path')
  }

  let decoded = trimmed

  for (let pass = 0; pass < 2; pass += 1) {
    assertSafeAssetPath(decoded)

    try {
      decoded = decodeURIComponent(decoded)
    } catch {
      throw new Error('Invalid percent-encoding in dashboard plugin asset path')
    }
  }

  assertSafeAssetPath(decoded)

  return trimmed
}

function assertSafeAssetPath(pathname) {
  if (
    URI_SCHEME_RE.test(pathname) ||
    pathname.includes('\\') ||
    pathname
      .split('/')
      .some(segment => !segment || segment === '.' || segment === '..')
  ) {
    throw new Error('Unsafe dashboard plugin asset path')
  }
}

export { dashboardPluginAssetBackendPath, normalizeDashboardPluginAssetPath }
