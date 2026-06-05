#!/usr/bin/env python3
import os
import json
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# Config via env (secrets)
SHEET_ID = os.environ.get("SHEET_ID")
# Optioneel: override default doelen via secrets (strings)
# Als leeg of niet gezet: gebruik standaardcombinaties hieronder
GOAL_DISTANCE_KM = os.environ.get("GOAL_DISTANCE_KM")  # optioneel, niet gebruikt bij standaardcombinaties
GOAL_STEPS = os.environ.get("GOAL_STEPS")              # optioneel

def sheets_client():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def read_sport_sheet():
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet("Sport")
    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return pd.DataFrame()
    header = vals[0]
    rows = vals[1:]
    df = pd.DataFrame(rows, columns=header)
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
    """
    Jouw gewenste combinaties:
      - >=10000 stappen
      - of >=25 km en >=5000 stappen
      - of >=100 km en >=1000 stappen
    Retourneert (bool, reason_string)
    """
    if steps is not None and steps >= 10000:
        return True, "≥ 10.000 stappen"
    if km is not None and steps is not None and km >= 25 and steps >= 5000:
        return True, "≥ 25 km en ≥ 5.000 stappen"
    if km is not None and steps is not None and km >= 100 and steps >= 1000:
        return True, "≥ 100 km en ≥ 1.000 stappen"
    return False, "Geen combinatie gehaald"

def main():
    df = read_sport_sheet()
    if df.empty:
        print("Geen data in Sport sheet gevonden.")
        return

    # normalize header lookup
    cols = {c.strip().lower(): c for c in df.columns}
    date_col = cols.get("starttimelocal") or cols.get("date") or df.columns[0]

    # distance candidates
    dist_col = None
    for cand in ("distance_km","distance","dist","distance_km"):
        if cand in cols:
            dist_col = cols[cand]
            break
    # steps candidates
    steps_col = None
    for cand in ("steps","totalsteps","stepcount"):
        if cand in cols:
            steps_col = cols[cand]
            break

    # parse
    df[date_col] = pd.to_datetime(df[date_col].astype(str).str[:10], errors="coerce")
    if dist_col:
        df["_dist"] = df[dist_col].apply(parse_number)
    else:
        df["_dist"] = None
    if steps_col:
        df["_steps"] = df[steps_col].apply(parse_int)
    else:
        df["_steps"] = None

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    def summarize_for(day):
        sub = df[df[date_col].dt.date == day]
        total_km = sub["_dist"].dropna().astype(float).sum() if "_dist" in sub else 0.0
        total_steps = sub["_steps"].dropna().astype(int).sum() if "_steps" in sub else 0
        return total_km, total_steps

    y_km, y_steps = summarize_for(yesterday)
    t_km, t_steps = summarize_for(today)

    # Evaluate combinations (or fallback to simple thresholds if provided)
    # If user provided GOAL_DISTANCE_KM and GOAL_STEPS, we can also evaluate simple AND/OR rules.
    # But by default we use the three predefined combinations.
    y_met, y_reason = goal_combinations_met(y_km, y_steps)
    t_met, t_reason = goal_combinations_met(t_km, t_steps)

    # Build email body
    lines = []
    lines.append(f"Garmin sync resultaat — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
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
    lines.append("Opmerking: de standaardcombinaties zijn:")
    lines.append("  • ≥10.000 stappen")
    lines.append("  • of ≥25 km en ≥5.000 stappen")
    lines.append("  • of ≥100 km en ≥1.000 stappen")
    lines.append("")
    lines.append("Groet,")
    lines.append("Je Garmin Sync")

    body = "\n".join(lines)

    out_dir = os.path.dirname(__file__)
    if not out_dir:
        out_dir = "."
    out_path = os.path.join(out_dir, "goals_email.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)

    print(body)

if __name__ == "__main__":
    main()
