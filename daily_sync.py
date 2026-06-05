#!/usr/bin/env python3
import os
import time
import pandas as pd
from garminconnect import Garmin
import gspread
import logging
import re
from google.oauth2.service_account import Credentials

# Use centralized cleaning
from sync_activities import get_clean_activities, seconds_to_hms, format_run_pace, format_swim_pace

MAX_RETRIES = 5
RETRY_DELAY = 10

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

# -----------------------------
# Small helpers
# -----------------------------
def _make_unique_headers(headers):
    seen = {}
    out = []
    for i, h in enumerate(headers):
        key = (h or "").strip()
        base = key if key != "" else f"col_{i+1}"
        if base in seen:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            out.append(base)
    return out

def safe_get_all_records_manual(ws):
    values = ws.get_all_values()
    if not values:
        logging.info("Worksheet empty: get_all_values returned no rows.")
        return []
    headers = values[0]
    rows = values[1:]
    logging.warning("get_all_values returned headers: %s", headers)
    if all((h is None or str(h).strip() == "") for h in headers):
        max_cols = max((len(r) for r in rows), default=0)
        headers = [f"col_{i+1}" for i in range(max_cols)]
        logging.warning("Header row entirely empty — using generated headers: %s", headers)
    unique_headers = _make_unique_headers(headers)
    records = []
    for row in rows:
        if len(row) < len(unique_headers):
            row = row + [""] * (len(unique_headers) - len(row))
        if len(row) > len(unique_headers):
            row = row[:len(unique_headers)]
        records.append(dict(zip(unique_headers, row)))
    return records

def retry(func):
    def wrapper(*args, **kwargs):
        delay = RETRY_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "rate limit" in msg.lower():
                    logging.warning("Rate limit hit (attempt %d/%d), retrying in %ds... Error: %s",
                                    attempt, MAX_RETRIES, delay, msg)
                    time.sleep(delay)
                    delay = min(delay * 2, 300)
                else:
                    logging.error("Error on attempt %d: %s", attempt, msg)
                    raise
        raise Exception("Max retries reached")
    return wrapper

@retry
def garmin_login():
    logging.info("Logging in to Garmin...")
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    logging.info("Garmin login OK")
    return client

def sheets_client():
    creds = Credentials.from_service_account_info(
        eval(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    return sh

# -----------------------------
# Distance formatting helper
# -----------------------------
def format_distance_series(s):
    try:
        numeric = pd.to_numeric(s, errors='coerce')
        if numeric.notna().any() and numeric.max() > 100:
            numeric = numeric / 1000.0
        def fmt_val(x):
            if pd.isna(x):
                return ""
            txt = repr(float(x))
            if 'e' in txt or 'E' in txt:
                txt = f"{float(x):f}"
            if '.' in txt:
                txt = txt.rstrip('0').rstrip('.')
            return txt.replace('.', ',')
        return numeric.apply(fmt_val)
    except Exception as e:
        logging.warning("Warning formatting distance: %s", e)
        return s.astype(str)

# -----------------------------
# Append new rows (idempotent: update existing rows, append new)
# -----------------------------
def append_new_rows(sh, tab_name, df, key_column):
    if df is None or df.empty:
        logging.info("No incoming rows to append for %s", tab_name)
        return

    if key_column not in df.columns:
        logging.error("Incoming dataframe missing key column '%s' — aborting append", key_column)
        return

    df = df.copy()
    df[key_column] = df[key_column].astype(str)
    before_in = len(df)
    df = df.drop_duplicates(subset=[key_column], keep="first").reset_index(drop=True)
    after_in = len(df)
    if before_in != after_in:
        logging.info("Dropped %d duplicate rows in incoming dataframe for key %s", before_in - after_in, key_column)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=max(20, len(df.columns)))
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        if key_column not in header:
            header.insert(0, key_column)
        if key_column not in df.columns:
            df[key_column] = ""
        ordered_cols = [c for c in header if c in df.columns]
        df_to_append = df[ordered_cols].astype(str)
        ws.update([header])
        if 'distance' in df_to_append.columns:
            df_to_append['distance'] = format_distance_series(df_to_append['distance'])
        ws.append_rows(df_to_append.values.tolist())
        logging.info("Created sheet %s with headers: %s", tab_name, header)
        return

    # Build existing_map: key -> sheet_row_number
    all_values = ws.get_all_values()
    existing_map = {}
    if all_values:
        header = all_values[0]
        key_idx = None
        for i, h in enumerate(header):
            if h and (h.strip().lower() == key_column.lower() or key_column.lower() in h.strip().lower() or "activity" in h.strip().lower() or "id" in h.strip().lower()):
                key_idx = i
                break
        if key_idx is None and len(header) > 10:
            key_idx = 10
        if key_idx is not None:
            for rownum, row in enumerate(all_values[1:], start=2):
                if len(row) > key_idx and row[key_idx] not in ("", None):
                    existing_map[str(row[key_idx])] = rownum

    # Prepare updates and appends
    updates = []      # (rownum, values)
    to_append = []    # values
    sheet_header = all_values[0] if all_values else None

    def _serialize_cell(v):
        # convert pandas / numpy / datetime types to JSON-safe strings
        try:
            if v is None:
                return ""
            # pandas Timestamp
            if isinstance(v, pd.Timestamp):
                return v.strftime("%Y-%m-%d %H:%M:%S")
            # datetime
            if isinstance(v, (datetime,)):
                return v.strftime("%Y-%m-%d %H:%M:%S")
            # numpy types or pandas NA
            if pd.isna(v):
                return ""
            # bool/int/float -> keep as-is (gspread will accept numeric types), but convert numpy scalars
            if isinstance(v, (int, float, bool)):
                return v
            # otherwise string
            return str(v)
        except Exception:
            return str(v)

    for _, r in df.iterrows():
        # Build values aligned to sheet header if possible
        if sheet_header:
            values = []
            for col in sheet_header:
                match = None
                for c in df.columns:
                    if c and c.strip().lower() == str(col).strip().lower():
                        match = c
                        break
                if match:
                    values.append(_serialize_cell(r.get(match, "")))
                else:
                    values.append("")
        else:
            values = [_serialize_cell(r.get(c, "")) for c in df.columns]

        key = str(r[key_column])
        if key in existing_map:
            updates.append((existing_map[key], values))
        else:
            to_append.append(values)

    # Execute updates (one-by-one)
    for rownum, values in updates:
        try:
            ws.update(f"A{rownum}", [values])
        except Exception as e:
            logging.warning("Failed to update row %d: %s", rownum, e)

    # Batch append remaining rows (ensure all values are JSON-safe)
    if to_append:
        # keep chronological order (oldest first)
        to_append.reverse()
        # ensure inner lists contain only primitives/strings
        safe_to_append = []
        for row in to_append:
            safe_row = []
            for cell in row:
                # leave numeric types as-is, convert others to string
                if isinstance(cell, (int, float, bool)):
                    safe_row.append(cell)
                else:
                    safe_row.append("" if cell is None else str(cell))
            safe_to_append.append(safe_row)
        ws.append_rows(safe_to_append, value_input_option='USER_ENTERED')
        logging.info("Added %d new rows to %s", len(safe_to_append), tab_name)
    else:
        logging.info("No new rows for %s", tab_name)


# -----------------------------
# Health helpers (copied from previous implementation)
# -----------------------------
@retry
def fetch_health_for_date(client, date_str):
    possible_methods = [
        "get_daily_summary",
        "get_daily_summary_by_date",
        "get_stats",
        "get_daily_stats",
        "get_user_summary",
        "get_daily_health_summary"
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
                last_exc = e
                if "429" in str(e) or "rate limit" in str(e).lower():
                    raise
                continue
    if last_exc:
        raise last_exc
    return None

def fetch_health_last_n_days(client, n=7):
    rows = []
    today = pd.Timestamp.now().normalize()
    for i in range(n):
        d = (today - pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rec = fetch_health_for_date(client, d)
        except Exception as e:
            logging.warning("Could not fetch health for %s: %s", d, e)
            rec = None
        if rec:
            rows.append(rec)
        else:
            logging.info("No health summary for %s", d)
    if not rows:
        return pd.DataFrame(columns=['calendarDate'])
    df = pd.DataFrame(rows)
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
    return df

def find_in_structure(obj, target_keys):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_norm = str(k).strip().lower()
            if k_norm in target_keys:
                return v
        for v in obj.values():
            found = find_in_structure(v, target_keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_in_structure(item, target_keys)
            if found is not None:
                return found
    return None

def get_value_for_column(rec, col_name):
    if rec is None:
        return ""
    if col_name in rec:
        return rec[col_name] if rec[col_name] is not None else ""
    lower_map = {str(k).lower(): v for k, v in rec.items()}
    if col_name.lower() in lower_map:
        return lower_map[col_name.lower()] if lower_map[col_name.lower()] is not None else ""
    alt_names = {
        "steps": ["steps", "totalSteps", "stepCount", "dailySteps", "summarySteps", "summary_steps"],
        "weight": ["weight", "bodyWeight", "weightKg", "weight_kg", "weightInKg", "body_weight", "userWeight"],
        "sleephours": ["sleepHours", "sleepDuration", "sleepMinutes", "totalSleepMinutes", "sleep", "sleepSummary", "sleep_total_minutes"],
        "restingheartrate": ["restingHeartRate", "restingHR", "resting_heart_rate", "resting_hr", "restingHeartRateBpm"],
        "distance": ["distance", "totalDistance", "totalDistanceMeters", "distanceMeters", "distance_meters", "total_distance"]
    }
    key_norm = ''.join(ch for ch in col_name.lower() if ch.isalnum())
    if key_norm in alt_names:
        targets = [n.lower() for n in alt_names[key_norm]]
        found = find_in_structure(rec, set(targets))
        if found is not None:
            return found
    for k, v in rec.items():
        if col_name.lower() in str(k).lower():
            return v if v is not None else ""
    found = find_in_structure(rec, {col_name.lower(), key_norm})
    if found is not None:
        return found
    return ""

def upsert_health_rows(sh, df):
    ws_title = "Health"
    try:
        ws = sh.worksheet(ws_title)
    except gspread.exceptions.WorksheetNotFound:
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        ws = sh.add_worksheet(title=ws_title, rows=2000, cols=max(20, len(header)))
        ws.update([header])
        df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
        ws.append_rows(df[header].astype(str).values.tolist())
        logging.info("Created Health sheet and inserted %d rows", len(df))
        return

    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
    all_values = ws.get_all_values()
    if not all_values:
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        ws.update([header])
        existing_rows = []
    else:
        header = all_values[0]
        existing_rows = all_values[1:]

    try:
        date_col_idx = header.index("calendarDate")
    except ValueError:
        raise Exception("Kolom 'calendarDate' niet gevonden in Health-sheet header")

    date_to_row = {}
    for i, row in enumerate(existing_rows, start=2):
        try:
            norm = pd.to_datetime(row[date_col_idx]).strftime('%Y-%m-%d')
            date_to_row[norm] = i
        except Exception:
            try:
                date_to_row[str(row[date_col_idx])[:10]] = i
            except Exception:
                continue

    for _, rec in df.iterrows():
        rec_dict = rec.to_dict()
        target_date = rec_dict.get("calendarDate")
        row_values = []
        for col in header:
            val = get_value_for_column(rec_dict, col)
            if isinstance(val, (dict, list)):
                inner = find_in_structure(val, {col.lower(), ''.join(ch for ch in col.lower() if ch.isalnum())})
                if inner is not None:
                    val = inner
                else:
                    try:
                        val = str(val)
                    except:
                        val = ""
            if pd.isna(val):
                val = ""
            row_values.append(str(val))

        try:
            sleep_idx = header.index("sleepHours")
        except ValueError:
            sleep_idx = None

        if sleep_idx is not None:
            raw = row_values[sleep_idx]
            try:
                v = float(raw)
                if v > 10:
                    v = round(v / 60.0, 2)
                row_values[sleep_idx] = str(v)
            except Exception:
                found = find_in_structure(rec_dict, {"sleepminutes", "sleepduration", "totalsleepminutes", "sleep"})
                if found is not None:
                    try:
                        minutes = float(found)
                        hours = round(minutes / 60.0, 2)
                        row_values[sleep_idx] = str(hours)
                    except:
                        pass

        try:
            weight_idx = header.index("weight")
        except ValueError:
            weight_idx = None

        if weight_idx is not None:
            raw = row_values[weight_idx]
            try:
                w = float(raw)
                row_values[weight_idx] = str(w)
            except:
                found = find_in_structure(rec_dict, {"weight", "bodyweight", "weightkg", "weight_kg", "userweight"})
                if found is not None:
                    if isinstance(found, dict):
                        for k in ("value", "weight", "kg"):
                            if k in found:
                                try:
                                    row_values[weight_idx] = str(float(found[k]))
                                    break
                                except:
                                    continue
                    else:
                        s = str(found)
                        m = re.search(r"[\d\.]+", s)
                        if m:
                            row_values[weight_idx] = m.group(0)

        if target_date in date_to_row:
            sheet_row = date_to_row[target_date]
            logging.info("match found for %s at sheet row %d — updating", target_date, sheet_row)
            range_name = f"A{sheet_row}"
            ws.update(range_name, [row_values])
            logging.info("Updated existing health row for %s", target_date)
        else:
            logging.info("no match for %s — appending", target_date)
            ws.append_row(row_values)
            logging.info("Inserted new health row for %s", target_date)

# -----------------------------
# Cleanup sheet
# -----------------------------
def cleanup_sheet(sh, tab_name, key_column, sort_column):
    logging.info("Cleaning up sheet: %s", tab_name)
    ws = sh.worksheet(tab_name)
    all_values = ws.get_all_values()
    if not all_values or len(all_values) == 0:
        logging.info("Nothing to clean")
        return
    header = all_values[0]
    rows = all_values[1:]
    normalized_header = []
    seen = {}
    for i, h in enumerate(header):
        name = str(h).strip()
        if name == "":
            name = f"col_{i+1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        normalized_header.append(name)
    max_cols = len(normalized_header)
    normalized_rows = []
    for r in rows:
        row = list(r) + [""] * (max_cols - len(r))
        normalized_rows.append(row[:max_cols])
    df = pd.DataFrame(normalized_rows, columns=normalized_header)
    df = df.dropna(how="all")
    if key_column not in df.columns:
        logging.warning("key_column '%s' not found in normalized header; skipping dedupe", key_column)
    else:
        df = df.drop_duplicates(subset=[key_column], keep="first")
    if sort_column in df.columns:
        try:
            df = df.sort_values(by=sort_column)
        except Exception:
            df = df.sort_values(by=sort_column, key=lambda s: s.astype(str))
    else:
        logging.warning("sort_column '%s' not found; skipping sort", sort_column)
    ws.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    ws.update(values)
    logging.info("Cleanup done for %s: %d rows remain", tab_name, len(df))

# -----------------------------
# MAIN
# -----------------------------
def main():
    logging.info("=== INCREMENTAL DAILY SYNC START ===")
    client = garmin_login()
    sh = sheets_client()

    # Try to fix generic headers if present by deriving header from a small preview
    try:
        ws_sport = sh.worksheet("Sport")
        header_vals = ws_sport.get_all_values()
        if header_vals and header_vals[0] and header_vals[0][0].startswith("col_"):
            df_preview = get_clean_activities(client, lookback_days=7, start=0, limit=10)
            if not df_preview.empty:
                new_header = _make_unique_headers(df_preview.columns.tolist())
                ws_sport.update([new_header])
                logging.info("Replaced generic Sport header with: %s", new_header)
    except Exception as e:
        logging.debug("Header-fix check skipped or failed: %s", e)

    # Normalize and dedupe existing sheet first
    try:
        cleanup_sheet(sh, "Sport", key_column="activityId", sort_column="startTimeLocal")
    except Exception as e:
        logging.warning("cleanup before append failed: %s", e)

    # Use centralized cleaning/dedupe function
    try:
        df_activities = get_clean_activities(client, lookback_days=7, start=0, limit=50)
    except Exception as e:
        logging.error("Failed to fetch/clean activities: %s", e)
        df_activities = pd.DataFrame()

    append_new_rows(sh, "Sport", df_activities, key_column="activityId")

    try:
        cleanup_sheet(sh, "Sport", key_column="activityId", sort_column="startTimeLocal")
    except Exception as e:
        logging.warning("cleanup after append failed: %s", e)

    # Health (UPSERT)
    df_health = fetch_health_last_n_days(client, n=7)
    logging.info("DEBUG: fetched health rows: %s", df_health)
    if not df_health.empty:
        upsert_health_rows(sh, df_health)
        try:
            cleanup_sheet(sh, "Health", key_column="calendarDate", sort_column="calendarDate")
        except Exception as e:
            logging.warning("cleanup Health failed: %s", e)
    else:
        logging.info("No health rows fetched for last 7 days")

    logging.info("=== INCREMENTAL DAILY SYNC DONE ===")

if __name__ == "__main__":
    main()
