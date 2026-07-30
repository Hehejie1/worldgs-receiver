import argparse
from pathlib import Path

import uvicorn

from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig
from worldgs_receiver.networking import local_lan_addresses


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the WorldGS local receiver.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--output", type=Path, default=Path("~/WorldGS_Imports").expanduser())
    parser.add_argument("--analytics", type=Path, default=None)
    parser.add_argument("--dashboard-username", default="worldgs")
    parser.add_argument("--dashboard-password", default="worldgs-admin")
    parser.add_argument(
        "--local-training-command",
        action="append",
        default=[],
        help="Local gsplat training command token. Repeat for each argv token.",
    )
    parser.add_argument("--local-training-cwd", default="", help="Working directory for local gsplat training command.")
    args = parser.parse_args()

    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    analytics_dir = args.analytics.expanduser().resolve() if args.analytics else None
    config = ReceiverConfig(
        output_dir=output_dir,
        host=args.host,
        port=args.port,
        analytics_dir=analytics_dir,
        dashboard_username=args.dashboard_username,
        dashboard_password=args.dashboard_password,
        local_training_enabled=bool(args.local_training_command),
        local_training_command=tuple(args.local_training_command),
        local_training_cwd=Path(args.local_training_cwd).expanduser() if args.local_training_cwd else None,
    )

    print(f"WorldGS Receiver listening on http://localhost:{config.port}")
    for address in local_lan_addresses():
        print(f"LAN URL: http://{address}:{config.port}")
    print(f"Output: {config.output_dir}")

    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
