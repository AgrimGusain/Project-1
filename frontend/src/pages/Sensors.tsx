import { useCallback, useEffect, useState } from "react";
import { SensorCard } from "../components/status";
import { CAP_LABEL, CAP_TONE, Chip, Empty, ErrorList, Notice, Panel, Select } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { DetectionEvent } from "../lib/types";
import { fmtDateTime } from "../lib/units";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

const REVIEW = [
  { value: "unreviewed", label: "Unreviewed" },
  { value: "confirmed_signal", label: "Confirmed signal" },
  { value: "false_positive", label: "False positive" },
  { value: "needs_follow_up", label: "Needs follow-up" },
  { value: "dismissed", label: "Dismissed" },
] as const;

export default function Sensors() {
  const { telemetry: t } = useTelemetry();
  if (!t) return <Loading />;
  const enabled = t.sensors.filter((s) => s.enabled);
  const disabled = t.sensors.filter((s) => !s.enabled);
  const capIds = ["obstacle_sensing", "magnetometer", "imu", "encoder_odometry", "heading_estimation", "detection_logging", "battery_monitoring", "temperature_monitoring"];
  const caps = t.capabilities.filter((c) => capIds.includes(c.id));
  const mag = t.localization.magnetic_heading;

  return (
    <div className="space-y-4">
      <Notice tone="info">
        Every sensor is optional. A missing or failing device only disables the features that depend on it. Simulated devices are always labelled,
        and drivers marked “unverified” have not yet been tested against the physical hardware.
      </Notice>
      <Panel title="Sensor capabilities">
        <div className="flex flex-wrap gap-2">
          {caps.map((c) => (
            <span key={c.id} title={[c.summary, ...c.reasons].join("\n")}>
              <Chip tone={CAP_TONE[c.status]}>{c.label}: {CAP_LABEL[c.status]}</Chip>
            </span>
          ))}
        </div>
      </Panel>
      <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
        {enabled.map((s) => <SensorCard key={s.id} s={s} heading={mag.find((h) => h.sensor_id === s.id)} />)}
      </div>
      {disabled.length > 0 && (
        <Panel title="Configured but disabled (not installed)">
          <ul className="grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
            {disabled.map((s) => (
              <li key={s.id} className="rounded-lg border border-ink-600 bg-ink-900 px-3 py-2">
                <div className="font-medium">{s.label}</div>
                <div className="font-mono text-[11px] text-fg-faint">{s.id} · {s.type} · {s.driver}</div>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-fg-faint">Enable a device in the backend configuration (<code>enabled: true</code>) once it is fitted, then restart the backend.</p>
        </Panel>
      )}
      <Detections />
    </div>
  );
}

function Detections() {
  const [items, setItems] = useState<DetectionEvent[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const load = useCallback(() => {
    api<DetectionEvent[]>(`/api/detections?limit=200${filter ? `&review_status=${filter}` : ""}`).then(setItems).catch(setError);
  }, [filter]);
  useEffect(() => {
    load();
    const h = window.setInterval(load, 5000);
    return () => window.clearInterval(h);
  }, [load]);

  const review = async (id: number, status: string) => {
    try {
      await api(`/api/detections/${id}`, { method: "PATCH", json: { status } });
      load();
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <Panel title={`Detection candidates (${items.length})`} actions={
      <div className="w-44"><Select value={filter} onChange={setFilter} ariaLabel="Review filter" options={[{ value: "", label: "All" }, ...REVIEW.map((r) => ({ value: r.value, label: r.label }))]} /></div>
    }>
      <div id="detections" />
      <Notice tone="warn" className="mb-3">
        A detection is a sensor signal above a threshold — not confirmation of any object. Confidence is signal strength, not probability of a hazard.
        Nothing here is a validated landmine detector.
      </Notice>
      {error && <ErrorList title={error.message} />}
      {items.length === 0 ? <Empty>No detection candidates recorded</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-fg-muted">
              <tr><th className="py-2">Time</th><th>Source</th><th>Signal</th><th>Confidence</th><th>Position</th><th>Localization</th><th>Review</th></tr>
            </thead>
            <tbody className="divide-y divide-ink-700">
              {items.map((d) => (
                <tr key={d.id}>
                  <td className="py-2 text-fg-muted">{fmtDateTime(d.ts)}</td>
                  <td>{d.simulated && <Chip tone="sim" className="mr-1">sim</Chip>}{d.source_kind} · {d.label}</td>
                  <td className="font-mono">{d.reading?.toFixed(3) ?? "—"}</td>
                  <td className="font-mono">{d.confidence?.toFixed(2) ?? "—"}</td>
                  <td className="font-mono text-xs">{d.position ? (d.position.lat != null ? `${d.position.lat.toFixed(6)}, ${d.position.lon?.toFixed(6)}` : `${d.position.x?.toFixed(2)}, ${d.position.y?.toFixed(2)}`) : <span className="text-fg-faint">none</span>}</td>
                  <td className="text-xs text-fg-muted">{d.localization_quality}</td>
                  <td className="w-44"><Select value={d.review_status} onChange={(v) => review(d.id, v)} ariaLabel="Review status" options={REVIEW.map((r) => ({ value: r.value, label: r.label }))} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
