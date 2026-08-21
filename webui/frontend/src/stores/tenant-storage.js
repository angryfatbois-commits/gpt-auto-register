export function tenantStorageKey(baseKey, userId) {
  return `${baseKey}:${userId || 'anonymous'}`
}

function legacyOwnerKey(baseKey) {
  return `${baseKey}:legacy-owner`
}

/**
 * Read browser state for one authenticated user.
 *
 * Before authentication existed, settings used an unscoped key. The first
 * administrator who needs that value claims it; an owner marker prevents a
 * later administrator in the same browser profile from inheriting it too.
 */
export function readTenantStorage(storage, baseKey, user) {
  if (!storage || !user?.id) return null
  const tenantKey = tenantStorageKey(baseKey, user.id)
  try {
    const current = storage.getItem(tenantKey)
    if (current !== null) return current
    if (user.role !== 'admin') return null

    const legacy = storage.getItem(baseKey)
    if (legacy === null) return null
    const ownerKey = legacyOwnerKey(baseKey)
    const owner = storage.getItem(ownerKey)
    if (owner && owner !== user.id) return null

    // Claim before copying. If storage is full, failing closed avoids exposing
    // the same legacy settings to multiple administrators.
    storage.setItem(ownerKey, user.id)
    storage.setItem(tenantKey, legacy)
    return legacy
  } catch (_) {
    return null
  }
}
