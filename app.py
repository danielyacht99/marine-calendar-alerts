from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def load_config() -> Dict[str, Any]:
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_calendar_service():
    creds = None
    token_file = Path("token.json")
    creds_file = Path("credentials.json")

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds)


def hash_event_id(key: str) -> str:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return f"m{digest[:24]}"


def fetch_open_meteo(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    loc = config["location"]
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "hourly": "wave_height,ocean_current_velocity",
        "wind_speed_unit": "kn",
        "timezone": "Asia/Jerusalem",
        "forecast_days": config.get("forecast_days", 3),
    }
    marine = requests.get(url, params=params, timeout=30)
    marine.raise_for_status()
    marine_data = marine.json()

    weather_url = "https://api.open-meteo.com/v1/forecast"
    weather_params = {
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "hourly": "wind_speed_10m",
        "wind_speed_unit": "kn",
        "timezone": "Asia/Jerusalem",
        "forecast_days": config.get("forecast_days", 3),
    }
    weather = requests.get(weather_url, params=weather_params, timeout=30)
    weather.raise_for_status()
    weather_data = weather.json()

    times = marine_data["hourly"]["time"]
    wave = marine_data["hourly"]["wave_height"]
    current = marine_data["hourly"]["ocean_current_velocity"]
    wind_map = dict(zip(weather_data["hourly"]["time"], weather_data["hourly"]["wind_speed_10m"]))

    rows = []
    for i, ts in enumerate(times):
        rows.append(
            {
                "time": ts,
                "source": "open_meteo",
                "wave_height_m": wave[i],
                "current_speed_kn": current[i],
                "wind_speed_kn": wind_map.get(ts),
            }
        )
    return rows


def fetch_copernicus(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = config.get("copernicus", {})
    if not cfg.get("enabled", False):
        return []

    try:
        import copernicusmarine  # type: ignore
    except ImportError:
        print("Copernicus disabled in practice: package 'copernicusmarine' not installed.")
        return []

    username = cfg.get("username") or os.getenv("COPERNICUSMARINE_SERVICE_USERNAME")
    password = cfg.get("password") or os.getenv("COPERNICUSMARINE_SERVICE_PASSWORD")

    if not username or not password:
        print("Copernicus disabled in practice: missing username/password.")
        return []

    loc = config["location"]
    days = config.get("forecast_days", 3)
    start_dt = dt.datetime.combine(dt.date.today(), dt.time.min)
    end_dt = start_dt + dt.timedelta(days=days)

    rows: List[Dict[str, Any]] = []

    # Wave product
    try:
        wave_ds = copernicusmarine.open_dataset(
            dataset_id=cfg.get("wave_dataset_id", "cmems_mod_med_wav_anfc_4.2km_PT1H-i"),
            username=username,
            password=password,
            variables=[cfg.get("wave_variable", "VHM0")],
            minimum_longitude=loc["longitude"] - 0.05,
            maximum_longitude=loc["longitude"] + 0.05,
            minimum_latitude=loc["latitude"] - 0.05,
            maximum_latitude=loc["latitude"] + 0.05,
            start_datetime=start_dt.isoformat(),
            end_datetime=end_dt.isoformat(),
        )

        wave_var = cfg.get("wave_variable", "VHM0")
        wave_series = wave_ds[wave_var]
        dims = set(wave_series.dims)
        selectors = {}
        if "latitude" in dims:
            selectors["latitude"] = loc["latitude"]
        if "lat" in dims:
            selectors["lat"] = loc["latitude"]
        if "longitude" in dims:
            selectors["longitude"] = loc["longitude"]
        if "lon" in dims:
            selectors["lon"] = loc["longitude"]
        if selectors:
            wave_series = wave_series.sel(method="nearest", **selectors)
        if hasattr(wave_series, "to_series"):
            for ts, value in wave_series.to_series().items():
                rows.append(
                    {
                        "time": ts.isoformat(),
                        "source": "copernicus",
                        "wave_height_m": None if value is None else float(value),
                        "current_speed_kn": None,
                        "wind_speed_kn": None,
                    }
                )
    except Exception as e:
        print(f"Copernicus wave fetch failed: {e}")

    # Physics product for currents
    try:
        phy_ds = copernicusmarine.open_dataset(
            dataset_id=cfg.get("physics_dataset_id", "cmems_mod_med_phy-cur_anfc_4.2km_PT1H-m"),
            username=username,
            password=password,
            variables=[cfg.get("u_current_variable", "uo"), cfg.get("v_current_variable", "vo")],
            minimum_longitude=loc["longitude"] - 0.05,
            maximum_longitude=loc["longitude"] + 0.05,
            minimum_latitude=loc["latitude"] - 0.05,
            maximum_latitude=loc["latitude"] + 0.05,
            start_datetime=start_dt.isoformat(),
            end_datetime=end_dt.isoformat(),
        )

        u_var = cfg.get("u_current_variable", "uo")
        v_var = cfg.get("v_current_variable", "vo")
        u_series = phy_ds[u_var]
        v_series = phy_ds[v_var]

        for series in (u_series, v_series):
            dims = set(series.dims)
            selectors = {}
            if "latitude" in dims:
                selectors["latitude"] = loc["latitude"]
            if "lat" in dims:
                selectors["lat"] = loc["latitude"]
            if "longitude" in dims:
                selectors["longitude"] = loc["longitude"]
            if "lon" in dims:
                selectors["lon"] = loc["longitude"]
            if "depth" in dims:
                selectors["depth"] = 0
            if selectors:
                series = series.sel(method="nearest", **selectors)
            if series is u_series:
                u_series = series
            else:
                v_series = series

        if hasattr(u_series, "to_series") and hasattr(v_series, "to_series"):
            u_map = {ts.isoformat(): float(val) for ts, val in u_series.to_series().items()}
            v_map = {ts.isoformat(): float(val) for ts, val in v_series.to_series().items()}
            for ts in sorted(set(u_map) | set(v_map)):
                u = u_map.get(ts)
                v = v_map.get(ts)
                speed_kn = None
                if u is not None and v is not None:
                    speed_ms = (u ** 2 + v ** 2) ** 0.5
                    speed_kn = speed_ms * 1.9438444924406
                rows.append(
                    {
                        "time": ts,
                        "source": "copernicus",
                        "wave_height_m": None,
                        "current_speed_kn": speed_kn,
                        "wind_speed_kn": None,
                    }
                )
    except Exception as e:
        print(f"Copernicus current fetch failed: {e}")

    return merge_source_rows(rows)


def merge_source_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["time"], row["source"])
        if key not in merged:
            merged[key] = {
                "time": row["time"],
                "source": row["source"],
                "wave_height_m": None,
                "current_speed_kn": None,
                "wind_speed_kn": None,
            }
        for field in ("wave_height_m", "current_speed_kn", "wind_speed_kn"):
            if row.get(field) is not None:
                merged[key][field] = row[field]
    return list(merged.values())


def fetch_stormglass(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    api_key = config.get("stormglass_api_key") or os.getenv("STORMGLASS_API_KEY")
    if not api_key:
        return []

    loc = config["location"]
    params = {
        "lat": loc["latitude"],
        "lng": loc["longitude"],
        "params": ",".join(["waveHeight", "currentSpeed", "windSpeed"]),
        "source": "sg",
    }
    headers = {"Authorization": api_key}
    url = "https://api.stormglass.io/v2/weather/point"
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for item in data.get("hours", []):
        rows.append(
            {
                "time": item["time"].replace("Z", "+00:00"),
                "source": "stormglass",
                "wave_height_m": read_sg_value(item.get("waveHeight")),
                "current_speed_kn": ms_to_kn(read_sg_value(item.get("currentSpeed"))),
                "wind_speed_kn": ms_to_kn(read_sg_value(item.get("windSpeed"))),
            }
        )
    return rows


def read_sg_value(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for key in ("sg", "noaa", "meto", "dwd", "icon"):
            if key in v and v[key] is not None:
                return float(v[key])
        for value in v.values():
            if isinstance(value, (int, float)):
                return float(value)
    return None


def ms_to_kn(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return v * 1.9438444924406


def group_by_day_and_hour(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for row in rows:
        dt_obj = dt.datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        day = dt_obj.date().isoformat()
        hour_key = dt_obj.replace(minute=0, second=0, microsecond=0).isoformat()
        grouped.setdefault(day, {}).setdefault(hour_key, []).append(row)
    return grouped


def summarize_day(day: str, per_hour_rows: Dict[str, List[Dict[str, Any]]], thresholds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    exceed_hours = []
    strong_hours = []
    severe_hours = []
    max_wave = None
    max_wind = None
    max_current = None
    worst_sources = set()

    for hour_key, rows in per_hour_rows.items():
        wave_values = [r["wave_height_m"] for r in rows if r.get("wave_height_m") is not None]
        wind_values = [r["wind_speed_kn"] for r in rows if r.get("wind_speed_kn") is not None]
        current_values = [r["current_speed_kn"] for r in rows if r.get("current_speed_kn") is not None]

        wave_med = median(wave_values) if wave_values else None
        wind_med = median(wind_values) if wind_values else None
        current_med = median(current_values) if current_values else None

        wave_regular = False
        wind_regular = False
        current_regular = False
        wave_severe = False
        wind_severe = False
        current_severe = False

        if wave_values:
            wave_regular = (wave_med is not None and wave_med > thresholds["wave_height_m"]) or max(wave_values) > thresholds["wave_height_m"] * 1.15
            wave_severe = max(wave_values) >= thresholds["severe"]["wave_height_m"]
            max_wave = max(max_wave or 0, max(wave_values))
        if wind_values:
            wind_regular = (wind_med is not None and wind_med > thresholds["wind_speed_kn"]) or max(wind_values) > thresholds["wind_speed_kn"] * 1.15
            wind_severe = max(wind_values) >= thresholds["severe"]["wind_speed_kn"]
            max_wind = max(max_wind or 0, max(wind_values))
        if current_values:
            current_regular = (current_med is not None and current_med > thresholds["current_speed_kn"]) or max(current_values) > thresholds["current_speed_kn"] * 1.15
            current_severe = max(current_values) >= thresholds["severe"]["current_speed_kn"]
            max_current = max(max_current or 0, max(current_values))

        triggered = wave_regular or wind_regular or current_regular
        severe = wave_severe or wind_severe or current_severe
        strong = False
        if wave_values and wind_values:
            strong = max(wave_values) >= thresholds["storm_combo"]["wave_height_m"] and max(wind_values) >= thresholds["storm_combo"]["wind_speed_kn"]

        if triggered:
            exceed_hours.append(hour_key)
            for r in rows:
                if (
                    (r.get("wave_height_m") is not None and r["wave_height_m"] > thresholds["wave_height_m"]) or
                    (r.get("wind_speed_kn") is not None and r["wind_speed_kn"] > thresholds["wind_speed_kn"]) or
                    (r.get("current_speed_kn") is not None and r["current_speed_kn"] > thresholds["current_speed_kn"])
                ):
                    worst_sources.add(r["source"])
        if strong:
            strong_hours.append(hour_key)
        if severe or strong:
            severe_hours.append(hour_key)

    if not exceed_hours:
        return None

    severe_flag = bool(severe_hours)
    date_obj = dt.date.fromisoformat(day)
    summary = build_summary(date_obj, severe_flag)
    description = build_description(
        day=date_obj,
        exceed_hours=exceed_hours,
        strong_hours=strong_hours,
        severe_hours=severe_hours,
        max_wave=max_wave,
        max_wind=max_wind,
        max_current=max_current,
        sources=sorted(worst_sources),
        thresholds=thresholds,
    )

    return {"day": day, "summary": summary, "description": description, "severe": severe_flag}


def build_summary(date_obj: dt.date, severe: bool) -> str:
    if severe:
        return f"⛔ התראת סערה / קשירת יאכטה - {date_obj.isoformat()}"
    return f"⚠ תנאי ים חריגים - מרינה הרצליה - {date_obj.isoformat()}"


def build_description(day: dt.date, exceed_hours, strong_hours, severe_hours, max_wave, max_wind, max_current, sources, thresholds) -> str:
    lines = [
        f"תאריך: {day.isoformat()}",
        "",
        "התרעה אוטומטית לפי תחזית ימית.",
        f"ספים רגילים: גל מעל {thresholds['wave_height_m']} מ', רוח מעל {thresholds['wind_speed_kn']} קשר, זרם מעל {thresholds['current_speed_kn']} קשר.",
        f"ספי סערה: גל מעל {thresholds['severe']['wave_height_m']} מ', רוח מעל {thresholds['severe']['wind_speed_kn']} קשר, זרם מעל {thresholds['severe']['current_speed_kn']} קשר.",
        f"סף משולב לקשירה מוגברת: גל מעל {thresholds['storm_combo']['wave_height_m']} מ' וגם רוח מעל {thresholds['storm_combo']['wind_speed_kn']} קשר.",
        "",
        f"שעות חריגה: {', '.join(extract_hour(h) for h in exceed_hours)}",
    ]
    if strong_hours:
        lines.append(f"שעות תנאי סערה משולבים: {', '.join(extract_hour(h) for h in strong_hours)}")
    if severe_hours:
        lines.append(f"שעות סערה / קשירה מוגברת: {', '.join(extract_hour(h) for h in severe_hours)}")
    lines.extend([
        "",
        f"מקסימום גלים: {fmt(max_wave, 'מ')}",
        f"מקסימום רוח: {fmt(max_wind, 'קשר')}",
        f"מקסימום זרם: {fmt(max_current, 'קשר')}",
        "",
        f"מקורות ששימשו לחישוב: {', '.join(sources) if sources else 'open_meteo'}",
    ])
    return "\n".join(lines)


def fmt(v: Optional[float], unit: str) -> str:
    return "לא זמין" if v is None else f"{v:.2f} {unit}"


def extract_hour(hour_key: str) -> str:
    try:
        return dt.datetime.fromisoformat(hour_key).strftime("%H:%M")
    except Exception:
        return hour_key


def upsert_all_day_event(service, calendar_id: str, event_payload: Dict[str, Any]) -> None:
    day = dt.date.fromisoformat(event_payload["day"])
    end_day = day + dt.timedelta(days=1)
    event_id = hash_event_id(f"marine-alert-{event_payload['day']}")

    body = {
        "id": event_id,
        "summary": event_payload["summary"],
        "description": event_payload["description"],
        "start": {"date": day.isoformat()},
        "end": {"date": end_day.isoformat()},
    }

    try:
        service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
        print(f"Updated all-day alert for {day.isoformat()}")
    except Exception:
        service.events().insert(calendarId=calendar_id, body=body).execute()
        print(f"Created all-day alert for {day.isoformat()}")


def delete_missing_events(service, calendar_id: str, active_days: List[str], lookahead_days: int = 3) -> None:
    today = dt.date.today()
    time_min = dt.datetime.combine(today, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(today + dt.timedelta(days=lookahead_days + 1), dt.time.min).isoformat() + "Z"

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    for event in events.get("items", []):
        event_id = event.get("id", "")
        start_date = event.get("start", {}).get("date")
        if event_id.startswith("m") and start_date and start_date not in active_days:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            print(f"Deleted outdated alert for {start_date}")


def run():
    config = load_config()
    calendar_id = config["calendar_id"]

    rows = []
    rows.extend(fetch_open_meteo(config))
    rows.extend(fetch_copernicus(config))
    rows.extend(fetch_stormglass(config))

    grouped = group_by_day_and_hour(rows)
    thresholds = config["thresholds"]

    service = get_calendar_service()

    active_days = []
    for day, per_hour_rows in grouped.items():
        summary = summarize_day(day, per_hour_rows, thresholds)
        if summary:
            active_days.append(day)
            upsert_all_day_event(service, calendar_id, summary)

    delete_missing_events(service, calendar_id, active_days, lookahead_days=config.get("forecast_days", 3))


if __name__ == "__main__":
    run()
