import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../lib/api";
import type { Direction } from "../lib/drive";

export type ControlCommand =
  | { mode: "direction"; direction: Direction; speed: number }
  | { mode: "vector"; throttle: number; steer: number; speed: number };

export type ControlLink = "disconnected" | "connecting" | "connected" | "acquired";

interface Ack { seq?: number; accepted?: boolean; left?: number; right?: number; code?: string; message?: string; type: string }

/**
 * Manual control over /ws/control.
 *
 * While a command is held it is re-sent every heartbeat interval; the backend's
 * dead-man lease stops the rover if the heartbeats stop for any reason. Releasing
 * sends an explicit stop. This hook never assumes it is the safety mechanism.
 */
export function useManualControl(clientId: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const seqRef = useRef(1);
  const cmdRef = useRef<ControlCommand | null>(null);
  const hbRef = useRef<number | undefined>(undefined);
  const intervalRef = useRef(100);
  const [link, setLink] = useState<ControlLink>("disconnected");
  const [lastError, setLastError] = useState<{ code: string; message: string } | null>(null);
  const [lastAck, setLastAck] = useState<Ack | null>(null);
  const [active, setActive] = useState<ControlCommand | null>(null);

  const send = useCallback((msg: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }, []);

  const stopHeartbeat = () => {
    window.clearInterval(hbRef.current);
    hbRef.current = undefined;
  };

  const sendCommand = useCallback(() => {
    const c = cmdRef.current;
    if (!c) return;
    send({ type: "drive", seq: seqRef.current++, ...c });
  }, [send]);

  const connect = useCallback(
    (takeover = false) => {
      wsRef.current?.close();
      setLink("connecting");
      setLastError(null);
      const ws = new WebSocket(wsUrl("/ws/control"));
      wsRef.current = ws;
      ws.onopen = () => {
        setLink("connected");
        ws.send(JSON.stringify({ type: "acquire", client_id: clientId, takeover }));
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data) as Ack & Record<string, unknown>;
        if (msg.type === "acquired") {
          intervalRef.current = Math.round(((msg.heartbeat_interval_s as number) ?? 0.1) * 1000);
          seqRef.current = 1;
          setLink("acquired");
        } else if (msg.type === "released") {
          setLink("connected");
        } else if (msg.type === "error") {
          setLastError({ code: msg.code ?? "error", message: msg.message ?? "" });
          if (msg.code === "stale_session" || msg.code === "no_session") setLink("connected");
          if (msg.code === "hold_limit") {
            cmdRef.current = null;
            setActive(null);
            stopHeartbeat();
          }
        } else if (msg.type === "ack") {
          setLastAck(msg);
          if (msg.accepted) setLastError(null);
        }
      };
      ws.onclose = () => {
        stopHeartbeat();
        cmdRef.current = null;
        setActive(null);
        setLink("disconnected");
      };
    },
    [clientId],
  );

  const disconnect = useCallback(() => {
    stopHeartbeat();
    cmdRef.current = null;
    send({ type: "release" });
    wsRef.current?.close();
    wsRef.current = null;
  }, [send]);

  const hold = useCallback(
    (cmd: ControlCommand) => {
      cmdRef.current = cmd;
      setActive(cmd);
      sendCommand();
      if (hbRef.current === undefined) hbRef.current = window.setInterval(sendCommand, intervalRef.current);
    },
    [sendCommand],
  );

  /** Change the held command without an immediate send (joystick drags); the heartbeat carries it. */
  const update = useCallback(
    (cmd: ControlCommand) => {
      if (hbRef.current === undefined) {
        hold(cmd);
        return;
      }
      cmdRef.current = cmd;
      setActive(cmd);
    },
    [hold],
  );

  const release = useCallback(() => {
    stopHeartbeat();
    cmdRef.current = null;
    setActive(null);
    send({ type: "stop", seq: seqRef.current++ });
  }, [send]);

  const estop = useCallback(() => {
    stopHeartbeat();
    cmdRef.current = null;
    setActive(null);
    return send({ type: "estop", seq: seqRef.current++ });
  }, [send]);

  // Stop immediately if the tab is hidden or the window loses focus.
  useEffect(() => {
    const onHide = () => {
      if (cmdRef.current) release();
    };
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("blur", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("blur", onHide);
      window.removeEventListener("pagehide", onHide);
    };
  }, [release]);

  useEffect(() => () => {
    stopHeartbeat();
    wsRef.current?.close();
  }, []);

  return { link, connect, disconnect, hold, update, release, estop, lastError, lastAck, active };
}
