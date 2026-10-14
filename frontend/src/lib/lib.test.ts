import { describe, expect, it } from "vitest";
import { directionFromKeys, joystickVector } from "./drive";
import { convert, fmtDistance, fmtDuration, toMetres } from "./units";

describe("units", () => {
  it("converts consistently", () => {
    expect(toMetres(250, "cm")).toBeCloseTo(2.5);
    expect(toMetres(1500, "mm")).toBeCloseTo(1.5);
    expect(convert(2.5, "m", "cm")).toBe(250);
    expect(convert(250, "cm", "mm")).toBe(2500);
  });
  it("formats", () => {
    expect(fmtDistance(0.42)).toBe("42 cm");
    expect(fmtDistance(12.345)).toBe("12.35 m");
    expect(fmtDuration(125)).toBe("2m 05s");
    expect(fmtDistance(null)).toBe("—");
  });
});

describe("drive mapping", () => {
  it("combines keys", () => {
    expect(directionFromKeys(new Set(["up"]))).toBe("forward");
    expect(directionFromKeys(new Set(["up", "left"]))).toBe("forward_left");
    expect(directionFromKeys(new Set(["down", "right"]))).toBe("reverse_right");
    expect(directionFromKeys(new Set(["up", "down"]))).toBe("stop");
    expect(directionFromKeys(new Set())).toBe("stop");
  });
  it("joystick dead zone and clamp", () => {
    expect(joystickVector(2, 2, 100)).toEqual({ throttle: 0, steer: 0 });
    const v = joystickVector(0, -200, 100);
    expect(v.throttle).toBe(1);
    expect(v.steer).toBe(0);
  });
});
