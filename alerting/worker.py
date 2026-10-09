# alerting/worker.py
import redis, time
from redis.exceptions import TimeoutError as RedisTimeoutError

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

def run():
    print("Alert worker started, watching 'flood_alerts' queue...")
    while True:
        try:
            item = r.brpop("flood_alerts", timeout=5)
            if item:
                _, message = item
                print(f"[ALERT DELIVERED] {message}")
        except RedisTimeoutError:
            pass 
        time.sleep(1)

if __name__ == "__main__":
    run()