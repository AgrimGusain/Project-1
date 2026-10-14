import { Link } from "react-router-dom";
import { CameraView } from "../components/camera";
import { ArmControl, MODE_META } from "../components/safety";
import { CapabilityList, EventLog, FaultList, LocalizationCard, SensorCard } from "../components/status";
import { Chip, Empty, Meter, Panel, Stat, cx } from "../components/ui";
import { fmtDuration } from "../lib/units";
import { useTelemetry } from "../state/telemetry";

export default function Dashboard() {
  const { telemetry: t, events, system } = useTelemetry();
  if (!t) return <Loading />;
  const mode = MODE_META[t.mode];
  const m = t.mission;
  const p = m.mission?.progress;
  const front = t.safety.obstacles?.front;
  const obstacleSensors = t.sensors.filter((s) => s.safety_role && s.safety_role !== "none" && s.enabled);
  const unavailable = t.capabilities.filter((c) => c.status === "unavailable" || c.status === "not_installed");

  return (
    <div className="space-y-4">
      <div className={cx("rounded-xl border px-4 py-3", mode.tone)}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-xs font-black uppercase tracking-[0.2em]">{mode.label}</div>
            <div className="text-sm text-fg/80">{mode.desc}</div>
          </div>
          {system && <div className="font-mono text-xs text-fg-muted">v{system.version} · up {fmtDuration(system.uptime_s)}</div>}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Safety">
          <ArmControl />
          <dl className="mt-4">
            <div className="kv"><dt>Motion permitted</dt><dd className={t.safety.motion_permitted ? "text-ok" : "text-fg-muted"}>{t.safety.motion_permitted ? "yes" : t.safety.motion_blocked_reason}</dd></div>
            <div className="kv"><dt>Control owner</dt><dd>{t.motion.owner}</dd></div>
            <div className="kv"><dt>Control loop</dt><dd className={t.motion.control_loop_ok ? "text-ok" : "text-bad"}>{t.motion.control_loop_ok ? "running" : "NOT RUNNING"}</dd></div>
            <div className="kv"><dt>Physical e-stop input</dt><dd>{t.safety.estop_input.configured ? (t.safety.estop_input.pressed ? "PRESSED" : "released") : "not fitted"}</dd></div>
          </dl>
        </Panel>

        <Panel title="Mission" actions={<Link to="/mission" className="btn btn-ghost px-2 py-1 text-xs">Open →</Link>}>
          {m.mission ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-semibold">{m.mission.name}</div>
                  <div className="text-xs text-fg-muted">{m.mission.kind} · {m.mission.execution.replace("_", "-")}</div>
                </div>
                <Chip tone={m.state === "RUNNING" ? "ok" : m.state === "PAUSED" ? "warn" : "info"}>{m.state}</Chip>
              </div>
              {p && (
                <>
                  <Meter value={p.percent} max={100} tone="ok" label="mission progress" />
                  <div className="grid grid-cols-3 gap-3">
                    <Stat label="Progress" value={`${p.percent.toFixed(0)}%`} sub={m.mission.execution === "open_loop" ? "commanded" : "tracked"} />
                    <Stat label="Lanes" value={`${p.lanes_completed}/${p.lane_count}`} />
                    <Stat label="Elapsed" value={fmtDuration(p.elapsed_s)} />
                  </div>
                </>
              )}
              {m.mission.pause_reason && <div className="text-sm text-warn">Paused: {m.mission.pause_reason}</div>}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between"><span className="text-sm text-fg-muted">No mission loaded</span><Chip>{m.state}</Chip></div>
              {m.last_result && <div className="text-xs text-fg-muted">Last: {m.last_result.status} — {m.last_result.reason}</div>}
              <div className="flex gap-2">
                <Link to="/plan/grid" className="btn btn-default">Plan grid survey</Link>
                <Link to="/missions" className="btn btn-ghost">History</Link>
              </div>
            </div>
          )}
        </Panel>

        <Panel title="Localization"><LocalizationCard loc={t.localization} /></Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Obstacle sensing" className="xl:col-span-2"
          actions={front && <Chip tone={front.state === "clear" ? "ok" : front.state === "disabled" ? "muted" : "bad"}>front {front.state}</Chip>}>
          {obstacleSensors.length === 0 ? (
            <Empty>No obstacle sensors configured — forward motion is not protected.</Empty>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {obstacleSensors.map((s) => <SensorCard key={s.id} s={s} stopDistance={undefined} />)}
            </div>
          )}
          {front && front.reasons.length > 0 && <ul className="mt-3 list-disc pl-5 text-xs text-warn">{front.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
          <p className="mt-3 text-xs text-fg-faint">Obstacle sensors gate motion only. They are not landmine detectors.</p>
        </Panel>
        <Panel title="Camera" actions={<Link to="/camera" className="btn btn-ghost px-2 py-1 text-xs">Open →</Link>}>
          <CameraView camera={t.camera} vision={t.vision} />
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Capabilities" className="xl:col-span-2"
          actions={unavailable.length > 0 && <span className="text-xs text-fg-faint">{unavailable.length} not available</span>}>
          <CapabilityList caps={t.capabilities} />
        </Panel>
        <div className="space-y-4">
          <Panel title={`Active faults (${t.faults.length})`}><FaultList faults={t.faults} /></Panel>
          <Panel title="Events"><EventLog events={events} limit={25} /></Panel>
        </div>
      </div>
    </div>
  );
}

export function Loading() {
  const { link } = useTelemetry();
  return (
    <div className="flex h-[60vh] flex-col items-center justify-center gap-2 text-center">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-ink-600 border-t-amber" />
      <div className="text-sm text-fg-muted">{link === "offline" ? "Waiting for the rover backend…" : link === "unauthorized" ? "Access token required" : "Connecting to the rover…"}</div>
      {(link === "offline" || link === "unauthorized") && <Link to="/settings" className="btn btn-default mt-2">Connection settings</Link>}
    </div>
  );
}
