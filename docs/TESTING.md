# Testing

```bash
cd backend && python -m pytest            # 124 tests, about 50 s
cd frontend && npm test && npm run build  # unit tests, type-check, production build
```

The backend suite runs entirely in simulation (no GPIO, no motor power). The
vision tests build a tiny synthetic YOLOv8-shaped ONNX model, so no weights are
downloaded.

## Requirement → test map

### Safety, motion and missions

| Requirement | Test |
| --- | --- |
| Starts disarmed; arming checks | `test_api.py::test_health_and_status`, `test_api.py::test_arm_estop_reset_cycle` |
| Lease expiry / dead-man stops motors | `test_motion.py::test_lease_expiry_zeroes_output`, `::test_deadman_stops_when_heartbeats_stop`, `test_api.py::test_ws_manual_control_deadman_and_disconnect` |
| Closing the control socket stops the rover | `test_api.py::test_ws_manual_control_deadman_and_disconnect` |
| Stale/replayed commands rejected | `test_motion.py::test_stale_sequence_rejected`, `::test_old_session_cannot_replay_after_reconnect` |
| Invalid values rejected | `test_motion.py::test_invalid_values_rejected` |
| Speed / accel limits, reversal interlock | `test_motion.py::test_max_output_and_accel_limits`, `::test_reversal_interlock_ramps_through_zero_and_dwells` |
| Continuous-drive limit | `test_motion.py::test_hold_limit` |
| Single motion owner; manual/mission interlock | `test_motion.py::test_only_owner_may_command_and_acquire_conflicts`, `test_mission.py::test_manual_and_mission_interlock` |
| Driver watchdog independent of the event loop | `test_motion.py::test_driver_watchdog_trips_when_updates_stop` |
| E-stop is immediate, latched, never resumes | `test_motion.py::test_emergency_halt_is_immediate_and_invalidates_lease`, `test_mission.py::test_emergency_stop_invalidates_and_never_resumes` |
| Missing measurement ≠ clear path | `test_sensors.py::test_invalid_measurement_not_treated_as_valid`, `::test_stale_measurement_detected` |
| Obstacle pauses mission; resume refused while blocked | `test_mission.py::test_obstacle_pauses_mission` |
| State machine rejects invalid transitions | `test_mission.py::test_state_machine_rejects_invalid_transitions`, `test_api.py::test_mission_crud_and_invalid_transitions` |
| Interrupted mission recovered, not resumed | `test_mission.py::test_interrupted_mission_recovered_after_restart` |
| GPS loss pauses; no invented position | `test_mission.py::test_gps_loss_pauses_mission`, `test_gnss.py::test_no_fix_never_reports_position` |
| GNSS approach and start-distance limit | `test_mission.py::test_gps_mission_approaches_start_and_enforces_distance`, `::test_gps_mission_refused_when_too_far` |
| Coverage planner (alternation, margins, units, determinism, arcs, concave cells, ENU) | `test_planner.py` |

### Optional hardware and extensibility

| # | Requirement | Test |
| --- | --- | --- |
| 1 | App starts with every optional sensor disabled | `test_sensors.py::test_all_optional_sensors_disabled_app_starts` |
| 2 | Manual control works without GPS, magnetometer, IMU, encoders or metal detector | `test_sensors.py::test_manual_driving_without_any_optional_hardware` |
| 3 | Grid planning works without localization hardware | `test_planner.py` (all grid tests), `test_sensors.py::test_required_sensor_failure_blocks_arming_only` |
| 4 | GPS planning works in simulation without a receiver | `test_planner.py::test_gps_plan_in_metres_not_degrees`, `test_mission.py::test_gps_mission_runs_with_simulated_gps` |
| 5 | Missing sensors produce truthful status | `test_sensors.py::test_all_optional_sensors_disabled_app_starts`, `::test_external_adapter_plugs_in` |
| 6 | Disconnection and stale data handled | `test_sensors.py::test_disconnect_and_reconnect`, `::test_stale_measurement_detected` |
| 7 | Invalid measurements not treated as valid | `test_sensors.py::test_invalid_measurement_not_treated_as_valid` |
| 8 | Magnetometer uses structured 3-axis data | `test_sensors.py::test_magnetometer_three_axis_reading`; heading honesty: `::test_uncalibrated_magnetometer_heading_is_unavailable`, `::test_calibrated_level_magnetometer_heading` |
| 9 | Simulated sensors never reported as real | `test_sensors.py::test_simulated_sensors_report_simulated`, `::test_simulated_driver_refused_in_hardware_context`, `::test_hardware_mode_failed_init_never_falls_back_to_simulation`, `test_misc.py::test_capabilities_with_simulated_gps` |
| 10 | New adapter needs no motor/planning changes | `test_sensors.py::test_external_adapter_plugs_in`, `test_misc.py::test_new_capability_registers_without_touching_others` |
| 11 | Capabilities needing absent hardware are disabled | `test_sensors.py::test_all_optional_sensors_disabled_app_starts`, `test_misc.py::test_capabilities_with_simulated_gps`, `::test_vision_enabled_without_model_degrades_gracefully` |
| 12 | Recovery when an optional sensor reconnects | `test_sensors.py::test_disconnect_and_reconnect` |

### Detection and vision

| Requirement | Test |
| --- | --- |
| No events without a detector; simulated events flagged; no invented positions | `test_sensors.py::test_missing_metal_detector_produces_no_events`, `::test_simulated_metal_events_flagged_simulated` |
| YOLOv8 ONNX pipeline (session, metadata names, letterbox, decode, NMS) | `test_vision_onnx.py::test_onnx_pipeline_end_to_end`, `test_misc.py::test_decode_yolov8_output_and_unletterbox` |
| Camera events only for configured classes; simulated flag | `test_vision_onnx.py::test_vision_service_on_simulated_camera` |

## What automated tests cannot cover

GPIO timing, motor direction, real sensor readings, ESP32-CAM streaming and GNSS
reception must be verified on the physical rover. Follow HARDWARE.md
"Verification on the bench" and SENSORS.md "Verifying real readings".
