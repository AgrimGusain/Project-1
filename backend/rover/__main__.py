"""Entry point: ``python -m rover --config config/simulation.yaml``."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from rover.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Survey rover backend")
    parser.add_argument("--config", help="YAML configuration file (or ROVER_CONFIG)")
    parser.add_argument("--host", help="override server.host")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        enabled = [f"{s.id} ({s.type}/{s.driver})" for s in cfg.sensors if s.enabled]
        disabled = [s.id for s in cfg.sensors if not s.enabled]
        print(f"OK: mode={cfg.mode.value} localization={cfg.localization.provider} camera={cfg.camera.provider}")
        print(f"  sensors enabled:  {', '.join(enabled) or 'none'}")
        print(f"  sensors disabled: {', '.join(disabled) or 'none'}")
        print(f"  vision: {'enabled (' + cfg.vision.backend + ')' if cfg.vision.enabled else 'disabled'}")
        return 0

    logging.basicConfig(
        level=cfg.logging.level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    from rover.api.app import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=args.host or cfg.server.host, port=args.port or cfg.server.port, log_level=cfg.logging.level.lower(),
                ws_ping_interval=10, ws_ping_timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
