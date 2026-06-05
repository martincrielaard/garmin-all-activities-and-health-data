#!/usr/bin/env python3
import os
import json
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# Config via env (secrets)
SHEET_ID = os.environ.get("SHEET_ID")
GOAL_DISTANCE_KM = float(os.environ.get("GOAL_DISTANCE_KM") or 0)  # 0 = disabled
GOAL_STEPS = int(os.environ.get("GOAL_STEPS") or 0)               # 0 = disabled

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
        # replace comma decimal with dot
        s = str(x).replace(",", ".")
        return float(s)
    except:
        return None

def main():
    df = read_sport_sheet()
    if df.empty:
        print("Geen data in Sport sheet gevonden.")
        return

    # normalize column names to lower for robust lookup
    cols = {c.strip().lower(): c for c in df.columns}
    date_col = None
    if "starttimelocal" in cols:
        date_col = cols["starttimelocal"]
    elif "date" in cols:
        date_col = cols["date"]
    else:
        # try first column
        date_col = df.columns[0]

    # distance column candidates
    dist_col = None
    for cand in ("distance_km", "distance", "dist"):
        if cand in cols:
            dist_col = cols[cand]
            break
    # steps column candidates
    steps_col = None
    for cand in ("steps", "totalsteps", "stepcount"):
        if cand in cols:
            steps_col = cols[cand]
            break

    # parse dates and numeric columns
    df[date_col] = pd.to_datetime(df[date_col].astype(str).str[:10], errors="coerce")
    if dist_col:
        df["_dist"] = df[dist_col].apply(parse_number)
    else:
        df["_dist"] = None
    if steps_col:
        df["_steps"] = df[steps_col].apply(lambda x: int(float(str(x).replace(",", "."))) if x not in (None,"") else None)
    else:
        df["_steps"] = None

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    def summarize_for(day):
        sub = df[df[date_col].dt.date == day]
        total_km = sub["_dist"].dropna().astype(float).sum() if "_dist" in sub else 0
        total_steps = sub["_steps"].dropna().astype(int).sum() if "_steps" in sub else 0
        return total_km, total_steps

    y_km, y_steps = summarize_for(yesterday)
    t_km, t_steps = summarize_for(today)

    # Build email body
    lines = []
    lines.append(f"Garmin sync resultaat — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"Gisteren ({yesterday.isoformat()}):")
    if GOAL_DISTANCE_KM > 0:
        lines.append(f"  Afstand: {y_km:.2f} km (doel: {GOAL_DISTANCE_KM} km) -> {'✅ gehaald' if y_km >= GOAL_DISTANCE_KM else '❌ niet gehaald'}")
    if GOAL_STEPS > 0:
        lines.append(f"  Stappen: {y_steps} (doel: {GOAL_STEPS}) -> {'✅ gehaald' if y_steps >= GOAL_STEPS else '❌ niet gehaald'}")
    if GOAL_DISTANCE_KM == 0 and GOAL_STEPS == 0:
        lines.append("  Geen doelen ingesteld (GOAL_DISTANCE_KM en GOAL_STEPS niet gezet).")

    lines.append("")
    lines.append(f"Vandaag ({today.isoformat()}):")
    if GOAL_DISTANCE_KM > 0:
        lines.append(f"  Afstand: {t_km:.2f} km (doel: {GOAL_DISTANCE_KM} km) -> {'✅ gehaald' if t_km >= GOAL_DISTANCE_KM else '❌ niet gehaald'}")
    if GOAL_STEPS > 0:
        lines.append(f"  Stappen: {t_steps} (doel: {GOAL_STEPS}) -> {'✅ gehaald' if t_steps >= GOAL_STEPS else '❌ niet gehaald'}")

    lines.append("")
    lines.append("Groet,")
    lines.append("Je Garmin Sync")

    body = "\n".join(lines)

    # write to file for workflow to pick up
    out_path = os.path.join(os.path.dirname(__file__), "goals_email.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)

    # also print to stdout (visible in Actions log)
    print(body)

if __name__ == "__main__":
    main()
