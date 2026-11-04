"""Notify streaming responses before Uvicorn waits for connections to drain."""

import uvicorn


class DrainingServer(uvicorn.Server):
    async def shutdown(self, sockets=None):
        self.config.app.state.draining = True
        await super().shutdown(sockets=sockets)
