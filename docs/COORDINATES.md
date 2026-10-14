# Coordinate conventions

## Local frame (metres)

* `x` → right / east, `y` → forward / north. Right-handed with `z` up.
* **Heading**: compass degrees. `0°` = +y, `90°` = +x, **clockwise positive**, range [0, 360).
* Signed rotations (`rotation_deg` in segments) are clockwise-positive. A pivot
  of `-90` turns left.
* Rover body frame: `x` right, `y` forward, `z` up. Sensor `mount` offsets use this frame.

## Grid surveys (no GPS)

```
 far_left (0, L) ┌──────────────┐ far_right (W, L)
                 │              │
                 │   survey     │      L = length (along +y)
                 │   area       │      W = width  (along +x)
                 │              │
 near_left (0,0) └──────────────┘ near_right (W, 0)
                    operator side
```

* Origin at the **near-left** corner, standing on the near edge facing the far edge.
* `lane_orientation: along_length` gives lanes along ±y (heading 0°/180°);
  `along_width` gives lanes along ±x (90°/270°).
* Lanes sit `swath/2` inside the margin-inset boundary and every `lane_spacing`
  after that, starting from the start-corner side. The final lane is clamped so it
  never leaves the inner boundary, so the last gap may be narrower.
* `start_pose` is where the operator places the rover: the first lane's start,
  facing the first lane (or `initial_heading_deg`; a pivot is then inserted).
  The UI prints it ("0.35 m right, 0.10 m forward of the near-left corner").
* Internally all lengths are metres. The UI accepts m, cm or mm and sends
  `units`; the backend converts.

## GPS surveys

* Boundaries are WGS-84 latitude/longitude.
* Planning uses a **local East-North-Up tangent plane** centred on the mean of the
  boundary vertices: x = east, y = north (the same conventions as above, so
  headings are true bearings). Conversions are exact ECEF↔ENU
  (`rover/planning/geo.py`).
* Lane spacing, margins and turn radii are in metres in the ENU frame and
  are never computed in degrees.
* Operating-area limit: `localization.geo_max_extent_m` (default 2 km). Flat
  tangent-plane error is well under a decimetre at that size.
* Every segment carries both local `points` and `geo_points`.
* `lane_heading_deg: null` chooses the bearing of the longest side of the
  minimum rotated bounding rectangle (fewest lanes and turns).

## Path segments

| Kind | Meaning |
| --- | --- |
| `lane` | survey sweep (sensor active area) |
| `pivot` | rotate in place, length 0, `rotation_deg` signed |
| `arc` | constant-radius turn (`turn_radius_m`, `arc_center`) |
| `shift` | straight move between adjacent lanes (pivot strategy, or Dubins straight part) |
| `transit` | move between disconnected cells of a concave boundary |

Waypoints are typed (`start`, `lane_start`, `lane_end`, `end`). Plans are
deterministic: identical inputs produce identical `plan_hash`.
