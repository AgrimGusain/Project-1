import { useState } from "react";
import { CameraView } from "../components/camera";
import { Chip, Notice, Panel } from "../components/ui";
import { httpUrl } from "../lib/api";
import { fmtTime } from "../lib/units";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

export default function Camera() {
  const { telemetry: t } = useTelemetry();
  const [overlay, setOverlay] = useState(true);
  if (!t) return <Loading />;
  const c = t.camera;
  const v = t.vision;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
      <Panel title="ESP32-CAM" actions={
        <label className="flex items-center gap-2 text-xs text-fg-muted">
          <input type="checkbox" checked={overlay} onChange={(e) => setOverlay(e.target.checked)} className="accent-[#f5b83d]" /> Detection overlay
        </label>
      }>
        <CameraView camera={c} vision={v} showDetections={overlay} />
        <div className="mt-3 flex flex-wrap gap-2">
          {c.online && <a className="btn btn-default" href={httpUrl("/api/camera/snapshot", true)} target="_blank" rel="noreferrer">Open snapshot</a>}
        </div>
      </Panel>
      <div className="space-y-4">
        <Panel title="Stream">
          <dl>
            <div className="kv"><dt>Provider</dt><dd>{c.provider}{c.simulated ? " (simulated)" : ""}</dd></div>
            <div className="kv"><dt>Status</dt><dd className={c.online ? "text-ok" : "text-bad"}>{c.online ? "online" : "offline"}</dd></div>
            <div className="kv"><dt>Frame rate</dt><dd>{c.fps.toFixed(1)} fps</dd></div>
            <div className="kv"><dt>Last frame</dt><dd>{fmtTime(c.last_frame_at)}</dd></div>
            <div className="kv"><dt>Frames</dt><dd>{c.frames_total}</dd></div>
            {c.stream_url && <div className="kv"><dt>Source</dt><dd className="truncate">{c.stream_url}</dd></div>}
          </dl>
          {c.error && <div className="mt-2 break-words text-xs text-warn">{c.error}</div>}
          <p className="mt-2 text-xs text-fg-faint">The backend is the only client of the ESP32-CAM and re-serves frames to every browser.</p>
        </Panel>
        <Panel title="Object detection (YOLOv8n)" actions={<Chip tone={v.ready ? "ok" : v.enabled ? "bad" : "muted"}>{v.ready ? "running" : v.enabled ? "unavailable" : "disabled"}</Chip>}>
          <dl>
            <div className="kv"><dt>Backend</dt><dd>{v.backend}</dd></div>
            <div className="kv"><dt>Model</dt><dd className="truncate">{v.model_path}</dd></div>
            <div className="kv"><dt>Classes</dt><dd>{v.classes || "—"}</dd></div>
            <div className="kv"><dt>Inference</dt><dd>{v.inference_ms ? `${v.inference_ms.toFixed(0)} ms` : "—"}</dd></div>
            <div className="kv"><dt>Frames processed</dt><dd>{v.frames_processed}</dd></div>
          </dl>
          {v.error && v.enabled && <div className="mt-2 break-words text-xs text-warn">{v.error}</div>}
          {!v.weights_validated_for_task && (
            <Notice tone="warn" className="mt-3" title="Unvalidated model">
              {v.model_description}. COCO-trained weights detect everyday objects only; they cannot find buried objects or mines. Treat boxes as unverified candidates.
            </Notice>
          )}
          {v.latest && v.latest.detections.length > 0 && (
            <ul className="mt-3 space-y-1 text-sm">
              {v.latest.detections.map((d, i) => (
                <li key={i} className="flex justify-between"><span>{d.label}</span><span className="font-mono">{(d.confidence * 100).toFixed(0)}%</span></li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
