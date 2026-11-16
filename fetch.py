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
MODELS = cfg.get("models", ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "jma_seamless"])
rain = None         # ค่าเฉลี่ยโอกาสฝนที่เหลือของวันนี้ (จากทุกโมเดลที่มีข้อมูล)
rain_day = None     # ค่าเฉลี่ยโอกาสฝนสูงสุดทั้งวัน
rain_models = {}    # ค่าของแต่ละโมเดล (เก็บไว้เทียบความแม่นทีหลัง)
try:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "hourly": "precipitation_probability",
            "models": ",".join(MODELS),
            "timezone": "Asia/Bangkok",
            "forecast_days": 1,
        },
        timeout=30,
    )
    w = r.json()
    times = w["hourly"]["time"]
    start = now.strftime("%Y-%m-%dT%H:00")
    day_vals = {}
    for m in MODELS:
        series = w["hourly"].get(f"precipitation_probability_{m}")
        if not series:
            continue
        valid = [(t, p) for t, p in zip(times, series) if p is not None]
        left = [p for t, p in valid if t >= start]
        if valid:
            day_vals[m] = max(p for t, p in valid)
        if left:
            rain_models[m] = max(left)
    if rain_models:
        rain = round(sum(rain_models.values()) / len(rain_models))
    if day_vals:
        rain_day = round(sum(day_vals.values()) / len(day_vals))
    print("models used:", rain_models)
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
    "rain_models": rain_models,
    "rain_threshold": cfg["rain_threshold"],
    "plots": plots,
    "last_sent": old.get("last_sent"),
}

# ---------- 4) ส่ง LINE ----------
ICON = {"skip": "✅", "ok": "✅", "wait": "⏳", "water": "💧", "unknown": "❓"}


def build_message():
    lines = [f"{cfg['garden_name']} {now.strftime('%H:%M')}"]
    if rain is not None:
        lines.append(f"ฝนวันนี้มีโอกาส {rain:.0f}% (เฉลี่ย {len(rain_models)} โมเดล)")
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
