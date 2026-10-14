import { useEffect, useState } from "react";
import { httpUrl } from "../lib/api";
import type { CameraStatus, VisionStatus } from "../lib/types";
import { Chip, cx } from "./ui";

/**
 * Shows the backend's re-served MJPEG stream (the backend is the single client of
 * the ESP32-CAM) with optional YOLOv8n detection boxes on top.
 */
export function CameraView({ camera, vision, showDetections = true, className }: {
  camera: CameraStatus | undefined; vision?: VisionStatus; showDetections?: boolean; className?: string;
}) {
  const [nonce, setNonce] = useState(0);
  const [broken, setBroken] = useState(false);
  const online = Boolean(camera?.online);

  // Reconnect the <img> stream when the camera comes back.
  useEffect(() => {
    if (online) {
      setBroken(false);
      setNonce((n) => n + 1);
    }
  }, [online]);

  const dets = showDetections && vision?.latest && (vision.result_age_s ?? 99) < 3 ? vision.latest.detections : [];

  return (
    <div className={cx("relative overflow-hidden rounded-lg bg-ink-950", className)} style={{ aspectRatio: "4 / 3" }}>
      {camera?.provider === "none" || !camera ? (
        <Placeholder title="No camera configured" sub="Set camera.provider in the backend configuration." />
      ) : !online || broken ? (
        <Placeholder title="Camera offline" sub={camera.error ?? "No frames received"} />
      ) : (
        <img
          key={nonce}
          src={httpUrl(`/api/camera/stream?n=${nonce}`, true)}
          alt="Rover camera stream"
          className="h-full w-full object-contain"
          onError={() => setBroken(true)}
        />
      )}
      {online && !broken && dets.map((d, i) => {
        const [x1, y1, x2, y2] = d.box;
        return (
          <div key={i} className="pointer-events-none absolute border-2 border-amber"
            style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }}>
            <span className="absolute -top-5 left-0 whitespace-nowrap rounded bg-amber px-1 text-[10px] font-bold text-ink-950">
              {d.label} {(d.confidence * 100).toFixed(0)}%
            </span>
          </div>
        );
      })}
      <div className="absolute left-2 top-2 flex flex-wrap gap-1">
        {camera?.simulated && <Chip tone="sim">simulated feed</Chip>}
        {camera && camera.provider !== "none" && <Chip tone={online ? "ok" : "bad"}>{online ? `${camera.fps.toFixed(1)} fps` : "offline"}</Chip>}
        {vision?.ready && <Chip tone="info">YOLO {vision.inference_ms ? `${vision.inference_ms.toFixed(0)} ms` : ""}</Chip>}
      </div>
    </div>
  );
}

function Placeholder({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-1 p-4 text-center">
      <svg viewBox="0 0 24 24" className="h-8 w-8 text-fg-faint" fill="none" stroke="currentColor" strokeWidth={1.5} aria-hidden>
        <path d="M3 8h4l2-3h6l2 3h4v11H3zM12 17a4 4 0 100-8 4 4 0 000 8zM3 3l18 18" />
      </svg>
      <div className="text-sm font-semibold text-fg-muted">{title}</div>
      <div className="max-w-xs text-xs text-fg-faint">{sub}</div>
    </div>
  );
}
