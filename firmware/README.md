# ESP32-CAM firmware

`esp32cam_stream/` runs on an AI-Thinker ESP32-CAM. It joins your Wi-Fi network
and serves:

| URL | Purpose |
| --- | --- |
| `http://<ip>:81/stream` | MJPEG stream consumed by the rover backend |
| `http://<ip>/capture` | single JPEG |
| `http://<ip>/status` | JSON: RSSI, heap, uptime, frame size, client count |
| `http://<ip>/control?framesize=<n>&quality=<q>` | limited runtime tuning |

## Build and flash

1. Install the Arduino IDE (or `arduino-cli`) and the **esp32** board package by Espressif (2.0.x or 3.x).
2. `cp esp32cam_stream/config.example.h esp32cam_stream/config.h` and set `WIFI_SSID` / `WIFI_PASSWORD`.
   Enable `USE_STATIC_IP` so the address in the backend config never changes.
3. Board: **AI Thinker ESP32-CAM**, Partition scheme: **Huge APP (3MB No OTA)**, PSRAM: **Enabled**.
4. The AI-Thinker board has no USB: use a USB-TTL adapter (5 V → 5V, GND, U0R/U0T crossed) and hold
   **IO0 to GND** while resetting to enter the bootloader. Remove the IO0 jumper and reset to run.
5. Open the serial monitor at 115200 baud; it prints the stream URL.

```bash
arduino-cli compile --fqbn esp32:esp32:esp32cam esp32cam_stream
arduino-cli upload  --fqbn esp32:esp32:esp32cam -p /dev/ttyUSB0 esp32cam_stream
```

## Connect to the backend

In the backend YAML:

```yaml
camera:
  provider: esp32cam
  stream_url: http://192.168.1.60:81/stream
  snapshot_url: http://192.168.1.60/capture
```

The backend is the only client of `/stream` (the ESP32-CAM handles one stream
client reliably) and re-serves frames to every browser at `/api/camera/stream`.

## Power

Feed the ESP32-CAM from a stable 5 V supply rated ≥ 500 mA. Brown-outs while
streaming are the most common cause of resets. Do not power it from the L298N's
5 V regulator output while the motors are running.
