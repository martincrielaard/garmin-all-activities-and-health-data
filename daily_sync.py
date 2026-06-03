import os
import time
import pandas as pd
from garminconnect import Garmin
import gspread
from google.oauth2.service_account import Credentials

MAX_RETRIES = 5
RETRY_DELAY = 10

# -----------------------------
# Retry helper
# -----------------------------


def retry(func):
    def wrapper(*args, **kwargs):
        delay = RETRY_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "rate limit" in msg.lower():
                    print(f"Rate limit hit (attempt {attempt}/{MAX_RETRIES}), retrying in {delay}s... Error: {msg}")
                    time.sleep(delay)
                    delay = min(delay * 2, 300)
                else:
                    # voor andere fouten: log en herraise zodat we niet maskeren
                    print(f"Error on attempt {attempt}: {msg}")
                    raise
        raise Exception("Max retries reached")
    return wrapper


# -----------------------------
# Garmin login
# -----------------------------
@retry
def garmin_login():
    print("Logging in to Garmin...")
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    print("Garmin login OK")
    return client

# -----------------------------
# Google Sheets client
# -----------------------------
def sheets_client():
    creds = Credentials.from_service_account_info(
        eval(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    return sh

# -----------------------------
# Fetch activities
# -----------------------------
@retry
def fetch_activities(client):
    print("Fetching latest activities...")
    activities = client.get_activities(0, 50)
    df = pd.DataFrame(activities)
    df["startTimeLocal"] = pd.to_datetime(df["startTimeLocal"]).dt.date
    df["activityId"] = df["activityId"].astype(str)
    return df

# -----------------------------
# Fetch health for a date (helper)
# -----------------------------

@retry
def fetch_health_for_date(client, date_str):
    """
    date_str: 'yyyy-mm-dd'
    returns: dict (or None) with daily summary for that date
    """
    # Probeer meerdere mogelijke client‑methodes (fallback) en laat exceptions door voor retry
    possible_methods = [
        "get_daily_summary",
        "get_daily_summary_by_date",
        "get_stats",
        "get_daily_stats",
        "get_user_summary"
    ]

    last_exc = None
    for m in possible_methods:
        if hasattr(client, m):
            try:
                func = getattr(client, m)
                health = func(date_str)
                if not health:
                    return None
                health['calendarDate'] = pd.to_datetime(health.get('calendarDate', date_str)).date()
                return health
            except Exception as e:
                # bewaar laatste exception en probeer volgende fallback
                last_exc = e
                # als het een 429 of netwerkfout is, laat het omhoog gaan zodat retry wrapper het oppakt
                if "429" in str(e) or "rate limit" in str(e).lower():
                    raise
                # anders probeer volgende fallback
                continue

    # Als geen methode werkte: raise de laatste exception zodat retry kan optreden,
    # of return None als er geen exception was maar geen data.
    if last_exc:
        raise last_exc
    return None


# -----------------------------
# Fetch health for last N days
# -----------------------------
def fetch_health_last_n_days(client, n=3):
    """
    returns DataFrame with up to n rows, one per date (most recent first)
    """
    rows = []
    today = pd.Timestamp.now().normalize()
    for i in range(n):
        d = (today - pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        rec = fetch_health_for_date(client, d)
        if rec:
            rows.append(rec)
        else:
            print(f"No health summary for {d}")
    if not rows:
        return pd.DataFrame(columns=['calendarDate'])  # empty df
    df = pd.DataFrame(rows)
    # normalize calendarDate to string yyyy-mm-dd
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
    return df


# -----------------------------
# Append new rows for Sport
# -----------------------------
def append_new_rows(sh, tab_name, df, key_column):
    try:
        ws = sh.worksheet(tab_name)
    except:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=20)
        ws.update([df.columns.values.tolist()])
        print(f"Created sheet {tab_name}")
        ws.append_rows(df.values.tolist())
        return

    existing = ws.get_all_records()

    if existing:
        existing_df = pd.DataFrame(existing)
        existing_keys = set(existing_df[key_column].astype(str))
    else:
        existing_keys = set()

    df[key_column] = df[key_column].astype(str)
    new_rows = df[~df[key_column].isin(existing_keys)]

    if new_rows.empty:
        print(f"No new rows for {tab_name}")
        return

    ws.append_rows(new_rows.values.tolist())
    print(f"Added {len(new_rows)} new rows to {tab_name}")

def upsert_health_rows(sh, df):
    """
    Upsert multiple rows from df into sheet 'Health'.
    df must contain a 'calendarDate' column (Date or 'yyyy-mm-dd' string).
    """
    ws_title = "Health"
    try:
        ws = sh.worksheet(ws_title)
    except gspread.exceptions.WorksheetNotFound:
        header = df.columns.tolist()
        ws = sh.add_worksheet(title=ws_title, rows=2000, cols=max(20, len(header)))
        ws.update([header])
        df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
        ws.append_rows(df[header].astype(str).values.tolist())
        print(f"Created Health sheet and inserted {len(df)} rows")
        return

    # Backup current sheet values (simple backup as new worksheet)
    all_values = ws.get_all_values()
    backup_name = f"Health_backup_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        backup_ws = sh.add_worksheet(title=backup_name, rows=max(1, len(all_values)), cols=max(1, len(all_values[0]) if all_values else 1))
        if all_values:
            backup_ws.update(all_values)
        print(f"Backup created: {backup_name}")
    except Exception as e:
        print(f"Warning: backup failed: {e}")

    # Normalize incoming df dates to 'yyyy-mm-dd'
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")

    # Read header and existing rows
    all_values = ws.get_all_values()
    if not all_values:
        header = df.columns.tolist()
        ws.update([header])
        existing_rows = []
    else:
        header = all_values[0]
        existing_rows = all_values[1:]

    print("DEBUG: sheet header:", header)
    print("DEBUG: first 5 existing rows:", existing_rows[:5])

    # find index of calendarDate column in sheet header
    try:
        date_col_idx = header.index("calendarDate")
    except ValueError:
        raise Exception("Kolom 'calendarDate' niet gevonden in Health-sheet header")

    # build map date -> sheet_row_number (1-based) with robust normalization
    date_to_row = {}
    for i, row in enumerate(existing_rows, start=2):
        try:
            norm = pd.to_datetime(row[date_col_idx]).strftime('%Y-%m-%d')
            date_to_row[norm] = i
        except Exception:
            # fallback: raw first 10 chars if parsing fails
            try:
                date_to_row[str(row[date_col_idx])[:10]] = i
            except Exception:
                continue

    print("DEBUG: date_to_row map (sample):", dict(list(date_to_row.items())[:10]))

    # Upsert each incoming row
    for _, rec in df.iterrows():
        rec_dict = rec.to_dict()
        target_date = rec_dict.get("calendarDate")
        row_values = [str(rec_dict.get(col, "")) for col in header]

        print(f"DEBUG: processing incoming date {target_date}")
        if target_date in date_to_row:
            sheet_row = date_to_row[target_date]
            print(f"DEBUG: match found for {target_date} at sheet row {sheet_row} — updating")
            ws.update(f"A{sheet_row}", [row_values])
            print(f"Updated existing health row for {target_date}")
        else:
            print(f"DEBUG: no match for {target_date} — appending")
            ws.append_row(row_values)
            print(f"Inserted new health row for {target_date}")


# -----------------------------
# Cleanup
# -----------------------------
def cleanup_sheet(sh, tab_name, key_column, sort_column):
    print(f"Cleaning up sheet: {tab_name}")

    ws = sh.worksheet(tab_name)
    records = ws.get_all_records()

    if not records:
        print("Nothing to clean")
        return

    df = pd.DataFrame(records)

    df = df.dropna(how="all")
    df = df.drop_duplicates(subset=[key_column], keep="first")
    df = df.sort_values(by=sort_column)

    ws.clear()
    ws.update([df.columns.values.tolist()] + df.values.tolist())

    print(f"Cleanup done for {tab_name}: {len(df)} rows remain")

# -----------------------------
# MAIN
# -----------------------------
def main():
    print("=== INCREMENTAL DAILY SYNC START ===")

    client = garmin_login()
    sh = sheets_client()

    # Sport (incremental append)
    df_activities = fetch_activities(client)
    append_new_rows(sh, "Sport", df_activities, key_column="activityId")
    cleanup_sheet(sh, "Sport", key_column="activityId", sort_column="startTimeLocal")

    # Health (UPSERT) - fetch last 3 days and upsert
    df_health = fetch_health_last_n_days(client, n=3)
    print("DEBUG: fetched health rows:")
    print(df_health)

    if not df_health.empty:
        upsert_health_rows(sh, df_health)
        cleanup_sheet(sh, "Health", key_column="calendarDate", sort_column="calendarDate")
    else:
        print("No health rows fetched for last 3 days")


    print("=== INCREMENTAL DAILY SYNC DONE ===")

if __name__ == "__main__":
    main()
