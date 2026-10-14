import { useState } from "react";
import { post } from "../lib/api";
import type { Capability, Fault, LocalizationSnapshot, LogEvent, MagneticHeading, SensorStatus } from "../lib/types";
import { fmtNum, fmtTime } from "../lib/units";
import { CAP_LABEL, CAP_TONE, Chip, Empty, Meter, Notice, cx, type Tone } from "./ui";

// ------------------------------------------------------------------ capabilities

export function CapabilityList({ caps, compact = false }: { caps: Capability[]; compact?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <ul className={cx("divide-y divide-ink-700/70", compact && "text-sm")}>
      {caps.map((c) => (
        <li key={c.id}>
          <button type="button" className="flex w-full items-start gap-3 py-2 text-left" onClick={() => setOpen(open === c.id ? null : c.id)}
            aria-expanded={open === c.id}>
            <Chip tone={CAP_TONE[c.status]} className="mt-0.5 w-[104px] shrink-0 justify-center">{CAP_LABEL[c.status]}</Chip>
            <span className="min-w-0 flex-1">
              <span className="block font-medium">{c.label}</span>
              <span className="block truncate text-xs text-fg-muted">{c.summary}</span>
            </span>
            {c.reasons.length > 0 && <span className="text-xs text-fg-faint">{open === c.id ? "−" : `+${c.reasons.length}`}</span>}
          </button>
          {open === c.id && c.reasons.length > 0 && (
            <ul className="mb-2 ml-[116px] list-disc space-y-0.5 pl-4 text-xs text-fg-muted">
              {c.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ sensors

const STATE_TONE: Record<SensorStatus["state"], Tone> = {
  connected: "ok", initializing: "info", configured: "muted", disabled: "muted", disconnected: "bad", faulty: "bad",
};
const HEALTH_TONE: Record<SensorStatus["reading_health"], Tone> = { ok: "ok", invalid: "warn", stale: "warn", none: "muted" };

export function SensorBadges({ s }: { s: SensorStatus }) {
  return (
    <div className="flex flex-wrap gap-1">
      <Chip tone={STATE_TONE[s.state]}>{s.state}</Chip>
      {s.enabled && s.state === "connected" && <Chip tone={HEALTH_TONE[s.reading_health]}>{s.reading_health === "ok" ? "valid" : s.reading_health}</Chip>}
      {s.simulated && <Chip tone="sim">simulated</Chip>}
      {!s.simulated && s.enabled && !s.physically_verified && <Chip tone="muted" title="Driver not yet verified on the physical device">unverified driver</Chip>}
      {s.required_for_arming && <Chip tone="info">required</Chip>}
    </div>
  );
}

function usable(s: SensorStatus) {
  return s.state === "connected" && s.reading_health === "ok" && s.last_reading?.valid;
}

function UltrasonicValue({ s, stopDistance }: { s: SensorStatus; stopDistance?: number }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  if (r.value.beyond_range) return <BigValue value="Clear" sub="nothing within range" tone="ok" />;
  const d = r.value.distance as number;
  const tone: Tone = stopDistance !== undefined && d <= stopDistance ? "bad" : d < 0.6 ? "warn" : "ok";
  return (
    <div>
      <BigValue value={`${d.toFixed(2)} m`} sub={tone === "bad" ? "obstacle — forward blocked" : "distance"} tone={tone} />
      <Meter value={Math.min(d, 4)} max={4} tone={tone} label="distance" />
    </div>
  );
}

function IrValue({ s }: { s: SensorStatus }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  const obstacle = Boolean(r.value.obstacle);
  return <BigValue value={obstacle ? "Obstacle" : "Clear"} sub={`raw level ${r.value.raw_level}`} tone={obstacle ? "bad" : "ok"} />;
}

export function MagnetometerValue({ s, heading }: { s: SensorStatus; heading?: MagneticHeading }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  const axes = ["x", "y", "z"] as const;
  const cal = s.calibration?.state;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-2">
        {axes.map((a) => {
          const v = r.value[a] as number;
          return (
            <div key={a} className="rounded-md bg-ink-900 px-2 py-1.5">
              <div className="text-[10px] uppercase text-fg-faint">B{a}</div>
              <div className="font-mono text-sm">{v.toFixed(1)}</div>
              <div className="relative mt-1 h-1 rounded bg-ink-700">
                <div className={cx("absolute top-0 h-1 rounded", v >= 0 ? "left-1/2 bg-info" : "right-1/2 bg-sim")}
                  style={{ width: `${Math.min(50, (Math.abs(v) / 80) * 50)}%` }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="flex items-center justify-between text-xs text-fg-muted">
        <span>|B| {fmtNum(r.value.magnitude as number, 1)} µT · body frame</span>
        <Chip tone={cal === "calibrated" ? "ok" : "warn"}>{cal ?? "unknown"}</Chip>
      </div>
      {heading && (
        <div className="rounded-md border border-ink-600 px-2 py-1.5 text-xs">
          <span className="text-fg-muted">Magnetic heading: </span>
          {heading.reliable ? <span className="font-mono">{heading.heading_deg?.toFixed(1)}°</span> : <span className="text-warn">unavailable / unreliable</span>}
          {!heading.reliable && <ul className="mt-1 list-disc pl-4 text-fg-faint">{heading.reasons.map((x, i) => <li key={i}>{x}</li>)}</ul>}
          <div className="mt-1 text-fg-faint">Diagnostic only — not used for navigation.</div>
        </div>
      )}
    </div>
  );
}

function GenericValue({ s }: { s: SensorStatus }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
      {Object.entries(r.value).map(([k, v]) => (
        <div key={k} className="kv">
          <dt>{k}</dt>
          <dd>{typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : String(v)} <span className="text-fg-faint">{r.units[k] ?? ""}</span></dd>
        </div>
      ))}
    </dl>
  );
}

function BatteryValue({ s }: { s: SensorStatus }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  return <BigValue value={`${(r.value.voltage as number).toFixed(2)} V`} sub={`${(r.value.current as number).toFixed(2)} A`} tone="info" />;
}

function TemperatureValue({ s }: { s: SensorStatus }) {
  const r = s.last_reading;
  if (!usable(s) || !r) return <ProblemValue s={s} />;
  const t = r.value.temperature as number;
  return <BigValue value={`${t.toFixed(1)} °C`} tone={t > 80 ? "bad" : t > 70 ? "warn" : "ok"} />;
}

function ProblemValue({ s }: { s: SensorStatus }) {
  const msg =
    s.state === "disabled" ? "Disabled in configuration (not installed)" :
    s.state === "faulty" ? "Failed to initialise" :
    s.state === "disconnected" ? "Disconnected — retrying" :
    s.state === "initializing" || s.state === "configured" ? "Starting…" :
    s.reading_health === "stale" ? "Measurement stale — no recent data" :
    s.reading_health === "invalid" ? `Measurement invalid (${s.last_reading?.error ?? s.error ?? "unknown"})` :
    "No reading";
  const tone: Tone = s.state === "disabled" ? "muted" : s.state === "initializing" ? "info" : "warn";
  return (
    <div>
      <BigValue value={s.state === "disabled" ? "—" : "No valid data"} tone={tone} />
      <div className="text-xs text-fg-muted">{msg}</div>
      {s.error && s.state !== "disabled" && <div className="mt-1 break-words font-mono text-[11px] text-fg-faint">{s.error}</div>}
    </div>
  );
}

function BigValue({ value, sub, tone = "muted" }: { value: string; sub?: string; tone?: Tone }) {
  const color = { ok: "text-ok", warn: "text-warn", bad: "text-bad", info: "text-info", sim: "text-sim", muted: "text-fg-muted" }[tone];
  return (
    <div className="mb-1.5">
      <div className={cx("font-mono text-2xl font-semibold", color)}>{value}</div>
      {sub && <div className="text-xs text-fg-muted">{sub}</div>}
    </div>
  );
}

export function SensorCard({ s, stopDistance, heading }: { s: SensorStatus; stopDistance?: number; heading?: MagneticHeading }) {
  let body;
  switch (s.type) {
    case "ultrasonic": body = <UltrasonicValue s={s} stopDistance={stopDistance} />; break;
    case "ir_obstacle": body = <IrValue s={s} />; break;
    case "magnetometer": body = <MagnetometerValue s={s} heading={heading} />; break;
    case "battery": body = <BatteryValue s={s} />; break;
    case "temperature": body = <TemperatureValue s={s} />; break;
    default: body = <GenericValue s={s} />;
  }
  return (
    <div className={cx("panel p-3", s.state === "disabled" && "opacity-70")}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-semibold">{s.label}</div>
          <div className="truncate font-mono text-[11px] text-fg-faint">{s.id} · {s.type} · {s.driver}{s.safety_role && s.safety_role !== "none" ? ` · guards ${s.safety_role}` : ""}</div>
        </div>
      </div>
      <SensorBadges s={s} />
      <div className="mt-3">{body}</div>
      {s.enabled && s.age_s !== null && (
        <div className="mt-2 text-[11px] text-fg-faint">updated {s.age_s.toFixed(1)} s ago · {s.rate_hz} Hz · stale after {s.stale_after_s.toFixed(1)} s</div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ localization

export function LocalizationCard({ loc }: { loc: LocalizationSnapshot }) {
  const e = loc.estimate;
  const tone: Tone = e.status === "ok" ? (e.simulated ? "sim" : "ok") : e.status === "unavailable" ? "muted" : "warn";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={tone}>{e.status.replace("_", " ")}</Chip>
        <Chip>{e.provider}</Chip>
        {e.simulated && <Chip tone="sim">simulated</Chip>}
        {e.experimental && <Chip tone="warn">experimental</Chip>}
      </div>
      <p className="text-sm text-fg-muted">{e.message}</p>
      <dl>
        <div className="kv"><dt>Position</dt><dd>{e.position_valid ? (e.lat !== null ? `${e.lat.toFixed(7)}, ${e.lon?.toFixed(7)}` : `x ${fmtNum(e.x)} · y ${fmtNum(e.y)} m`) : <span className="text-fg-faint">unavailable</span>}</dd></div>
        <div className="kv"><dt>Accuracy</dt><dd>{e.horizontal_accuracy_m !== null ? `±${e.horizontal_accuracy_m.toFixed(2)} m` : "—"}{e.accuracy_source ? <span className="text-fg-faint"> ({e.accuracy_source})</span> : null}</dd></div>
        <div className="kv"><dt>Heading</dt><dd>{e.heading_valid ? `${e.heading_deg?.toFixed(1)}° (${e.heading_source})` : <span className="text-fg-faint">unavailable</span>}</dd></div>
        {e.kind === "geodetic" && (
          <>
            <div className="kv"><dt>Fix / sats / HDOP</dt><dd>{e.fix_quality ?? "—"} / {e.satellites ?? "—"} / {fmtNum(e.hdop, 1)}</dd></div>
            <div className="kv"><dt>Fix age</dt><dd>{e.age_s !== null ? `${e.age_s.toFixed(1)} s` : "—"}</dd></div>
          </>
        )}
      </dl>
      {e.kind === "none" && (
        <Notice tone="warn">Without a position source the rover cannot verify where it is. Grid missions execute open-loop and coverage is not verified.</Notice>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ faults & events

const SEV_TONE: Record<string, Tone> = { info: "info", warning: "warn", error: "bad", critical: "bad" };

export function FaultList({ faults }: { faults: Fault[] }) {
  if (!faults.length) return <Empty>No active faults</Empty>;
  return (
    <ul className="space-y-2">
      {faults.map((f) => (
        <li key={f.code} className="flex items-start gap-2 rounded-lg border border-ink-600 bg-ink-900 p-2.5">
          <Chip tone={SEV_TONE[f.severity]} className="shrink-0">{f.severity}</Chip>
          <div className="min-w-0 flex-1">
            <div className="text-sm">{f.message}</div>
            <div className="font-mono text-[11px] text-fg-faint">
              {f.code} · {f.source} · since {fmtTime(f.first_seen)}{f.count > 1 ? ` · ×${f.count}` : ""}
              {f.blocks_motion ? " · blocks motion" : f.blocks_arming ? " · blocks arming" : ""}
            </div>
          </div>
          <button type="button" className="btn btn-ghost px-2 py-1 text-xs" onClick={() => post(`/api/faults/${encodeURIComponent(f.code)}/clear`).catch(() => undefined)}>
            Acknowledge
          </button>
        </li>
      ))}
    </ul>
  );
}

export function EventLog({ events, limit = 60 }: { events: LogEvent[]; limit?: number }) {
  if (!events.length) return <Empty>No events yet</Empty>;
  return (
    <ol className="scrollbar-thin max-h-80 space-y-1 overflow-y-auto pr-1 font-mono text-xs">
      {events.slice(0, limit).map((e) => (
        <li key={e.id} className="flex gap-2">
          <span className="shrink-0 text-fg-faint">{fmtTime(e.ts)}</span>
          <span className={cx("shrink-0 uppercase", { info: "text-info", warning: "text-warn", error: "text-bad", critical: "text-bad font-bold" }[e.level])}>
            {e.level.slice(0, 4)}
          </span>
          <span className="min-w-0 break-words text-fg/90">{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
