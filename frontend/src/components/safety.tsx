import { useState } from "react";
import { ApiError, post } from "../lib/api";
import type { OperatingMode, PreflightCheck } from "../lib/types";
import { useTelemetry } from "../state/telemetry";
import { Button, Chip, ErrorList, cx } from "./ui";

export const MODE_META: Record<OperatingMode, { label: string; short: string; tone: string; desc: string }> = {
  simulation: {
    label: "SIMULATION",
    short: "SIM",
    tone: "bg-sim/15 text-sim border-sim/40",
    desc: "No physical motors. All simulated devices are labelled.",
  },
  hardware: {
    label: "REAL HARDWARE · NO GPS",
    short: "HW",
    tone: "bg-amber/15 text-amber border-amber/50",
    desc: "Physical motors. No position source: missions run open-loop.",
  },
  hardware_gps: {
    label: "REAL HARDWARE · GPS",
    short: "HW+GPS",
    tone: "bg-info/15 text-info border-info/50",
    desc: "Physical motors with a GNSS receiver.",
  },
};

export function ModeBadge({ compact = false }: { compact?: boolean }) {
  const { telemetry, system } = useTelemetry();
  const mode = telemetry?.mode ?? system?.mode;
  if (!mode) return <Chip>Unknown mode</Chip>;
  const m = MODE_META[mode];
  return (
    <span title={m.desc} className={cx("chip whitespace-nowrap", m.tone)}>
      {compact ? m.short : m.label}
    </span>
  );
}

/** Always-visible emergency stop. Uses REST so it works without a control session. */
export function EStopButton({ large = false, onControlWs }: { large?: boolean; onControlWs?: () => boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fire = async () => {
    setBusy(true);
    setErr(null);
    onControlWs?.(); // fastest path when a control socket is open
    try {
      await post("/api/safety/estop", { reason: "operator e-stop (UI)" });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "E-stop request failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-stretch">
      <button
        type="button"
        onClick={fire}
        disabled={busy}
        aria-label="Emergency stop"
        className={cx(
          "select-none rounded-xl border-2 border-red-300/60 bg-gradient-to-b from-[#ff4a4f] to-[#c81e24] font-black uppercase tracking-widest text-white",
          "shadow-[0_0_0_3px_rgba(255,90,95,0.25),0_10px_24px_-8px_rgba(255,40,40,0.6)] transition active:translate-y-px active:brightness-90",
          large ? "px-6 py-5 text-xl" : "px-3 py-1.5 text-xs sm:px-4 sm:text-sm",
        )}
      >
        E-STOP
      </button>
      {err && <span className="mt-1 text-xs text-bad">{err}</span>}
    </div>
  );
}

export function EstopBanner() {
  const { telemetry } = useTelemetry();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const s = telemetry?.safety;
  if (!s?.estop_latched) return null;
  const reset = async () => {
    if (!confirm("Reset the emergency stop? The rover stays DISARMED and the previous mission will not resume.")) return;
    setBusy(true);
    setErr(null);
    try {
      await post("/api/safety/estop/reset");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="hazard p-1" role="alert">
      <div className="flex flex-col gap-2 rounded-md bg-ink-950/95 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="font-black uppercase tracking-widest text-bad">Emergency stop latched</div>
          <div className="text-sm text-fg-muted">
            {s.estop_reason} · source {s.estop_source}. Motors held at zero. Software e-stop only — not an independent power cut-off.
          </div>
          {err && <div className="mt-1 text-xs text-bad">{err.message} {err.details.join("; ")}</div>}
        </div>
        <Button variant="default" onClick={reset} disabled={busy}>Reset e-stop…</Button>
      </div>
    </div>
  );
}

export function ArmControl() {
  const { telemetry } = useTelemetry();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [checks, setChecks] = useState<PreflightCheck[] | null>(null);
  const s = telemetry?.safety;
  if (!s) return null;
  const sim = telemetry?.simulated;

  const arm = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await post<{ checks: PreflightCheck[] }>("/api/safety/arm");
      setChecks(res.checks);
    } catch (e) {
      const err = e as ApiError;
      setError(err);
      setChecks((err.body.checks as PreflightCheck[]) ?? null);
    } finally {
      setBusy(false);
    }
  };
  const disarm = async () => {
    setBusy(true);
    try {
      await post("/api/safety/disarm");
      setChecks(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <div
          className={cx(
            "flex items-center gap-2 rounded-lg border px-3 py-2 font-mono text-sm font-bold uppercase tracking-wider",
            s.armed ? "border-warn/60 bg-warn/15 text-warn" : "border-ink-500 bg-ink-800 text-fg-muted",
          )}
        >
          <span className={cx("h-2.5 w-2.5 rounded-full", s.armed ? "animate-pulse bg-warn" : "bg-fg-faint")} />
          {s.armed ? (sim ? "Armed (simulated)" : "ARMED — motors live") : "Disarmed"}
        </div>
        {s.armed ? (
          <Button onClick={disarm} disabled={busy}>Disarm</Button>
        ) : (
          <Button variant="primary" onClick={arm} disabled={busy || s.estop_latched}>
            {sim ? "Arm (simulated)" : "Arm rover"}
          </Button>
        )}
      </div>
      {!s.armed && !sim && (
        <p className="text-xs text-fg-faint">
          Arming enables the motors. Confirm the area is clear and the physical motor power switch is within reach.
        </p>
      )}
      {error && <ErrorList title="Arming refused" lines={error.details.length ? error.details : [error.message]} />}
      {checks && !error && (
        <ul className="space-y-1 text-xs">
          {checks.filter((c) => !c.passed || !c.required).map((c) => (
            <li key={c.id} className={c.passed ? "text-fg-muted" : "text-warn"}>
              {c.passed ? "✓" : "!"} {c.label}{c.message ? ` — ${c.message}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
