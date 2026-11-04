"""Authenticated one-robot gateway. Each robot uses an isolated configured instance."""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from .policy import PolicyRunner
from .protocol import Outbox, decode, encode
from .security import Credentials


def live_contracts_match(cfg, robot_status, client_contract):
    return robot_status.get("config_contract") == client_contract == cfg.control_contract


def create_gateway(store):
    cfg = store.current
    peers, status = {}, {}
    status_time = 0.0
    client_contract = None
    credentials = Credentials(cfg)
    runner = None
    loop = None

    def deliver(role, message):
        peer = peers.get(role)
        if peer:
            try:
                peer[1].put(message)
            except asyncio.QueueFull:
                asyncio.create_task(peer[0].close(code=1013))

    def policy_emit(message):
        def publish():
            if live_contracts_match(cfg, status, client_contract):
                deliver("robot", {**message, "source": "cloud", "config_contract": cfg.control_contract})
            else:
                deliver("robot", {"type": "config_mismatch"})

        if loop:
            loop.call_soon_threadsafe(publish)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal runner, loop
        loop = asyncio.get_running_loop()
        if cfg.policy.runner_location == "cloud":
            runner = PolicyRunner(cfg, "cloud", policy_emit)
            runner.start()
        yield
        for ws, _ in list(peers.values()):
            await ws.close(code=1001)
        if runner:
            runner.close()

    app = FastAPI(title="Remote Teleop Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        return {"ok": True, "protocol": 1}

    async def send_loop(ws, outbox):
        while True:
            await ws.send_text(encode(await outbox.get()))

    async def receive_loop(ws, role, token):
        nonlocal status, status_time, client_contract
        allowed = {
            "robot": {"status", "observation", "rpc_response", "signal"},
            "client": {
                "rpc_request",
                "human_action",
                "policy_action",
                "policy_request",
                "policy_error",
                "leader_heartbeat",
                "mode_ready",
                "signal",
                "stop",
            },
        }
        interval, received = time.monotonic(), 0
        while credentials.verify(token, role, cfg.general.robot_id):
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=1)
            except asyncio.TimeoutError:
                continue
            message = decode(raw, cfg.network.max_message_bytes)
            if time.monotonic() - interval >= 1:
                interval, received = time.monotonic(), 0
            received += 1
            if received > 500:
                raise ValueError("MESSAGE_RATE_LIMIT")
            if message["type"] not in allowed[role]:
                raise ValueError("MESSAGE_NOT_AUTHORIZED")
            message["source"] = role
            if role == "client":
                client_contract = message.get("config_contract")
            if (
                role == "client"
                and message["type"] in ("policy_action", "policy_request", "policy_error")
                and cfg.policy.runner_location != "client"
            ):
                raise ValueError("POLICY_SOURCE_NOT_AUTHORIZED")
            if role == "robot" and message["type"] == "status":
                status, status_time = message, time.monotonic()
                message["config_in_sync"] = live_contracts_match(cfg, status, client_contract)
                message["gateway_contract"] = cfg.control_contract
                if not message["config_in_sync"]:
                    deliver("robot", {"type": "config_mismatch"})
            if role == "client" and not live_contracts_match(cfg, status, client_contract):
                if message["type"] in (
                    "human_action", "policy_action", "policy_request", "policy_error", "mode_ready", "leader_heartbeat"
                ):
                    deliver("robot", {"type": "config_mismatch"})
                    continue
                if message["type"] == "rpc_request" and message.get("operation") in (
                    "arm",
                    "start",
                    "toggle",
                    "record_start",
                    "home",
                ):
                    deliver(
                        "client",
                        {
                            "type": "rpc_response",
                            "id": message["id"],
                            "ok": False,
                            "error": "CONFIG_NOT_SYNCHRONIZED",
                        },
                    )
                    continue
            if role == "robot" and message["type"] == "observation" and runner:
                runner.update(message)
            if role == "client" and message["type"] == "rpc_request" and message.get("target") == "cloud":
                try:
                    op, data = message["operation"], message.get("data", {})
                    if op == "config_get":
                        result = store.read()
                    elif op == "config_defaults":
                        result = store.read_defaults()
                    elif op in ("config_save", "config_apply"):
                        if "robot" not in peers or (
                            status.get("mode") != "DISARMED"
                            or time.monotonic() - status_time > 1
                            or status.get("recorder", {}).get("state") != "IDLE"
                        ):
                            raise ValueError("DISARM_REQUIRED")
                        result = (
                            store.save(data["config"], data["revision"])
                            if op == "config_save"
                            else store.apply_safety(data["revision"])
                        )
                    else:
                        raise ValueError("UNKNOWN_OPERATION")
                    response = {"ok": True, "result": result}
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)[:1000]}
                deliver("client", {"type": "rpc_response", "id": message["id"], **response})
            else:
                deliver("client" if role == "robot" else "robot", message)
        await ws.close(code=4003)

    @app.websocket("/ws/{robot_id}/{role}")
    async def websocket(ws: WebSocket, robot_id: str, role: str):
        header = ws.headers.get("authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if (
            role not in ("robot", "client")
            or robot_id != cfg.general.robot_id
            or not credentials.verify(token, role, robot_id)
        ):
            await ws.close(code=1008)
            return
        if role in peers:
            await ws.close(code=1008)
            return
        if ws.headers.get("x-teleop-contract") != cfg.connection_contract:
            await ws.close(code=1008)
            return
        origin = ws.headers.get("origin")
        if origin and origin not in cfg.security.allowed_origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        outbox = Outbox()
        peers[role] = (ws, outbox)
        other = "client" if role == "robot" else "robot"
        deliver(other, {"type": "peer", "role": role, "connected": True})
        deliver(role, {"type": "peer", "role": other, "connected": other in peers})
        tasks = [
            asyncio.create_task(send_loop(ws, outbox)),
            asyncio.create_task(receive_loop(ws, role, token)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            peers.pop(role, None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            deliver(other, {"type": "peer", "role": role, "connected": False})
            try:
                await ws.close()
            except RuntimeError:
                pass

    return app
