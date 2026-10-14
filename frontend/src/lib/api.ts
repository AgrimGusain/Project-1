// Minimal typed REST client. The backend URL and token are runtime settings
// (Settings page) so the frontend can be deployed separately from the Pi.

const LS_BASE = "rover.backendUrl";
const LS_TOKEN = "rover.token";

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string | null) {
  try {
    if (value === null || value === "") localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: settings last for this page load only */
  }
}

let memoryBase: string | null = null;
let memoryToken: string | null = null;

export function backendUrl(): string {
  const v = memoryBase ?? safeGet(LS_BASE) ?? (import.meta.env.VITE_BACKEND_URL as string | undefined) ?? "";
  return v.replace(/\/+$/, "");
}

export function authToken(): string | null {
  return memoryToken ?? safeGet(LS_TOKEN);
}

export function setConnection(url: string, token: string | null) {
  memoryBase = url.trim().replace(/\/+$/, "");
  memoryToken = token?.trim() || null;
  safeSet(LS_BASE, memoryBase);
  safeSet(LS_TOKEN, memoryToken);
}

export function wsUrl(path: string): string {
  const base = backendUrl();
  let url: string;
  if (base) url = base.replace(/^http/, "ws") + path;
  else url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
  const token = authToken();
  return token ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : url;
}

export function httpUrl(path: string, withToken = false): string {
  const url = backendUrl() + path;
  const token = authToken();
  return withToken && token ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : url;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
  }
  /** Extra detail lines (failed checks, plan issues, validation fields). */
  get details(): string[] {
    const b = this.body;
    const out: string[] = [];
    if (Array.isArray(b.details)) out.push(...(b.details as string[]).map(String));
    if (Array.isArray(b.issues)) out.push(...(b.issues as { message: string }[]).map((i) => i.message));
    if (Array.isArray(b.checks))
      out.push(...(b.checks as { passed: boolean; required: boolean; label: string; message: string }[])
        .filter((c) => c.required && !c.passed).map((c) => `${c.label}: ${c.message}`));
    if (Array.isArray(b.fields))
      out.push(...(b.fields as { loc: unknown[]; msg: string }[]).map((f) => `${f.loc.slice(1).join(".")}: ${f.msg}`));
    return out;
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = authToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  let res: Response;
  try {
    res = await fetch(backendUrl() + path, { ...init, headers, body });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the rover backend");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const err = (data as { error?: { code: string; message: string } })?.error;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `HTTP ${res.status}`, (err ?? {}) as Record<string, unknown>);
  }
  return data as T;
}

export const post = <T = unknown>(path: string, json?: unknown) => api<T>(path, { method: "POST", json });
