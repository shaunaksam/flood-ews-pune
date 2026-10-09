import redis
from api.main import compute_risk, DEFAULT_CUSECS, fetch_forecast_rainfall

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

CRITICAL_THRESHOLD = 0.8
MIN_RISE_ABOVE_BASELINE = 0.05

def check_and_queue_alerts():
    raw_mm, corrected_mm, error = fetch_forecast_rainfall()
    if error:
        print(f"Skipping alert check, forecast unavailable: {error}")
        return

    wards = compute_risk(DEFAULT_CUSECS, corrected_mm or 0.0)
    wards["risk_rise"] = wards["risk_score"] - wards["static_vulnerability"]

    critical = wards[
        (wards["risk_score"] >= CRITICAL_THRESHOLD) &
        (wards["risk_rise"] >= MIN_RISE_ABOVE_BASELINE)
    ]

    for _, ward in critical.iterrows():
        message = f"ALERT: {ward['name']} at risk score {round(ward['risk_score'], 3)} (baseline {round(ward['static_vulnerability'], 3)})"
        r.lpush("flood_alerts", message)
        print("Queued:", message)

    if critical.empty:
        print("No wards currently critical beyond baseline, nothing queued.")

if __name__ == "__main__":
    print("--- Calm conditions check ---")
    check_and_queue_alerts()

    print("\n--- Simulated high-discharge test (not a real condition) ---")
    wards = compute_risk(30000, 205)
    wards["risk_rise"] = wards["risk_score"] - wards["static_vulnerability"]
    critical = wards[(wards["risk_score"] >= CRITICAL_THRESHOLD) & (wards["risk_rise"] >= MIN_RISE_ABOVE_BASELINE)]

    for _, ward in critical.iterrows():
        message = f"ALERT: {ward['name']} at risk score {round(ward['risk_score'], 3)} (baseline {round(ward['static_vulnerability'], 3)})"
        r.lpush("flood_alerts", message)
        print("Queued:", message)