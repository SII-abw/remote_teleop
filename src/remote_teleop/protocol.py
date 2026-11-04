"""Strict envelope and bounded transport queues. Safety events cannot sit behind video."""

import asyncio
import json

PROTOCOL_VERSION = 1


def decode(raw: str, limit: int) -> dict:
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        raise ValueError("MESSAGE_SIZE")

    def invalid(value):
        raise ValueError("NON_FINITE_JSON")

    msg = json.loads(raw, parse_constant=invalid)
    if not isinstance(msg, dict) or msg.get("v") != PROTOCOL_VERSION or not isinstance(msg.get("type"), str):
        raise ValueError("PROTOCOL_VERSION")
    return msg


def encode(msg: dict) -> str:
    return json.dumps({**msg, "v": PROTOCOL_VERSION}, allow_nan=False, separators=(",", ":"))


class Outbox:
    def __init__(self):
        self.events = asyncio.Queue(maxsize=64)
        self.latest = {}
        self.ready = asyncio.Event()

    def put(self, msg):
        if msg["type"] in ("status", "observation", "human_action", "policy_action", "leader_heartbeat"):
            self.latest[msg["type"]] = msg
        elif msg["type"] == "stop":
            self.latest.clear()
            self.latest["stop"] = msg
        elif msg["type"] in ("policy_request", "policy_error"):
            # The request follows the previous chunk. Do not send a coalesced
            # tail action after this higher-priority transaction.
            self.latest.pop("policy_action", None)
            self.events.put_nowait(msg)
        else:
            # Overflow is a transport failure, never silently drop a transaction.
            self.events.put_nowait(msg)
        self.ready.set()

    async def get(self):
        while True:
            await self.ready.wait()
            if "stop" in self.latest:
                return self.latest.pop("stop")
            if not self.events.empty():
                return self.events.get_nowait()
            for key in ("leader_heartbeat", "human_action", "policy_action", "status", "observation"):
                if key in self.latest:
                    return self.latest.pop(key)
            self.ready.clear()
