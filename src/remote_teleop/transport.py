"""Reconnectable authenticated endpoints, with zero command replay after disconnect."""

import asyncio
import logging
import uuid

import websockets

from .protocol import Outbox, decode, encode
from .security import Credentials, tls_context

logger = logging.getLogger(__name__)


class Endpoint:
    def __init__(self, cfg, role, on_message, on_disconnect):
        self.cfg, self.role = cfg, role
        self.on_message, self.on_disconnect = on_message, on_disconnect
        self.loop = None
        self.outbox = None
        self.online = False
        self.stopping = False
        self.pending = {}
        self.task = None
        self.ws = None

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._connect())

    def send(self, message):
        if not self.online or self.loop is None:
            return False
        message = {**message, "config_contract": self.cfg.control_contract}

        def put():
            if self.online:
                try:
                    self.outbox.put(message)
                except asyncio.QueueFull:
                    logger.error("Control transaction queue overflow")
                    self.online = False
                    self.on_disconnect()
                    if self.ws:
                        asyncio.create_task(self.ws.close(code=1011))

        self.loop.call_soon_threadsafe(put)
        return True

    async def _send(self, ws):
        while self.online:
            msg = await self.outbox.get()
            await ws.send(encode(msg))

    async def _receive(self, ws):
        async for raw in ws:
            msg = decode(raw, self.cfg.network.max_message_bytes)
            if msg["type"] == "rpc_response":
                future = self.pending.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg)
            else:
                await self.on_message(msg)

    async def _connect(self):
        url = f"{self.cfg.network.gateway_url.rstrip('/')}/ws/{self.cfg.general.robot_id}/{self.role}"
        while not self.stopping:
            tasks = []
            try:
                token = Credentials(self.cfg).token(self.role)
                async with websockets.connect(
                    url,
                    ssl=tls_context(self.cfg),
                    additional_headers={
                        "Authorization": f"Bearer {token}",
                        "X-Teleop-Contract": self.cfg.connection_contract,
                    },
                    max_size=self.cfg.network.max_message_bytes,
                    max_queue=4,
                    ping_interval=10,
                    ping_timeout=5,
                    open_timeout=5,
                ) as ws:
                    self.ws = ws
                    self.outbox = Outbox()
                    self.online = True
                    tasks = [asyncio.create_task(self._send(ws)), asyncio.create_task(self._receive(ws))]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("%s transport disconnected (%s)", self.role, type(exc).__name__)
            finally:
                self.online = False
                self.ws = None
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("DISCONNECTED"))
                self.on_disconnect()
            if not self.stopping:
                await asyncio.sleep(self.cfg.network.reconnect_seconds)

    async def rpc(self, operation, data=None, target="robot"):
        if not self.online:
            raise ConnectionError("DISCONNECTED")
        identifier = uuid.uuid4().hex
        future = self.loop.create_future()
        self.pending[identifier] = future
        try:
            self.send(
                {
                    "type": "rpc_request",
                    "id": identifier,
                    "operation": operation,
                    "data": data or {},
                    "target": target,
                }
            )
            result = await asyncio.wait_for(future, self.cfg.network.rpc_timeout_seconds)
            if not result.get("ok"):
                raise ValueError(result.get("error", "REQUEST_FAILED"))
            return result.get("result")
        finally:
            self.pending.pop(identifier, None)

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
