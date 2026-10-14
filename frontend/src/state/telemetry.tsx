import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { wsUrl } from "../lib/api";
import type { LogEvent, SystemInfo, Telemetry } from "../lib/types";

export type LinkState = "connecting" | "live" | "stale" | "offline" | "unauthorized";

interface TelemetryCtx {
  telemetry: Telemetry | null;
  system: SystemInfo | null;
  events: LogEvent[];
  link: LinkState;
  lastUpdate: number | null;
  reconnect: () => void;
}

const Ctx = createContext<TelemetryCtx | null>(null);
const STALE_MS = 2500;

export function TelemetryProvider({ children }: { children: ReactNode }) {
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [link, setLink] = useState<LinkState>("connecting");
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const [generation, setGeneration] = useState(0);
  const lastRef = useRef<number | null>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer: number | undefined;

    const connect = () => {
      setLink("connecting");
      ws = new WebSocket(wsUrl("/ws/telemetry"));
      ws.onmessage = (ev) => {
        let msg: { type: string } & Record<string, unknown>;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        retry = 500;
        if (msg.type === "telemetry") {
          setTelemetry(msg as unknown as Telemetry);
          lastRef.current = Date.now();
          setLastUpdate(lastRef.current);
          setLink("live");
        } else if (msg.type === "event") {
          const e = msg.event as LogEvent;
          setEvents((prev) => (prev.some((p) => p.id === e.id) ? prev : [e, ...prev].slice(0, 300)));
        } else if (msg.type === "hello") {
          setSystem(msg.system as SystemInfo);
          setEvents(((msg.events as LogEvent[]) ?? []).slice().reverse());
        }
      };
      ws.onclose = (ev) => {
        if (closed) return;
        setLink(ev.code === 4401 ? "unauthorized" : "offline");
        timer = window.setTimeout(connect, retry);
        retry = Math.min(retry * 2, 5000);
      };
      ws.onerror = () => ws?.close();
    };
    connect();

    const staleTimer = window.setInterval(() => {
      if (lastRef.current && Date.now() - lastRef.current > STALE_MS) {
        setLink((l) => (l === "live" ? "stale" : l));
      }
    }, 500);

    return () => {
      closed = true;
      window.clearTimeout(timer);
      window.clearInterval(staleTimer);
      ws?.close();
    };
  }, [generation]);

  return (
    <Ctx.Provider value={{ telemetry, system, events, link, lastUpdate, reconnect: () => setGeneration((g) => g + 1) }}>
      {children}
    </Ctx.Provider>
  );
}

export function useTelemetry(): TelemetryCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTelemetry outside provider");
  return ctx;
}

export function useCapability(id: string) {
  const { telemetry } = useTelemetry();
  return telemetry?.capabilities.find((c) => c.id === id) ?? null;
}
