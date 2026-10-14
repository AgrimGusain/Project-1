import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useMemo } from "react";
import { Circle, CircleMarker, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import type { CoveragePlan, GeoPoint, LocalizationEstimate } from "../lib/types";

const vertexIcon = (index: number, start: boolean) =>
  L.divIcon({
    className: "",
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    html: `<div style="width:22px;height:22px;border-radius:999px;background:${start ? "#3ecf8e" : "#f5b83d"};color:#07090c;
      font:700 11px/22px system-ui;text-align:center;border:2px solid #07090c;box-shadow:0 0 0 2px ${start ? "#3ecf8e55" : "#f5b83d55"}">${index + 1}</div>`,
  });

function ClickToAdd({ enabled, onAdd }: { enabled: boolean; onAdd: (p: GeoPoint) => void }) {
  useMapEvents({
    click(e) {
      if (enabled) onAdd({ lat: e.latlng.lat, lon: e.latlng.lng });
    },
  });
  return null;
}

function FitTo({ points, token }: { points: GeoPoint[]; token: number }) {
  const map = useMap();
  useEffect(() => {
    if (points.length >= 2) map.fitBounds(L.latLngBounds(points.map((p) => [p.lat, p.lon])), { padding: [30, 30], maxZoom: 21 });
    else if (points.length === 1) map.setView([points[0].lat, points[0].lon], 19);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);
  return null;
}

export interface GeoMapProps {
  center: GeoPoint;
  tileUrl: string;
  attribution: string;
  maxZoom: number;
  boundary: GeoPoint[];
  onBoundaryChange?: (pts: GeoPoint[]) => void;
  editing?: boolean;
  startVertex?: number;
  plan?: CoveragePlan | null;
  completedSegments?: number;
  position?: LocalizationEstimate | null;
  trace?: GeoPoint[];
  fitToken?: number;
  detections?: { lat: number; lon: number; simulated: boolean }[];
  height?: string;
}

export default function GeoMap({
  center, tileUrl, attribution, maxZoom, boundary, onBoundaryChange, editing = false, startVertex = 0, plan,
  completedSegments = 0, position, trace, fitToken = 0, detections = [], height = "520px",
}: GeoMapProps) {
  const segs = useMemo(() => plan?.segments.filter((s) => s.geo_points && s.kind !== "pivot") ?? [], [plan]);
  const update = (i: number, p: GeoPoint) => onBoundaryChange?.(boundary.map((b, k) => (k === i ? p : b)));
  const remove = (i: number) => onBoundaryChange?.(boundary.filter((_, k) => k !== i));

  return (
    <div style={{ height }} className="overflow-hidden rounded-xl border border-ink-600">
      <MapContainer center={[center.lat, center.lon]} zoom={19} maxZoom={maxZoom} className="h-full w-full" zoomControl>
        <TileLayer url={tileUrl} attribution={attribution} maxZoom={maxZoom} maxNativeZoom={19} />
        <ClickToAdd enabled={editing} onAdd={(p) => onBoundaryChange?.([...boundary, p])} />
        <FitTo points={boundary.length ? boundary : [center]} token={fitToken} />
        {boundary.length >= 2 && (
          <Polygon positions={boundary.map((p) => [p.lat, p.lon])}
            pathOptions={{ color: "#f5b83d", weight: 2, dashArray: "6 4", fillOpacity: 0.06 }} />
        )}
        {segs.map((s) => (
          <Polyline key={s.index} positions={s.geo_points!.map((p) => [p.lat, p.lon])}
            pathOptions={{
              color: !s.within_boundary ? "#ff5a5f" : s.index < completedSegments ? "#3ecf8e" : s.kind === "lane" ? "#5aa9ff" : "#9aa7b8",
              weight: s.kind === "lane" ? 3 : 2, opacity: s.index < completedSegments ? 0.6 : 0.9,
              dashArray: s.kind === "transit" ? "4 4" : undefined,
            }} />
        ))}
        {trace && trace.length > 1 && (
          <Polyline positions={trace.map((p) => [p.lat, p.lon])} pathOptions={{ color: "#3ecf8e", weight: 2 }} />
        )}
        {editing && boundary.map((p, i) => (
          <Marker key={i} position={[p.lat, p.lon]} draggable icon={vertexIcon(i, i === startVertex)}
            eventHandlers={{
              dragend: (e) => {
                const ll = (e.target as L.Marker).getLatLng();
                update(i, { lat: ll.lat, lon: ll.lng });
              },
              contextmenu: () => remove(i),
              dblclick: () => remove(i),
            }}>
            <Tooltip direction="top" offset={[0, -10]}>Vertex {i + 1}{i === startVertex ? " (start)" : ""} — drag to move, double-click to delete</Tooltip>
          </Marker>
        ))}
        {!editing && boundary.map((p, i) => (
          <CircleMarker key={i} center={[p.lat, p.lon]} radius={4} pathOptions={{ color: i === startVertex ? "#3ecf8e" : "#f5b83d", fillOpacity: 1 }} />
        ))}
        {detections.map((d, i) => (
          <CircleMarker key={`d${i}`} center={[d.lat, d.lon]} radius={7}
            pathOptions={{ color: d.simulated ? "#b197fc" : "#ff5a5f", fillOpacity: 0.4 }}>
            <Tooltip>{d.simulated ? "Simulated detection candidate" : "Detection candidate (unverified)"}</Tooltip>
          </CircleMarker>
        ))}
        {position && position.position_valid && position.lat !== null && position.lon !== null && (
          <>
            {position.horizontal_accuracy_m !== null && (
              <Circle center={[position.lat, position.lon]} radius={position.horizontal_accuracy_m}
                pathOptions={{ color: position.simulated ? "#b197fc" : "#3ecf8e", weight: 1, fillOpacity: 0.12 }} />
            )}
            <CircleMarker center={[position.lat, position.lon]} radius={7}
              pathOptions={{ color: "#07090c", weight: 2, fillColor: position.simulated ? "#b197fc" : "#3ecf8e", fillOpacity: 1 }}>
              <Tooltip permanent direction="right" offset={[8, 0]}>
                {position.simulated ? "Simulated GPS" : "GPS"} ±{position.horizontal_accuracy_m?.toFixed(2) ?? "?"} m
              </Tooltip>
            </CircleMarker>
          </>
        )}
      </MapContainer>
    </div>
  );
}
