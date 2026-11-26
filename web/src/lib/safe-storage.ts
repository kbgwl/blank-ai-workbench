const memory = new Map<string, string>();

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function storage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readString(key: string): string {
  const store = storage();
  if (!store) return memory.get(key) ?? "";
  try {
    return store.getItem(key) ?? memory.get(key) ?? "";
  } catch {
    return memory.get(key) ?? "";
  }
}

export function writeString(key: string, value: string): void {
  memory.set(key, value);
  const store = storage();
  if (!store) return;
  try {
    store.setItem(key, value);
  } catch {
    return;
  }
}

export function removeString(key: string): void {
  memory.delete(key);
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(key);
  } catch {
    return;
  }
}

export function readJson<T>(key: string, fallback: T): T {
  const raw = readString(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    removeString(key);
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  writeString(key, JSON.stringify(value));
}
