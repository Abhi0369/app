# RAYNEX hub digital twin

An interactive hardware and operator-UI simulation derived from the **One Sun, Five Jobs** proposal. It models the 15 kWp array, five-load priority router, eight irrigation zones, soil probes, pump/flow, weather, 5 t cold room, thermal store, grid exchange and farmer approvals in five-minute steps.

## Run

From the directory containing the `app` folder:

```powershell
python -m pip install -r app/requirements.txt
python -m uvicorn app.server:app --reload
```

Open <http://127.0.0.1:8000>. From inside this folder, `python -m uvicorn server:app --reload` also works.

## Validate scenarios

- Use **+1 hour** or a faster clock to watch priority routing change with sunlight.
- Approve the irrigation recommendation and verify the pump only runs in daylight with enough PV.
- Inject cloud, rain, heatwave, grid outage, probe fault and open-door events.
- Confirm cold safety, exact energy balance and the project targets in **Claim validation**.
- Toggle the same scenario button again to clear grid, probe and door faults; **Reset** restores the baseline.

## Decision intelligence

The simulator retains a rolling seven-day telemetry window and turns it into operational evidence:

- Solar, productive load and export trends
- Cold-chain safety percentage and estimated ice autonomy
- Driest-zone priority and soil-probe availability
- Automatic findings explaining risks and opportunities
- Timestamped scenario-event history
- One-click telemetry CSV and complete JSON run-report exports

Use **+1 day** or **+1 week** to build a representative analysis window quickly. Short windows are intentionally labelled as provisional.

This is a deterministic functional digital twin. It validates control logic and user journeys; it does not replace a calibrated PVsyst, EPANET, refrigeration or AquaCrop study for procurement-grade sizing.
