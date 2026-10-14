import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { CapabilityStatus } from "../lib/types";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Panel({ title, actions, children, className, bodyClass }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string;
}) {
  return (
    <section className={cx("panel", className)}>
      {(title || actions) && (
        <header className="panel-head">
          <h2 className="panel-title">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx("panel-body", bodyClass)}>{children}</div>
    </section>
  );
}

type Variant = "default" | "primary" | "danger" | "ghost" | "ok";

// Literal class names so Tailwind keeps them (dynamic `btn-${variant}` would be purged).
const VARIANT: Record<Variant, string> = {
  default: "btn-default", primary: "btn-primary", danger: "btn-danger", ghost: "btn-ghost", ok: "btn-ok",
};

export function Button({ variant = "default", className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type="button" {...rest} className={cx("btn", VARIANT[variant], className)} />;
}

export type Tone = "ok" | "warn" | "bad" | "info" | "sim" | "muted";

const TONE: Record<Tone, string> = {
  ok: "border-ok/40 bg-ok/10 text-ok",
  warn: "border-warn/40 bg-warn/10 text-warn",
  bad: "border-bad/50 bg-bad/10 text-bad",
  info: "border-info/40 bg-info/10 text-info",
  sim: "border-sim/40 bg-sim/10 text-sim",
  muted: "border-ink-500 bg-ink-700/60 text-fg-muted",
};

const DOT: Record<Tone, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", info: "bg-info", sim: "bg-sim", muted: "bg-fg-faint" };

export function Chip({ tone = "muted", children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return <span title={title} className={cx("chip", TONE[tone], className)}>{children}</span>;
}

export function Dot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2 w-2">
      {pulse && <span className={cx("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", DOT[tone])} />}
      <span className={cx("relative inline-flex h-2 w-2 rounded-full", DOT[tone])} />
    </span>
  );
}

export const CAP_TONE: Record<CapabilityStatus, Tone> = {
  ready: "ok", simulated: "sim", degraded: "warn", unavailable: "bad", not_installed: "muted",
};

export const CAP_LABEL: Record<CapabilityStatus, string> = {
  ready: "Ready", simulated: "Simulated", degraded: "Degraded", unavailable: "Unavailable", not_installed: "Not installed",
};

export function Notice({ tone = "info", title, children, className }: { tone?: Tone; title?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <div className={cx("rounded-lg border px-3 py-2.5 text-sm", TONE[tone], className)} role={tone === "bad" ? "alert" : undefined}>
      {title && <div className="font-semibold">{title}</div>}
      {children && <div className={cx("text-fg/90", title ? "mt-0.5" : "")}>{children}</div>}
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  const color = tone ? { ok: "text-ok", warn: "text-warn", bad: "text-bad", info: "text-info", sim: "text-sim", muted: "text-fg-muted" }[tone] : "text-fg";
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wider text-fg-muted">{label}</div>
      <div className={cx("mt-0.5 truncate font-mono text-xl font-semibold", color)}>{value}</div>
      {sub && <div className="truncate text-xs text-fg-faint">{sub}</div>}
    </div>
  );
}

export function Field({ label, hint, children, className }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cx("block", className)}>
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-fg-faint">{hint}</span>}
    </label>
  );
}

export function NumberInput({ value, onChange, min, max, step, disabled, ariaLabel }: {
  value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; disabled?: boolean; ariaLabel?: string;
}) {
  return (
    <input
      type="number"
      inputMode="decimal"
      className="input font-mono"
      value={Number.isFinite(value) ? value : ""}
      min={min}
      max={max}
      step={step ?? "any"}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(e) => onChange(e.target.value === "" ? Number.NaN : Number(e.target.value))}
    />
  );
}

export function Select<T extends string>({ value, onChange, options, disabled, ariaLabel }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; disabled?: boolean; ariaLabel?: string;
}) {
  return (
    <select className="input" value={value} disabled={disabled} aria-label={ariaLabel} onChange={(e) => onChange(e.target.value as T)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

export function Segmented<T extends string>({ value, onChange, options, ariaLabel }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; ariaLabel?: string;
}) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className="inline-flex rounded-lg border border-ink-600 bg-ink-900 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-md px-3 py-1.5 text-xs font-semibold transition",
            value === o.value ? "bg-ink-600 text-fg" : "text-fg-muted hover:text-fg",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-ink-600 px-4 py-6 text-center text-sm text-fg-faint">{children}</div>;
}

export function ErrorList({ title, lines }: { title: string; lines?: string[] }) {
  return (
    <Notice tone="bad" title={title}>
      {lines && lines.length > 0 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
          {lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
    </Notice>
  );
}

export function Meter({ value, max = 1, tone = "info", label }: { value: number; max?: number; tone?: Tone; label?: string }) {
  const pct = Math.max(0, Math.min(100, (Math.abs(value) / max) * 100));
  const bar = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", info: "bg-info", sim: "bg-sim", muted: "bg-fg-faint" }[tone];
  return (
    <div aria-label={label} role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max} className="h-1.5 w-full overflow-hidden rounded-full bg-ink-700">
      <div className={cx("h-full rounded-full transition-[width] duration-200", bar)} style={{ width: `${pct}%` }} />
    </div>
  );
}
