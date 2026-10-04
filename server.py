"""SolarSinchai simulation server.

    uvicorn app.server:app --reload        then open http://localhost:8000

Loop: hub (simulated hardware) -> telemetry -> edge controller -> commands -> hub ...
The farmer UI receives a snapshot over a WebSocket and sends decisions back over REST.
"""
from __future__ import annotations

import asyncio
import json
import math
from collections import deque
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
        self.history: deque[dict] = deque(maxlen=2016)  # seven simulated days
        self.events: deque[dict] = deque(maxlen=40)
        self.step()                     # first telemetry so the UI has data immediately

    def step(self) -> None:
        self.hub.cold.load_kg = 400 + self.edge.stored_kg()      # neighbours' crates + Ramesh's lots
        tel = self.hub.step(self.cmd)
        self.cmd = self.edge.on_telemetry(tel)
        loads = tel.get("loads_kw", {})
        self.history.append({
            "time": tel["time"], "pv": tel.get("pv_kw", 0),
            "cold_kw": loads.get("cold", 0), "pump_kw": loads.get("irrigation", 0),
            "ice_kw": loads.get("ice", 0), "dryer_kw": loads.get("dryer", 0),
            "export_kw": loads.get("export", 0), "cold_c": tel["cold"]["temp_c"],
            "soil_avg": round(sum(z["moisture"] for z in tel["zones"]) / len(tel["zones"]), 1),
            "soil_min": min(z["moisture"] for z in tel["zones"]),
            "water_m3": tel["totals"]["water_m3"], "outage": tel["grid_outage"],
        })

    def log_event(self, title: str, detail: str) -> None:
        self.events.appendleft({"time": self.hub.t.isoformat(), "title": title, "detail": detail})

    def analytics(self) -> dict:
        tel, hist = self.hub.telemetry(), list(self.history)
        totals = tel["totals"]
        onsite = sum(totals[f"{name}_kwh"] for name in ("cold", "irrigation", "ice", "dryer"))
        onsite_pct = round(100 * onsite / max(totals["pv_kwh"], .001), 1)
        safe_pct = round(100 * sum(p["cold_c"] <= 7 for p in hist) / max(len(hist), 1), 1)
        driest = min(tel["zones"], key=lambda z: z["moisture"])
        faults = sum(bool(p["fault"]) for z in tel["zones"] for p in z["probes"])
        autonomy = round(tel["cold"]["ice_kwh"] / 2.3, 1)
        run_hours = round(len(hist) * 5 / 60, 1)
        insights = []
        if len(hist) < 24:
            insights.append({"level": "info", "title": "Build a representative window",
                             "detail": "Run at least 2 simulated hours before judging solar utilisation."})
        elif onsite_pct < 60:
            insights.append({"level": "opportunity", "title": "Surplus solar is available",
                             "detail": f"Only {onsite_pct}% has been used on site. Schedule drying or ice charging in the solar window."})
        else:
            insights.append({"level": "good", "title": "Solar is serving productive loads",
                             "detail": f"{onsite_pct}% of generated energy has stayed on site; the project target is 72%."})
        gap = round(driest["target"] - driest["moisture"], 1)
        insights.append({"level": "warning" if gap > 5 else "good", "title": f"{driest['id']} is the priority plot",
                         "detail": f"{driest['crop']} soil is {driest['moisture']}%, {max(0, gap)} points below its target."})
        insights.append({"level": "warning" if autonomy < 3 else "good", "title": f"{autonomy} h thermal autonomy",
                         "detail": f"The ice reserve can support roughly {autonomy} hours at the nominal 2.3 kW cooling load."})
        insights.append({"level": "warning" if faults else "good", "title": "Sensor confidence " + ("reduced" if faults else "healthy"),
                         "detail": f"{faults} of 16 soil probes are unavailable." if faults else "All 16 soil probes are reporting."})
        stride = max(1, math.ceil(len(hist) / 96))
        trend = hist[::stride]
        if hist and trend[-1] is not hist[-1]:
            trend.append(hist[-1])
        load_mix = [{"name": n.title(), "kwh": round(totals[f"{n}_kwh"], 2)}
                    for n in ("cold", "irrigation", "ice", "dryer", "export")]
        return {
            "window_hours": run_hours, "cold_safe_pct": safe_pct, "thermal_autonomy_h": autonomy,
            "onsite_pct": onsite_pct, "export_revenue_inr": round(totals["export_kwh"] * 3.05, 2),
            "water_delivered_m3": totals["water_m3"], "driest_zone": driest["id"],
            "probe_availability_pct": round(100 * (16 - faults) / 16), "insights": insights,
            "trend": trend, "load_mix": load_mix, "events": list(self.events),
        }

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
        s["analytics"] = self.analytics()
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
    return FileResponse(UI, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "X-RAYNEX-Build": "insights-2026-10-04",
    })


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "build": "insights-2026-10-04",
            "simulation_time": world.hub.t.isoformat()}


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
    elif a.action == "step_day":
        for _ in range(288):
            world.step()
    elif a.action == "step_week":
        for _ in range(2016):
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
    event_names = {"heatwave": ("Heatwave scheduled", "Tomorrow reaches 41°C."),
                   "rain": ("Rain forecast injected", "Tomorrow receives 22 mm."),
                   "cloudy": ("Cloud cover injected", "PV output is derated today."),
                   "probe_fault": ("Probe state changed", "Z2 deep probe toggled."),
                   "grid_outage": ("Grid state changed", "Grid availability toggled."),
                   "door": ("Cold-room door changed", "Door contact toggled."),
                   "harvest": ("Harvest received", f"{a.value or 200} kg added to storage."),
                   "step_day": ("Day advanced", "288 control cycles completed."),
                   "step_week": ("Week advanced", "2,016 control cycles completed.")}
    if a.action in event_names:
        world.log_event(*event_names[a.action])
    if a.action in {"heatwave", "rain", "cloudy", "probe_fault", "grid_outage", "door", "harvest"}:
        world.step()  # make the injected condition visible in telemetry immediately
    await broadcast()
    return {"result": "ok"}
