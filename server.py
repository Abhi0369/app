"""SolarSinchai simulation server.

    uvicorn app.server:app --reload        then open http://localhost:8000

Loop: hub (simulated hardware) -> telemetry -> edge controller -> commands -> hub ...
The farmer UI receives a snapshot over a WebSocket and sends decisions back over REST.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:  # package launch: python -m uvicorn app.server:app
    from .edge.controller import ME_ZONE, Controller
    from .sim.hub import Hub
except ImportError:  # local launch from this directory: uvicorn server:app
    from edge.controller import ME_ZONE, Controller
    from sim.hub import Hub

UI = Path(__file__).resolve().parent / "ui" / "index.html"
TICK_S = 0.5
SPEEDS = {"1x": 1 / 600, "60x": 0.1, "600x": 1.0, "3000x": 5.0}   # sim steps (5 min each) per tick


class World:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.hub = Hub()
        self.edge = Controller(forecast_fn=lambda d: self.hub.weather.forecast(d))
        self.speed = "600x"
        self.paused = False
        self._acc = 0.0
        self.cmd: dict = {}
        self.step()                     # first telemetry so the UI has data immediately

    def step(self) -> None:
        self.hub.cold.load_kg = 400 + self.edge.stored_kg()      # neighbours' crates + Ramesh's lots
        tel = self.hub.step(self.cmd)
        self.cmd = self.edge.on_telemetry(tel)

    def tick(self) -> bool:
        if self.paused:
            return False
        self._acc += SPEEDS[self.speed]
        moved = False
        while self._acc >= 1:
            self.step()
            self._acc -= 1
            moved = True
        return moved

    def snapshot(self) -> dict:
        s = self.edge.snapshot()
        s["sim"] = {"speed": self.speed, "paused": self.paused, "truth": self.hub.truth(),
                    "speeds": list(SPEEDS)}
        return s


world = World()
clients: set[WebSocket] = set()


async def loop() -> None:
    while True:
        if world.tick() and clients:
            await broadcast()
        await asyncio.sleep(TICK_S)


async def broadcast() -> None:
    msg = json.dumps(world.snapshot())
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            clients.discard(ws)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="SolarSinchai hub simulator", lifespan=lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "simulation_time": world.hub.t.isoformat()}


@app.get("/api/state")
def state() -> dict:
    return world.snapshot()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps(world.snapshot()))
    try:
        while True:
            await ws.receive_text()      # the UI does not send over the socket; keep it open
    except WebSocketDisconnect:
        clients.discard(ws)


# ---------------------------------------------------------------- farmer actions
class Decision(BaseModel):
    id: str
    choice: str


@app.post("/api/decide")
async def decide(d: Decision) -> dict:
    res = world.edge.decide(d.id, d.choice, world.hub.t)
    await broadcast()
    return {"result": res}


@app.post("/api/pump/stop")
async def stop_pump() -> dict:
    world.edge.stop_pump(world.hub.t, 60)
    await broadcast()
    return {"result": "stopped for 60 minutes"}


# ---------------------------------------------------------------- simulator controls (not in the farmer app)
class SimAction(BaseModel):
    action: str
    value: str | None = None


@app.post("/api/sim")
async def sim(a: SimAction) -> dict:
    h, today = world.hub, world.hub.t.date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    if a.action == "speed" and a.value in SPEEDS:
        world.speed = a.value
    elif a.action == "pause":
        world.paused = not world.paused
    elif a.action == "step_hour":
        for _ in range(12):
            world.step()
    elif a.action == "heatwave":
        h.weather.overrides[tomorrow] = {"tmax": 41.0, "tmin": 27.0, "cloud": 0.0, "rain_mm": 0.0}
        world.edge.forecast = h.weather.forecast(today)
    elif a.action == "rain":
        h.weather.overrides[tomorrow] = {"rain_mm": 22.0, "cloud": 0.75}
        world.edge.forecast = h.weather.forecast(today)
    elif a.action == "cloudy":
        h.weather.overrides[today.isoformat()] = {"cloud": 0.85}
    elif a.action == "probe_fault":
        z = next(z for z in h.zones if z.zid == ME_ZONE)
        z.probes[1].fault = None if z.probes[1].fault else "dropout"
    elif a.action == "grid_outage":
        h.grid.outage = not h.grid.outage
    elif a.action == "door":
        h.cold.door_open = not h.cold.door_open
    elif a.action == "harvest":
        world.edge.add_lot(float(a.value or 200), h.t)
    elif a.action == "reset":
        world.reset()
    else:
        return {"result": "unknown action"}
    if a.action in {"heatwave", "rain", "cloudy", "probe_fault", "grid_outage", "door", "harvest"}:
        world.step()  # make the injected condition visible in telemetry immediately
    await broadcast()
    return {"result": "ok"}
