#!/usr/bin/env python3
import os
import json
import hashlib
import gspread
import pandas as pd
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# -----------------------------
# Helpers
# -----------------------------
def get_garmin_client():
    client = Garmin(os.environ.get('GARMIN_EMAIL'), os.environ.get('GARMIN_PASSWORD'))
    client.login()
    return client

def seconds_to_hms(seconds):
    if not seconds:
        return "00:00:00"
    return str(timedelta(seconds=int(float(seconds))))

def format_run_pace(ms):
    if not ms or ms <= 0:
        return ""
    seconds_per_km = 1000 / ms
    return f"{int(seconds_per_km // 60):02d}:{int(seconds_per_km % 60):02d}"

def format_swim_pace(ms):
    if not ms or ms <= 0:
        return ""
    seconds_100m = 100 / ms
    return f"{int(seconds_100m // 60):02d}:{int(seconds_100m % 60):02d}"

def _row_checksum(rec):
    s = "|".join(str(rec.get(k, "")) for k in ("startTimeLocal", "distance", "duration", "activityType"))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# -----------------------------
# Core: fetch + clean activities
# -----------------------------
def get_clean_activities(client, lookback_days=7, start=0, limit=50):
    """
    Fetch activities and return a cleaned pandas DataFrame:
      - normalize startTimeLocal to datetime
      - ensure activityId exists (fallback to checksum)
      - distance in km (distance_km)
      - duration in seconds (duration_s)
      - activityType_key normalized
      - drop duplicate activityId rows (keep first)
      - filter by lookback_days
    """
    activities = client.get_activities(start, limit)
    if not activities:
        return pd.DataFrame()

    df = pd.DataFrame(activities)

    # Normalize startTimeLocal
    if "startTimeLocal" in df.columns:
        df["startTimeLocal"] = pd.to_datetime(df["startTimeLocal"], errors="coerce")

    # Ensure activityId exists
    if "activityId" in df.columns:
        df["activityId"] = df["activityId"].astype(str)
    else:
        df["activityId"] = df.apply(lambda r: _row_checksum(r.to_dict()), axis=1)

    # Distance numeric (km)
    if "distance" in df.columns:
        df["distance_km"] = pd.to_numeric(df["distance"], errors="coerce")
        # If values look like meters (>100), convert to km
        mask = df["distance_km"].notna() & (df["distance_km"] > 100)
        df.loc[mask, "distance_km"] = df.loc[mask, "distance_km"] / 1000.0

    # Duration numeric
    if "duration" in df.columns:
        df["duration_s"] = pd.to_numeric(df["duration"], errors="coerce")

    # activityType normalization (string)
    if "activityType" in df.columns:
        def _type_key(x):
            try:
                return (x or {}).get("typeKey", "").lower() if isinstance(x, dict) else str(x).lower()
            except Exception:
                return str(x).lower()
        df["activityType_key"] = df["activityType"].apply(_type_key)
    else:
        df["activityType_key"] = ""

    # Drop duplicate activityId rows
    df = df.drop_duplicates(subset=["activityId"], keep="first").reset_index(drop=True)

    # Filter by lookback_days if startTimeLocal present
    if "startTimeLocal" in df.columns:
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
        df = df[df["startTimeLocal"] >= cutoff]

    return df

# -----------------------------
# Sync to Google Sheet (append new)
# -----------------------------
def sync_activities_to_sheet(lookback_days=7, start=0, limit=50):
    """
    Fetch cleaned activities and append only truly new activities to the 'Sport' sheet.
    """
    print(f"🚀 Start Daily Sport Sync (last {lookback_days} days)...")
    client = get_garmin_client()

    # Google Sheets Auth
    gc = gspread.authorize(Credentials.from_service_account_info(
        json.loads(os.environ.get('GOOGLE_CREDENTIALS')),
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    ))
    sh = gc.open_by_key(os.environ.get('SHEET_ID'))
    sport_sheet = sh.worksheet("Sport")

    # Load existing IDs from sheet robustly: try to find a header column that looks like activityId
    all_values = sport_sheet.get_all_values()
    existing_ids = set()
    if all_values:
        header = all_values[0]
        # find index of column that contains 'activity' or 'id'
        idx = None
        for i, h in enumerate(header):
            if h and ("activity" in h.lower() or "id" in h.lower()):
                idx = i
                break
        if idx is not None:
            for row in all_values[1:]:
                if len(row) > idx and row[idx] not in ("", None):
                    existing_ids.add(str(row[idx]))
        else:
            # fallback: if sheet has at least 11 columns, assume column 11 (index 10) is activityId (legacy)
            if len(header) > 10:
                for row in all_values[1:]:
                    if len(row) > 10 and row[10]:
                        existing_ids.add(str(row[10]))

    df = get_clean_activities(client, lookback_days=lookback_days, start=start, limit=limit)
    if df.empty:
        print("  💤 No activities fetched.")
        return

    new_rows = []
    for _, act in df.iterrows():
        start_time = act.get("startTimeLocal")
        start_time_str = start_time.strftime("%Y-%m-%d %H:%M:%S") if not pd.isna(start_time) else ""
        act_date_iso = start_time_str[:10] if start_time_str else ""

        act_id = str(act.get("activityId", ""))
        if act_id in existing_ids:
            continue

        type_key = act.get("activityType_key", "")
        # cadence / swolf logic
        swolf = act.get("averageSwolf") if "averageSwolf" in act else None
        cadence = (
            act.get('averageRunningCadenceInStepsPerMinute') or
            act.get('averageCadence') or
            act.get('averageBikingCadenceInRevPerMinute') or
            0
        )
        if "swimming" in str(type_key) and swolf:
            value_col_i = round(swolf, 0)
        else:
            value_col_i = round(cadence, 0) if cadence else 0

        avg_speed_ms = round(act.get('averageSpeed', 0) or 0, 3)
        pace_run = format_run_pace(avg_speed_ms) if "running" in str(type_key) else ""
        speed_bike = round(avg_speed_ms * 3.6, 2) if any(x in str(type_key) for x in ["cycling", "biking"]) else ""
        pace_swim = format_swim_pace(avg_speed_ms) if "swimming" in str(type_key) else ""

        distance_km = ""
        if "distance_km" in act and not pd.isna(act["distance_km"]):
            distance_km = round(float(act["distance_km"]), 2)
        else:
            raw = act.get("distance")
            try:
                raw_n = float(raw)
                if raw_n > 100:
                    distance_km = round(raw_n / 1000.0, 2)
                else:
                    distance_km = round(raw_n, 2)
            except Exception:
                distance_km = ""

        duration_s = act.get("duration_s") if "duration_s" in act else act.get("duration", 0)
        duration_hms = seconds_to_hms(duration_s)

        new_rows.append([
            act_date_iso,
            act.get('activityName', '-'),
            type_key,
            distance_km,
            duration_hms,
            act.get('calories', 0),
            act.get('averageHR', 0),
            act.get('maxHR', 0),
            value_col_i,
            round(act.get('elevationGain', 0) or 0, 0),
            act_id,
            avg_speed_ms,
            pace_run,
            speed_bike,
            pace_swim
        ])

    if new_rows:
        # keep chronological order (oldest first) so append_rows adds newest at bottom
        new_rows.reverse()
        sport_sheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        print(f"  ✅ {len(new_rows)} new activities added.")
    else:
        print("  💤 All up to date.")


if __name__ == "__main__":
    sync_activities_to_sheet()
