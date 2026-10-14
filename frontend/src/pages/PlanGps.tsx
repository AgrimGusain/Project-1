import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { IssueList, LocalizationNote, PlanSummaryView } from "../components/plan";
import { LocalizationCard } from "../components/status";
import { Button, CAP_LABEL, CAP_TONE, Chip, ErrorList, Field, Notice, NumberInput, Panel, Segmented, Select } from "../components/ui";
import { ApiError, api, post } from "../lib/api";
import type { Boundary, CoveragePlan, GeoPoint, MissionRecord } from "../lib/types";
import { convert, type Unit } from "../lib/units";
import { useCapability, useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

const GeoMap = lazy(() => import("../components/GeoMap"));

export default function PlanGps() {
  const { telemetry: t, system } = useTelemetry();
  const nav = useNavigate();
  const gpsNav = useCapability("gps_navigation");
  const [boundary, setBoundary] = useState<GeoPoint[]>([]);
  const [editing, setEditing] = useState(true);
  const [fit, setFit] = useState(0);
  const [units, setUnits] = useState<Unit>("m");
  const [spacing, setSpacing] = useState(1);
  const [margin, setMargin] = useState(0.3);
  const [headingMode, setHeadingMode] = useState<"auto" | "custom">("auto");
  const [heading, setHeading] = useState(0);
  const [startVertex, setStartVertex] = useState(0);
  const [turn, setTurn] = useState<"pivot" | "arc">("pivot");
  const [radius, setRadius] = useState(0.4);
  const [speed, setSpeed] = useState(0.2);
  const [name, setName] = useState("GPS survey");
  const [plan, setPlan] = useState<CoveragePlan | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState<Boundary[]>([]);
  const [boundaryName, setBoundaryName] = useState("");
  const [busy, setBusy] = useState(false);

  const refreshSaved = () => api<Boundary[]>("/api/boundaries").then(setSaved).catch(() => undefined);
  useEffect(() => {
    refreshSaved();
  }, []);

  const est = t?.localization.estimate;
  const center: GeoPoint = useMemo(() => {
    if (est?.position_valid && est.lat !== null && est.lon !== null) return { lat: est.lat, lon: est.lon };
    if (t?.simulation) return { lat: t.simulation.geo_origin.lat, lon: t.simulation.geo_origin.lon };
    return { lat: system?.ui.map_center_lat ?? 0, lon: system?.ui.map_center_lon ?? 0 };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [system, t?.simulation?.geo_origin.lat]);

  const params = useMemo(() => {
    if (!t || boundary.length < 3) return null;
    return {
      boundary,
      units,
      lane_spacing: spacing,
      margin,
      lane_heading_deg: headingMode === "custom" ? ((heading % 360) + 360) % 360 : null,
      start_vertex: Math.min(startVertex, boundary.length - 1),
      turn_strategy: turn,
      turn_radius: turn === "arc" ? radius : null,
      speed_mps: speed,
      execution_mode: t.mode,
    };
  }, [t?.mode, boundary, units, spacing, margin, headingMode, heading, startVertex, turn, radius, speed]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!params) {
      setPlan(null);
      return;
    }
    if ([spacing, margin, speed].some((v) => !Number.isFinite(v))) return;
    const h = window.setTimeout(async () => {
      try {
        setPlan(await post<CoveragePlan>("/api/plan/gps", params));
        setError(null);
      } catch (e) {
        setPlan(null);
        setError(e as ApiError);
      }
    }, 350);
    return () => window.clearTimeout(h);
  }, [params]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!t || !system) return <Loading />;

  const changeUnits = (u: Unit) => {
    setSpacing((v) => convert(v, units, u));
    setMargin((v) => convert(v, units, u));
    setRadius((v) => convert(v, units, u));
    setUnits(u);
  };

  const addRoverPosition = () => {
    if (est?.position_valid && est.lat !== null && est.lon !== null) setBoundary((b) => [...b, { lat: est.lat!, lon: est.lon! }]);
  };

  const saveBoundary = async () => {
    if (boundary.length < 3) return;
    setBusy(true);
    try {
      const existing = saved.find((s) => s.name === boundaryName.trim());
      const body = { name: boundaryName.trim() || `Boundary ${saved.length + 1}`, points: boundary };
      if (existing) await api(`/api/boundaries/${existing.id}`, { method: "PUT", json: body });
      else await post("/api/boundaries", body);
      await refreshSaved();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const loadBoundary = (id: string) => {
    const b = saved.find((s) => String(s.id) === id);
    if (!b) return;
    setBoundary(b.points);
    setBoundaryName(b.name);
    setStartVertex(0);
    setFit((n) => n + 1);
  };

  const deleteBoundary = async () => {
    const b = saved.find((s) => s.name === boundaryName);
    if (!b || !confirm(`Delete saved boundary "${b.name}"?`)) return;
    await api(`/api/boundaries/${b.id}`, { method: "DELETE" });
    setBoundaryName("");
    refreshSaved();
  };

  const saveMission = async (load: boolean) => {
    if (!params) return;
    setBusy(true);
    setError(null);
    try {
      const rec = await post<MissionRecord>("/api/missions", { name, kind: "gps", params });
      if (load) {
        await post("/api/mission/load", { mission_id: rec.id });
        nav("/mission");
      }
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      {gpsNav && gpsNav.status !== "ready" && (
        <Notice tone={gpsNav.status === "simulated" ? "info" : "warn"}
          title={`GPS-guided execution: ${CAP_LABEL[gpsNav.status]}`}>
          {gpsNav.summary} Planning, editing and saving boundaries work regardless.
        </Notice>
      )}
      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant={editing ? "primary" : "default"} onClick={() => setEditing((e) => !e)}>{editing ? "Done editing" : "Edit boundary"}</Button>
            <Button onClick={() => setBoundary((b) => b.slice(0, -1))} disabled={!boundary.length}>Undo vertex</Button>
            <Button onClick={() => { setBoundary([]); setStartVertex(0); }} disabled={!boundary.length}>Clear</Button>
            <Button onClick={addRoverPosition} disabled={!est?.position_valid}>Add rover position</Button>
            <Button variant="ghost" onClick={() => setFit((n) => n + 1)}>Fit</Button>
            <span className="text-xs text-fg-faint">{editing ? "Click the map to add vertices; drag to move; double-click a vertex to delete." : `${boundary.length} vertices`}</span>
          </div>
          <Suspense fallback={<div className="h-[520px] animate-pulse rounded-xl bg-ink-800" />}>
            <GeoMap
              center={center}
              tileUrl={system.ui.map_tile_url}
              attribution={system.ui.map_tile_attribution}
              maxZoom={system.ui.map_max_zoom}
              boundary={boundary}
              onBoundaryChange={setBoundary}
              editing={editing}
              startVertex={startVertex}
              plan={plan}
              position={est}
              fitToken={fit}
              height="calc(100vh - 260px)"
            />
          </Suspense>
          <p className="text-xs text-fg-faint">
            Map tiles need internet access; without it the boundary and route still render on a blank background.
            Paths are planned in a local metric (ENU) frame, never in degrees. Survey extent is limited to a few kilometres.
          </p>
        </div>

        <div className="space-y-4">
          <Panel title="Boundary">
            <div className="space-y-3">
              <Field label="Saved boundaries">
                <select className="input" value="" onChange={(e) => loadBoundary(e.target.value)} aria-label="Load saved boundary">
                  <option value="">{saved.length ? "Load…" : "None saved"}</option>
                  {saved.map((b) => <option key={b.id} value={b.id}>{b.name} ({b.points.length} pts)</option>)}
                </select>
              </Field>
              <div className="flex gap-2">
                <input className="input" placeholder="Boundary name" value={boundaryName} maxLength={120} onChange={(e) => setBoundaryName(e.target.value)} />
                <Button onClick={saveBoundary} disabled={boundary.length < 3 || busy}>Save</Button>
              </div>
              {saved.some((s) => s.name === boundaryName) && <Button variant="ghost" className="text-bad" onClick={deleteBoundary}>Delete “{boundaryName}”</Button>}
              {boundary.length > 0 && boundary.length < 3 && <div className="text-xs text-warn">Add at least 3 vertices.</div>}
            </div>
          </Panel>

          <Panel title="Survey parameters">
            <div className="space-y-3">
              <Field label="Mission name"><input className="input" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></Field>
              <Field label="Units">
                <Segmented value={units} onChange={changeUnits} ariaLabel="Units"
                  options={[{ value: "m", label: "m" }, { value: "cm", label: "cm" }, { value: "mm", label: "mm" }]} />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label={`Lane spacing (${units})`}><NumberInput value={spacing} onChange={setSpacing} min={0} /></Field>
                <Field label={`Margin (${units})`}><NumberInput value={margin} onChange={setMargin} min={0} /></Field>
                <Field label="Lane direction">
                  <Select value={headingMode} onChange={setHeadingMode} options={[{ value: "auto", label: "Auto (fewest turns)" }, { value: "custom", label: "Custom bearing" }]} />
                </Field>
                {headingMode === "custom" && <Field label="Bearing (° true)"><NumberInput value={heading} onChange={setHeading} min={0} max={359} /></Field>}
                <Field label="Start vertex">
                  <Select value={String(startVertex)} onChange={(v) => setStartVertex(Number(v))}
                    options={(boundary.length ? boundary : [{ lat: 0, lon: 0 }]).map((_, i) => ({ value: String(i), label: `#${i + 1}` }))} />
                </Field>
                <Field label="Turning">
                  <Select value={turn} onChange={setTurn} options={[{ value: "pivot", label: "Pivot" }, { value: "arc", label: "Arc" }]} />
                </Field>
                {turn === "arc" && <Field label={`Turn radius (${units})`}><NumberInput value={radius} onChange={setRadius} min={0} /></Field>}
                <Field label="Speed (m/s)"><NumberInput value={speed} onChange={setSpeed} min={0.05} max={3} step={0.05} /></Field>
              </div>
              {error && <ErrorList title={error.message} lines={error.details} />}
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" disabled={!plan?.valid || busy} onClick={() => saveMission(true)}>Save &amp; load</Button>
                <Button disabled={!plan?.valid || busy} onClick={() => saveMission(false)}>Save only</Button>
              </div>
            </div>
          </Panel>

          {plan && (
            <Panel title="Plan" actions={<Chip tone={plan.valid ? "ok" : "bad"}>{plan.valid ? "valid" : "invalid"}</Chip>}>
              <div className="space-y-3">
                <PlanSummaryView s={plan.summary} />
                <div className="text-xs text-fg-muted">Lane bearing {plan.lane_heading_deg.toFixed(1)}° · origin {plan.origin?.lat.toFixed(6)}, {plan.origin?.lon.toFixed(6)}</div>
                <IssueList issues={plan.issues} />
                <LocalizationNote note={plan.localization_note} />
              </div>
            </Panel>
          )}

          <Panel title="GPS status" actions={gpsNav && <Chip tone={CAP_TONE[gpsNav.status]}>{CAP_LABEL[gpsNav.status]}</Chip>}>
            <LocalizationCard loc={t.localization} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
