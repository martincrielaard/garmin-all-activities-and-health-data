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
# Fetch health
# -----------------------------
@retry
def fetch_health(client):
    print("Fetching health data...")
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    health = client.get_daily_summary(today)
    df = pd.DataFrame([health])
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.date
    df["calendarDate"] = df["calendarDate"].astype(str)
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
# UPSERT for Health
# -----------------------------
def upsert_health_row(sh, df):
    ws = sh.worksheet("Health")

    # Zorg dat calendarDate een string YYYY-MM-DD is
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
    new_row = df.iloc[0].to_dict()
    target_date = new_row["calendarDate"]

    # Haal alle waarden op, inclusief header
    all_values = ws.get_all_values()
    if not all_values:
        # Sheet is leeg → header + eerste rij schrijven
        header = list(new_row.keys())
        ws.update("A1", [header, list(new_row.values())])
        print(f"Health sheet was empty, created header and inserted {target_date}")
        return

    header = all_values[0]
    rows = all_values[1:]

    # Zoek index van calendarDate-kolom
    try:
        date_col_idx = header.index("calendarDate")
    except ValueError:
        raise Exception("Kolom 'calendarDate' niet gevonden in Health-sheet header")

    # Bouw rij in dezelfde kolomvolgorde als de sheet-header
    row_values_in_sheet_order = []
    for col_name in header:
        row_values_in_sheet_order.append(str(new_row.get(col_name, "")))

    # Zoek bestaande rij met dezelfde datum (eerste 10 tekens vergelijken)
    row_to_update = None
    for i, row in enumerate(rows, start=2):  # start=2 vanwege header
        if len(row) > date_col_idx:
            cell_value = str(row[date_col_idx])[:10]
            if cell_value == target_date:
                row_to_update = i
                break

    if row_to_update:
        # Update bestaande rij
        ws.update(f"A{row_to_update}", [row_values_in_sheet_order])
        print(f"Updated existing health row for {target_date}")
    else:
        # Append nieuwe rij
        ws.append_row(row_values_in_sheet_order)
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

    # Health (UPSERT)
    df_health = fetch_health(client)
    upsert_health_row(sh, df_health)
    cleanup_sheet(sh, "Health", key_column="calendarDate", sort_column="calendarDate")

    print("=== INCREMENTAL DAILY SYNC DONE ===")

if __name__ == "__main__":
    main()
