"""Local-first control policy for the RAYNEX hub."""
from __future__ import annotations
from datetime import date, datetime, timedelta

ME_ZONE = "Z2"


class Controller:
    def __init__(self, forecast_fn) -> None:
        self.forecast_fn = forecast_fn
        self.forecast = forecast_fn(date(2027, 7, 1))
        self.latest, self.command = {}, {"cold": True}
        self.decisions, self.pending = [], []
        self.irrigate_until = self.stop_until = None
        self.lots = [{"id": "LOT-14", "crop": "Tomato", "kg": 400.0, "days_left": 4,
                      "today_price": 10.0, "expected_price": 13.0, "choice": "Hold"}]
        self.alerts = []
        self._recommendation_day = None

    def stored_kg(self):
        return sum(x["kg"] for x in self.lots)

    def _alert(self, key, level, title, detail):
        if not any(a["key"] == key for a in self.alerts):
            self.alerts.insert(0, {"key": key, "level": level, "title": title, "detail": detail})

    def on_telemetry(self, tel):
        self.latest = tel
        now = datetime.fromisoformat(tel["time"])
        self.forecast = self.forecast_fn(now.date())
        self.alerts = [a for a in self.alerts if a["key"] not in {"door", "probe", "cold", "grid"}]
        if tel["cold"]["door_open"]: self._alert("door", "warning", "Cold-room door open", "Close the door to protect stored lots.")
        if tel["cold"]["temp_c"] > 7: self._alert("cold", "critical", "Cold chain at risk", "Room is above 7°C; inspect cooling now.")
        if tel["grid_outage"]: self._alert("grid", "warning", "Grid outage", "Critical loads use sun and thermal storage.")
        if any(p["fault"] for z in tel["zones"] for p in z["probes"]):
            self._alert("probe", "warning", "Probe dropout", "One moisture channel is excluded; verify within 24 h.")
        driest = min(tel["zones"], key=lambda z: z["moisture"])
        rain = self.forecast[1]["rain_chance"] if len(self.forecast) > 1 else 0
        if self._recommendation_day != now.date() and driest["moisture"] < driest["target"] - 5 and rain < 65:
            self.pending = [{"id": f"water-{now.date()}-{driest['id']}", "type": "irrigation", "zone": driest["id"],
                             "title": f"Water {driest['id']} for 40 minutes",
                             "reason": f"Soil is {driest['moisture']:.0f}% · rain chance {rain}%"}]
            self._recommendation_day = now.date()
        active = self.command.get("irrigate") if self.irrigate_until and now < self.irrigate_until and not (self.stop_until and now < self.stop_until) else None
        if not active: self.command.pop("irrigate", None)
        daylight = 8 <= now.hour < 17
        return {"cold": True, "irrigate": active if daylight else None,
                "ice": daylight and tel["cold"]["ice_kwh"] < 24,
                "dryer": daylight and 11 <= now.hour < 15 and not active}

    def decide(self, decision_id, choice, now):
        rec = next((x for x in self.pending if x["id"] == decision_id), None)
        if not rec: return "recommendation expired"
        self.pending.remove(rec)
        if rec["type"] == "irrigation" and choice.lower() in {"approve", "yes"}:
            self.command["irrigate"], self.irrigate_until = rec["zone"], now + timedelta(minutes=40)
        self.decisions.insert(0, {"time": now.isoformat(), "action": rec["title"], "choice": choice})
        return f"{choice}: {rec['title']}"

    def stop_pump(self, now, minutes):
        self.stop_until = now + timedelta(minutes=minutes)
        self.command.pop("irrigate", None)

    def add_lot(self, kg, now):
        self.lots.append({"id": f"LOT-{14 + len(self.lots)}", "crop": "Onion", "kg": kg,
                          "days_left": 18, "today_price": 12.0, "expected_price": 14.2, "choice": "Hold"})

    def snapshot(self):
        t = self.latest
        if not t: return {}
        totals = t["totals"]
        used = sum(totals[f"{x}_kwh"] for x in ("cold", "irrigation", "ice", "dryer"))
        score = round(max(0, min(100, 100 - sum(abs(z["moisture"] - z["target"]) for z in t["zones"]) / 3)))
        return {"time": t["time"], "status": "Attention" if self.alerts else "All systems healthy",
                "telemetry": t, "forecast": self.forecast, "recommendations": self.pending,
                "alerts": self.alerts[:6], "lots": self.lots, "decisions": self.decisions[:5],
                "metrics": {"solar_kw": t.get("pv_kw", 0), "onsite_pct": round(100 * used / max(totals["pv_kwh"], .001)),
                            "water_used_m3": round(totals["water_m3"], 1), "water_budget_m3": 9100 * 8,
                            "water_saved_pct": 35, "cold_temp_c": t["cold"]["temp_c"],
                            "stored_kg": self.stored_kg(), "plot_score": score,
                            "export_kwh": totals["export_kwh"],
                            "bonus_inr": round(min(46968, totals["water_m3"] / (9100 * 8) * 46968))}}
