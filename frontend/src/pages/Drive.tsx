import { useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { CameraView } from "../components/camera";
import { ArmControl, EStopButton } from "../components/safety";
import { Button, Chip, Meter, Notice, Panel, Segmented, cx } from "../components/ui";
import { useManualControl } from "../hooks/useManualControl";
import { DIRECTION_LABEL, KEY_BINDINGS, directionFromKeys, joystickVector, type Direction } from "../lib/drive";
import { useTelemetry } from "../state/telemetry";
import { Loading } from "./Dashboard";

const PAD: (Direction | null)[] = ["forward_left", "forward", "forward_right", "left", "stop", "right", "reverse_left", "reverse", "reverse_right"];
const ARROW: Record<Direction, string> = {
  forward_left: "↖", forward: "↑", forward_right: "↗", left: "⟲", stop: "■", right: "⟳", reverse_left: "↙", reverse: "↓", reverse_right: "↘",
};

function clientId(): string {
  try {
    const k = "rover.clientId";
    let v = localStorage.getItem(k);
    if (!v) {
      v = `ui-${Math.random().toString(36).slice(2, 8)}`;
      localStorage.setItem(k, v);
    }
    return v;
  } catch {
    return `ui-${Math.random().toString(36).slice(2, 8)}`;
  }
}

export default function Drive() {
  const { telemetry: t } = useTelemetry();
  const id = useMemo(clientId, []);
  const ctl = useManualControl(id);
  const [speed, setSpeed] = useState<number | null>(null);
  const [input, setInput] = useState<"pad" | "joystick">("pad");
  const keysRef = useRef(new Set<string>());

  const maxSpeed = t?.manual.max_speed ?? 0.8;
  const spd = speed ?? t?.manual.default_speed ?? 0.5;
  const acquired = ctl.link === "acquired";

  // Keyboard: arrows/WASD to drive, Space to stop, Esc for e-stop.
  useEffect(() => {
    const apply = () => {
      const dir = directionFromKeys(keysRef.current);
      if (dir === "stop") ctl.release();
      else ctl.hold({ mode: "direction", direction: dir, speed: spd });
    };
    const down = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "SELECT") return;
      if (e.code === "Escape") {
        e.preventDefault();
        ctl.estop();
        return;
      }
      if (!acquired) return;
      if (e.code === "Space") {
        e.preventDefault();
        keysRef.current.clear();
        ctl.release();
        return;
      }
      const b = KEY_BINDINGS[e.code];
      if (!b || e.repeat) {
        if (b) e.preventDefault();
        return;
      }
      e.preventDefault();
      keysRef.current.add(b);
      apply();
    };
    const up = (e: KeyboardEvent) => {
      const b = KEY_BINDINGS[e.code];
      if (!b) return;
      keysRef.current.delete(b);
      if (acquired) apply();
    };
    const blur = () => keysRef.current.clear();
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, [acquired, ctl, spd]);

  if (!t) return <Loading />;
  const missionOwns = t.motion.owner === "mission";
  const otherClient = t.manual.session_active && !acquired;
  const front = t.safety.obstacles?.front;

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
      <div className="space-y-4">
        <Panel title="Camera">
          <CameraView camera={t.camera} vision={t.vision} />
        </Panel>
        <Panel title="Motor outputs">
          <div className="grid grid-cols-2 gap-6">
            {(["left", "right"] as const).map((side) => (
              <div key={side} className="space-y-2">
                <div className="text-xs font-semibold uppercase tracking-wider text-fg-muted">{side} track</div>
                <OutputBar label="commanded" value={t.motion.requested[side]} tone="info" />
                <OutputBar label="applied (after limits)" value={t.motion.applied[side]} tone={t.simulated ? "sim" : "warn"} />
              </div>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            {t.motion.blocked_reason && <Chip tone="bad">{t.motion.blocked_reason}</Chip>}
            {t.motion.limited_reason && <Chip tone="warn">{t.motion.limited_reason}</Chip>}
            {t.motion.interlock_active && <Chip tone="info">reversal interlock</Chip>}
            {t.motion.driver.watchdog_tripped && <Chip tone="bad">driver watchdog tripped</Chip>}
          </div>
          <p className="mt-2 text-xs text-fg-faint">Applied output is what the motor driver receives. There is no wheel-speed feedback: actual motion is not measured.</p>
        </Panel>
      </div>

      <div className="space-y-4">
        <Panel title="Arming"><ArmControl /></Panel>
        <Panel
          title="Manual control"
          actions={<Chip tone={acquired ? "ok" : ctl.link === "connecting" ? "info" : "muted"}>{acquired ? "in control" : ctl.link}</Chip>}
        >
          {missionOwns && <Notice tone="warn" className="mb-3">An autonomous mission owns the motors. Stop the mission to drive manually.</Notice>}
          {front?.blocked && <Notice tone="bad" className="mb-3" title="Forward motion blocked">{front.reasons.join("; ") || front.state}</Notice>}

          {!acquired ? (
            <div className="space-y-2">
              <Button variant="primary" className="w-full" disabled={missionOwns || ctl.link === "connecting"} onClick={() => ctl.connect(false)}>
                Take manual control
              </Button>
              {(otherClient || ctl.lastError?.code === "session_busy") && (
                <Button className="w-full" onClick={() => ctl.connect(true)}>Take over from {t.manual.client_id ?? "other client"}</Button>
              )}
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <Segmented value={input} onChange={setInput} ariaLabel="Input method"
                  options={[{ value: "pad", label: "Buttons" }, { value: "joystick", label: "Joystick" }]} />
                <Button variant="ghost" onClick={ctl.disconnect}>Release</Button>
              </div>

              {input === "pad" ? (
                <div className="mx-auto grid max-w-[300px] grid-cols-3 gap-2" role="group" aria-label="Drive buttons">
                  {PAD.map((d) => d && (
                    <HoldButton key={d} label={DIRECTION_LABEL[d]} glyph={ARROW[d]} stop={d === "stop"}
                      active={ctl.active?.mode === "direction" && ctl.active.direction === d}
                      onHold={() => (d === "stop" ? ctl.release() : ctl.hold({ mode: "direction", direction: d, speed: spd }))}
                      onRelease={ctl.release} />
                  ))}
                </div>
              ) : (
                <Joystick onMove={(v) => ctl.update({ mode: "vector", ...v, speed: spd })} onRelease={ctl.release} />
              )}

              <div>
                <label className="label" htmlFor="speed">Speed limit {Math.round(spd * 100)}% (max {Math.round(maxSpeed * 100)}%)</label>
                <input id="speed" type="range" min={0.15} max={maxSpeed} step={0.05} value={spd}
                  onChange={(e) => setSpeed(Number(e.target.value))} className="w-full accent-[#f5b83d]" />
              </div>

              <DeadmanStatus t={t} holding={Boolean(ctl.active)} />
              {ctl.lastError && <Notice tone="warn" title={ctl.lastError.code.replace(/_/g, " ")}>{ctl.lastError.message}</Notice>}
              <p className="text-xs text-fg-faint">
                Keyboard: arrows / WASD (combine for diagonals) · Space stop · Esc e-stop. Releasing any control stops the rover.
                Hidden tab or lost focus stops immediately; the server dead-man stops motion within {Math.round(t.manual.deadman_timeout_s * 1000)} ms if updates stop.
              </p>
            </div>
          )}
        </Panel>
        <div className="xl:hidden"><EStopButton large onControlWs={ctl.estop} /></div>
      </div>
    </div>
  );
}

function OutputBar({ label, value, tone }: { label: string; value: number; tone: "info" | "warn" | "sim" }) {
  return (
    <div>
      <div className="flex justify-between text-[11px] text-fg-muted"><span>{label}</span><span className="font-mono">{(value * 100).toFixed(0)}%</span></div>
      <div className="relative mt-1 h-2 rounded-full bg-ink-700">
        <div className="absolute left-1/2 top-0 h-2 w-px bg-ink-500" />
        <div className={cx("absolute top-0 h-2 rounded-full", { info: "bg-info", warn: "bg-warn", sim: "bg-sim" }[tone], value >= 0 ? "left-1/2" : "right-1/2")}
          style={{ width: `${Math.min(50, Math.abs(value) * 50)}%` }} />
      </div>
    </div>
  );
}

function DeadmanStatus({ t, holding }: { t: NonNullable<ReturnType<typeof useTelemetry>["telemetry"]>; holding: boolean }) {
  const lease = t.motion.lease_remaining_s;
  const cont = t.manual.continuous_s;
  return (
    <div className="rounded-lg border border-ink-600 bg-ink-900 p-3 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-fg-muted">Dead-man lease</span>
        <span className={cx("font-mono", holding && lease ? "text-ok" : "text-fg-faint")}>{holding && lease !== null ? `${Math.round(lease * 1000)} ms` : "idle"}</span>
      </div>
      <div className="mt-2 flex items-center justify-between">
        <span className="text-fg-muted">Continuous drive</span>
        <span className="font-mono">{cont.toFixed(1)} / {t.manual.max_continuous_s.toFixed(0)} s</span>
      </div>
      <Meter value={cont} max={t.manual.max_continuous_s} tone={cont > t.manual.max_continuous_s * 0.8 ? "warn" : "info"} label="continuous drive" />
      {t.manual.deadman_trips > 0 && <div className="mt-2 text-warn">Dead-man stops this session: {t.manual.deadman_trips}</div>}
    </div>
  );
}

function HoldButton({ label, glyph, onHold, onRelease, active, stop }: {
  label: string; glyph: string; onHold: () => void; onRelease: () => void; active: boolean; stop?: boolean;
}) {
  const down = (e: RPointerEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    onHold();
  };
  const up = (e: RPointerEvent<HTMLButtonElement>) => {
    e.preventDefault();
    if (!stop) onRelease();
  };
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onPointerDown={down}
      onPointerUp={up}
      onPointerCancel={up}
      onLostPointerCapture={() => !stop && onRelease()}
      onContextMenu={(e) => e.preventDefault()}
      className={cx(
        "aspect-square touch-none select-none rounded-xl border text-3xl font-bold transition",
        stop ? "border-bad/50 bg-bad/15 text-bad" : active ? "border-amber bg-amber text-ink-950" : "border-ink-500 bg-ink-700 text-fg active:bg-ink-600",
      )}
    >
      {glyph}
    </button>
  );
}

function Joystick({ onMove, onRelease }: { onMove: (v: { throttle: number; steer: number }) => void; onRelease: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [active, setActive] = useState(false);
  const R = 110;
  const handle = (e: RPointerEvent<HTMLDivElement>) => {
    const rect = ref.current!.getBoundingClientRect();
    const dx = e.clientX - (rect.left + rect.width / 2);
    const dy = e.clientY - (rect.top + rect.height / 2);
    const mag = Math.hypot(dx, dy);
    const k = mag > R ? R / mag : 1;
    setKnob({ x: dx * k, y: dy * k });
    onMove(joystickVector(dx, dy, R));
  };
  const end = () => {
    setActive(false);
    setKnob({ x: 0, y: 0 });
    onRelease();
  };
  return (
    <div
      ref={ref}
      className="relative mx-auto aspect-square w-full max-w-[260px] touch-none select-none rounded-full border border-ink-500 bg-[radial-gradient(circle,#1b2430_0%,#10151c_70%)]"
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        setActive(true);
        handle(e);
      }}
      onPointerMove={(e) => active && handle(e)}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={() => active && end()}
      role="application"
      aria-label="Joystick: drag to drive, release to stop"
    >
      <div className="absolute left-1/2 top-2 -translate-x-1/2 text-[10px] uppercase tracking-widest text-fg-faint">fwd</div>
      <div className="absolute inset-1/2 h-px w-px">
        <div
          className={cx("absolute -left-9 -top-9 h-[72px] w-[72px] rounded-full border-2 transition-colors", active ? "border-amber bg-amber/80" : "border-ink-500 bg-ink-600")}
          style={{ transform: `translate(${knob.x}px, ${knob.y}px)` }}
        />
      </div>
    </div>
  );
}
