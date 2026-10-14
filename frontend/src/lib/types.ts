// Types mirroring the backend API (rover/api, rover/runtime.telemetry).

export type OperatingMode = "simulation" | "hardware" | "hardware_gps";
export type MissionState =
  | "IDLE" | "READY" | "ARMING" | "RUNNING" | "PAUSED" | "STOPPING" | "COMPLETED" | "FAILED" | "EMERGENCY_STOP";
export type Severity = "info" | "warning" | "error" | "critical";
export type CapabilityStatus = "ready" | "simulated" | "degraded" | "unavailable" | "not_installed";

export interface Capability {
  id: string;
  label: string;
  status: CapabilityStatus;
  summary: string;
  reasons: string[];
  depends_on: string[];
  usable: boolean;
}

export interface Fault {
  code: string;
  severity: Severity;
  source: string;
  message: string;
  first_seen: string;
  last_seen: string;
  count: number;
  blocks_arming: boolean;
  blocks_motion: boolean;
}

export interface LogEvent {
  id: number;
  ts: string;
  level: Severity;
  source: string;
  code: string;
  message: string;
  mission_id: string | null;
  data: Record<string, unknown>;
}

export interface SensorReading {
  sensor_id: string;
  sensor_type: string;
  timestamp: string;
  value: Record<string, number | boolean | null>;
  units: Record<string, string>;
  valid: boolean;
  simulated: boolean;
  error: string | null;
  metadata: Record<string, unknown>;
}

export interface SensorStatus {
  id: string;
  type: string;
  label: string;
  driver: string;
  enabled: boolean;
  simulated: boolean;
  physically_verified: boolean;
  required_for_arming: boolean;
  state: "disabled" | "configured" | "initializing" | "connected" | "disconnected" | "faulty";
  reading_health: "ok" | "invalid" | "stale" | "none";
  available: boolean;
  error: string | null;
  rate_hz: number;
  stale_after_s: number;
  age_s: number | null;
  reconnects: number;
  readings_total: number;
  invalid_total: number;
  last_reading: SensorReading | null;
  last_valid_at: string | null;
  calibration: Record<string, unknown> & { state?: string };
  safety_role: string | null;
}

export interface DirectionAssessment {
  state: "clear" | "obstacle" | "unavailable" | "invalid" | "stale" | "disabled";
  distance_m: number | null;
  blocked: boolean;
  slow: boolean;
  reasons: string[];
}

export interface SafetySnapshot {
  armed: boolean;
  armed_at: string | null;
  armed_by: string | null;
  simulated_arming: boolean;
  estop_latched: boolean;
  estop_reason: string | null;
  estop_source: string | null;
  estop_at: string | null;
  estop_input: { configured: boolean; pressed: boolean | null; error: string | null; hardware_cutoff: boolean };
  motion_permitted: boolean;
  motion_blocked_reason: string | null;
  obstacles: { front: DirectionAssessment; rear: DirectionAssessment; sensors: unknown[] } | null;
}

export interface LR { left: number; right: number }

export interface MotionSnapshot {
  owner: "none" | "manual" | "mission";
  holder_id: string | null;
  requested: LR;
  target: LR;
  applied: LR;
  lease_remaining_s: number | null;
  blocked_reason: string | null;
  limited_reason: string | null;
  interlock_active: boolean;
  control_loop_ok: boolean;
  max_output: number;
  driver: { driver: string; simulated: boolean; healthy: boolean; error: string | null; watchdog_tripped?: boolean };
}

export interface ManualSnapshot {
  session_active: boolean;
  client_id: string | null;
  last_message_age_s: number | null;
  deadman_timeout_s: number;
  heartbeat_interval_s: number;
  max_continuous_s: number;
  continuous_s: number;
  hold_limit_hit: boolean;
  deadman_trips: number;
  default_speed: number;
  max_speed: number;
}

export interface Pose { x: number; y: number; heading_deg: number }

export interface MissionProgress {
  segment_index: number;
  segment_count: number;
  segment_fraction: number;
  lanes_completed: number;
  lane_count: number;
  percent: number;
  distance_commanded_m: number;
  phase: string;
  elapsed_s: number;
  cross_track_m: number | null;
  execution: "open_loop" | "closed_loop" | "gnss";
  estimated_pose: Pose | null;
  estimated_pose_kind: string;
  measured_pose: Pose | null;
}

export interface ActiveMissionInfo {
  id: string;
  name: string;
  kind: "grid" | "gps";
  execution: "open_loop" | "closed_loop" | "gnss";
  pause_reason: string | null;
  invalidated: string | null;
  message: string;
  progress: MissionProgress | null;
  plan_hash: string;
  summary: PlanSummary;
  localization_note: string;
  trace?: [number, number][];
}

export interface MissionSnapshot {
  state: MissionState;
  last_result: { mission_id: string; status: string; reason: string } | null;
  mission: ActiveMissionInfo | null;
  plan?: CoveragePlan;
}

export interface LocalizationEstimate {
  provider: string;
  kind: "none" | "local" | "geodetic";
  simulated: boolean;
  status: "ok" | "degraded" | "no_fix" | "disconnected" | "unavailable" | "starting";
  message: string;
  position_valid: boolean;
  x: number | null;
  y: number | null;
  lat: number | null;
  lon: number | null;
  alt: number | null;
  horizontal_accuracy_m: number | null;
  accuracy_source: string | null;
  heading_valid: boolean;
  heading_deg: number | null;
  heading_source: string | null;
  speed_mps: number | null;
  fix_quality: number | null;
  satellites: number | null;
  hdop: number | null;
  timestamp: string | null;
  age_s: number | null;
  experimental: boolean;
}

export interface MagneticHeading {
  sensor_id: string;
  available: boolean;
  reliable: boolean;
  heading_deg: number | null;
  reasons: string[];
  field_ut: { x: number; y: number; z: number; magnitude: number } | null;
  calibrated?: boolean;
  simulated: boolean;
  used_for_navigation: boolean;
}

export interface LocalizationSnapshot {
  provider: { provider: string; kind: string; simulated: boolean; experimental: boolean; stationary_heading: boolean };
  estimate: LocalizationEstimate;
  magnetic_heading: MagneticHeading[];
  fusion: { implemented: string[]; not_implemented: string[] };
}

export interface CameraStatus {
  provider: string;
  simulated: boolean;
  online: boolean;
  fps: number;
  last_frame_at: string | null;
  last_frame_age_s: number | null;
  frames_total: number;
  error: string | null;
  stream_url?: string;
}

export interface VisionDetection { label: string; class_id: number; confidence: number; box: [number, number, number, number] }

export interface VisionStatus {
  enabled: boolean;
  backend: string;
  model_path: string;
  model_description: string;
  weights_validated_for_task: boolean;
  ready: boolean;
  error: string | null;
  classes: number;
  frames_processed: number;
  inference_ms: number | null;
  result_age_s: number | null;
  on_simulated_camera: boolean;
  latest: { model: string; detections: VisionDetection[]; inference_ms: number; width: number; height: number; frame_seq: number } | null;
}

export interface SimSnapshot {
  speed_factor: number;
  truth: { pose: Pose; speed_mps: number; yaw_rate_dps: number; collided: boolean; sim_time_s: number };
  faults: string[];
  available_faults: Record<string, string>;
  obstacles: { type: "circle" | "rect"; x: number; y: number; r?: number; w?: number; h?: number }[];
  metal_targets: { x: number; y: number; strength: number }[];
  geo_origin: { lat: number; lon: number; alt: number };
}

export interface Telemetry {
  type: "telemetry";
  seq: number;
  ts: string;
  mode: OperatingMode;
  simulated: boolean;
  rover_name: string;
  safety: SafetySnapshot;
  motion: MotionSnapshot;
  manual: ManualSnapshot;
  mission: MissionSnapshot;
  localization: LocalizationSnapshot;
  sensors: SensorStatus[];
  faults: Fault[];
  camera: CameraStatus;
  vision: VisionStatus;
  capabilities: Capability[];
  simulation: SimSnapshot | null;
}

export interface SystemInfo {
  version: string;
  mode: OperatingMode;
  simulated: boolean;
  rover_name: string;
  started_at: string;
  uptime_s: number;
  config_fingerprint: string;
  hardware_errors: Record<string, string>;
  motor_driver: Record<string, unknown>;
  gpio_chip: { number: number; label: string } | null;
  auth_enabled: boolean;
  ui: { map_tile_url: string; map_tile_attribution: string; map_max_zoom: number; map_center_lat: number; map_center_lon: number };
  manual: { deadman_timeout_s: number; heartbeat_interval_s: number; default_speed: number; max_speed: number };
  calibration: { full_speed_mps: number; effective_track_m: number; calibrated: boolean; full_pivot_dps: number; notes: string };
}

// ------------------------------------------------------------------ planning

export interface Point { x: number; y: number }
export interface GeoPoint { lat: number; lon: number }
export type SegmentKind = "lane" | "arc" | "pivot" | "shift" | "transit";

export interface PathSegment {
  index: number;
  kind: SegmentKind;
  lane_index: number | null;
  cell_index: number | null;
  start: Point;
  end: Point;
  heading_start_deg: number;
  heading_end_deg: number;
  length_m: number;
  rotation_deg: number;
  turn_radius_m: number | null;
  points: Point[];
  geo_points: GeoPoint[] | null;
  within_boundary: boolean;
  estimated_duration_s: number;
}

export interface Waypoint {
  index: number;
  kind: "start" | "lane_start" | "lane_end" | "end";
  x: number;
  y: number;
  heading_deg: number;
  segment_index: number;
  lane_index: number | null;
  lat: number | null;
  lon: number | null;
}

export interface PlanIssue { severity: "error" | "warning" | "info"; code: string; message: string }

export interface PlanSummary {
  lane_count: number;
  cell_count: number;
  segment_count: number;
  total_length_m: number;
  lane_length_m: number;
  turn_length_m: number;
  transit_length_m: number;
  pivot_count: number;
  total_rotation_deg: number;
  estimated_duration_s: number;
  area_m2: number;
  inner_area_m2: number;
  covered_area_m2: number;
  nominal_coverage_ratio: number;
  lane_spacing_m: number;
  swath_width_m: number;
  speed_mps: number;
}

export interface CoveragePlan {
  planner_version: string;
  kind: "grid" | "gps";
  frame: "grid_local" | "enu";
  origin: GeoPoint | null;
  boundary: Point[];
  boundary_geo: GeoPoint[] | null;
  inner_boundary: Point[][];
  lane_heading_deg: number;
  start_pose: Pose;
  segments: PathSegment[];
  waypoints: Waypoint[];
  summary: PlanSummary;
  issues: PlanIssue[];
  valid: boolean;
  parameters: Record<string, unknown>;
  plan_hash: string;
  localization_note: string;
}

export interface MissionRecord {
  id: string;
  name: string;
  kind: "grid" | "gps";
  operating_mode: OperatingMode;
  status: "planned" | "active" | "completed" | "cancelled" | "failed" | "emergency_stopped" | "interrupted";
  execution: string | null;
  params: Record<string, unknown>;
  plan_hash: string;
  summary: PlanSummary;
  progress: MissionProgress | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  paused_at: string | null;
  resumed_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  ended_reason: string | null;
  review_required: boolean;
  reviewed_at: string | null;
  review_note: string | null;
  plan?: CoveragePlan;
}

export interface DetectionEvent {
  id: number;
  mission_id: string | null;
  ts: string;
  source_kind: "metal_detector" | "camera";
  sensor_id: string;
  simulated: boolean;
  label: string | null;
  reading: number | null;
  confidence: number | null;
  data: Record<string, unknown> | null;
  position: { frame: string; x: number | null; y: number | null; lat: number | null; lon: number | null; accuracy_m: number | null; provider: string } | null;
  localization_quality: string;
  review_status: "unreviewed" | "confirmed_signal" | "false_positive" | "dismissed" | "needs_follow_up";
  review_note: string | null;
  reviewed_at: string | null;
}

export interface Boundary { id: number; name: string; points: GeoPoint[]; created_at: string; updated_at: string }

export interface PreflightCheck { id: string; label: string; passed: boolean; required: boolean; message: string }
