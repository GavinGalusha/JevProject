from __future__ import annotations

import hmac
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .commands import parse_command
from .config import Settings
from .controller import RemoteController
from .pairing import Pairing
from .saved import SavedCommands

STATIC_DIR = Path(__file__).with_name("static")


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class CommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    start_url: str | None = Field(default=None, max_length=2000)
    fullscreen: bool = False
    vision_recovery: bool = False
    guided: bool = False


class SavedBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=2000)
    start_url: str | None = Field(default=None, max_length=2000)
    fullscreen: bool = False


def create_app(
    settings: Settings,
    controller: RemoteController | None = None,
    saved: SavedCommands | None = None,
) -> FastAPI:
    saved_commands = saved or SavedCommands()
    remote = controller or RemoteController(
        settings.start_url,
        command_timeout=settings.command_timeout_seconds,
        stall_timeout=settings.stall_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        remote.close()

    app = FastAPI(title="Jev Remote", version="0.1.0", lifespan=lifespan)
    app.state.controller = remote
    app.state.pairing = Pairing()

    @app.middleware("http")
    async def require_lan(request: Request, call_next):
        host = request.client.host if request.client else ""
        try:
            address = ipaddress.ip_address(host)
            if mapped := getattr(address, "ipv4_mapped", None):
                address = mapped
            if not (address.is_private or address.is_loopback or address.is_link_local):
                return JSONResponse(status_code=403, content={"detail": "LAN access only"})
        except ValueError:
            # ASGI test clients may use a symbolic peer name. Real socket peers are IP addresses.
            pass
        return await call_next(request)

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        valid = scheme.lower() == "bearer" and hmac.compare_digest(supplied, settings.token)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid remote token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "jev-remote"}

    @app.post("/api/pair")
    def pair(body: PairBody):
        if not app.state.pairing.consume(body.code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired pairing code. Restart jev-remote for a new one.",
            )
        return {"token": settings.token}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def get_status():
        return remote.status()

    @app.post("/api/command", dependencies=[Depends(authorize)])
    def command(body: CommandBody):
        try:
            return remote.submit(
                parse_command(body.text),
                start_url=body.start_url,
                then_fullscreen=body.fullscreen,
                vision_recovery=body.vision_recovery,
                guided=body.guided,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/saved", dependencies=[Depends(authorize)])
    def list_saved():
        return saved_commands.list()

    @app.post("/api/saved", dependencies=[Depends(authorize)])
    def add_saved(body: SavedBody):
        try:
            return saved_commands.add(body.name, body.text, body.start_url, body.fullscreen)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/saved/{saved_id}", dependencies=[Depends(authorize)])
    def delete_saved(saved_id: str):
        if not saved_commands.delete(saved_id):
            raise HTTPException(status_code=404, detail="No such saved command")
        return {"ok": True}

    @app.post("/api/kill", dependencies=[Depends(authorize)])
    def kill():
        return remote.kill()

    @app.post("/api/arm", dependencies=[Depends(authorize)])
    def arm():
        try:
            return remote.arm()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
