"""Operator API: authenticated, origin-checked, with saved-vs-active configuration."""

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import AppConfig
from .arm_profiles import builtin_arm_profiles
from .config_sync import ConfigSynchronizer
from .runtime import ClientRuntime
from .security import Credentials


def create_client_app(store, runtime=None):
    cfg = store.current
    runtime = runtime or ClientRuntime(store)
    sessions, attempts = {}, {}
    owner_seen = 0.0
    config_transaction = asyncio.Lock()
    credentials = Credentials(cfg)
    assets = Path(__file__).with_name("web")

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Remote Teleop Operator", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    app.state.draining = False

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.url.hostname
        allowed_hosts = {"127.0.0.1", "localhost", "::1", cfg.ui.host}
        allowed_hosts |= {urlparse(origin).hostname for origin in cfg.security.allowed_origins}
        if host not in allowed_hosts:
            return JSONResponse({"error": "HOST_FORBIDDEN"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (
                request.headers.get("x-teleop-request") != "1"
                or origin
                and origin not in [expected_origin, *cfg.security.allowed_origins]
            ):
                return JSONResponse({"error": "ORIGIN_FORBIDDEN"}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "INVALID_LENGTH"}, status_code=400)
            if size > 1048576:
                return JSONResponse({"error": "BODY_TOO_LARGE"}, status_code=413)
            # Enforce the limit for chunked bodies too, before JSON parsing.
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 1048576:
                    return JSONResponse({"error": "BODY_TOO_LARGE"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if request.url.path.startswith("/api/") and request.url.path != "/api/login":
            session = sessions.get(request.cookies.get("teleop_session"))
            if (
                not session
                or session[0] < time.time()
                or not credentials.verify(session[1], "ui", cfg.general.robot_id)
            ):
                return JSONResponse({"error": "AUTH_REQUIRED"}, status_code=401)
        try:
            response = await call_next(request)
        except (ValueError, KeyError, ConnectionError, TimeoutError) as exc:
            response = JSONResponse({"error": str(exc)[:1000]}, status_code=400)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index():
        return FileResponse(assets / "index.html")

    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.post("/api/login")
    async def login(request: Request):
        nonlocal owner_seen
        address = request.client.host
        now = time.time()
        attempts[address] = [t for t in attempts.get(address, []) if now - t < 60]
        if len(attempts[address]) >= 10:
            raise HTTPException(429, "LOGIN_RATE_LIMIT")
        attempts[address].append(now)
        if len(attempts) > 1024:
            attempts.clear()
        body = await request.json()
        token = body.get("token", "")
        if not credentials.verify(token, "ui", cfg.general.robot_id):
            raise HTTPException(401, "AUTH_FAILED")
        if sessions and time.monotonic() - owner_seen < cfg.safety.operator_lease_seconds:
            raise HTTPException(409, "OPERATOR_IN_USE")
        sessions.clear()
        for key in list(sessions):
            if sessions[key][0] < now:
                sessions.pop(key)
        if len(sessions) >= 32:
            raise HTTPException(429, "SESSION_LIMIT")
        session_id = secrets.token_urlsafe(32)
        sessions[session_id] = (now + cfg.security.ui_session_seconds, token)
        owner_seen = time.monotonic()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "teleop_session",
            session_id,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=cfg.security.ui_session_seconds,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get("teleop_session"), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("teleop_session")
        return response

    @app.get("/api/status")
    async def status():
        nonlocal owner_seen
        owner_seen = runtime.operator_seen_at = time.monotonic()
        return runtime.snapshot()

    @app.get("/api/recording/name")
    async def recording_name(name: str | None = None):
        return await runtime.endpoint.rpc("record_name", {} if name is None else {"name": name})

    @app.get("/api/tasks")
    async def tasks():
        return await runtime.endpoint.rpc("task_list")

    @app.post("/api/tasks")
    async def new_task(request: Request):
        async with config_transaction:
            return await runtime.endpoint.rpc("task_create", await request.json())

    @app.post("/api/tasks/select")
    async def select_task(request: Request):
        async with config_transaction:
            return await runtime.endpoint.rpc("task_select", await request.json())

    @app.post("/api/control/{operation}")
    async def control(operation: str, request: Request):
        if operation not in (
            "arm",
            "start",
            "toggle",
            "pause",
            "estop",
            "disarm",
            "reset",
            "record_start",
            "record_finish",
            "home",
            "home_cancel",
        ):
            raise HTTPException(404)
        if operation == "estop":
            if not runtime.endpoint.send({"type": "stop"}):
                raise ConnectionError("DISCONNECTED")
            return {"requested": True}
        data = await request.json()
        data.setdefault("epoch", runtime.snapshot().get("epoch"))
        if operation in ("arm", "start", "toggle", "record_start", "home"):
            async with config_transaction:
                if operation == "home" and runtime.snapshot().get("config_in_sync") is not True:
                    raise ValueError("CONFIG_NOT_SYNCHRONIZED")
                return await runtime.endpoint.rpc(operation, data)
        return await runtime.endpoint.rpc(operation, data)

    @app.post("/api/simulation/leader")
    async def simulate(request: Request):
        if not cfg.general.simulation:
            raise HTTPException(404)
        from .control import vector

        values = vector((await request.json())["q"])
        runtime.leader.feedback(values)
        return {"ok": True}

    @app.get("/api/config/schema")
    async def schema():
        return AppConfig.model_json_schema()

    @app.get("/api/arm-profiles")
    async def arm_profiles():
        return builtin_arm_profiles()

    @app.get("/api/config/{target}")
    async def get_config(target: str):
        if target == "client":
            return store.read()
        if target not in ("robot", "cloud"):
            raise HTTPException(404)
        return await runtime.endpoint.rpc("config_get", target=target)

    @app.get("/api/config/defaults/{target}")
    async def get_defaults(target: str):
        if target == "client":
            return store.read_defaults()
        if target not in ("robot", "cloud"):
            raise HTTPException(404)
        return await runtime.endpoint.rpc("config_defaults", target=target)

    @app.post("/api/config/validate")
    async def validate(request: Request):
        body = await request.json()
        try:
            AppConfig.model_validate(body["config"])
            return {"valid": True}
        except ValueError as exc:
            return JSONResponse({"valid": False, "error": str(exc)}, status_code=422)

    @app.post("/api/config/{target}")
    async def save_config(target: str, request: Request):
        if target not in ("client", "robot", "cloud"):
            raise HTTPException(404)
        body = await request.json()

        def require_disarmed():
            state = runtime.snapshot()
            if (
                state.get("mode") != "DISARMED"
                or (state.get("telemetry_age_ms") or 999999) > 1000
                or state.get("recorder", {}).get("state") != "IDLE"
                or (state.get("record_job") or {}).get("state") in ("QUEUED", "RUNNING")
            ):
                raise ValueError("DISARM_REQUIRED")

        async def save_one(role, values, revision):
            require_disarmed()
            if role == "client":
                return store.save(values, revision)
            return await runtime.endpoint.rpc(
                "config_save", {"config": values, "revision": revision}, target=role
            )

        async def apply_one(role, revision):
            require_disarmed()
            if role == "client":
                with runtime.lock:
                    return store.apply_safety(revision)
            return await runtime.endpoint.rpc("config_apply", {"revision": revision}, target=role)

        async with config_transaction:
            require_disarmed()
            result = await ConfigSynchronizer(get_config, save_one, apply_one).update(
                target, body["config"], body["revision"]
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = runtime.snapshot()
                if (
                    state.get("config_contract") == result["active_contract"]
                    and state.get("gateway_contract") == result["active_contract"]
                    and state.get("config_in_sync") is True
                    and (state.get("telemetry_age_ms") or 999999) < 1000
                ):
                    return result
                await asyncio.sleep(0.02)
            raise ValueError("CONFIG_APPLY_PENDING")

    @app.get("/api/datasets")
    async def datasets():
        return await runtime.endpoint.rpc("datasets_list")

    @app.post("/api/datasets/inspect")
    async def inspect(request: Request):
        return await runtime.endpoint.rpc("datasets_inspect", await request.json())

    @app.get("/api/datasets/preview")
    async def preview(id: str, camera: str, frame: int = 0):
        import base64

        result = await runtime.endpoint.rpc("datasets_preview", {"id": id, "camera": camera, "frame": frame})
        return Response(base64.b64decode(result["jpeg"]), media_type="image/jpeg")

    @app.get("/api/video/{name}")
    async def video(name: str, request: Request):
        if name not in ("main", "left_hand", "right_hand"):
            raise HTTPException(404)
        session_id = request.cookies.get("teleop_session")

        async def stream():
            while not app.state.draining and not await request.is_disconnected():
                session = sessions.get(session_id)
                if (
                    not session
                    or session[0] < time.time()
                    or not credentials.verify(session[1], "ui", cfg.general.robot_id)
                ):
                    return
                value = runtime.frames.snapshot().get(name)
                if value and time.monotonic() - value[1] < cfg.safety.camera_max_age_ms / 1000:
                    jpeg = await asyncio.to_thread(runtime.frames.jpeg, name, cfg.ui.jpeg_quality)
                    if jpeg:
                        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(1 / cfg.ui.display_fps)

        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app
