import json
import os
from datetime import datetime, timezone, timedelta

import requests

TZ = timezone(timedelta(hours=7))  # เวลาไทย
now = datetime.now(TZ)
today = now.strftime("%Y-%m-%d")


def env(name):
    return os.environ.get(name, "").strip()


with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

try:
    with open("data.json", encoding="utf-8") as f:
        old = json.load(f)
except Exception:
    old = {}

# ---------- 1) อ่านค่าจาก Ecowitt ----------
soil = {}
temp = None
humidity = None
try:
    channels = [f"soil_ch{p['channel']}" for p in cfg["plots"]]
    r = requests.get(
        "https://api.ecowitt.net/api/v3/device/real_time",
        params={
            "application_key": env("ECOWITT_APP_KEY"),
            "api_key": env("ECOWITT_API_KEY"),
            "mac": env("ECOWITT_MAC"),
            "call_back": ",".join(["outdoor", "indoor"] + channels),
            "temp_unitid": 1,  # องศาเซลเซียส
        },
        timeout=30,
    )
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(j.get("msg"))
    d = j["data"]

    def val(*path):
        x = d
        for k in path:
            if not isinstance(x, dict) or k not in x:
                return None
            x = x[k]
        try:
            return float(x)
        except Exception:
            return None

    for p in cfg["plots"]:
        soil[p["channel"]] = val(f"soil_ch{p['channel']}", "soilmoisture", "value")
    temp = val("outdoor", "temperature", "value")
    if temp is None:
        temp = val("indoor", "temperature", "value")
    humidity = val("outdoor", "humidity", "value")
    if humidity is None:
        humidity = val("indoor", "humidity", "value")
except Exception as e:
    print("Ecowitt error:", e)

# ---------- 2) พยากรณ์ฝนจาก Open-Meteo ----------
rain = None      # โอกาสฝนที่เหลือของวันนี้ (สูงสุด)
rain_day = None  # โอกาสฝนสูงสุดทั้งวัน
try:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "hourly": "precipitation_probability",
            "daily": "precipitation_probability_max",
            "timezone": "Asia/Bangkok",
            "forecast_days": 1,
        },
        timeout=30,
    )
    w = r.json()
    rain_day = w["daily"]["precipitation_probability_max"][0]
    start = now.strftime("%Y-%m-%dT%H:00")
    left = [
        p
        for t, p in zip(w["hourly"]["time"], w["hourly"]["precipitation_probability"])
        if t >= start and p is not None
    ]
    rain = max(left) if left else rain_day
except Exception as e:
    print("Open-Meteo error:", e)


# ---------- 3) ตัดสินใจรดน้ำ ----------
def decide(s, p):
    rainy = rain is not None and rain >= cfg["rain_threshold"]
    if s is None:
        return "unknown", "ไม่มีข้อมูลดิน"
    if s >= p["skip_above"]:
        return "skip", "ดินชื้นพอ ไม่ต้องรดน้ำ"
    if s < p["water_below"]:
        if rainy:
            return "wait", "ดินแห้ง แต่ฝนมีโอกาสสูง รอฝนก่อน"
        return "water", "ควรรดน้ำ"
    if rainy:
        return "skip", "ฝนมีโอกาสสูง ไม่ต้องรดน้ำ"
    return "ok", "ยังไม่ต้องรดน้ำ"


plots = []
for p in cfg["plots"]:
    s = soil.get(p["channel"])
    status, text = decide(s, p)
    plots.append(
        {
            "name": p["name"],
            "soil": s,
            "water_below": p["water_below"],
            "skip_above": p["skip_above"],
            "status": status,
            "text": text,
        }
    )

data = {
    "garden_name": cfg["garden_name"],
    "updated_at": now.isoformat(timespec="minutes"),
    "temp": temp,
    "humidity": humidity,
    "rain": rain,
    "rain_day": rain_day,
    "rain_threshold": cfg["rain_threshold"],
    "plots": plots,
    "last_sent": old.get("last_sent"),
}

# ---------- 4) ส่ง LINE ----------
ICON = {"skip": "✅", "ok": "✅", "wait": "⏳", "water": "💧", "unknown": "❓"}


def build_message():
    lines = [f"{cfg['garden_name']} {now.strftime('%H:%M')}"]
    if rain is not None:
        lines.append(f"ฝนวันนี้มีโอกาส {rain:.0f}%")
    else:
        lines.append("ดึงพยากรณ์ฝนไม่ได้")
    if temp is not None:
        lines.append(f"อุณหภูมิ {temp:.1f}°C")
    lines.append("")
    for pl in plots:
        s = "-" if pl["soil"] is None else f"{pl['soil']:.0f}%"
        lines.append(f"{ICON[pl['status']]} {pl['name']}: ดิน {s} → {pl['text']}")
    return "\n".join(lines)


def send_line(text):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": "Bearer " + env("LINE_TOKEN")},
        json={"to": env("LINE_USER_ID"), "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    print("LINE:", r.status_code, r.text)
    return r.status_code == 200


slot = f"{today}-{now.hour}"
manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
due = (
    now.hour in cfg["alert_hours"]
    and now.minute < 30
    and data["last_sent"] != slot
)

if manual:
    send_line("ทดสอบระบบ\n" + build_message())
elif due:
    if send_line(build_message()):
        data["last_sent"] = slot

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print("done", data["updated_at"])
