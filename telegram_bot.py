import os
import re
import json
import time
import threading
import hashlib
from datetime import datetime, timedelta, timezone
import requests
from sqlalchemy import create_engine, text, inspect

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/flood_ews")
SALT = os.getenv("REPORT_HASH_SALT", "floodews-dev-salt")
CTX_TTL_SECONDS = 600
MAX_REPORTS_PER_HOUR = 6
WARD_COOLDOWN_MINUTES = 15
ALERT_MM = float(os.getenv("ALERT_RAIN_MM", "30"))
MANUAL_SOURCE = "IMD station data (manual entry)"
SEVERITY_LABELS = {1: "Water on the road", 2: "Knee-deep or vehicles stuck", 3: "Dangerous flooding"}
RAIN_LABELS = {1: "Light rain", 2: "Steady rain", 3: "Heavy downpour"}

WELCOME = (
    "Pune Rain & Safety bot\n\n"
    "Share your location and I can:\n"
    "- subscribe you to alerts for your ward\n"
    "- let you report flooding or waterlogging near you\n"
    "- tell us if it is raining where you are\n\n"
    "Privacy: I keep only your ward and a one-way code made from your chat ID. "
    "Reports show on the public map anonymously, by ward, as unverified citizen reports for 6 hours. "
    "I never store your exact location. /stop deletes your subscription.\n\n"
    "This bot is not an emergency service. If anyone is in danger, call 112.\n\n"
    "Commands: /report /subscribe /status /stop /help"
)
HELP = "Tap /report to report flooding, /subscribe to get alerts for your ward, /status to see your subscription, /stop to unsubscribe. You will be asked to share your location; only your ward is used."
LOCATION_KEYBOARD = {"keyboard": [[{"text": "Share my location", "request_location": True}]], "resize_keyboard": True, "one_time_keyboard": True}


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def init_db(engine):
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine.dialect.name == "sqlite" else "SERIAL PRIMARY KEY"
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE IF NOT EXISTS citizen_reports (id {pk}, ward INTEGER NOT NULL, severity INTEGER NOT NULL, reporter TEXT NOT NULL, created_at TIMESTAMP NOT NULL)"))
        c.execute(text("CREATE TABLE IF NOT EXISTS tg_subscribers (reporter TEXT PRIMARY KEY, chat_id BIGINT NOT NULL, ward INTEGER NOT NULL, created_at TIMESTAMP NOT NULL)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS idx_citizen_reports_time ON citizen_reports (created_at)"))
        c.execute(text(f"CREATE TABLE IF NOT EXISTS rain_observations (id {pk}, station TEXT NOT NULL, mm REAL NOT NULL, entered_at TIMESTAMP NOT NULL)"))
        c.execute(text(f"CREATE TABLE IF NOT EXISTS rain_reports (id {pk}, ward INTEGER NOT NULL, intensity INTEGER NOT NULL, reporter TEXT NOT NULL, created_at TIMESTAMP NOT NULL)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS idx_rain_reports_time ON rain_reports (created_at)"))
        c.execute(text("CREATE TABLE IF NOT EXISTS alert_drafts (key TEXT PRIMARY KEY, created_at TIMESTAMP NOT NULL)"))
    if "source" not in [col["name"] for col in inspect(engine).get_columns("rain_observations")]:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE rain_observations ADD COLUMN source TEXT"))


OBS_HELP = (
    "Admin command. Send observed 24-hour rain like:\n"
    "/obs Shivajinagar 41.1, Hadapsar 35.5, Magarpatta 30.5\n\n"
    "/obs list shows what is on the page. /obs clear removes it all. Entries show on the page for 30 hours."
)


def parse_obs(body):
    good, bad = [], []
    for chunk in re.split(r"[,;\n]+", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        nums = re.findall(r"\d+(?:\.\d+)?", chunk)
        name = re.sub(r"\d+(?:\.\d+)?|mm|[:=\-]", " ", chunk, flags=re.I)
        name = " ".join(name.split())
        if len(nums) != 1 or not name or not (0 <= float(nums[0]) <= 600):
            bad.append(chunk)
            continue
        good.append((name.title(), float(nums[0])))
    return good, bad


def hash_id(chat_id):
    return hashlib.sha256(f"{SALT}:{chat_id}".encode()).hexdigest()[:24]


class Bot:
    def __init__(self, engine, ward_lookup, send, admin_chat_id=None):
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else None
        self.engine = engine
        self.ward_lookup = ward_lookup
        self.send = send
        self.ctx = {}
        self.pending = None

    def say(self, chat, message, markup=None):
        payload = {"chat_id": chat, "text": message}
        if markup:
            payload["reply_markup"] = markup
        self.send("sendMessage", payload)

    def handle_update(self, u):
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        m = u.get("message")
        if not m or m.get("chat", {}).get("type") != "private":
            return
        chat = m["chat"]["id"]
        if "location" in m:
            return self.on_location(chat, m["location"])
        raw = (m.get("text") or "").strip()
        t = raw.lower()
        cmd = t.split()[0].split("@")[0] if t else ""
        if cmd == "/start":
            self.say(chat, WELCOME, LOCATION_KEYBOARD)
        elif cmd in ("/report", "/subscribe"):
            self.say(chat, "Please share your location with the button below. Only your ward is used.", LOCATION_KEYBOARD)
        elif cmd == "/status":
            self.on_status(chat)
        elif cmd == "/stop":
            self.on_stop(chat)
        elif cmd == "/obs":
            self.on_obs(chat, raw)
        elif cmd in ("/sendalert", "/dropalert"):
            self.on_alert_command(chat, cmd)
        elif cmd == "/help":
            self.say(chat, HELP)
        else:
            self.say(chat, "I did not understand that. Try /report, /subscribe or /help.")

    def on_location(self, chat, loc):
        hit = self.ward_lookup(float(loc["longitude"]), float(loc["latitude"]))
        if not hit:
            self.say(chat, "That location is outside Pune's 58 wards, so I can't use it.")
            return
        ward, name = hit
        self.ctx[chat] = (int(ward), str(name), time.time())
        markup = {"inline_keyboard": [
            [{"text": "Get alerts for this ward", "callback_data": "sub"}],
            [{"text": "Report: water on the road", "callback_data": "rep:1"}],
            [{"text": "Report: knee-deep / vehicles stuck", "callback_data": "rep:2"}],
            [{"text": "Report: dangerous flooding", "callback_data": "rep:3"}],
            [{"text": "Rain here: light", "callback_data": "rain:1"}, {"text": "steady", "callback_data": "rain:2"}, {"text": "heavy", "callback_data": "rain:3"}],
        ]}
        self.say(chat, f"You are in {name} (ward {ward}). What would you like to do?", markup)

    def on_callback(self, cb):
        chat = cb.get("message", {}).get("chat", {}).get("id")
        if chat is None or cb.get("message", {}).get("chat", {}).get("type") != "private":
            return
        self.send("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        entry = self.ctx.get(chat)
        if not entry or time.time() - entry[2] > CTX_TTL_SECONDS:
            self.say(chat, "That location has expired. Please share your location again.", LOCATION_KEYBOARD)
            return
        ward, name, _ = entry
        data = cb.get("data", "")
        if data == "sub":
            self.subscribe(chat, ward, name)
        elif data.startswith("rep:") and data[4:].isdigit() and int(data[4:]) in SEVERITY_LABELS:
            self.report(chat, ward, name, int(data[4:]))
        elif data.startswith("rain:") and data[5:].isdigit() and int(data[5:]) in RAIN_LABELS:
            self.rain_report(chat, ward, name, int(data[5:]))

    def subscribe(self, chat, ward, name):
        who = hash_id(chat)
        with self.engine.begin() as c:
            c.execute(text("DELETE FROM tg_subscribers WHERE reporter = :r"), {"r": who})
            c.execute(text("INSERT INTO tg_subscribers (reporter, chat_id, ward, created_at) VALUES (:r, :c, :w, :t)"), {"r": who, "c": int(chat), "w": ward, "t": now_utc()})
        self.say(chat, f"Subscribed to {name} (ward {ward}). Alert delivery to subscribers is not switched on yet; you will be told before it starts. Send /stop any time to unsubscribe.")

    def report(self, chat, ward, name, severity):
        who = hash_id(chat)
        t = now_utc()
        with self.engine.begin() as c:
            hour = c.execute(text("SELECT COUNT(*) FROM citizen_reports WHERE reporter = :r AND created_at >= :s"), {"r": who, "s": t - timedelta(hours=1)}).scalar()
            same = c.execute(text("SELECT COUNT(*) FROM citizen_reports WHERE reporter = :r AND ward = :w AND created_at >= :s"), {"r": who, "w": ward, "s": t - timedelta(minutes=WARD_COOLDOWN_MINUTES)}).scalar()
            if hour >= MAX_REPORTS_PER_HOUR:
                self.say(chat, "You have reached the hourly report limit. Thank you, please try again later.")
                return
            if same:
                self.say(chat, f"You already reported for {name} in the last {WARD_COOLDOWN_MINUTES} minutes. Thank you.")
                return
            c.execute(text("INSERT INTO citizen_reports (ward, severity, reporter, created_at) VALUES (:w, :s, :r, :t)"), {"w": ward, "s": severity, "r": who, "t": t})
        msg = f"Thank you. Recorded for {name}: {SEVERITY_LABELS[severity].lower()}. It appears on the map as an unverified citizen report for 6 hours."
        if severity == 3:
            msg += "\n\nIf anyone is in danger, call 112 now."
        self.say(chat, msg)

    def rain_report(self, chat, ward, name, intensity):
        who = hash_id(chat)
        t = now_utc()
        with self.engine.begin() as c:
            hour = c.execute(text("SELECT COUNT(*) FROM rain_reports WHERE reporter = :r AND created_at >= :s"), {"r": who, "s": t - timedelta(hours=1)}).scalar()
            same = c.execute(text("SELECT COUNT(*) FROM rain_reports WHERE reporter = :r AND ward = :w AND created_at >= :s"), {"r": who, "w": ward, "s": t - timedelta(minutes=WARD_COOLDOWN_MINUTES)}).scalar()
            if hour >= MAX_REPORTS_PER_HOUR:
                self.say(chat, "You have reached the hourly report limit. Thank you, please try again later.")
                return
            if same:
                self.say(chat, f"You already sent a rain report for {name} in the last {WARD_COOLDOWN_MINUTES} minutes. Thank you.")
                return
            c.execute(text("INSERT INTO rain_reports (ward, intensity, reporter, created_at) VALUES (:w, :i, :r, :t)"), {"w": ward, "i": intensity, "r": who, "t": t})
        self.say(chat, f"Thank you. Recorded for {name}: {RAIN_LABELS[intensity].lower()}. Resident rain reports show on the map for 3 hours, unverified.")

    def on_obs(self, chat, raw):
        if not self.admin_chat_id:
            self.say(chat, "Admin commands are not set up. Set ADMIN_CHAT_ID (or TELEGRAM_TEST_CHAT_ID) and restart the bot.")
            return
        if str(chat) != self.admin_chat_id:
            self.say(chat, "That command is for the project owner only.")
            return
        body = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        low = body.lower()
        if not body:
            self.say(chat, OBS_HELP)
            return
        if low == "clear":
            with self.engine.begin() as c:
                c.execute(text("DELETE FROM rain_observations"))
            self.say(chat, "All observed-rain entries removed.")
            return
        if low == "list":
            with self.engine.connect() as c:
                rows = c.execute(text("SELECT station, mm, entered_at FROM rain_observations WHERE entered_at >= :s ORDER BY entered_at DESC"), {"s": now_utc() - timedelta(hours=30)}).fetchall()
            seen, lines = set(), []
            for st, mm, ts in rows:
                if st not in seen:
                    seen.add(st)
                    lines.append(f"{st}: {mm:.1f} mm")
            self.say(chat, "On the page now:\n" + "\n".join(lines) if lines else "Nothing on the page right now.")
            return
        good, bad = parse_obs(body)
        if good:
            self.record_observations(good, MANUAL_SOURCE)
        msg = f"Saved {len(good)} station(s): " + ", ".join(f"{s} {m:.1f} mm" for s, m in good) + ". Shown on the page for 30 hours." if good else "Nothing saved."
        if bad:
            msg += "\nCould not read: " + "; ".join(bad)
        self.say(chat, msg)

    def record_observations(self, pairs, source):
        t = now_utc()
        with self.engine.begin() as c:
            for st, mm in pairs:
                c.execute(text("INSERT INTO rain_observations (station, mm, entered_at, source) VALUES (:s, :m, :t, :src)"), {"s": st, "m": mm, "t": t, "src": source})
        top = max(pairs, key=lambda p: p[1])
        self.maybe_draft_alert(top[0], top[1], source)

    def maybe_draft_alert(self, station, mm, source):
        if mm < ALERT_MM or not self.admin_chat_id:
            return
        level = "very heavy" if mm >= 60 else "heavy"
        key = f"{(now_utc() + timedelta(hours=5, minutes=30)).date()}:{level}"
        with self.engine.begin() as c:
            if c.execute(text("SELECT COUNT(*) FROM alert_drafts WHERE key = :k"), {"k": key}).scalar():
                return
            c.execute(text("INSERT INTO alert_drafts (key, created_at) VALUES (:k, :t)"), {"k": key, "t": now_utc()})
            n = c.execute(text("SELECT COUNT(*) FROM tg_subscribers")).scalar()
        body = (f"Pune Rain & Safety: {level.capitalize()} rain recorded in the last 24 hours ({station}: {mm:.1f} mm, {source}). "
                "Expect waterlogging and slow traffic. Avoid underpasses and low-lying roads, and follow IMD and local-authority updates. Send /stop to unsubscribe.")
        self.pending = {"key": key, "text": body}
        enabled = os.getenv("SUBSCRIBER_ALERTS_ENABLED", "") == "1"
        note = "Send /sendalert to deliver it, or /dropalert to discard." if enabled else "Delivery to subscribers is switched off (SUBSCRIBER_ALERTS_ENABLED is not 1), so /sendalert will not send."
        self.say(int(self.admin_chat_id), f"DRAFT ALERT, not sent. {n} subscriber(s) would receive:\n\n{body}\n\n{note}")

    def on_alert_command(self, chat, cmd):
        if not self.admin_chat_id or str(chat) != self.admin_chat_id:
            self.say(chat, "That command is for the project owner only.")
            return
        if not self.pending:
            self.say(chat, "There is no draft alert waiting.")
            return
        if cmd == "/dropalert":
            self.pending = None
            self.say(chat, "Draft discarded.")
            return
        if os.getenv("SUBSCRIBER_ALERTS_ENABLED", "") != "1":
            self.say(chat, "Subscriber alerts are switched off. Set SUBSCRIBER_ALERTS_ENABLED=1 in the bot's terminal and restart to allow sending.")
            return
        with self.engine.connect() as c:
            ids = [int(r[0]) for r in c.execute(text("SELECT chat_id FROM tg_subscribers")).fetchall()]
        sent = 0
        for cid in ids:
            res = self.send("sendMessage", {"chat_id": cid, "text": self.pending["text"]})
            sent += 1 if isinstance(res, dict) and res.get("ok") else 0
            time.sleep(0.06)
        self.pending = None
        self.say(chat, f"Alert sent to {sent} of {len(ids)} subscriber(s).")

    def on_status(self, chat):
        with self.engine.connect() as c:
            row = c.execute(text("SELECT ward FROM tg_subscribers WHERE reporter = :r"), {"r": hash_id(chat)}).first()
        self.say(chat, f"You are subscribed to ward {row[0]}." if row else "You are not subscribed. Send /subscribe to start.")

    def on_stop(self, chat):
        with self.engine.begin() as c:
            c.execute(text("DELETE FROM tg_subscribers WHERE reporter = :r"), {"r": hash_id(chat)})
        self.say(chat, "Unsubscribed. Your subscription record has been deleted.")


def build_lookup(engine):
    import geopandas as gpd
    import shapely
    from shapely.geometry import Point
    g = gpd.read_postgis("SELECT wardnum, name, geometry FROM wards", engine, geom_col="geometry")
    if g.crs is not None and g.crs.to_epsg() not in (None, 4326):
        g = g.to_crs(epsg=4326)
    geoms = list(g.geometry)
    tree = shapely.STRtree(geoms)
    nums = list(g["wardnum"])
    names = list(g["name"])

    def lookup(lon, lat):
        idx = tree.query(Point(lon, lat), predicate="intersects")
        if len(idx) == 0:
            return None
        i = int(idx[0])
        return int(nums[i]), str(names[i])

    return lookup


def imd_loop(bot):
    import imd_reader
    last = None
    minutes = max(30, int(os.getenv("IMD_POLL_MINUTES", "60")))
    while True:
        try:
            r = imd_reader.fetch_homepage_reading()
            if r and (r["updated_on"], r["rain_mm"]) != last:
                last = (r["updated_on"], r["rain_mm"])
                bot.record_observations([("Shivajinagar", r["rain_mm"])], imd_reader.SOURCE)
                print(f"IMD Pune: {r['rain_mm']} mm (updated {r['updated_on']})")
        except Exception as exc:
            print("IMD reader error:", type(exc).__name__)
        time.sleep(minutes * 60)


def run():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Set it in the same way the API reads it, then start again.")
        return
    api = f"https://api.telegram.org/bot{token}"

    def send(method, payload):
        try:
            return requests.post(f"{api}/{method}", json=payload, timeout=20).json()
        except Exception as exc:
            print("send error:", type(exc).__name__)
            return {}

    engine = create_engine(DB_URL)
    init_db(engine)
    admin = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_TEST_CHAT_ID")
    bot = Bot(engine, build_lookup(engine), send, admin)
    if os.getenv("IMD_SCRAPE_ENABLED", "") == "1":
        threading.Thread(target=imd_loop, args=(bot,), daemon=True).start()
        print("IMD Pune homepage reader is ON (read-only, polite polling). Make sure you have IMD's permission to use this data publicly.")
    else:
        print("IMD homepage reader is off. Set IMD_SCRAPE_ENABLED=1 to turn it on.")
    try:
        me = requests.get(f"{api}/getMe", timeout=15).json()
    except Exception as exc:
        print("Could not reach Telegram:", type(exc).__name__, "- check your internet connection.")
        return
    if not me.get("ok"):
        print(f"Telegram rejected the token (error {me.get('error_code')}: {me.get('description')}).")
        print("Copy the full token from BotFather (looks like 123456789:ABC...), with no spaces or quotes, and start again.")
        return
    print("Bot running as @" + str(me.get("result", {}).get("username")))
    try:
        requests.post(f"{api}/deleteWebhook", json={"drop_pending_updates": False}, timeout=15)
    except Exception as exc:
        print("startup warning:", type(exc).__name__)
    print("Listening for messages. Press Ctrl+C to stop. Only replies to people who message the bot; nothing is broadcast.")
    offset = None
    last_error = None
    while True:
        try:
            data = requests.get(f"{api}/getUpdates", params={"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=45).json()
            if not data.get("ok"):
                msg = f"Telegram error {data.get('error_code')}: {data.get('description')}"
                if msg != last_error:
                    print(msg)
                    if data.get("error_code") == 409:
                        print("Another program is reading this bot's messages (for example telegram_find_chat.py). Close it.")
                    last_error = msg
                time.sleep(5)
                continue
            last_error = None
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                try:
                    bot.handle_update(u)
                except Exception as exc:
                    print("update error:", type(exc).__name__)
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as exc:
            print("poll error:", type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    run()
