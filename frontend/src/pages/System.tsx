import { useCallback, useEffect, useState } from "react";
import { CapabilityList, EventLog, FaultList } from "../components/status";
import { Button, Chip, ErrorList, Field, Notice, NumberInput, Panel } from "../components/ui";
import { ApiError, api, post } from "../lib/api";
import type { PreflightCheck } from "../lib/types";
import { fmtDateTime, fmtDuration } from "../lib/units";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

export default function System() {
  const { telemetry: t, system, events } = useTelemetry();
  const [checks, setChecks] = useState<PreflightCheck[] | null>(null);
  const [config, setConfig] = useState<unknown>(null);
  const [showConfig, setShowConfig] = useState(false);
  const [history, setHistory] = useState<{ id: number; code: string; severity: string; message: string; raised_at: string; cleared_at: string | null; cleared_by: string | null }[]>([]);

  const refresh = useCallback(() => {
    api<{ checks: PreflightCheck[] }>("/api/safety/preflight").then((r) => setChecks(r.checks)).catch(() => undefined);
    api<{ history: typeof history }>("/api/faults?history=true&limit=50").then((r) => setHistory(r.history ?? [])).catch(() => undefined);
  }, []);
  useEffect(() => {
    refresh();
    const h = window.setInterval(refresh, 3000);
    return () => window.clearInterval(h);
  }, [refresh]);

  if (!t || !system) return <Loading />;
  const hwErrors = Object.entries(system.hardware_errors);

  return (
    <div className="space-y-4">
      {t.simulation && <SimulationPanel />}
      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="System">
          <dl>
            <div className="kv"><dt>Version</dt><dd>{system.version}</dd></div>
            <div className="kv"><dt>Mode</dt><dd>{system.mode}</dd></div>
            <div className="kv"><dt>Started</dt><dd>{fmtDateTime(system.started_at)}</dd></div>
            <div className="kv"><dt>Uptime</dt><dd>{fmtDuration(system.uptime_s)}</dd></div>
            <div className="kv"><dt>Config fingerprint</dt><dd>{system.config_fingerprint}</dd></div>
            <div className="kv"><dt>GPIO chip</dt><dd>{system.gpio_chip ? `gpiochip${system.gpio_chip.number} (${system.gpio_chip.label})` : "not used"}</dd></div>
            <div className="kv"><dt>Motor driver</dt><dd>{t.motion.driver.driver} · {t.motion.driver.healthy ? "healthy" : "UNAVAILABLE"}</dd></div>
            <div className="kv"><dt>Auth</dt><dd>{system.auth_enabled ? "token required" : "open (trusted network)"}</dd></div>
          </dl>
          {hwErrors.length > 0 && <ErrorList title="Hardware initialisation errors" lines={hwErrors.map(([k, v]) => `${k}: ${v}`)} />}
        </Panel>
        <Panel title="Pre-arm checks" actions={<Button variant="ghost" className="px-2 py-1 text-xs" onClick={refresh}>Refresh</Button>}>
          {checks ? (
            <ul className="space-y-1.5 text-sm">
              {checks.map((c) => (
                <li key={c.id} className="flex items-start gap-2">
                  <span className={c.passed ? "text-ok" : c.required ? "text-bad" : "text-warn"}>{c.passed ? "✓" : c.required ? "✗" : "!"}</span>
                  <span>{c.label}{c.message && <span className="block text-xs text-fg-muted">{c.message}</span>}</span>
                </li>
              ))}
            </ul>
          ) : <div className="text-sm text-fg-muted">Loading…</div>}
        </Panel>
        <Panel title="Open-loop calibration">
          <dl>
            <div className="kv"><dt>Full speed</dt><dd>{system.calibration.full_speed_mps} m/s</dd></div>
            <div className="kv"><dt>Effective track</dt><dd>{system.calibration.effective_track_m} m</dd></div>
            <div className="kv"><dt>Derived pivot rate</dt><dd>{system.calibration.full_pivot_dps} °/s</dd></div>
            <div className="kv"><dt>Status</dt><dd className={system.calibration.calibrated ? "text-ok" : "text-warn"}>{system.calibration.calibrated ? "measured" : "NOT measured"}</dd></div>
          </dl>
          <p className="mt-2 text-xs text-fg-faint">Measured values are entered in the backend configuration. See docs/CALIBRATION.md.</p>
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Capabilities"><CapabilityList caps={t.capabilities} /></Panel>
        <div className="space-y-4">
          <Panel title="Active faults"><FaultList faults={t.faults} /></Panel>
          <Panel title="Fault history">
            <ul className="scrollbar-thin max-h-64 space-y-1 overflow-y-auto font-mono text-xs">
              {history.map((f) => (
                <li key={f.id}><span className="text-fg-faint">{fmtDateTime(f.raised_at)}</span> <span className={f.severity === "warning" ? "text-warn" : "text-bad"}>{f.severity}</span> {f.message}
                  {f.cleared_at && <span className="text-fg-faint"> — cleared ({f.cleared_by})</span>}</li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>

      <Panel title="Event log"><EventLog events={events} limit={200} /></Panel>
      <Panel title="Effective configuration (read-only)" actions={
        <Button variant="ghost" className="px-2 py-1 text-xs" onClick={async () => { setShowConfig((s) => !s); if (!config) setConfig(await api("/api/config")); }}>
          {showConfig ? "Hide" : "Show"}
        </Button>
      }>
        {showConfig ? <pre className="scrollbar-thin max-h-[60vh] overflow-auto rounded-lg bg-ink-950 p-3 text-xs">{JSON.stringify(config, null, 2)}</pre>
          : <div className="text-sm text-fg-muted">Secrets are redacted. Edit the YAML file on the rover and restart the backend to change configuration.</div>}
      </Panel>
    </div>
  );
}

function SimulationPanel() {
  const { telemetry: t } = useTelemetry();
  const [error, setError] = useState<ApiError | null>(null);
  const [pose, setPose] = useState({ x: 0, y: 0, heading_deg: 0 });
  if (!t?.simulation) return null;
  const sim = t.simulation;
  const call = async (path: string, body: unknown) => {
    try {
      setError(null);
      await post(path, body);
    } catch (e) {
      setError(e as ApiError);
    }
  };
  return (
    <Panel title="Simulation controls" actions={<Chip tone="sim">simulation only</Chip>}>
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-3">
          <Field label={`Simulation speed ×${sim.speed_factor}`} hint="Physics, missions and simulated sensors run faster; safety timeouts stay real-time.">
            <input type="range" min={0.5} max={20} step={0.5} value={sim.speed_factor} className="w-full accent-[#b197fc]"
              onChange={(e) => call("/api/sim/speed", { speed_factor: Number(e.target.value) })} />
          </Field>
          <dl className="text-sm">
            <div className="kv"><dt>Truth pose</dt><dd>{sim.truth.pose.x.toFixed(2)}, {sim.truth.pose.y.toFixed(2)} · {sim.truth.pose.heading_deg.toFixed(0)}°</dd></div>
            <div className="kv"><dt>Speed</dt><dd>{sim.truth.speed_mps.toFixed(2)} m/s</dd></div>
            <div className="kv"><dt>Sim time</dt><dd>{fmtDuration(sim.truth.sim_time_s)}</dd></div>
            {sim.truth.collided && <div className="text-bad">Collision with a simulated obstacle</div>}
          </dl>
        </div>
        <div>
          <span className="label">Fault injection</span>
          <ul className="space-y-1.5">
            {Object.entries(sim.available_faults).map(([k, desc]) => {
              const on = sim.faults.includes(k);
              return (
                <li key={k}>
                  <label className="flex items-start gap-2 text-sm">
                    <input type="checkbox" checked={on} className="mt-0.5 accent-[#b197fc]" onChange={() => call("/api/sim/faults", { fault: k, active: !on })} />
                    <span><span className="font-mono text-xs">{k}</span><span className="block text-xs text-fg-muted">{desc}</span></span>
                  </label>
                </li>
              );
            })}
          </ul>
        </div>
        <div className="space-y-3">
          <span className="label">Place simulated rover (disarmed only)</span>
          <div className="grid grid-cols-3 gap-2">
            <Field label="x (m)"><NumberInput value={pose.x} onChange={(v) => setPose({ ...pose, x: v })} /></Field>
            <Field label="y (m)"><NumberInput value={pose.y} onChange={(v) => setPose({ ...pose, y: v })} /></Field>
            <Field label="hdg (°)"><NumberInput value={pose.heading_deg} onChange={(v) => setPose({ ...pose, heading_deg: v })} /></Field>
          </div>
          <Button onClick={() => call("/api/sim/pose", pose)}>Place rover</Button>
          <Notice tone="info">Mission start places the simulated rover at the plan's start pose, standing in for the operator placing it by hand.</Notice>
          {error && <ErrorList title={error.message} />}
        </div>
      </div>
    </Panel>
  );
}
