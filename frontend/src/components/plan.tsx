import { useMemo } from "react";
import type { CoveragePlan, PathSegment, PlanIssue, PlanSummary, Pose, SimSnapshot } from "../lib/types";
import { fmtDistance, fmtDuration } from "../lib/units";
import { Chip, Notice, cx } from "./ui";

const KIND_STYLE: Record<PathSegment["kind"], { stroke: string; dash?: string; width: number }> = {
  lane: { stroke: "#5aa9ff", width: 2.2 },
  arc: { stroke: "#9aa7b8", width: 1.4 },
  shift: { stroke: "#9aa7b8", width: 1.4 },
  pivot: { stroke: "#9aa7b8", width: 1.4 },
  transit: { stroke: "#b197fc", dash: "4 3", width: 1.4 },
};

function RoverGlyph({ pose, color, size, label, outline = false }: { pose: Pose; color: string; size: number; label: string; outline?: boolean }) {
  // Heading is compass (0 = +y, clockwise). SVG is y-flipped by the parent group, so rotate by -heading.
  return (
    <g transform={`translate(${pose.x} ${pose.y}) rotate(${-pose.heading_deg})`}>
      <title>{label}</title>
      <path
        d={`M 0 ${size} L ${size * 0.6} ${-size * 0.6} L 0 ${-size * 0.25} L ${-size * 0.6} ${-size * 0.6} Z`}
        fill={outline ? "none" : color}
        stroke={color}
        strokeWidth={1.6}
        vectorEffect="non-scaling-stroke"
        fillOpacity={outline ? 0 : 0.9}
      />
    </g>
  );
}

export interface PathViewProps {
  plan: CoveragePlan;
  completedSegments?: number;
  currentSegment?: number;
  estimated?: Pose | null;
  estimatedLabel?: string;
  measured?: Pose | null;
  trace?: [number, number][];
  traceKind?: "estimated" | "measured";
  sim?: SimSnapshot | null;
  showSimTruth?: boolean;
  detections?: { x: number; y: number; simulated: boolean }[];
  className?: string;
}

export function PathView({
  plan, completedSegments = 0, currentSegment, estimated, estimatedLabel = "Commanded estimate", measured, trace, traceKind = "estimated",
  sim, showSimTruth = true, detections = [], className,
}: PathViewProps) {
  const bounds = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    plan.boundary.forEach((p) => { xs.push(p.x); ys.push(p.y); });
    plan.segments.forEach((s) => s.points.forEach((p) => { xs.push(p.x); ys.push(p.y); }));
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const pad = Math.max(maxX - minX, maxY - minY) * 0.08 + 0.3;
    return { minX: minX - pad, maxX: maxX + pad, minY: minY - pad, maxY: maxY + pad };
  }, [plan]);

  const w = bounds.maxX - bounds.minX;
  const h = bounds.maxY - bounds.minY;
  const size = Math.max(w, h) * 0.022;
  const gridStep = w > 40 ? 10 : w > 12 ? 2 : w > 4 ? 1 : 0.5;
  const gridLines = [];
  for (let x = Math.ceil(bounds.minX / gridStep) * gridStep; x <= bounds.maxX; x += gridStep) gridLines.push(<line key={`gx${x}`} x1={x} x2={x} y1={bounds.minY} y2={bounds.maxY} />);
  for (let y = Math.ceil(bounds.minY / gridStep) * gridStep; y <= bounds.maxY; y += gridStep) gridLines.push(<line key={`gy${y}`} y1={y} y2={y} x1={bounds.minX} x2={bounds.maxX} />);

  const truth = sim?.truth.pose;
  const start = plan.start_pose;
  const pts = (seg: PathSegment) => seg.points.map((p) => `${p.x},${p.y}`).join(" ");

  return (
    <div className={cx("relative", className)}>
      <svg
        viewBox={`${bounds.minX} ${-bounds.maxY} ${w} ${h}`}
        className="h-full w-full rounded-lg bg-ink-950"
        role="img"
        aria-label={`Planned survey path with ${plan.summary.lane_count} lanes`}
        preserveAspectRatio="xMidYMid meet"
      >
        <g transform="scale(1,-1)">
          <g stroke="#1b2430" strokeWidth={1} vectorEffect="non-scaling-stroke">{gridLines}</g>
          <polygon points={plan.boundary.map((p) => `${p.x},${p.y}`).join(" ")} fill="rgba(245,184,61,0.05)" stroke="#f5b83d"
            strokeWidth={1.5} strokeDasharray="6 4" vectorEffect="non-scaling-stroke" />
          {plan.inner_boundary.map((ring, i) => (
            <polygon key={i} points={ring.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" stroke="#8a6a24" strokeWidth={1}
              strokeDasharray="2 3" vectorEffect="non-scaling-stroke" />
          ))}
          {sim && showSimTruth && sim.obstacles.map((o, i) =>
            o.type === "circle"
              ? <circle key={i} cx={o.x} cy={o.y} r={o.r} fill="rgba(255,90,95,0.25)" stroke="#ff5a5f" strokeWidth={1} vectorEffect="non-scaling-stroke"><title>Simulated obstacle</title></circle>
              : <rect key={i} x={o.x - (o.w ?? 0) / 2} y={o.y - (o.h ?? 0) / 2} width={o.w} height={o.h} fill="rgba(255,90,95,0.25)" stroke="#ff5a5f" strokeWidth={1} vectorEffect="non-scaling-stroke"><title>Simulated obstacle</title></rect>,
          )}
          {plan.segments.map((seg) => {
            const st = KIND_STYLE[seg.kind];
            const done = seg.index < completedSegments;
            const current = seg.index === currentSegment;
            if (seg.kind === "pivot") return null;
            return (
              <polyline
                key={seg.index}
                points={pts(seg)}
                fill="none"
                stroke={!seg.within_boundary ? "#ff5a5f" : done ? "#3ecf8e" : st.stroke}
                strokeOpacity={done ? 0.55 : current ? 1 : 0.85}
                strokeWidth={current ? st.width + 1.6 : st.width}
                strokeDasharray={st.dash}
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
            );
          })}
          {trace && trace.length > 1 && (
            <polyline points={trace.map(([x, y]) => `${x},${y}`).join(" ")} fill="none"
              stroke={traceKind === "measured" ? "#3ecf8e" : "#f5b83d"} strokeWidth={1.6}
              strokeDasharray={traceKind === "measured" ? undefined : "3 3"} vectorEffect="non-scaling-stroke" />
          )}
          {detections.map((d, i) => (
            <g key={i} transform={`translate(${d.x} ${d.y})`}>
              <circle r={size * 0.7} fill={d.simulated ? "#b197fc" : "#ff5a5f"} fillOpacity={0.35} stroke={d.simulated ? "#b197fc" : "#ff5a5f"} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
            </g>
          ))}
          <circle cx={start.x} cy={start.y} r={size * 0.55} fill="#3ecf8e" />
          {estimated && <RoverGlyph pose={estimated} color="#f5b83d" size={size} label={estimatedLabel} outline />}
          {measured && <RoverGlyph pose={measured} color="#3ecf8e" size={size} label="Measured pose" />}
          {sim && showSimTruth && truth && <RoverGlyph pose={truth} color="#b197fc" size={size} label="Simulation ground truth" />}
        </g>
      </svg>
      <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap gap-x-3 gap-y-1 rounded-md bg-ink-950/80 px-2 py-1 text-[10px] text-fg-muted">
        <span><i className="mr-1 inline-block h-0.5 w-3 bg-info align-middle" />lanes</span>
        <span><i className="mr-1 inline-block h-0.5 w-3 bg-fg-muted align-middle" />turns</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-ok align-middle" />start</span>
        {estimated && <span className="text-amber">△ {estimatedLabel.toLowerCase()}</span>}
        {measured && <span className="text-ok">▲ measured</span>}
        {sim && showSimTruth && <span className="text-sim">▲ sim truth</span>}
        <span>grid {gridStep} m</span>
      </div>
    </div>
  );
}

export function PlanSummaryView({ s }: { s: PlanSummary }) {
  const items: [string, string][] = [
    ["Lanes", `${s.lane_count}${s.cell_count > 1 ? ` in ${s.cell_count} cells` : ""}`],
    ["Route length", fmtDistance(s.total_length_m)],
    ["Survey lanes", fmtDistance(s.lane_length_m)],
    ["Turns / shifts", fmtDistance(s.turn_length_m)],
    ["Pivots", `${s.pivot_count} (${Math.round(s.total_rotation_deg)}°)`],
    ["Est. duration", fmtDuration(s.estimated_duration_s)],
    ["Area", `${s.area_m2.toFixed(2)} m²`],
    ["Nominal coverage", `${(s.nominal_coverage_ratio * 100).toFixed(1)} %`],
  ];
  return (
    <dl className="grid grid-cols-2 gap-x-6">
      {items.map(([k, v]) => (
        <div key={k} className="kv border-b border-ink-700/60"><dt>{k}</dt><dd>{v}</dd></div>
      ))}
    </dl>
  );
}

export function IssueList({ issues }: { issues: PlanIssue[] }) {
  if (!issues.length) return null;
  return (
    <ul className="space-y-1.5">
      {issues.map((i, k) => (
        <li key={k} className="flex items-start gap-2 text-sm">
          <Chip tone={i.severity === "error" ? "bad" : i.severity === "warning" ? "warn" : "info"} className="shrink-0">{i.severity}</Chip>
          <span>{i.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function LocalizationNote({ note }: { note: string }) {
  if (!note) return null;
  return <Notice tone="warn" title="How this route will be executed">{note}</Notice>;
}
