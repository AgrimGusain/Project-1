import { NavLink, Outlet } from "react-router-dom";
import { useTelemetry } from "../state/telemetry";
import { EStopButton, EstopBanner, ModeBadge } from "./safety";
import { Chip, Dot, cx, type Tone } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard", icon: "M3 12l9-8 9 8M5 10v10h14V10" },
  { to: "/drive", label: "Drive", icon: "M12 3v18M3 12h18M7 7l10 10M17 7L7 17" },
  { to: "/plan/grid", label: "Grid plan", icon: "M4 4h16v16H4zM4 9h16M4 14h16M9 4v16M14 4v16" },
  { to: "/plan/gps", label: "GPS plan", icon: "M12 21s-7-6.2-7-11a7 7 0 0114 0c0 4.8-7 11-7 11zM12 12a2 2 0 100-4 2 2 0 000 4z" },
  { to: "/mission", label: "Mission", icon: "M5 3v18M5 4h11l-2 4 2 4H5" },
  { to: "/missions", label: "History", icon: "M12 8v5l3 2M21 12a9 9 0 11-3-6.7M21 4v5h-5" },
  { to: "/sensors", label: "Sensors", icon: "M12 12m-2 0a2 2 0 104 0 2 2 0 10-4 0M6.3 6.3a8 8 0 000 11.4M17.7 6.3a8 8 0 010 11.4" },
  { to: "/camera", label: "Camera", icon: "M3 8h4l2-3h6l2 3h4v11H3zM12 17a4 4 0 100-8 4 4 0 000 8z" },
  { to: "/system", label: "System", icon: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a8 8 0 000-6l2-1.5-2-3.5-2.4 1a8 8 0 00-5-3L11.5 0h-4" },
  { to: "/settings", label: "Connection", icon: "M4 12a8 8 0 0116 0M8 12a4 4 0 018 0M12 12v.01" },
];

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={d} />
    </svg>
  );
}

const LINK_META: Record<string, { tone: Tone; label: string }> = {
  live: { tone: "ok", label: "Live" },
  connecting: { tone: "info", label: "Connecting" },
  stale: { tone: "warn", label: "Stale" },
  offline: { tone: "bad", label: "Offline" },
  unauthorized: { tone: "bad", label: "Auth required" },
};

const STATE_TONE: Record<string, Tone> = {
  IDLE: "muted", READY: "info", ARMING: "warn", RUNNING: "ok", PAUSED: "warn", STOPPING: "warn",
  COMPLETED: "ok", FAILED: "bad", EMERGENCY_STOP: "bad",
};

export default function Layout() {
  const { telemetry, link } = useTelemetry();
  const lm = LINK_META[link];
  const s = telemetry?.safety;
  const state = telemetry?.mission.state;
  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-[1000] border-b border-ink-600/70 bg-ink-900/95 backdrop-blur">
        <div className="flex items-center gap-3 px-3 py-2 sm:px-4">
          <div className="flex min-w-0 items-center gap-2">
            <img src="/rover.svg" alt="" className="h-7 w-7" />
            <div className="hidden min-w-0 sm:block">
              <div className="truncate text-sm font-bold tracking-tight">{telemetry?.rover_name ?? "Survey Rover"}</div>
              <div className="text-[10px] uppercase tracking-[0.18em] text-fg-faint">Survey control</div>
            </div>
          </div>
          <ModeBadge compact />
          <div className="ml-auto flex items-center gap-2">
            <Chip tone={lm.tone} className="hidden sm:inline-flex"><Dot tone={lm.tone} pulse={link === "live"} />{lm.label}</Chip>
            {s && (
              <Chip tone={s.armed ? "warn" : "muted"}>{s.armed ? "Armed" : "Disarmed"}</Chip>
            )}
            {state && state !== "IDLE" && <Chip tone={STATE_TONE[state]} className="hidden md:inline-flex">{state.replace("_", " ")}</Chip>}
            <EStopButton />
          </div>
        </div>
        <nav aria-label="Main" className="scrollbar-thin flex gap-1 overflow-x-auto border-t border-ink-700/60 px-2 py-1.5 lg:hidden">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}
              className={({ isActive }) => cx("flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-semibold",
                isActive ? "bg-ink-700 text-amber" : "text-fg-muted")}>
              <Icon d={n.icon} />{n.label}
            </NavLink>
          ))}
        </nav>
        <EstopBanner />
        {link !== "live" && link !== "connecting" && (
          <div className="border-t border-warn/40 bg-warn/10 px-4 py-1.5 text-xs text-warn" role="status">
            {link === "unauthorized"
              ? "The backend requires an access token — set it on the Connection page."
              : "Telemetry is not live. Displayed values may be out of date. The backend dead-man stops manual motion when the link drops."}
          </div>
        )}
      </header>
      <div className="flex flex-1">
        <aside className="hidden w-52 shrink-0 border-r border-ink-600/60 lg:block">
          <nav aria-label="Main" className="sticky top-[60px] space-y-0.5 p-3">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.to === "/"}
                className={({ isActive }) => cx("flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition",
                  isActive ? "bg-ink-700 text-fg shadow-[inset_2px_0_0_0_#f5b83d]" : "text-fg-muted hover:bg-ink-800 hover:text-fg")}>
                <Icon d={n.icon} />{n.label}
              </NavLink>
            ))}
          </nav>
        </aside>
        <main className="min-w-0 flex-1 p-3 sm:p-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
