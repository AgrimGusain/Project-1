# Calibrating the open-loop motion model

Without GPS, encoders or an IMU, grid missions are executed from the
calibration model alone:

* side ground speed = `output × calibration.full_speed_mps`
* yaw rate (rad/s) = `(v_left − v_right) / calibration.effective_track_m`
* derived pivot rate at full output = `2 × full_speed_mps / effective_track_m`

The defaults are placeholders, and open-loop distances are meaningless until you
measure them. Expect results to vary with battery charge, surface and payload.
Re-measure when any of these change.

## Procedure (enclosed, flat area; tape measure; stopwatch or phone video)

1. **Prepare.** Fully charge the motor battery. Set `motion.max_output` to the
   value you will survey with (e.g. 0.7). Use the same surface you will survey.
2. **Straight speed.** Drive forward at a fixed speed setting `s` (e.g. 50 % in the
   Drive page) for a timed run of 5–10 s, after the acceleration ramp.
   Measure distance `d` and time `t`. Then
   `full_speed_mps = d / (t × s)`. Repeat three times and average.
   If the rover curves, the two sides differ: trim with `invert`/wiring checks,
   and note it as a limitation (open-loop cannot correct it).
3. **Pivot rate.** Pivot right at output `p` for a counted number of full turns
   `N` in time `t`. Then `ω = 2π·N / t` (rad/s at output p), so
   `ω_full = ω / p` and `effective_track_m = 2 × full_speed_mps / ω_full`.
   Skid-steer chassis scrub, so this is typically 1.5–2.5× the physical track width.
4. **Enter the values** in the YAML and set `calibrated: true`, with a note:

```yaml
calibration:
  full_speed_mps: 0.41
  effective_track_m: 0.44
  calibrated: true
  notes: "2026-10-20, 2S 8.1V, short grass, max_output 0.7"
```

5. **Validate.** Plan a 3 × 1 m grid with pivot turns, run it, and measure where
   the rover actually stops compared with the plan's end point. Record the
   error. This is the honest accuracy of open-loop coverage on that surface.

## Simulation

The simulator's chassis deliberately differs from the calibration model
(`simulation.speed_bias`, `turn_bias`, `wheel_noise`), so open-loop drift is
visible as the gap between the amber commanded estimate and the violet ground
truth.
