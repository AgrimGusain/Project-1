# Field operations checklist

## Before powering the motors
- [ ] Physical motor e-stop fitted, tested, and within reach of the operator.
- [ ] L298N input pull-downs fitted; ENA/ENB jumpers removed.
- [ ] HC-SR04 Echo level-shifted.
- [ ] Batteries charged; Pi powered separately from the motor supply.
- [ ] `python -m rover --config config/hardware.yaml --check-config` passes.

## Start-up
- [ ] Dashboard shows the expected mode badge (**REAL HARDWARE**, not SIM).
- [ ] *Disarmed*, e-stop clear, no unexpected faults; System → Pre-arm checks are all green.
- [ ] Sensors page: required sensors *connected* and *valid*; values plausible
      (hand test for ultrasonic and IR).
- [ ] Capabilities: note anything *unavailable* or *degraded* and why.
- [ ] Camera online (if used).

## Manual driving
- [ ] Area clear. Arm. Take manual control.
- [ ] Short test in each direction at low speed; confirm release-to-stop.
- [ ] Remember: the rover does not measure its own motion. Watch it.

## Grid mission (no GPS)
- [ ] Mark the near-left corner and the edges on the ground.
- [ ] Place the rover at the start point shown in the planner, facing the shown heading.
- [ ] Load the mission, arm, start. Stand clear during the countdown.
- [ ] Watch the rover, not the screen. Progress is *commanded*, not measured.
- [ ] Pause/stop from the UI; use the physical e-stop for anything unexpected.

## GPS mission
- [ ] GPS status: valid fix with accuracy better than half the lane spacing.
- [ ] Rover within `max_approach_distance_m` of the first waypoint.
- [ ] Check the boundary and route on the map before loading.

## After
- [ ] Disarm. Turn motor power off at the e-stop.
- [ ] Review detection candidates and set their review status.
- [ ] Missions marked **interrupted** need a review note before reuse (Duplicate to re-run).
- [ ] Export or back up `backend/data/rover.db` if required.

## After an emergency stop
1. Make the rover safe physically (motor power off).
2. Find the cause (event log, faults).
3. Reset the e-stop in the UI. The rover stays disarmed and the mission is not resumed.
4. Duplicate the mission if the survey must be repeated.
