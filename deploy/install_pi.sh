#!/usr/bin/env bash
# Install the rover backend on Raspberry Pi OS (Bookworm, 64-bit) on a Raspberry Pi 5.
#
#   cd ~/rover && ./deploy/install_pi.sh [--config config/hardware.yaml] [--vision] [--build-frontend]
#
# - installs system packages (python3-lgpio for Pi 5 GPIO)
# - creates backend/.venv with --system-site-packages so lgpio is importable
# - adds the user to the gpio and dialout groups (GPIO, GPS serial)
# - installs and enables the rover-backend systemd service
# The service starts the rover DISARMED. Nothing moves until an operator arms it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="config/hardware.yaml"
VISION=0
BUILD_FRONTEND=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --vision) VISION=1; shift ;;
    --build-frontend) BUILD_FRONTEND=1; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
USER_NAME="${SUDO_USER:-$USER}"

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev python3-lgpio python3-pip git

echo "==> Groups (gpio, dialout) for ${USER_NAME}"
sudo usermod -aG gpio,dialout "${USER_NAME}" || true

echo "==> Python environment"
cd "${ROOT}/backend"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
if [[ ${VISION} -eq 1 ]]; then
  .venv/bin/pip install onnxruntime
fi
.venv/bin/python -c "import lgpio; print('lgpio OK')" || echo "WARNING: lgpio not importable - real hardware modes will report the motor driver unavailable"

echo "==> Validate configuration ${CONFIG}"
.venv/bin/python -m rover --config "${CONFIG}" --check-config

if [[ ${BUILD_FRONTEND} -eq 1 ]]; then
  echo "==> Building frontend (requires Node.js 18+)"
  cd "${ROOT}/frontend"
  npm ci
  npm run build
fi

echo "==> systemd service"
sudo mkdir -p /etc/rover
if [[ ! -f /etc/rover/rover.env ]]; then
  echo "# ROVER_AUTH_TOKEN=change-me" | sudo tee /etc/rover/rover.env >/dev/null
  sudo chmod 600 /etc/rover/rover.env
fi
sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" -e "s#@CONFIG@#${CONFIG}#g" \
  "${ROOT}/deploy/rover-backend.service" | sudo tee /etc/systemd/system/rover-backend.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable rover-backend.service
sudo systemctl restart rover-backend.service

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Open http://${IP}:8000 from a laptop or tablet on the same Wi-Fi network."
echo "Logs: journalctl -u rover-backend -f"
echo "Log out and back in (or reboot) for the new group membership to apply to your shell."
