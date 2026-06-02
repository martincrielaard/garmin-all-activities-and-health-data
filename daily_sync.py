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
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "429" in str(e):
                    print(f"Rate limit hit, retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                else:
                    raise e
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
    try:
        health = client.get_daily_summary(date_str)
        if not health:
            return None
        # ensure calendarDate exists and normalized
        health['calendarDate'] = pd.to_datetime(health.get('calendarDate', date_str)).date()
        return health
    except Exception as e:
        print(f"Warning: could not fetch health for {date_str}: {e}")
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

# -----------------------------
# UPSERT for Health (gspread)
# -----------------------------
def upsert_health_rows(sh, df):
    """
    Upsert multiple rows from df into sheet 'Health'.
    df must contain a 'calendarDate' column (Date or 'yyyy-mm-dd' string).
    """
    ws_title = "Health"
    try:
        ws = sh.worksheet(ws_title)
    except gspread.exceptions.WorksheetNotFound:
        # create sheet with header from df
        header = df.columns.tolist()
        ws = sh.add_worksheet(title=ws_title, rows=2000, cols=max(20, len(header)))
        ws.update([header])
        # append all rows
        df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
        ws.append_rows(df[header].astype(str).values.tolist())
        print(f"Created Health sheet and inserted {len(df)} rows")
        return

    # Backup current sheet values (simple backup as new worksheet)
    all_values = ws.get_all_values()
    backup_name = f"Health_backup_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        # create backup sheet and write values
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
        # no header present, create header from df
        header = df.columns.tolist()
        ws.update([header])
        existing_rows = []
    else:
        header = all_values[0]
        existing_rows = all_values[1:]

    # find index of calendarDate column in sheet header
    try:
        date_col_idx = header.index("calendarDate")
    except ValueError:
        raise Exception("Kolom 'calendarDate' niet gevonden in Health-sheet header")

    # build map date -> sheet_row_number (1-based)
    date_to_row = {}
    for i, row in enumerate(existing_rows, start=2):  # sheet rows start at 1, header is row 1
        if len(row) > date_col_idx:
            cell_val = row[date_col_idx][:10]  # first 10 chars yyyy-mm-dd
            date_to_row[cell_val] = i

    # Upsert each incoming row
    # Ensure we write values in the same column order as the sheet header
    for _, rec in df.iterrows():
        rec_dict = rec.to_dict()
        target_date = rec_dict.get("calendarDate")
        # build row values in sheet order
        row_values = [str(rec_dict.get(col, "")) for col in header]

        if target_date in date_to_row:
            sheet_row = date_to_row[target_date]
            ws.update(f"A{sheet_row}", [row_values])
            print(f"Updated existing health row for {target_date}")
        else:
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
    if not df_health.empty:
        upsert_health_rows(sh, df_health)
        cleanup_sheet(sh, "Health", key_column="calendarDate", sort_column="calendarDate")
    else:
        print("No health rows fetched for last 3 days")


    print("=== INCREMENTAL DAILY SYNC DONE ===")

if __name__ == "__main__":
    main()
