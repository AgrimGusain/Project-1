# Hardware

## Default pin map (BCM numbering, configurable in YAML)

| Function | BCM | Header pin | Notes |
| --- | --- | --- | --- |
| L298N IN1 (left A) | 17 | 11 | 10 kΩ pull-down |
| L298N IN2 (left B) | 27 | 13 | 10 kΩ pull-down |
| L298N ENA (left PWM) | 12 | 32 | remove the ENA jumper; 10 kΩ pull-down |
| L298N IN3 (right A) | 22 | 15 | 10 kΩ pull-down |
| L298N IN4 (right B) | 23 | 16 | 10 kΩ pull-down |
| L298N ENB (right PWM) | 13 | 33 | remove the ENB jumper; 10 kΩ pull-down |
| HC-SR04 Trig | 24 | 18 | 3.3 V drive is sufficient |
| HC-SR04 Echo | 25 | 22 | **5 V → divider → 3.3 V** |
| IR sensor OUT | 16 | 36 | module powered from 3.3 V where possible |
| E-stop aux contact (optional) | 26 | 37 | `safety.estop_input` |
| Metal detector digital out (optional) | 5 | 29 | `driver: gpio_digital` |
| GND | — | 6, 9, 14, 20, 25, 30, 34, 39 | common ground with L298N and sensors |

GPS (optional): UART on GPIO14/15 (`/dev/ttyAMA0`, enable the serial port and
disable the login shell in `raspi-config`) or a USB receiver (`/dev/ttyACM0`).

## GPIO on the Raspberry Pi 5

The Pi 5 header is driven by the RP1 chip. `RPi.GPIO` does **not** work on the
Pi 5. This project uses **lgpio** (`sudo apt install python3-lgpio`), the backend
of gpiozero on Raspberry Pi OS Bookworm. `gpio.chip: auto` selects the RP1 chip
(`gpiochip0` on current kernels, `gpiochip4` on early Bookworm kernels).

PWM uses lgpio's software-timed PWM at 1 kHz on ENA/ENB, which is adequate for
an L298N. `motors.min_duty` maps any non-zero command above the motors' stall
duty.

## L298N

* Remove the ENA/ENB jumpers so the Pi controls speed.
* The on-board 5 V regulator only works with a motor supply ≤ 12 V. Do **not** power
  the Pi from it.
* The L298N drops roughly 2 V, so expect less speed than the battery voltage suggests.
* Wire left-side motors in parallel to OUT1/OUT2 and right-side motors to OUT3/OUT4.
  If a side runs backwards, set `invert: true` for that side instead of rewiring.
* **Pull-downs**: 10 kΩ from each of IN1–IN4, ENA and ENB to GND keep the bridge
  off whenever the Pi is not actively driving the pins.
* **Physical cut-off**: a latching e-stop switch in the motor battery positive lead.

## HC-SR04 level shifting

Echo outputs 5 V. Use a voltage divider: Echo → 1 kΩ → GPIO25, and GPIO25 → 2 kΩ
→ GND (≈3.3 V). Alternatively use a bidirectional level shifter, or an
HC-SR04P/RCWL-1601 module that runs at 3.3 V.

The driver times the echo pulse from lgpio edge timestamps. An echo that starts
but returns nothing within range is reported as a valid "nothing within range"
reading. No echo pulse at all (wiring, power or divider fault) is reported as an
**invalid** reading and blocks forward motion.

## IR obstacle module

Most modules (LM393 comparator) pull OUT **low** when they detect something:
`active_low: true`, `pull: up`. Some modules are active-high; check with the
Sensors page (it shows `raw_level`) and set `active_low` accordingly. Adjust the
module's potentiometer for the desired range (typically 2–15 cm). Power it from
3.3 V if it tolerates it. Otherwise level-shift OUT.

## Power

* Pi 5: official 27 W USB-C supply or a 5 V/5 A regulator from a separate battery.
* Motors: 2S–3S Li-ion/LiPo through the e-stop switch into the L298N.
* ESP32-CAM: stable 5 V ≥ 500 mA; brown-outs while streaming cause resets.
* Common ground between the Pi, L298N logic and sensors.

## Verification on the bench (wheels off the ground)

1. `python -m rover --config config/hardware.yaml --check-config`
2. Start the service. The Dashboard must show **REAL HARDWARE**, *Disarmed*, and
   the motor driver *healthy*. Any init failure appears under System →
   Hardware initialisation errors and the motors stay unavailable.
3. Sensors page: ultrasonic distance tracks your hand; IR toggles; with Echo
   disconnected the ultrasonic shows *invalid (no_echo)* and forward is blocked.
4. With the wheels lifted, arm, then drive each direction at 30 %. Check the
   direction of each side, the stop on release, and the stop on closing the
   browser tab. Cut Wi-Fi to the tablet and confirm the dead-man stops the motors.
5. Press the physical e-stop and confirm the motors lose power without software involvement.
6. Then calibrate (CALIBRATION.md) before any autonomous run.
