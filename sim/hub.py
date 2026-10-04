"""Five-minute physical model of a RAYNEX solar hub."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

STEP_H = 5 / 60
PV_KWP = 15.0
WATER_BUDGET_M3 = 9100.0 * 8
BASELINE_WATER_M3 = 14000.0 * 8


@dataclass
class Probe:
    depth_cm: int
    value: float
    fault: str | None = None


@dataclass
class Zone:
    zid: str
    crop: str
    area_ha: float
    moisture: float
    target: float
    probes: list[Probe] = field(default_factory=list)
    watered_m3: float = 0.0


@dataclass
class ColdRoom:
    temp_c: float = 5.1
    target_c: float = 5.0
    load_kg: float = 400.0
    door_open: bool = False
    ice_kwh: float = 13.0
    capacity_kg: float = 5000.0


@dataclass
class Grid:
    outage: bool = False


class Weather:
    def __init__(self) -> None:
        self.overrides: dict[str, dict] = {}

    def at(self, t: datetime) -> dict:
        doy = t.timetuple().tm_yday
        seasonal = 30.5 + 3.0 * math.sin((doy - 75) * 2 * math.pi / 365)
        hour = t.hour + t.minute / 60
        over = self.overrides.get(t.date().isoformat(), {})
        cloud = float(over.get("cloud", 0.28 + 0.12 * math.sin(doy * 0.73)))
        tmax = float(over.get("tmax", seasonal + 3.5))
        tmin = float(over.get("tmin", seasonal - 8.5))
        temp = (tmax + tmin) / 2 + (tmax - tmin) / 2 * math.sin((hour - 9) * math.pi / 12)
        return {"temp_c": round(temp, 1), "cloud": max(0.0, min(1.0, cloud)),
                "rain_mm": float(over.get("rain_mm", 0.0)), "tmax": tmax, "tmin": tmin}

    def forecast(self, day: date) -> list[dict]:
        result = []
        for n in range(3):
            d = day + timedelta(days=n)
            w = self.at(datetime.combine(d, datetime.min.time()).replace(hour=14))
            result.append({"date": d.isoformat(), "tmax": round(w["tmax"]), "rain_mm": w["rain_mm"],
                           "rain_chance": 80 if w["rain_mm"] else int(10 + w["cloud"] * 55)})
        return result


class Hub:
    """Physical hub with conservative load allocation and explicit accounting."""

    def __init__(self) -> None:
        self.t = datetime(2027, 7, 1, 9, 0)
        self.weather, self.grid, self.cold = Weather(), Grid(), ColdRoom()
        crops = ["Onion", "Tomato", "Grape", "Onion", "Tomato", "Onion", "Chilli", "Tomato"]
        self.zones = []
        for i, crop in enumerate(crops):
            moisture = 42 - i * 1.55
            self.zones.append(Zone(f"Z{i+1}", crop, 1.0, moisture, 42.0,
                                   [Probe(15, moisture), Probe(45, moisture + 5)]))
        self.total = {"pv_kwh": 0.0, "cold_kwh": 0.0, "irrigation_kwh": 0.0,
                      "ice_kwh": 0.0, "dryer_kwh": 0.0, "export_kwh": 0.0,
                      "grid_kwh": 0.0, "curtailed_kwh": 0.0, "water_m3": 0.0}
        self.last: dict = {}
        self._rng = random.Random(42)

    @staticmethod
    def _solar_kw(t: datetime, cloud: float) -> float:
        h = t.hour + t.minute / 60
        if h < 6 or h > 18.5:
            return 0.0
        return max(0.0, PV_KWP * math.sin(math.pi * (h - 6) / 12.5) ** 1.35 * (1 - 0.72 * cloud))

    def step(self, command: dict | None = None) -> dict:
        command = command or {}
        w, active = self.weather.at(self.t), command.get("irrigate")
        pv = self._solar_kw(self.t, w["cloud"])
        demands = {"cold": 2.3 if command.get("cold", True) and self.cold.temp_c > 5.15 else 0.0,
                   "irrigation": 7.5 if active else 0.0,
                   "ice": 2.4 if command.get("ice") and self.cold.ice_kwh < 28 else 0.0,
                   "dryer": 3.0 if command.get("dryer") else 0.0}
        available = pv
        allocation = {k: 0.0 for k in (*demands, "export")}
        allocation["cold"] = min(demands["cold"], available)
        available -= allocation["cold"]
        cold_grid = 0.0 if self.grid.outage else max(0.0, demands["cold"] - allocation["cold"])
        for load in ("irrigation", "ice", "dryer"):
            allocation[load] = min(demands[load], available)
            available -= allocation[load]
        allocation["export"] = 0.0 if self.grid.outage else available
        curtailed = available if self.grid.outage else 0.0

        cooling = allocation["cold"] + cold_grid
        ambient_gain = .055 + max(0, w["temp_c"] - 25) * .006 + (.16 if self.cold.door_open else 0)
        if cooling < .5 and self.cold.ice_kwh > 0:
            ice_used = min(self.cold.ice_kwh, .11)
            self.cold.ice_kwh -= ice_used
            cooling += ice_used / STEP_H
        self.cold.temp_c = max(2.8, self.cold.temp_c + ambient_gain - cooling * .075)
        self.cold.ice_kwh = min(30.0, self.cold.ice_kwh + allocation["ice"] * STEP_H * .8)

        flow = 0.0
        if active and allocation["irrigation"] >= 5.0:
            flow = 22.0 * allocation["irrigation"] / 7.5
            applied = flow * STEP_H
            zone = next((z for z in self.zones if z.zid == active), None)
            if zone:
                zone.watered_m3 += applied
                zone.moisture = min(65.0, zone.moisture + applied * .18 / zone.area_ha)
            self.total["water_m3"] += applied

        for z in self.zones:
            daylight = 1 if 7 <= self.t.hour <= 18 else .25
            z.moisture -= (.022 + max(0, w["temp_c"] - 30) * .002) * daylight
            if w["rain_mm"] and self.t.hour == 15:
                z.moisture += w["rain_mm"] * .035
            z.moisture = max(5.0, min(70.0, z.moisture))
            for p in z.probes:
                if not p.fault:
                    p.value = z.moisture + (4.5 if p.depth_cm == 45 else 0) + self._rng.uniform(-.08, .08)

        for key in ("cold", "irrigation", "ice", "dryer", "export"):
            self.total[f"{key}_kwh"] += allocation[key] * STEP_H
        self.total["pv_kwh"] += pv * STEP_H
        self.total["grid_kwh"] += cold_grid * STEP_H
        self.total["curtailed_kwh"] += curtailed * STEP_H
        self.last = {"time": self.t.isoformat(), "weather": w, "pv_kw": round(pv, 2),
                     "loads_kw": {k: round(v, 2) for k, v in allocation.items()},
                     "grid_kw": round(cold_grid, 2), "flow_m3h": round(flow, 1), "active_zone": active}
        self.t += timedelta(minutes=5)
        return self.telemetry()

    def telemetry(self) -> dict:
        return {**self.last, "time": self.t.isoformat(),
                "cold": {"temp_c": round(self.cold.temp_c, 1), "target_c": self.cold.target_c,
                         "load_kg": round(self.cold.load_kg), "capacity_kg": self.cold.capacity_kg,
                         "door_open": self.cold.door_open, "ice_kwh": round(self.cold.ice_kwh, 1)},
                "zones": [{"id": z.zid, "crop": z.crop, "area_ha": z.area_ha,
                           "moisture": round(z.moisture, 1), "target": z.target,
                           "watered_m3": round(z.watered_m3, 1),
                           "probes": [{"depth_cm": p.depth_cm, "value": None if p.fault else round(p.value, 1),
                                       "fault": p.fault} for p in z.probes]} for z in self.zones],
                "totals": {k: round(v, 2) for k, v in self.total.items()}, "grid_outage": self.grid.outage}

    def truth(self) -> dict:
        onsite = sum(self.total[f"{x}_kwh"] for x in ("cold", "irrigation", "ice", "dryer"))
        return {"pv_kwp": PV_KWP, "step_minutes": 5, "water_budget_m3": WATER_BUDGET_M3,
                "baseline_water_m3": BASELINE_WATER_M3,
                "on_site_pct": round(100 * onsite / max(self.total["pv_kwh"], .001), 1),
                "energy_balance_error_kwh": round(self.total["pv_kwh"] - onsite - self.total["export_kwh"]
                                                  - self.total["curtailed_kwh"], 4)}
