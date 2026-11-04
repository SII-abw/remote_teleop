"""Explicit role commands. Defaults are simulation-only."""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .config import AppConfig, ConfigStore, atomic_json
from .security import create_credentials


def main():
    parser = argparse.ArgumentParser(prog="remote-teleop")
    parser.add_argument(
        "command",
        choices=["cloud", "master", "slave", "init", "validate", "doctor", "credentials", "inspect"],
    )
    parser.add_argument("--config", default="configs/simulation.json")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--dataset", default="")
    args = parser.parse_args()
    if args.command == "init":
        path = Path(args.config)
        if path.exists():
            parser.error("configuration already exists")
        atomic_json(path, AppConfig().model_dump())
        print(str(path.resolve()))
        return
    store = ConfigStore(args.config)
    cfg = store.current
    logging.basicConfig(level=cfg.general.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if cfg.general.log_level != "DEBUG":
        logging.getLogger("aioice").setLevel(logging.WARNING)
    if args.command == "credentials":
        if not 1 <= args.hours <= 168:
            parser.error("hours must be between 1 and 168")
        create_credentials(cfg.security.credentials_file, cfg.general.robot_id, args.hours)
        print("Scoped credentials written with mode 0600: " + cfg.security.credentials_file)
        return
    if args.command == "validate":
        print("Configuration valid; simulation=" + str(cfg.general.simulation))
        return
    if args.command == "doctor":
        versions = {}
        for name in (
            "fastapi",
            "uvicorn",
            "pydantic",
            "websockets",
            "numpy",
            "aiortc",
            "av",
            "pyarrow",
            "torch",
            "torchvision",
            "datasets",
            "msgpack",
            "pyserial",
            "feetech-servo-sdk",
        ):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        print(
            json.dumps(
                {
                    "python": sys.executable,
                    "versions": versions,
                    "simulation": cfg.general.simulation,
                    "credentials_exist": Path(cfg.security.credentials_file).is_file(),
                    "components_path": str(Path(__file__).parent / "components"),
                },
                indent=2,
            )
        )
        return
    if args.command == "inspect":
        from .datasets import DatasetInspector

        inspector = DatasetInspector(cfg)
        print(
            json.dumps(
                inspector.inspect(args.dataset, True) if args.dataset else inspector.list(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    # Bound the built-in image writer as well as the recorder queue before use.
    os.environ["LEROBOT_IMAGE_WRITER_QUEUE_MAXSIZE"] = str(cfg.recording.queue_size)
    if args.command == "slave":
        from .runtime import RobotRuntime

        async def run():
            runtime = RobotRuntime(store)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            try:
                await runtime.start()
                await stop.wait()
            finally:
                await runtime.close()

        asyncio.run(run())
    else:
        import uvicorn
        from .server import DrainingServer

        if args.command == "cloud":
            from .gateway import create_gateway

            app, host, port = create_gateway(store), cfg.network.gateway_host, cfg.network.gateway_port
        else:
            from .webapp import create_client_app

            app, host, port = create_client_app(store), cfg.ui.host, cfg.ui.port
        if host not in ("127.0.0.1", "localhost", "::1") and not (
            cfg.network.certificate_file and cfg.network.private_key_file
        ):
            raise SystemExit("Non-loopback listeners require a TLS certificate and private key")
        server_config = uvicorn.Config(
            app,
            host=host,
            port=port,
            access_log=False,
            ssl_certfile=cfg.network.certificate_file or None,
            ssl_keyfile=cfg.network.private_key_file or None,
            ws_max_size=cfg.network.max_message_bytes,
            timeout_graceful_shutdown=15,
        )
        DrainingServer(server_config).run()


if __name__ == "__main__":
    main()
