import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { IssueList, LocalizationNote, PathView, PlanSummaryView } from "../components/plan";
import { Button, ErrorList, Field, Notice, NumberInput, Panel, Segmented, Select, cx } from "../components/ui";
import { ApiError, post } from "../lib/api";
import type { CoveragePlan, MissionRecord } from "../lib/types";
import { convert, type Unit } from "../lib/units";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

type Corner = "near_left" | "near_right" | "far_left" | "far_right";

interface Form {
  name: string;
  units: Unit;
  length: number;
  width: number;
  lane_spacing: number;
  useSwath: boolean;
  swath_width: number;
  margin: number;
  start_corner: Corner;
  lane_orientation: "along_length" | "along_width";
  headingMode: "aligned" | "custom";
  initial_heading_deg: number;
  turn_strategy: "pivot" | "arc";
  turn_radius: number;
  speed_mps: number;
}

const DEFAULT: Form = {
  name: "Grid survey",
  units: "m",
  length: 4,
  width: 2,
  lane_spacing: 0.4,
  useSwath: false,
  swath_width: 0.4,
  margin: 0.1,
  start_corner: "near_left",
  lane_orientation: "along_length",
  headingMode: "aligned",
  initial_heading_deg: 0,
  turn_strategy: "pivot",
  turn_radius: 0.3,
  speed_mps: 0.2,
};

const LENGTH_FIELDS = ["length", "width", "lane_spacing", "swath_width", "margin", "turn_radius"] as const;

function reverseCorner(c: Corner, orientation: Form["lane_orientation"]): Corner {
  // Mirror the start corner along the lane axis so the first lane runs the other way.
  const [ns, lr] = c.split("_") as ["near" | "far", "left" | "right"];
  if (orientation === "along_length") return `${ns === "near" ? "far" : "near"}_${lr}` as Corner;
  return `${ns}_${lr === "left" ? "right" : "left"}` as Corner;
}

export default function PlanGrid() {
  const { telemetry: t, system } = useTelemetry();
  const nav = useNavigate();
  const [f, setF] = useState<Form>(DEFAULT);
  const [plan, setPlan] = useState<CoveragePlan | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<MissionRecord | null>(null);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => {
    setSaved(null);
    setF((p) => ({ ...p, [k]: v }));
  };

  const params = useMemo(() => {
    if (!t) return null;
    return {
      units: f.units,
      length: f.length,
      width: f.width,
      lane_spacing: f.lane_spacing,
      swath_width: f.useSwath ? f.swath_width : null,
      margin: f.margin,
      start_corner: f.start_corner,
      lane_orientation: f.lane_orientation,
      initial_heading_deg: f.headingMode === "custom" ? f.initial_heading_deg : null,
      turn_strategy: f.turn_strategy,
      turn_radius: f.turn_strategy === "arc" ? f.turn_radius : null,
      speed_mps: f.speed_mps,
      execution_mode: t.mode,
    };
  }, [f, t?.mode]); // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced live preview.
  useEffect(() => {
    if (!params) return;
    const bad = Object.values(params).some((v) => typeof v === "number" && !Number.isFinite(v));
    if (bad) return;
    const h = window.setTimeout(async () => {
      try {
        setPlan(await post<CoveragePlan>("/api/plan/grid", params));
        setError(null);
      } catch (e) {
        setPlan(null);
        setError(e as ApiError);
      }
    }, 300);
    return () => window.clearTimeout(h);
  }, [params]);

  if (!t) return <Loading />;

  const changeUnits = (u: Unit) => {
    setF((p) => {
      const next = { ...p, units: u };
      for (const k of LENGTH_FIELDS) next[k] = convert(p[k], p.units, u);
      return next;
    });
  };

  const save = async (load: boolean) => {
    if (!params) return;
    setSaving(true);
    setError(null);
    try {
      const rec = await post<MissionRecord>("/api/missions", { name: f.name, kind: "grid", params });
      setSaved(rec);
      if (load) {
        await post("/api/mission/load", { mission_id: rec.id });
        nav("/mission");
      }
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setSaving(false);
    }
  };

  const u = f.units;
  const corners: Corner[] = ["far_left", "far_right", "near_left", "near_right"];

  return (
    <div className="grid gap-4 xl:grid-cols-[400px_1fr]">
      <Panel title="Grid survey (no GPS)">
        <div className="space-y-4">
          <Field label="Mission name">
            <input className="input" value={f.name} maxLength={120} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <div className="flex items-end justify-between gap-3">
            <Field label="Units">
              <Segmented value={u} onChange={changeUnits} ariaLabel="Units"
                options={[{ value: "m", label: "m" }, { value: "cm", label: "cm" }, { value: "mm", label: "mm" }]} />
            </Field>
            <div className="text-right text-xs text-fg-faint">Execution: <span className="font-semibold text-fg">{t.mode}</span><br />fixed at backend start</div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label={`Length (${u})`} hint="forward from the near edge"><NumberInput value={f.length} onChange={(v) => set("length", v)} min={0} /></Field>
            <Field label={`Width (${u})`} hint="to the right"><NumberInput value={f.width} onChange={(v) => set("width", v)} min={0} /></Field>
            <Field label={`Lane spacing (${u})`}><NumberInput value={f.lane_spacing} onChange={(v) => set("lane_spacing", v)} min={0} /></Field>
            <Field label={`Margin (${u})`} hint="inset from every edge"><NumberInput value={f.margin} onChange={(v) => set("margin", v)} min={0} /></Field>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={f.useSwath} onChange={(e) => set("useSwath", e.target.checked)} className="accent-[#f5b83d]" />
            Sensor swath differs from lane spacing
          </label>
          {f.useSwath && <Field label={`Sensor swath width (${u})`}><NumberInput value={f.swath_width} onChange={(v) => set("swath_width", v)} min={0} /></Field>}

          <div className="grid grid-cols-[auto_1fr] items-start gap-4">
            <div>
              <span className="label">Start corner</span>
              <div className="grid w-[112px] grid-cols-2 gap-1 rounded-lg border border-ink-600 bg-ink-900 p-1" role="radiogroup" aria-label="Start corner">
                {corners.map((c) => (
                  <button key={c} type="button" role="radio" aria-checked={f.start_corner === c} title={c.replace("_", " ")}
                    onClick={() => set("start_corner", c)}
                    className={cx("h-12 rounded-md text-[10px] font-semibold uppercase", f.start_corner === c ? "bg-amber text-ink-950" : "bg-ink-700 text-fg-muted")}>
                    {c.replace("_", "\n")}
                  </button>
                ))}
              </div>
              <div className="mt-1 text-center text-[10px] text-fg-faint">operator side ↓</div>
            </div>
            <div className="space-y-3">
              <Field label="Lanes run">
                <Select value={f.lane_orientation} onChange={(v) => set("lane_orientation", v)}
                  options={[{ value: "along_length", label: "Along length (↑↓)" }, { value: "along_width", label: "Along width (←→)" }]} />
              </Field>
              <Button className="w-full" onClick={() => set("start_corner", reverseCorner(f.start_corner, f.lane_orientation))}>⇅ Reverse direction</Button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Initial heading">
              <Select value={f.headingMode} onChange={(v) => set("headingMode", v)}
                options={[{ value: "aligned", label: "Facing first lane" }, { value: "custom", label: "Custom" }]} />
            </Field>
            {f.headingMode === "custom" && (
              <Field label="Heading (°)" hint="0 = toward far edge, 90 = right">
                <NumberInput value={f.initial_heading_deg} onChange={(v) => set("initial_heading_deg", v)} min={-360} max={360} />
              </Field>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Turning">
              <Select value={f.turn_strategy} onChange={(v) => set("turn_strategy", v)}
                options={[{ value: "pivot", label: "Pivot in place" }, { value: "arc", label: "Arc turns" }]} />
            </Field>
            {f.turn_strategy === "arc" && (
              <Field label={`Turn radius (${u})`}><NumberInput value={f.turn_radius} onChange={(v) => set("turn_radius", v)} min={0} /></Field>
            )}
            <Field label="Speed (m/s)" hint={`max ≈ ${(t.motion.max_output * (system?.calibration.full_speed_mps ?? 0.45)).toFixed(2)} m/s (calibration)`}>
              <NumberInput value={f.speed_mps} onChange={(v) => set("speed_mps", v)} min={0.05} max={3} step={0.05} />
            </Field>
          </div>

          {error && <ErrorList title={error.message} lines={error.details} />}
          {saved && <Notice tone="ok" title="Mission saved">{saved.name} · {saved.id.slice(0, 8)}</Notice>}
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={!plan?.valid || saving} onClick={() => save(true)}>Save &amp; load mission</Button>
            <Button disabled={!plan?.valid || saving} onClick={() => save(false)}>Save only</Button>
            <Button variant="ghost" onClick={() => { setF(DEFAULT); setSaved(null); }}>Reset</Button>
          </div>
        </div>
      </Panel>

      <div className="space-y-4">
        <Panel title="Route preview" actions={plan && <span className="font-mono text-[11px] text-fg-faint">#{plan.plan_hash}</span>}>
          {plan ? (
            <PathView plan={plan} className="h-[52vh] min-h-[320px]" sim={t.simulation} showSimTruth={false} />
          ) : (
            <div className="flex h-[40vh] items-center justify-center text-sm text-fg-faint">{error ? "Fix the parameters to see a preview" : "Planning…"}</div>
          )}
        </Panel>
        {plan && (
          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Plan summary"><PlanSummaryView s={plan.summary} /></Panel>
            <Panel title="Checks">
              <div className="space-y-3">
                {!plan.valid && <Notice tone="bad" title="This route cannot be executed" />}
                <IssueList issues={plan.issues} />
                {plan.issues.length === 0 && <div className="text-sm text-ok">No issues found.</div>}
                <LocalizationNote note={plan.localization_note} />
                <p className="text-xs text-fg-faint">
                  Place the rover at the green start marker ({plan.start_pose.x.toFixed(2)} m right, {plan.start_pose.y.toFixed(2)} m forward of the near-left corner) facing {plan.start_pose.heading_deg.toFixed(0)}°.
                </p>
              </div>
            </Panel>
          </div>
        )}
      </div>
    </div>
  );
}
