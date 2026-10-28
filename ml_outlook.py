import time
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter()

ROOT = Path(__file__).resolve().parent
CV_PATH = ROOT / "data" / "processed" / "rain_model_cv_predictions_v1.csv"
BAND_WATCH = 0.25
BAND_HIGH = 0.50
UPSTREAM_ELEVATED = 0.30
EVENT_LABELS = {
    "2024-07-25": "Jul 2024: flooding and dam release (25 Jul)",
    "2024-08-25": "Aug 2024: flash-flood warning (25 Aug)",
    "2025-08-20": "Aug 2025: heavy rain and dam release (20 Aug)",
}
_fetcher = None
_cv = None
_wcache = {"t": 0.0, "v": None}


def register_fetcher(fn):
    global _fetcher
    _fetcher = fn


def get_weather(max_age=90):
    now = time.monotonic()
    if _wcache["v"] is not None and now - _wcache["t"] < max_age:
        return _wcache["v"]
    if _fetcher is None:
        raise RuntimeError("Forecast source not registered.")
    v = _fetcher()
    if isinstance(v, dict) and not v.get("error"):
        _wcache["t"] = now
        _wcache["v"] = v
    return v


def _band(p):
    if p >= BAND_HIGH:
        return "High"
    if p >= BAND_WATCH:
        return "Watch"
    return "Low"


def _pct(p):
    n = int(round(p * 100))
    return n, ("under 1%" if n == 0 else f"{n}%")


def _package(p_local, p_up, local24, upstream24, model):
    n, label = _pct(p_local)
    out = {
        "available": True,
        "heavy_rain_chance": {
            "meaning": "regional average of 20 mm or more in the day",
            "probability": round(p_local, 3),
            "percent": n,
            "percent_label": label,
            "band": _band(p_local),
        },
        "upstream_heavy_rain_chance": None,
        "inputs": {"local_24h_mm": float(local24), "upstream_max_24h_mm": float(upstream24)},
        "model": model,
        "note": "Learned from archived forecasts vs observed rain (about 300 days, 2024-25). Estimates the chance of heavy rain, not of flooding. Not an official warning.",
    }
    if p_up is not None:
        un, ul = _pct(p_up)
        out["upstream_heavy_rain_chance"] = {
            "meaning": "any western or dam-side grid cell at 64.5 mm or more",
            "probability": round(p_up, 3),
            "percent": un,
            "percent_label": ul,
            "elevated": bool(p_up >= UPSTREAM_ELEVATED),
        }
    return out


def _line(out):
    hr = out["heavy_rain_chance"]
    line = f"Heavy-rain outlook (experimental, learned from past forecasts): {hr['percent_label']} chance of 20 mm+ regional rain in 24 h ({hr['band']})."
    up = out.get("upstream_heavy_rain_chance")
    if up and up["elevated"]:
        line += f" Dam-side heavy-rain chance {up['percent_label']}."
    return line


def compute_outlook(weather):
    try:
        local24 = weather.get("forecast_24h_mm")
        upstream_ok = bool(weather.get("upstream_forecast_available")) and not weather.get("upstream_error")
        upstream24 = weather.get("upstream_max_forecast_24h_mm") if upstream_ok else None
        if local24 is None or upstream24 is None:
            return {"available": False, "reason": "Forecast inputs incomplete; no estimate shown."}
        from rain_model import predict_heavy_rain_probs, model_info
        probs = predict_heavy_rain_probs(local24, upstream24)
        p_local = probs.get("regional_mean_ge_20mm")
        if p_local is None:
            return {"available": False, "reason": "Model not available."}
        return _package(p_local, probs.get("any_cell_ge_64_5mm"), local24, upstream24, model_info())
    except Exception as exc:
        return {"available": False, "reason": f"Estimate unavailable: {type(exc).__name__}"}


def telegram_line(weather):
    out = compute_outlook(weather)
    return _line(out) if out.get("available") else ""


def _load_cv():
    global _cv
    if _cv is None:
        import pandas as pd
        df = pd.read_csv(CV_PATH, parse_dates=["date"])
        df["key"] = df["date"].dt.strftime("%Y-%m-%d")
        _cv = df
    return _cv


def _label(key, row):
    if key in EVENT_LABELS:
        return EVENT_LABELS[key]
    day = row["date"].strftime("%d %b %Y")
    return f"Heavy-rain day, {day}" if row["obs_mean"] >= 10 else f"Quiet day, {day}"


@router.get("/ml/replay/events")
def ml_replay_events():
    try:
        df = _load_cv()
    except Exception:
        return JSONResponse(status_code=503, content={"available": False, "reason": "Replay data not found. Run train_rain_model.py first."})
    items = []
    for key in EVENT_LABELS:
        hit = df[df["key"] == key]
        if len(hit):
            items.append({"date": key, "label": EVENT_LABELS[key]})
    extras = df[~df["key"].isin(EVENT_LABELS)].sort_values("obs_mean", ascending=False).head(4)
    for _, r in extras.iterrows():
        items.append({"date": r["key"], "label": _label(r["key"], r)})
    quiet = df[(df["obs_mean"] < 1.0) & (df["local24_mm"] < 1.0) & (~df["key"].isin(EVENT_LABELS))]
    if len(quiet):
        r = quiet.iloc[len(quiet) // 2]
        items.append({"date": r["key"], "label": _label(r["key"], r)})
    return {"available": True, "events": items}


@router.get("/ml/replay")
def ml_replay(date: str):
    try:
        df = _load_cv()
    except Exception:
        return JSONResponse(status_code=503, content={"available": False, "reason": "Replay data not found."})
    hit = df[df["key"] == date]
    if hit.empty:
        return JSONResponse(status_code=404, content={"available": False, "reason": "Date not in replay data."})
    r = hit.iloc[0]
    p_local = r.get("p_regional_mean_ge_20mm")
    p_up = r.get("p_any_cell_ge_64_5mm")
    if p_local != p_local:
        return JSONResponse(status_code=503, content={"available": False, "reason": "No stored estimate for that day."})
    p_up = None if p_up != p_up else float(p_up)
    try:
        from rain_model import model_info
        info = model_info()
    except Exception:
        info = {}
    out = _package(float(p_local), p_up, r["local24_mm"], r["upstream24_mm"], info)
    out["replay"] = True
    out["date"] = date
    out["label"] = _label(date, r)
    out["actual"] = {"regional_mean_mm": round(float(r["obs_mean"]), 1), "highest_cell_mm": round(float(r["obs_max"]), 1)}
    out["message_preview"] = _line(out)
    out["note"] = "Replay of a past day from stored forecasts. The estimate was made with that month hidden from the model. Nothing is sent to anyone."
    return out


@router.get("/ml/outlook")
def ml_outlook():
    if _fetcher is None:
        return JSONResponse(status_code=503, content={"available": False, "reason": "Forecast source not registered."})
    try:
        weather = get_weather()
    except Exception as exc:
        return JSONResponse(status_code=503, content={"available": False, "reason": f"Forecast unavailable: {type(exc).__name__}"})
    if not isinstance(weather, dict) or weather.get("error"):
        return JSONResponse(status_code=503, content={"available": False, "reason": "Forecast unavailable; no estimate shown."})
    out = compute_outlook(weather)
    out["fetched_at_local"] = weather.get("fetched_at_local")
    return out


WIDGET_JS = r"""
(() => {
  const anchor = document.querySelector("main .card");
  if (!anchor || document.getElementById("mlCard")) return;
  const card = document.createElement("section");
  card.className = "card"; card.id = "mlCard"; card.setAttribute("aria-live", "polite");
  card.innerHTML = '<h2 id="mlHead">Chance of heavy rain · next 24 hours</h2><div class="status"><span id="mlDot" class="dot"></span><div><p id="mlTitle" class="statusTitle">Checking…</p><p id="mlText" class="statusText"></p></div></div><p id="mlUp" class="muted small"></p><details style="margin-top:10px"><summary class="link" style="cursor:pointer">Technical details</summary><div id="mlDetail" class="officialDetail small" style="white-space:pre-line"></div></details><div style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(120,140,150,.25)"><label class="muted small" for="mlSel">Demo: replay a past event</label><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px"><select id="mlSel" style="flex:1;min-width:200px;padding:9px 10px;border-radius:10px;border:1px solid rgba(120,140,150,.45);font:inherit;background:#fff;color:inherit"></select><button id="mlGo" type="button" style="padding:9px 14px;border-radius:10px;border:0;background:#0f6b73;color:#fff;font:inherit;font-weight:700;cursor:pointer">Replay</button><button id="mlLive" type="button" style="padding:9px 14px;border-radius:10px;border:1px solid rgba(120,140,150,.45);background:transparent;color:inherit;font:inherit;font-weight:700;cursor:pointer;display:none">Back to live</button></div></div>';
  anchor.after(card);
  const $ = id => document.getElementById(id);
  const TEXT = {
    Low: ["good", "Heavy rain across Pune looks unlikely in the next 24 hours. Keep an eye on updates."],
    Watch: ["watch", "Carry rain gear, avoid known waterlogging spots and check updates before travelling."],
    High: ["bad", "Plan travel carefully, avoid underpasses and riverside roads, and follow official updates."]
  };
  const PAST = {
    Low: ["good", "Heavy rain across Pune looked unlikely."],
    Watch: ["watch", "Advice would have been: carry rain gear, avoid waterlogging spots, check updates."],
    High: ["bad", "Advice would have been: plan travel carefully, avoid underpasses and riverside roads."]
  };
  function render(d, replay) {
    const h = d.heavy_rain_chance, t = (replay ? PAST : TEXT)[h.band], u = d.upstream_heavy_rain_chance, m = d.model || {};
    $("mlHead").textContent = replay ? "REPLAY · " + d.label : "Chance of heavy rain · next 24 hours";
    $("mlDot").className = "dot " + t[0];
    $("mlTitle").textContent = h.band + " chance of heavy rain (" + h.percent_label + ")";
    let text = t[1];
    if (replay) text += " What actually fell: regional average " + d.actual.regional_mean_mm + " mm, wettest area " + d.actual.highest_cell_mm + " mm.";
    $("mlText").textContent = text;
    const upText = u && u.elevated ? "Heavy rain is also possible in the hills and dam areas (" + u.percent_label + "). This does not confirm dam releases or flooding." : "";
    $("mlUp").textContent = replay ? (upText ? upText + " " : "") + "This is a past day, not today's forecast. Nothing was sent to anyone." : upText;
    const lines = [
      (replay ? "Forecast on that day: " : "Forecast used: ") + "city average " + d.inputs.local_24h_mm.toFixed(1) + " mm, highest upstream point " + d.inputs.upstream_max_24h_mm.toFixed(1) + " mm (24 h).",
      "Chance of regional average of 20 mm or more: " + h.percent_label + ".",
      u ? "Chance of any dam-side area at 64.5 mm or more: " + u.percent_label + "." : "",
      "Model: simple logistic regression trained on " + m.trained_days + " days (" + (m.date_range || []).join(" to ") + ").",
      replay ? "Alert line that would have been included: " + d.message_preview : "",
      d.note
    ];
    $("mlDetail").textContent = lines.filter(Boolean).join("\n");
    $("mlLive").style.display = replay ? "inline-block" : "none";
  }
  async function load() {
    try {
      const r = await fetch("/ml/outlook", {cache: "no-store"});
      const d = await r.json();
      if (!r.ok || !d.available) throw new Error(d.reason || "unavailable");
      render(d, false);
    } catch (e) {
      $("mlHead").textContent = "Chance of heavy rain · next 24 hours";
      $("mlDot").className = "dot";
      $("mlTitle").textContent = "Heavy-rain estimate unavailable";
      $("mlText").textContent = "Use the forecast above and check official updates.";
      $("mlUp").textContent = ""; $("mlDetail").textContent = ""; $("mlLive").style.display = "none";
    }
  }
  async function replay() {
    const v = $("mlSel").value;
    if (!v) return;
    try {
      const r = await fetch("/ml/replay?date=" + encodeURIComponent(v), {cache: "no-store"});
      const d = await r.json();
      if (!r.ok || !d.available) throw new Error(d.reason || "unavailable");
      render(d, true);
    } catch (e) {
      $("mlTitle").textContent = "Replay unavailable";
      $("mlText").textContent = String(e.message || e);
    }
  }
  async function loadEvents() {
    try {
      const r = await fetch("/ml/replay/events", {cache: "no-store"});
      const d = await r.json();
      if (!r.ok || !d.available) throw new Error(d.reason || "none");
      d.events.forEach(ev => { const o = document.createElement("option"); o.value = ev.date; o.textContent = ev.label; $("mlSel").appendChild(o); });
    } catch (e) {
      const o = document.createElement("option"); o.value = ""; o.textContent = "Replay data not found"; $("mlSel").appendChild(o); $("mlGo").disabled = true;
    }
  }
  $("mlGo").addEventListener("click", replay);
  $("mlLive").addEventListener("click", load);
  const btn = document.getElementById("refreshBtn");
  if (btn) btn.addEventListener("click", () => setTimeout(load, 300));
  loadEvents();
  load();
})();
"""


@router.get("/ml/widget.js")
def ml_widget():
    return Response(content=WIDGET_JS, media_type="application/javascript", headers={"Cache-Control": "no-store"})


@router.get("/citizen/v2", response_class=HTMLResponse)
def citizen_v2():
    from citizen_v2 import PAGE_HTML
    return HTMLResponse(content=PAGE_HTML, headers={"Cache-Control": "no-store"})


from ml_map import router as _map_router

router.include_router(_map_router)
