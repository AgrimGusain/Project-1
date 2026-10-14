export type Unit = "m" | "cm" | "mm";

export const UNIT_SCALE: Record<Unit, number> = { m: 1, cm: 0.01, mm: 0.001 };

export function toMetres(value: number, unit: Unit): number {
  return value * UNIT_SCALE[unit];
}

export function fromMetres(metres: number, unit: Unit): number {
  return metres / UNIT_SCALE[unit];
}

/** Convert a value between units, rounded to avoid floating noise in inputs. */
export function convert(value: number, from: Unit, to: Unit): number {
  const out = fromMetres(toMetres(value, from), to);
  return Math.round(out * 1e6) / 1e6;
}

export function fmtDistance(m: number | null | undefined, digits = 2): string {
  if (m === null || m === undefined || !Number.isFinite(m)) return "—";
  if (Math.abs(m) >= 1000) return `${(m / 1000).toFixed(2)} km`;
  if (Math.abs(m) < 1) return `${(m * 100).toFixed(digits === 2 ? 0 : 1)} cm`;
  return `${m.toFixed(digits)} m`;
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

export function fmtNum(v: number | null | undefined, digits = 2, suffix = ""): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${v.toFixed(digits)}${suffix}`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
