/*
 * ESP32-CAM (AI-Thinker) streaming firmware for the survey rover.
 *
 * Endpoints
 *   http://<ip>/            -> plain-text info
 *   http://<ip>/capture     -> single JPEG frame
 *   http://<ip>/status      -> JSON status (rssi, heap, uptime, frame size)
 *   http://<ip>/control?framesize=<n>&quality=<10-63>   (limited runtime tuning)
 *   http://<ip>:81/stream   -> MJPEG stream (multipart/x-mixed-replace)
 *
 * The rover backend is the single client of /stream and re-serves frames to
 * browsers, because the ESP32-CAM can only serve one stream client reliably.
 *
 * Build: Arduino IDE / arduino-cli with the "esp32" core (2.0.x or 3.x),
 * board "AI Thinker ESP32-CAM", partition scheme "Huge APP", PSRAM enabled.
 * Copy config.example.h to config.h and set your Wi-Fi credentials.
 */

#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include <WiFi.h>

#include "config.h"

// ---- AI-Thinker ESP32-CAM pin map ----------------------------------------
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22
#define FLASH_LED_GPIO 4

#define PART_BOUNDARY "rovercamframe"
static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static httpd_handle_t control_httpd = NULL;
static httpd_handle_t stream_httpd = NULL;
static volatile uint32_t frames_served = 0;
static volatile uint8_t stream_clients = 0;
static uint32_t last_wifi_check = 0;

static bool init_camera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode = CAMERA_GRAB_LATEST;  // always serve the newest frame (low latency)
  if (psramFound()) {
    c.frame_size = CAM_FRAME_SIZE;
    c.jpeg_quality = CAM_JPEG_QUALITY;
    c.fb_count = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size = FRAMESIZE_QVGA;
    c.jpeg_quality = 15;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  s->set_vflip(s, CAM_VFLIP);
  s->set_hmirror(s, CAM_HMIRROR);
  return true;
}

// ---- port 80 handlers -------------------------------------------------------

static esp_err_t index_handler(httpd_req_t *req) {
  char buf[256];
  snprintf(buf, sizeof(buf),
           "Survey rover ESP32-CAM\nstream:  http://%s:81/stream\ncapture: http://%s/capture\nstatus:  http://%s/status\n",
           WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str());
  httpd_resp_set_type(req, "text/plain");
  return httpd_resp_send(req, buf, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

static esp_err_t status_handler(httpd_req_t *req) {
  sensor_t *s = esp_camera_sensor_get();
  char json[320];
  snprintf(json, sizeof(json),
           "{\"device\":\"esp32cam\",\"firmware\":\"%s\",\"uptime_ms\":%lu,\"rssi\":%d,\"free_heap\":%u,"
           "\"psram\":%s,\"framesize\":%d,\"quality\":%d,\"stream_clients\":%u,\"frames_served\":%lu}",
           FIRMWARE_VERSION, (unsigned long)millis(), WiFi.RSSI(), ESP.getFreeHeap(), psramFound() ? "true" : "false",
           s->status.framesize, s->status.quality, stream_clients, (unsigned long)frames_served);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t control_handler(httpd_req_t *req) {
  char query[96], value[16];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "missing query");
    return ESP_FAIL;
  }
  sensor_t *s = esp_camera_sensor_get();
  int res = 0;
  if (httpd_query_key_value(query, "framesize", value, sizeof(value)) == ESP_OK) {
    int v = atoi(value);
    // Limit to sizes that stream reliably over Wi-Fi (QQVGA..SVGA).
    if (v < FRAMESIZE_QQVGA || v > FRAMESIZE_SVGA) res = -1;
    else res |= s->set_framesize(s, (framesize_t)v);
  }
  if (httpd_query_key_value(query, "quality", value, sizeof(value)) == ESP_OK) {
    int v = atoi(value);
    if (v < 10 || v > 63) res = -1;
    else res |= s->set_quality(s, v);
  }
  if (res != 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "invalid value");
    return ESP_FAIL;
  }
  return status_handler(req);
}

// ---- port 81 stream ---------------------------------------------------------

static esp_err_t stream_handler(httpd_req_t *req) {
  char part[64];
  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  stream_clients++;
  int64_t last = esp_timer_get_time();
  const int64_t min_period_us = 1000000LL / STREAM_MAX_FPS;
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      res = ESP_FAIL;
      break;
    }
    size_t hlen = snprintf(part, sizeof(part), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;  // client disconnected
    frames_served++;
    int64_t now = esp_timer_get_time();
    int64_t wait = min_period_us - (now - last);
    if (wait > 0) delay(wait / 1000);
    last = esp_timer_get_time();
  }
  stream_clients--;
  return res;
}

static void start_servers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.ctrl_port = 32768;
  httpd_uri_t index_uri = {"/", HTTP_GET, index_handler, NULL};
  httpd_uri_t capture_uri = {"/capture", HTTP_GET, capture_handler, NULL};
  httpd_uri_t status_uri = {"/status", HTTP_GET, status_handler, NULL};
  httpd_uri_t control_uri = {"/control", HTTP_GET, control_handler, NULL};
  if (httpd_start(&control_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(control_httpd, &index_uri);
    httpd_register_uri_handler(control_httpd, &capture_uri);
    httpd_register_uri_handler(control_httpd, &status_uri);
    httpd_register_uri_handler(control_httpd, &control_uri);
  }
  cfg.server_port = 81;
  cfg.ctrl_port = 32769;
  httpd_uri_t stream_uri = {"/stream", HTTP_GET, stream_handler, NULL};
  if (httpd_start(&stream_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

static void connect_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_HOSTNAME);
#ifdef USE_STATIC_IP
  WiFi.config(STATIC_IP, STATIC_GATEWAY, STATIC_SUBNET, STATIC_DNS);
#endif
  WiFi.setSleep(false);  // lower latency streaming
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  pinMode(FLASH_LED_GPIO, OUTPUT);
  digitalWrite(FLASH_LED_GPIO, LOW);  // keep the bright flash LED off
  if (!init_camera()) {
    delay(3000);
    ESP.restart();
  }
  connect_wifi();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi failed; restarting");
    delay(2000);
    ESP.restart();
  }
  start_servers();
  Serial.printf("Stream ready: http://%s:81/stream\n", WiFi.localIP().toString().c_str());
}

void loop() {
  // Reconnect if Wi-Fi drops; restart if it stays down (the backend reconnects automatically).
  if (millis() - last_wifi_check > 5000) {
    last_wifi_check = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("Wi-Fi lost, reconnecting");
      WiFi.reconnect();
      uint32_t start = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(250);
      if (WiFi.status() != WL_CONNECTED) ESP.restart();
    }
  }
  delay(100);
}
