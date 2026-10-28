from pathlib import Path
import numpy as np
import joblib

MODEL_PATH = Path(__file__).resolve().parent / "models" / "rain_prob_v1.joblib"
_bundle = None


def _load():
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(MODEL_PATH)
    return _bundle


def predict_heavy_rain_probs(local_24h_mm, upstream_24h_mm):
    bundle = _load()
    x = np.log1p(np.array([[max(float(local_24h_mm), 0.0), max(float(upstream_24h_mm), 0.0)]]))
    return {name: float(m.predict_proba(x)[0, 1]) for name, m in bundle["models"].items()}


def model_info():
    bundle = _load()
    return {
        "source": bundle.get("source"),
        "trained_days": bundle.get("trained_days"),
        "date_range": bundle.get("date_range"),
        "targets": bundle.get("targets"),
    }
