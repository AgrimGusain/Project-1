import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { LocalizationNote, PathView, PlanSummaryView } from "../components/plan";
import { ArmControl } from "../components/safety";
import { EventLog } from "../components/status";
import { Button, Chip, Empty, ErrorList, Meter, Notice, Panel, Stat, type Tone } from "../components/ui";
import { ApiError, api, post } from "../lib/api";
import type { CoveragePlan, GeoPoint, MissionRecord, MissionSnapshot } from "../lib/types";
import { fmtDateTime, fmtDistance, fmtDuration } from "../lib/units";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

const GeoMap = lazy(() => import("../components/GeoMap"));

const STATE_TONE: Record<string, Tone> = {
  IDLE: "muted", READY: "info", ARMING: "warn", RUNNING: "ok", PAUSED: "warn", STOPPING: "warn", COMPLETED: "ok", FAILED: "bad", EMERGENCY_STOP: "bad",
};

/** Display-only flat-earth conversion from the plan's local ENU frame to lat/lon. */
function enuToGeo(origin: GeoPoint, x: number, y: number): GeoPoint {
  const lat = origin.lat + y / 111_320;
  return { lat, lon: origin.lon + x / (111_320 * Math.cos((origin.lat * Math.PI) / 180)) };
}

export default function Mission() {
  const { telemetry: t, events, system } = useTelemetry();
  const [plan, setPlan] = useState<CoveragePlan | null>(null);
  const [trace, setTrace] = useState<[number, number][]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const planFor = useRef<string | null>(null);
  const missionId = t?.mission.mission?.id ?? null;

  // Fetch the full plan once per mission, and the trace periodically.
  useEffect(() => {
    if (!missionId) {
      setPlan(null);
      setTrace([]);
      planFor.current = null;
      return;
    }
    let stop = false;
    const load = async () => {
      try {
        const snap = await api<MissionSnapshot>(`/api/mission/active?trace=true`);
        if (stop) return;
        if (snap.plan && planFor.current !== missionId) {
          setPlan(snap.plan);
          planFor.current = missionId;
        }
        setTrace(snap.mission?.trace ?? []);
      } catch {
        /* telemetry banner already reports connectivity */
      }
    };
    load();
    const h = window.setInterval(load, 1000);
    return () => {
      stop = true;
      window.clearInterval(h);
    };
  }, [missionId]);

  const act = async (path: string, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    setBusy(true);
    setError(null);
    try {
      await post(path);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const missionEvents = useMemo(() => events.filter((e) => e.mission_id && e.mission_id === missionId), [events, missionId]);

  if (!t) return <Loading />;
  const ms = t.mission;
  const m = ms.mission;
  const p = m?.progress;
  const state = ms.state;

  if (!m) return <NoMission state={state} lastResult={ms.last_result} />;

  const openLoop = m.execution === "open_loop";
  const geoTrace = plan?.origin ? trace.map(([x, y]) => enuToGeo(plan.origin!, x, y)) : [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-bold">{m.name}</h1>
          <div className="text-xs text-fg-muted">{m.kind} survey · execution <span className="font-semibold text-fg">{m.execution.replace("_", "-")}</span> · plan #{m.plan_hash}</div>
        </div>
        <Chip tone={STATE_TONE[state]} className="px-3 py-1 text-xs">{state.replace("_", " ")}</Chip>
      </div>

      {openLoop && (
        <Notice tone="warn" title="Open-loop execution — position is NOT measured">
          The amber outline shows where the rover <em>should</em> be if it followed the commands exactly. Wheel slip, battery level and terrain make the real path drift.
          Progress is commanded progress, not verified coverage.
        </Notice>
      )}
      {m.pause_reason && <Notice tone="warn" title="Mission paused">{m.pause_reason}</Notice>}
      {m.invalidated && <Notice tone="bad" title="Mission invalidated">{m.invalidated}</Notice>}
      {m.message && <Notice tone="warn">{m.message}</Notice>}
      {error && <ErrorList title={error.message} lines={error.details} />}

      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <Panel title={m.kind === "gps" ? "Route map" : "Route (local grid frame)"}>
          {!plan ? (
            <div className="h-[50vh] animate-pulse rounded-lg bg-ink-800" />
          ) : m.kind === "gps" && system ? (
            <Suspense fallback={<div className="h-[50vh] animate-pulse rounded-lg bg-ink-800" />}>
              <GeoMap center={plan.boundary_geo?.[0] ?? plan.origin!} tileUrl={system.ui.map_tile_url} attribution={system.ui.map_tile_attribution}
                maxZoom={system.ui.map_max_zoom} boundary={plan.boundary_geo ?? []} plan={plan} completedSegments={p?.segment_index ?? 0}
                position={t.localization.estimate} trace={geoTrace} height="58vh" fitToken={1} />
            </Suspense>
          ) : (
            <PathView
              plan={plan}
              className="h-[58vh] min-h-[320px]"
              completedSegments={p?.segment_index ?? 0}
              currentSegment={p?.segment_index}
              estimated={openLoop ? p?.estimated_pose : null}
              estimatedLabel="Commanded estimate"
              measured={p?.measured_pose}
              trace={trace}
              traceKind={openLoop ? "estimated" : "measured"}
              sim={t.simulation}
            />
          )}
        </Panel>

        <div className="space-y-4">
          <Panel title="Control">
            <div className="space-y-3">
              {state === "READY" && (
                <>
                  <ArmControl />
                  <Notice tone="info" title="Before starting">
                    {plan && `Place the rover at the start point (${plan.start_pose.x.toFixed(2)}, ${plan.start_pose.y.toFixed(2)}) facing ${plan.start_pose.heading_deg.toFixed(0)}°. `}
                    Clear the survey area. A {t.simulated ? "simulated " : ""}countdown runs before motion starts.
                  </Notice>
                  <div className="flex gap-2">
                    <Button variant="primary" className="flex-1" disabled={busy || !t.safety.armed}
                      onClick={() => act("/api/mission/start", t.simulated ? undefined : "Start the mission? The rover will move autonomously.")}>
                      Start mission
                    </Button>
                    <Button disabled={busy} onClick={() => act("/api/mission/unload")}>Unload</Button>
                  </div>
                </>
              )}
              {state === "ARMING" && (
                <Button variant="danger" className="w-full" disabled={busy} onClick={() => act("/api/mission/stop")}>Abort start</Button>
              )}
              {state === "RUNNING" && (
                <div className="flex gap-2">
                  <Button variant="default" className="flex-1" disabled={busy} onClick={() => act("/api/mission/pause")}>Pause</Button>
                  <Button variant="danger" className="flex-1" disabled={busy} onClick={() => act("/api/mission/stop", "Stop and cancel this mission? It cannot be resumed.")}>Stop</Button>
                </div>
              )}
              {state === "PAUSED" && (
                <div className="flex gap-2">
                  <Button variant="primary" className="flex-1" disabled={busy || !!m.invalidated} onClick={() => act("/api/mission/resume")}>Resume</Button>
                  <Button variant="danger" className="flex-1" disabled={busy} onClick={() => act("/api/mission/stop", "Stop and cancel this mission?")}>Stop</Button>
                </div>
              )}
              {(state === "COMPLETED" || state === "FAILED") && (
                <>
                  {ms.last_result && <Notice tone={state === "COMPLETED" ? "ok" : "bad"} title={ms.last_result.status}>{ms.last_result.reason}</Notice>}
                  <Button className="w-full" onClick={() => act("/api/mission/acknowledge")}>Acknowledge &amp; close</Button>
                </>
              )}
              {state === "EMERGENCY_STOP" && <Notice tone="bad">Emergency stop latched. Reset it from the banner; this mission will not resume.</Notice>}
              {state === "PAUSED" && !t.safety.armed && <ArmControl />}
            </div>
          </Panel>

          {p && (
            <Panel title="Progress">
              <Meter value={p.percent} max={100} tone="ok" label="progress" />
              <div className="mt-3 grid grid-cols-2 gap-4">
                <Stat label={openLoop ? "Commanded" : "Progress"} value={`${p.percent.toFixed(1)}%`} />
                <Stat label="Lanes" value={`${p.lanes_completed} / ${p.lane_count}`} />
                <Stat label="Segment" value={`${p.segment_index + 1} / ${p.segment_count}`} sub={p.phase} />
                <Stat label="Elapsed" value={fmtDuration(p.elapsed_s)} sub={`of ~${fmtDuration(m.summary.estimated_duration_s)}`} />
                <Stat label="Distance commanded" value={fmtDistance(p.distance_commanded_m)} />
                {p.cross_track_m !== null && <Stat label="Cross-track" value={fmtDistance(Math.abs(p.cross_track_m))} tone={Math.abs(p.cross_track_m) > 0.15 ? "warn" : "ok"} />}
              </div>
              {t.simulation && plan?.kind === "grid" && (
                <p className="mt-3 text-xs text-sim">Violet glyph: simulation ground truth (only exists in simulation).</p>
              )}
            </Panel>
          )}

          <Panel title="Plan"><PlanSummaryView s={m.summary} /><div className="mt-3"><LocalizationNote note={m.localization_note} /></div></Panel>
          <Panel title="Mission events"><EventLog events={missionEvents} limit={40} /></Panel>
        </div>
      </div>
    </div>
  );
}

function NoMission({ state, lastResult }: { state: string; lastResult: MissionSnapshot["last_result"] }) {
  const [missions, setMissions] = useState<MissionRecord[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    api<{ items: MissionRecord[] }>("/api/missions?status=planned&limit=20").then((r) => setMissions(r.items)).catch((e) => setError(e));
  }, []);
  const load = async (id: string) => {
    try {
      await post("/api/mission/load", { mission_id: id });
    } catch (e) {
      setError(e as ApiError);
    }
  };
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Panel title="No mission loaded" actions={<Chip>{state}</Chip>}>
        {lastResult && <Notice tone="info" className="mb-3" title={`Last mission: ${lastResult.status}`}>{lastResult.reason}</Notice>}
        {error && <ErrorList title={error.message} lines={error.details} />}
        {missions === null ? <div className="text-sm text-fg-muted">Loading…</div> : missions.length === 0 ? (
          <Empty>No planned missions. <Link className="text-amber underline" to="/plan/grid">Plan a grid survey</Link> or <Link className="text-amber underline" to="/plan/gps">a GPS survey</Link>.</Empty>
        ) : (
          <ul className="divide-y divide-ink-700">
            {missions.map((m) => (
              <li key={m.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <div className="truncate font-medium">{m.name}</div>
                  <div className="text-xs text-fg-muted">{m.kind} · {m.summary.lane_count} lanes · {fmtDistance(m.summary.total_length_m)} · {fmtDateTime(m.created_at)}</div>
                </div>
                <Button onClick={() => load(m.id)}>Load</Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
