// Keyboard and pad mapping for manual driving. The backend performs the actual
// tank mixing and enforces every limit; this only decides which button is held.

export type Direction =
  | "stop" | "forward" | "reverse" | "left" | "right"
  | "forward_left" | "forward_right" | "reverse_left" | "reverse_right";

export const KEY_BINDINGS: Record<string, "up" | "down" | "left" | "right"> = {
  ArrowUp: "up", KeyW: "up",
  ArrowDown: "down", KeyS: "down",
  ArrowLeft: "left", KeyA: "left",
  ArrowRight: "right", KeyD: "right",
};

/** Combine the currently held arrow/WASD keys into one direction. */
export function directionFromKeys(held: Set<string>): Direction {
  const up = held.has("up");
  const down = held.has("down");
  const left = held.has("left");
  const right = held.has("right");
  const v = up && !down ? 1 : down && !up ? -1 : 0;
  const h = left && !right ? -1 : right && !left ? 1 : 0;
  if (v === 1 && h === -1) return "forward_left";
  if (v === 1 && h === 1) return "forward_right";
  if (v === -1 && h === -1) return "reverse_left";
  if (v === -1 && h === 1) return "reverse_right";
  if (v === 1) return "forward";
  if (v === -1) return "reverse";
  if (h === -1) return "left";
  if (h === 1) return "right";
  return "stop";
}

/** Clamp joystick deflection to the unit circle and apply a small dead zone. */
export function joystickVector(dx: number, dy: number, radius: number, deadZone = 0.12): { throttle: number; steer: number } {
  let x = dx / radius;
  let y = -dy / radius;
  const mag = Math.hypot(x, y);
  if (mag > 1) {
    x /= mag;
    y /= mag;
  }
  if (Math.hypot(x, y) < deadZone) return { throttle: 0, steer: 0 };
  return { throttle: Math.round(y * 100) / 100, steer: Math.round(x * 100) / 100 };
}

export const DIRECTION_LABEL: Record<Direction, string> = {
  stop: "Stop",
  forward: "Forward",
  reverse: "Reverse",
  left: "Pivot left",
  right: "Pivot right",
  forward_left: "Forward-left",
  forward_right: "Forward-right",
  reverse_left: "Reverse-left",
  reverse_right: "Reverse-right",
};
