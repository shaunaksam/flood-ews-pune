"""Safe local test for the alert de-duplication logic.

This script imports the existing API scoring code and uses a fake in-memory
Redis replacement. It does NOT write to the real Redis alert list, send SMS,
or call Open-Meteo. It does read the local PostgreSQL wards table through the
existing scoring function.

Run from the project root with the same virtual environment as the API:
    python test_alert_deduplication.py
"""

from api import main as flood


class FakeRedis:
    """Minimal Redis substitute for testing NX/cooldown behaviour in memory."""
    def __init__(self):
        self.values = {}
        self.lists = {}

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start : end + 1 if end >= 0 else end]
        return True

    def expire(self, key, seconds):
        return True

    def delete(self, key):
        existed = key in self.values or key in self.lists
        self.values.pop(key, None)
        self.lists.pop(key, None)
        return int(existed)


def main():
    # Replace Redis only in this short-lived process. The real Redis service
    # is never written to by this test.
    fake_redis = FakeRedis()
    flood.alerts_redis = fake_redis

    with flood.engine.connect() as conn:
        wardnums = conn.execute(
            flood.text("SELECT wardnum FROM wards ORDER BY wardnum")
        ).scalars().all()

    if len(wardnums) != 58:
        raise SystemExit(f"Expected 58 wards in PostgreSQL, found {len(wardnums)}.")

    # Controlled synthetic heavy-rain case. Never a live forecast.
    forecast = {
        "source": "TEST ONLY — synthetic rainfall scenario",
        "data_type": "simulated_scenario_not_live",
        "forecast_6h_mm": 0.0,
        "forecast_24h_mm": 80.0,
        "max_forecast_6h_mm": 0.0,
        "max_forecast_24h_mm": 80.0,
        "ward_forecasts": [
            {
                "wardnum": int(wardnum),
                "forecast_6h_mm": 0.0,
                "forecast_24h_mm": 80.0,
            }
            for wardnum in wardnums
        ],
        "upstream_forecast_available": True,
        "upstream_forecast_6h_mm": 0.0,
        "upstream_forecast_24h_mm": 0.0,
        "upstream_max_forecast_6h_mm": 0.0,
        "upstream_max_forecast_24h_mm": 0.0,
        "upstream_forecasts": [],
        "upstream_error": None,
        "error": None,
    }

    first = flood.evaluate_rainfall_alert(forecast)
    second = flood.evaluate_rainfall_alert(forecast)

    print("SAFE TEST MODE: fake in-memory Redis; no real Redis alerts were written.")
    print("Scenario: synthetic 80 mm/24h at all 58 wards; not a live forecast.")
    print("First evaluation :", first.get("status"),
          "alert_created=", first.get("alert_created"))
    print("Second evaluation:", second.get("status"),
          "alert_created=", second.get("alert_created"))
    print("Fake Redis stored alerts:", len(fake_redis.lists.get(flood.ALERTS_REDIS_KEY, [])))

    passed = (
        first.get("status") == "alert_created"
        and first.get("alert_created") is True
        and second.get("status") == "duplicate_suppressed"
        and second.get("alert_created") is False
        and len(fake_redis.lists.get(flood.ALERTS_REDIS_KEY, [])) == 1
    )

    print("DEDUPLICATION TEST:", "PASS" if passed else "FAIL")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
