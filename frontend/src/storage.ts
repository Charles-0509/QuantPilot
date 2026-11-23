// localStorage can be missing or throw (blocked storage, private mode); UI prefs must never break a page.
export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = globalThis.localStorage?.getItem(key)
    return raw ? JSON.parse(raw) as T : fallback
  } catch {
    return fallback
  }
}

export function writeJson(key: string, value: unknown) {
  try { globalThis.localStorage?.setItem(key, JSON.stringify(value)) } catch { /* preference not persisted */ }
}
