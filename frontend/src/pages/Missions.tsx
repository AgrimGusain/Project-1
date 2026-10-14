import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { IssueList, PathView, PlanSummaryView } from "../components/plan";
import { Button, Chip, Empty, ErrorList, Notice, Panel, Select, type Tone } from "../components/ui";
import { ApiError, api, post } from "../lib/api";
import type { CoveragePlan, DetectionEvent, MissionRecord } from "../lib/types";
import { fmtDateTime, fmtDistance, fmtDuration, fmtTime } from "../lib/units";

export const STATUS_TONE: Record<MissionRecord["status"], Tone> = {
  planned: "info", active: "ok", completed: "ok", cancelled: "muted", failed: "bad", emergency_stopped: "bad", interrupted: "warn",
};

export default function Missions() {
  const [items, setItems] = useState<MissionRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [error, setError] = useState<ApiError | null>(null);
  const size = 25;

  const load = useCallback(() => {
    api<{ items: MissionRecord[]; total: number }>(`/api/missions?limit=${size}&offset=${page * size}${status ? `&status=${status}` : ""}`)
      .then((r) => { setItems(r.items); setTotal(r.total); setError(null); })
      .catch(setError);
  }, [page, status]);
  useEffect(load, [load]);

  return (
    <Panel title={`Missions (${total})`} actions={
      <div className="w-44">
        <Select value={status} onChange={(v) => { setStatus(v); setPage(0); }} ariaLabel="Filter by status" options={[
          { value: "", label: "All statuses" }, { value: "planned", label: "Planned" }, { value: "completed", label: "Completed" },
          { value: "cancelled", label: "Cancelled" }, { value: "failed", label: "Failed" }, { value: "emergency_stopped", label: "E-stopped" },
          { value: "interrupted", label: "Interrupted" },
        ]} />
      </div>
    }>
      {error && <ErrorList title={error.message} />}
      {items.length === 0 ? <Empty>No missions</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-fg-muted">
              <tr><th className="py-2">Name</th><th>Kind</th><th>Mode</th><th>Status</th><th>Lanes</th><th>Length</th><th>Created</th></tr>
            </thead>
            <tbody className="divide-y divide-ink-700">
              {items.map((m) => (
                <tr key={m.id} className="hover:bg-ink-800/60">
                  <td className="py-2"><Link to={`/missions/${m.id}`} className="font-medium text-fg hover:text-amber">{m.name}</Link>
                    {m.review_required && <Chip tone="warn" className="ml-2">review</Chip>}</td>
                  <td>{m.kind}</td>
                  <td className="text-fg-muted">{m.operating_mode}</td>
                  <td><Chip tone={STATUS_TONE[m.status]}>{m.status.replace("_", " ")}</Chip></td>
                  <td className="font-mono">{m.summary.lane_count}</td>
                  <td className="font-mono">{fmtDistance(m.summary.total_length_m)}</td>
                  <td className="text-fg-muted">{fmtDateTime(m.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {total > size && (
        <div className="mt-3 flex items-center justify-end gap-2 text-sm">
          <Button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Prev</Button>
          <span className="text-fg-muted">{page + 1} / {Math.ceil(total / size)}</span>
          <Button disabled={(page + 1) * size >= total} onClick={() => setPage((p) => p + 1)}>Next</Button>
        </div>
      )}
    </Panel>
  );
}

interface MissionEvents {
  transitions: { id: number; ts: string; type: string; from_state: string | null; to_state: string | null; message: string }[];
  log: { id: number; ts: string; level: string; message: string }[];
  faults: { id: number; code: string; severity: string; message: string; raised_at: string; cleared_at: string | null }[];
}

export function MissionDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [m, setM] = useState<MissionRecord | null>(null);
  const [ev, setEv] = useState<MissionEvents | null>(null);
  const [dets, setDets] = useState<DetectionEvent[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [note, setNote] = useState("");

  const load = useCallback(() => {
    if (!id) return;
    api<MissionRecord>(`/api/missions/${id}`).then(setM).catch(setError);
    api<MissionEvents>(`/api/missions/${id}/events`).then(setEv).catch(() => undefined);
    api<DetectionEvent[]>(`/api/missions/${id}/detections`).then(setDets).catch(() => undefined);
  }, [id]);
  useEffect(load, [load]);

  if (error) return <ErrorList title={error.message} />;
  if (!m) return <div className="text-fg-muted">Loading…</div>;
  const plan = m.plan as CoveragePlan | undefined;

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      load();
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to="/missions" className="text-xs text-fg-muted hover:text-fg">← Missions</Link>
          <h1 className="text-lg font-bold">{m.name}</h1>
          <div className="text-xs text-fg-muted">{m.kind} · {m.operating_mode} · {m.execution ?? "not run"} · plan #{m.plan_hash}</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={STATUS_TONE[m.status]} className="px-3 py-1">{m.status.replace("_", " ")}</Chip>
          {m.status === "planned" && <Button variant="primary" onClick={() => run(async () => { await post("/api/mission/load", { mission_id: m.id }); nav("/mission"); })}>Load</Button>}
          <Button onClick={() => run(async () => { const c = await post<MissionRecord>(`/api/missions/${m.id}/duplicate`); nav(`/missions/${c.id}`); })}>Duplicate</Button>
          {m.status !== "active" && <Button variant="ghost" className="text-bad" onClick={() => confirm("Delete this mission record?") && run(async () => { await api(`/api/missions/${m.id}`, { method: "DELETE" }); nav("/missions"); })}>Delete</Button>}
        </div>
      </div>

      {m.review_required && (
        <Notice tone="warn" title="Operator review required">
          {m.ended_reason}
          <div className="mt-2 flex gap-2">
            <input className="input" placeholder="Review note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            <Button onClick={() => run(() => post(`/api/missions/${m.id}/acknowledge`, { note: note || null }))}>Mark reviewed</Button>
          </div>
        </Notice>
      )}
      {!m.review_required && m.ended_reason && <Notice tone="info" title="Outcome">{m.ended_reason}</Notice>}

      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <Panel title="Planned route">
          {plan ? (
            <PathView plan={plan} className="h-[50vh] min-h-[300px]" completedSegments={m.progress?.segment_index ?? 0}
              estimated={m.progress?.execution === "open_loop" ? m.progress?.estimated_pose : null} measured={m.progress?.measured_pose}
              detections={dets.filter((d) => d.position?.x != null && d.position?.frame === "local").map((d) => ({ x: d.position!.x!, y: d.position!.y!, simulated: d.simulated }))} />
          ) : <Empty>No plan stored</Empty>}
        </Panel>
        <div className="space-y-4">
          <Panel title="Summary">
            <PlanSummaryView s={m.summary} />
            {plan && <div className="mt-3"><IssueList issues={plan.issues} /></div>}
          </Panel>
          <Panel title="Timeline">
            <dl>
              <div className="kv"><dt>Created</dt><dd>{fmtDateTime(m.created_at)}</dd></div>
              <div className="kv"><dt>Started</dt><dd>{fmtDateTime(m.started_at)}</dd></div>
              <div className="kv"><dt>Last paused</dt><dd>{fmtDateTime(m.paused_at)}</dd></div>
              <div className="kv"><dt>Last resumed</dt><dd>{fmtDateTime(m.resumed_at)}</dd></div>
              <div className="kv"><dt>Completed</dt><dd>{fmtDateTime(m.completed_at)}</dd></div>
              <div className="kv"><dt>Cancelled</dt><dd>{fmtDateTime(m.cancelled_at)}</dd></div>
              {m.progress && <div className="kv"><dt>Progress</dt><dd>{m.progress.percent.toFixed(1)}% · {fmtDuration(m.progress.elapsed_s)}</dd></div>}
            </dl>
          </Panel>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="State transitions">
          {ev?.transitions.length ? (
            <ol className="space-y-1 font-mono text-xs">
              {ev.transitions.map((e) => (
                <li key={e.id}><span className="text-fg-faint">{fmtTime(e.ts)}</span> {e.from_state ? `${e.from_state} → ${e.to_state}` : e.type} <span className="text-fg-muted">{e.message}</span></li>
              ))}
            </ol>
          ) : <Empty>None</Empty>}
        </Panel>
        <Panel title="Faults">
          {ev?.faults.length ? (
            <ul className="space-y-1 text-xs">{ev.faults.map((f) => <li key={f.id}><Chip tone={f.severity === "warning" ? "warn" : "bad"}>{f.severity}</Chip> {f.message}</li>)}</ul>
          ) : <Empty>No faults recorded</Empty>}
        </Panel>
        <Panel title={`Detection candidates (${dets.length})`}>
          {dets.length ? (
            <ul className="space-y-1 text-xs">
              {dets.map((d) => (
                <li key={d.id}>
                  {d.simulated && <Chip tone="sim">sim</Chip>} {d.label} · conf {d.confidence ?? "—"} · {d.review_status} · {d.position ? "positioned" : "no position"}
                </li>
              ))}
            </ul>
          ) : <Empty>None</Empty>}
          <Link to="/sensors#detections" className="mt-2 inline-block text-xs text-amber">Review detections →</Link>
        </Panel>
      </div>
    </div>
  );
}
