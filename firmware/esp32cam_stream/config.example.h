// Copy to config.h (which is git-ignored) and edit.
#pragma once

#define FIRMWARE_VERSION "rover-cam-1.0"

#define WIFI_SSID "your-network"
#define WIFI_PASSWORD "your-password"
#define DEVICE_HOSTNAME "rover-cam"

// Optional static IP so the backend's camera.stream_url never changes.
// #define USE_STATIC_IP
#ifdef USE_STATIC_IP
#define STATIC_IP IPAddress(192, 168, 1, 60)
#define STATIC_GATEWAY IPAddress(192, 168, 1, 1)
#define STATIC_SUBNET IPAddress(255, 255, 255, 0)
#define STATIC_DNS IPAddress(192, 168, 1, 1)
#endif

// FRAMESIZE_QVGA (320x240) or FRAMESIZE_VGA (640x480) are good choices for
// YOLOv8n at imgsz 320 on the Pi 5 and keep Wi-Fi bandwidth low.
#define CAM_FRAME_SIZE FRAMESIZE_VGA
#define CAM_JPEG_QUALITY 12  // 10 (best) .. 63 (smallest)
#define CAM_VFLIP 0
#define CAM_HMIRROR 0
#define STREAM_MAX_FPS 15
