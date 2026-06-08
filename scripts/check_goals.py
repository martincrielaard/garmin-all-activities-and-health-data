#!/usr/bin/env python3
import os
import json
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo

SHEET_ID = os.environ.get("SHEET_ID")

def sheets_client():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def read_dashboard_sheet():
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Dashboard")
    except Exception as e:
        raise RuntimeError(f"Kan worksheet 'Dashboard' niet openen: {e}")

    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return pd.DataFrame()

    rows = vals[1:]
    normalized = []
    for r in rows:
        row = list(r) + [""] * max(0, 3 - len(r))
        normalized.append(row[:3])

    df = pd.DataFrame(normalized, columns=["date", "distance", "steps"])
    return df

def parse_number(x):
    if x is None or x == "":
        return None
    try:
        s = str(x).replace(",", ".")
        return float(s)
    except:
        return None

def parse_int(x):
    if x is None or x == "":
        return None
    try:
        s = str(x).replace(",", ".")
        return int(float(s))
    except:
        return None

def goal_combinations_met(km, steps):
    if steps is not None and steps >= 10000:
        return True, "≥ 10.000 stappen"
    if km is not None and steps is not None and km >= 25 and steps >= 5000:
        return True, "≥ 25 km en ≥ 5.000 stappen"
    if km is not None and steps is not None and km >= 100 and steps >= 1000:
        return True, "≥ 100 km en ≥ 1.000 stappen"
    return False, "Geen combinatie gehaald"

def main():
    try:
        df = read_dashboard_sheet()
    except Exception as e:
        body = f"Fout bij lezen van Dashboard sheet: {e}\n"
        print(body)
        # schrijf naar workspace zodat workflow het bestand altijd kan vinden
        workspace = os.environ.get("GITHUB_WORKSPACE", ".")
        out_dir = os.path.join(workspace, "scripts")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "goals_email.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body)
        return

    if df.empty:
        body = "Geen data in Dashboard sheet gevonden.\n"
        print(body)
        workspace = os.environ.get("GITHUB_WORKSPACE", ".")
        out_dir = os.path.join(workspace, "scripts")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "goals_email.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body)
        return

    df["date_parsed"] = pd.to_datetime(df["date"].astype(str).str[:10], errors="coerce")
    df["_dist"] = df["distance"].apply(parse_number)
    df["_steps"] = df["steps"].apply(parse_int)

    # gebruik Amsterdam tijdzone
    tz = ZoneInfo("Europe/Amsterdam")
    now_local = datetime.now(tz)
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    # en bij het maken van de header/timestamp:
    lines.append(f"Garmin sync resultaat — {now_local.strftime('%Y-%m-%d %H:%M')}")

    def summarize_for(day):
        sub = df[df["date_parsed"].dt.date == day]
        total_km = sub["_dist"].dropna().astype(float).sum() if "_dist" in sub else 0.0
        total_steps = sub["_steps"].dropna().astype(int).sum() if "_steps" in sub else 0
        return total_km, total_steps

    y_km, y_steps = summarize_for(yesterday)
    t_km, t_steps = summarize_for(today)

    y_met, y_reason = goal_combinations_met(y_km, y_steps)
    t_met, t_reason = goal_combinations_met(t_km, t_steps)

    lines = []
    lines.append(f"Garmin sync resultaat — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("Bron: tabblad 'Dashboard' (kolom A = datum, B = afstand, C = stappen)")
    lines.append("")
    lines.append(f"Gisteren ({yesterday.isoformat()}):")
    lines.append(f"  Afstand: {y_km:.2f} km")
    lines.append(f"  Stappen: {y_steps}")
    lines.append(f"  Doelstatus: {'✅ gehaald' if y_met else '❌ niet gehaald'} ({y_reason})")
    lines.append("")
    lines.append(f"Vandaag ({today.isoformat()}):")
    lines.append(f"  Afstand: {t_km:.2f} km")
    lines.append(f"  Stappen: {t_steps}")
    lines.append(f"  Doelstatus: {'✅ gehaald' if t_met else '❌ niet gehaald'} ({t_reason})")
    lines.append("")
    lines.append("Opmerking: standaardcombinaties:")
    lines.append("  • ≥10.000 stappen")
    lines.append("  • of ≥25 km en ≥5.000 stappen")
    lines.append("  • of ≥100 km en ≥1.000 stappen")
    lines.append("")
    lines.append("Groet,")
    lines.append("Je Garmin Sync")

    body = "\n".join(lines)

    # Schrijf expliciet naar de runner workspace zodat de workflowstappen het bestand altijd vinden
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    out_dir = os.path.join(workspace, "scripts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "goals_email.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Wrote email body to: {out_path}")
    print(body)

if __name__ == "__main__":
    main()
