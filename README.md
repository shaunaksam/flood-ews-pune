# Urban Flood & Waterlogging Early Warning System — Pune Pilot

Final-year internship project — a city-wide flood early warning platform for Pune,
modeling two connected flood sources: local rainfall-driven waterlogging and
Khadakwasla dam-discharge-driven river-corridor flooding.

## Repo structure

- `docs/` — project plan, feasibility study, POC findings, proposal & pitch materials
- `ingestion/` — data ingestion services (rainfall, ward boundaries, elevation, discharge data)
- `risk-engine/` — Python/FastAPI ward-level + river-corridor risk-scoring engine
- `api/` — Node/Express backend API & auth layer
- `alerting/` — Redis-based alert/escalation worker
- `dashboard/` — React PWA (citizen + authority views)

## Project plan

See `docs/Project_Plan.xlsx` for the full milestone breakdown, task-level schedule,
and demo dates. Status: **M0 (Discovery & Feasibility) complete — M1 in progress.**

## Status

| Milestone | Status |
|---|---|
| M0 — Discovery & Feasibility | Completed |
| M1 — Data Foundation & Ward-Level Risk Engine | Not Started |
| M2 — River-Corridor Model & Database Layer | Not Started |
| M3 — API Layer & Alerting | Not Started |
| M4 — Citizen & Authority Dashboard | Not Started |
| M5 — Containerization & Pilot Deployment | Not Started |
