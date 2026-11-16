import json
import math
import os
from datetime import datetime, timezone, timedelta

import requests

TZ = timezone(timedelta(hours=7))  # เวลาไทย
now = datetime.now(TZ)
today = now.strftime("%Y-%m-%d")
hour_key = now.strftime("%Y-%m-%dT%H")
prev_hour_key = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")


def env(name):
    return os.environ.get(name, "").strip()


def send_line(text):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": "Bearer " + env("LINE_TOKEN")},
        json={"to": env("LINE_USER_ID"), "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    print("LINE:", r.status_code, r.text)
    return r.status_code == 200


with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

try:
    with open("data.json", encoding="utf-8") as f:
        old = json.load(f)
except Exception:
    old = {}

try:
    with open("history.json", encoding="utf-8") as f:
        history = json.load(f)
except Exception:
    history = []

# ---------- 1) อ่านค่าจาก Ecowitt ----------
soil = {}
temp = None
humidity = None
th_source = None
rain_gauge = None  # ค่าฝนที่วัดได้จริงในแปลง (ถ้ามีเซ็นเซอร์ฝน)
try:
    channels = [f"soil_ch{p['channel']}" for p in cfg["plots"]]
    r = requests.get(
        "https://api.ecowitt.net/api/v3/device/real_time",
        params={
            "application_key": env("ECOWITT_APP_KEY"),
            "api_key": env("ECOWITT_API_KEY"),
            "mac": env("ECOWITT_MAC"),
            "call_back": ",".join(["outdoor", "indoor", "rainfall", "rainfall_piezo"] + channels),
            "temp_unitid": 1,      # องศาเซลเซียส
            "rainfall_unitid": 12,  # มิลลิเมตร
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

    # ใช้อุณหภูมิ+ความชื้นจากจุดเดียวกัน (นอกบ้านก่อน ถ้าไม่มีใช้ในตัว gateway)
    temp = val("outdoor", "temperature", "value")
    humidity = val("outdoor", "humidity", "value")
    th_source = "outdoor"
    if temp is None or humidity is None:
        temp = val("indoor", "temperature", "value")
        humidity = val("indoor", "humidity", "value")
        th_source = "indoor"

    for key in ("rainfall", "rainfall_piezo"):
        dv = val(key, "daily", "value")
        if dv is not None:
            rain_gauge = {
                "source": key,
                "daily": dv,
                "rate": val(key, "rain_rate", "value") or 0.0,
            }
            break
except Exception as e:
    print("Ecowitt error:", e)

# ---------- 2) พยากรณ์ฝนจาก Open-Meteo (หลายโมเดล เฉลี่ยกัน) ----------
MODELS = cfg.get("models", ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "jma_seamless"])
rain = None         # ค่าเฉลี่ยโอกาสฝนที่เหลือของวันนี้
rain_day = None     # ค่าเฉลี่ยโอกาสฝนสูงสุดทั้งวัน
rain_models = {}    # ค่าของแต่ละโมเดล (โอกาสฝนสูงสุดที่เหลือของวัน)
hour_prob = None    # โอกาสฝนเฉลี่ยของชั่วโมงนี้
hour_probs = {}     # โอกาสฝนชั่วโมงนี้แยกตามโมเดล
hour_mm_fc = None   # ปริมาณฝนที่โมเดลคาดชั่วโมงนี้ (มม. เฉลี่ย)
try:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "hourly": "precipitation_probability,precipitation",
            "models": ",".join(MODELS),
            "timezone": "Asia/Bangkok",
            "forecast_days": 1,
        },
        timeout=30,
    )
    w = r.json()
    times = w["hourly"]["time"]
    start = now.strftime("%Y-%m-%dT%H:00")
    cur = times.index(start) if start in times else None
    day_vals = {}
    mm_vals = []
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
        if cur is not None and series[cur] is not None:
            hour_probs[m] = series[cur]
        mm_series = w["hourly"].get(f"precipitation_{m}")
        if mm_series and cur is not None and mm_series[cur] is not None:
            mm_vals.append(mm_series[cur])
    if rain_models:
        rain = round(sum(rain_models.values()) / len(rain_models))
    if day_vals:
        rain_day = round(sum(day_vals.values()) / len(day_vals))
    if hour_probs:
        hour_prob = round(sum(hour_probs.values()) / len(hour_probs))
    if mm_vals:
        hour_mm_fc = round(sum(mm_vals) / len(mm_vals), 1)
    print("models used:", rain_models)
except Exception as e:
    print("Open-Meteo error:", e)

# ---------- VPD ----------
STAGES = cfg.get("vpd_stages", {})
STAGE = cfg.get("current_stage")
vpd = None
if temp is not None and humidity is not None:
    svp = 0.6108 * math.exp(17.27 * temp / (temp + 237.3))  # kPa
    vpd = round(svp * (1 - humidity / 100), 2)
vpd_match = [n for n, (lo, hi) in STAGES.items() if vpd is not None and lo <= vpd <= hi]
vpd_range = STAGES.get(STAGE)
vpd_status = None
if vpd is not None and vpd_range:
    vpd_status = "low" if vpd < vpd_range[0] else "high" if vpd > vpd_range[1] else "ok"
VPD_TEXT = {
    "ok": "อยู่ในช่วงเหมาะสม",
    "low": "ต่ำกว่าช่วงเหมาะสม (อากาศชื้นมาก)",
    "high": "สูงกว่าช่วงเหมาะสม (อากาศแห้ง พืชคายน้ำมาก)",
}


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

# ---------- 4) เทียบฝนจริงในแปลง กับที่โมเดลบอก ----------
RAIN_MIN = cfg.get("rain_min_mm", 0.2)   # ต่ำกว่านี้ถือว่า "ไม่มีฝน"
LOW_PROB = cfg.get("low_prob", 30)       # โมเดลบอกต่ำกว่านี้ถือว่า "บอกว่าไม่ตก"
state = old.get("state") or {}
rain_alerts = []
hour_mm = None

if rain_gauge:
    daily = rain_gauge["daily"]

    def since(start_daily):
        if start_daily is None:
            return None
        return round(daily - start_daily, 1) if daily >= start_daily else round(daily, 1)

    # เริ่มชั่วโมงใหม่: สรุปชั่วโมงที่แล้ว
    if state.get("hour") != hour_key:
        if state.get("hour") == prev_hour_key and state.get("daily_start") is not None:
            actual = since(state["daily_start"])
            history.append(
                {
                    "hour": state["hour"],
                    "prob": state.get("prob"),
                    "models": state.get("models"),
                    "mm_fc": state.get("mm_fc"),
                    "mm": actual,
                }
            )
            p0 = state.get("prob")
            if p0 is not None and p0 >= cfg["rain_threshold"] and actual < RAIN_MIN:
                last = state.get("last_false_alarm")
                if not last or (now - datetime.fromisoformat(last)).total_seconds() > 3 * 3600:
                    hh = int(state["hour"][-2:])
                    msg = (
                        f"☀️ โมเดลบอกฝน {p0:.0f}% ช่วง {hh:02d}:00-{(hh + 1) % 24:02d}:00 "
                        f"แต่ในแปลงไม่พบฝน (วัดได้ {actual:.1f} มม.)"
                    )
                    rain_alerts.append(msg)
                    state["last_false_alarm"] = now.isoformat(timespec="minutes")
                    state["last_note"] = {"time": now.isoformat(timespec="minutes"), "kind": "false_alarm", "text": msg}
        state.update(
            {
                "hour": hour_key,
                "daily_start": daily,
                "prob": hour_prob,
                "models": hour_probs,
                "mm_fc": hour_mm_fc,
                "alerted": False,
            }
        )

    hour_mm = since(state.get("daily_start"))
    raining = rain_gauge["rate"] > 0 or (hour_mm is not None and hour_mm >= RAIN_MIN)
    if raining and not state.get("alerted"):
        fc = state.get("prob")
        fc_mm = state.get("mm_fc")
        head = f"🌧 ฝนตกในแปลง ชั่วโมงนี้ {hour_mm or 0:.1f} มม. (วันนี้ {daily:.1f} มม.)"
        if fc is None:
            kind, tag = "info", "ไม่มีพยากรณ์ชั่วโมงนี้เทียบ"
        elif fc < LOW_PROB:
            kind, tag = "miss", f"⚠️ โมเดลพลาด: บอกโอกาสฝนชั่วโมงนี้แค่ {fc:.0f}%"
        else:
            kind, tag = "match", f"✅ ตรงกับโมเดล: บอกโอกาสฝน {fc:.0f}%"
        if fc_mm is not None:
            tag += f" (คาดฝน ~{fc_mm:.1f} มม.)"
        rain_alerts.append(head + "\n" + tag)
        state["alerted"] = True
        state["last_note"] = {"time": now.isoformat(timespec="minutes"), "kind": kind, "text": tag.replace("⚠️ ", "").replace("✅ ", "")}

history = history[-2880:]  # เก็บย้อนหลังประมาณ 120 วัน

data = {
    "garden_name": cfg["garden_name"],
    "updated_at": now.isoformat(timespec="minutes"),
    "temp": temp,
    "humidity": humidity,
    "rain": rain,
    "rain_day": rain_day,
    "rain_models": rain_models,
    "vpd": vpd,
    "vpd_status": vpd_status,
    "vpd_text": VPD_TEXT.get(vpd_status),
    "vpd_stage": STAGE,
    "vpd_range": vpd_range,
    "vpd_match": vpd_match,
    "th_source": th_source,
    "rain_threshold": cfg["rain_threshold"],
    "rain_gauge": ({**rain_gauge, "hour_mm": hour_mm} if rain_gauge else None),
    "hour_prob": hour_prob,
    "plots": plots,
    "state": state,
    "last_sent": old.get("last_sent"),
}

# ---------- 5) ส่ง LINE ----------
ICON = {"skip": "✅", "ok": "✅", "wait": "⏳", "water": "💧", "unknown": "❓"}


def build_message():
    lines = [f"{cfg['garden_name']} {now.strftime('%H:%M')}"]
    if rain is not None:
        lines.append(f"ฝนวันนี้มีโอกาส {rain:.0f}% (เฉลี่ย {len(rain_models)} โมเดล)")
    else:
        lines.append("ดึงพยากรณ์ฝนไม่ได้")
    if rain_gauge:
        lines.append(f"ฝนที่วัดได้วันนี้ {rain_gauge['daily']:.1f} มม.")
    if temp is not None:
        lines.append(f"อุณหภูมิ {temp:.1f}°C")
    if vpd is not None:
        lines.append(f"VPD {vpd:.2f} kPa")
        if vpd_status:
            lines.append(f"ระยะ{STAGE} ({vpd_range[0]}-{vpd_range[1]}): {VPD_TEXT[vpd_status]}")
        if th_source == "indoor":
            lines.append("(อุณหภูมิ/ความชื้นจากตัว gateway ในอาคาร)")
    lines.append("")
    for pl in plots:
        s = "-" if pl["soil"] is None else f"{pl['soil']:.0f}%"
        lines.append(f"{ICON[pl['status']]} {pl['name']}: ดิน {s} → {pl['text']}")
    return "\n".join(lines)


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

if rain_alerts and not manual:
    send_line("\n\n".join(rain_alerts))

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
with open("history.json", "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
print("done", data["updated_at"])
