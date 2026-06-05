#!/usr/bin/env python3
import os
import time
import pandas as pd
from garminconnect import Garmin
import gspread
import logging
from gspread.exceptions import GSpreadException
import re
from google.oauth2.service_account import Credentials

MAX_RETRIES = 5
RETRY_DELAY = 10

# configure basic logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")


def _make_unique_headers(headers):
    """
    Maak een lijst met unieke headernamen van een header-rij.
    Lege namen worden 'blank' (of col_N), duplicaten krijgen suffix _1, _2, ...
    """
    seen = {}
    out = []
    for i, h in enumerate(headers):
        # normaliseer None naar lege string en strip whitespace
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
    """
    Fallback: lees raw values en bouw zelf records met unieke headers.
    Logt originele headers en retourneert lijst van dicts.
    """
    values = ws.get_all_values()
    if not values:
        logging.info("Worksheet empty: get_all_values returned no rows.")
        return []

    headers = values[0]
    rows = values[1:]

    logging.warning("get_all_values returned headers: %s", headers)

    # Als alle headers leeg zijn, maak een generieke set kolomnamen op basis van max kolommen
    if all((h is None or str(h).strip() == "") for h in headers):
        max_cols = max((len(r) for r in rows), default=0)
        headers = [f"col_{i+1}" for i in range(max_cols)]
        logging.warning("Header row entirely empty — using generated headers: %s", headers)

    unique_headers = _make_unique_headers(headers)

    records = []
    for row in rows:
        # vul ontbrekende cellen met lege string zodat zip even lang is
        if len(row) < len(unique_headers):
            row = row + [""] * (len(unique_headers) - len(row))
        # snip extra waarden als row langer is dan headers
        if len(row) > len(unique_headers):
            row = row[:len(unique_headers)]
        records.append(dict(zip(unique_headers, row)))
    return records


# -----------------------------
# Retry helper (exponential backoff)
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
                    logging.warning("Rate limit hit (attempt %d/%d), retrying in %ds... Error: %s",
                                    attempt, MAX_RETRIES, delay, msg)
                    time.sleep(delay)
                    delay = min(delay * 2, 300)
                else:
                    logging.error("Error on attempt %d: %s", attempt, msg)
                    raise
        raise Exception("Max retries reached")
    return wrapper


# -----------------------------
# Garmin login
# -----------------------------
@retry
def garmin_login():
    logging.info("Logging in to Garmin...")
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    logging.info("Garmin login OK")
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
    logging.info("Fetching latest activities...")
    activities = client.get_activities(0, 50)
    df = pd.DataFrame(activities)
    # parse startTimeLocal robustly; if parsing warning occurs it's fine
    if "startTimeLocal" in df.columns:
        df["startTimeLocal"] = pd.to_datetime(df["startTimeLocal"]).dt.date
    # ensure activityId exists and is string
    if "activityId" in df.columns:
        df["activityId"] = df["activityId"].astype(str)
        # verwijder dubbele activityId's (houd de eerste)
        before = len(df)
        df = df.drop_duplicates(subset=["activityId"], keep="first").reset_index(drop=True)
        after = len(df)
        if before != after:
            logging.info("Dropped %d duplicate activities from fetched data", before - after)
    else:
        logging.warning("Fetched activities do not contain 'activityId' column")
    return df



# -----------------------------
# Fetch health for a date (helper) with fallbacks
# -----------------------------
@retry
def fetch_health_for_date(client, date_str):
    """
    date_str: 'yyyy-mm-dd'
    returns: dict (or None) with daily summary for that date
    """
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
                # ensure calendarDate exists and normalized
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


# -----------------------------
# Fetch health for last N days
# -----------------------------
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


# -----------------------------
# Append new rows for Sport (fixed + distance formatting)
# -----------------------------


# helper used by append_new_rows
def format_distance_series(s):
    """
    Format a pandas Series of distances for human-friendly display.
    Heuristics:
      - try numeric conversion
      - if numeric max > 100 assume meters and convert to km
      - return strings with comma as decimal separator (existing behaviour)
    Note: this returns strings for sheet display. If you need numeric km values,
    create a separate numeric column (distance_km) before calling this.
    """
    try:
        numeric = pd.to_numeric(s, errors='coerce')
        # heuristiek meters->km
        if numeric.notna().any() and numeric.max() > 100:
            numeric = numeric / 1000.0
        # format: behoud zoveel decimalen als aanwezig (strip trailing zeros optioneel)
        def fmt_val(x):
            if pd.isna(x):
                return ""
            # convert to string with full precision, then normalize decimal separator
            txt = repr(float(x))
            # remove scientific notation if any
            if 'e' in txt or 'E' in txt:
                txt = f"{float(x):f}"
            # strip trailing zeros and optional trailing dot
            if '.' in txt:
                txt = txt.rstrip('0').rstrip('.')
            return txt.replace('.', ',')
        return numeric.apply(fmt_val)
    except Exception as e:
        logging.warning("Warning formatting distance: %s", e)
        return s.astype(str)



def append_new_rows(sh, tab_name, df, key_column):
    """
    Append new rows from df into sheet tab_name.
    key_column: column name in df used as unique key (e.g., 'activityId')
    """
    # safety: incoming df must contain the key column
    if key_column not in df.columns:
        logging.error("Incoming dataframe missing key column '%s' — aborting append", key_column)
        return

    # ensure incoming key column is string
    df[key_column] = df[key_column].astype(str)

    # verwijder dubbele keys in de incoming dataframe zelf (veiligheidsmaatregel)
    before_in = len(df)
    df = df.drop_duplicates(subset=[key_column], keep="first").reset_index(drop=True)
    after_in = len(df)
    if before_in != after_in:
        logging.info("Dropped %d duplicate rows in incoming dataframe for key %s", before_in - after_in, key_column)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        # create sheet and ensure header contains the key_column
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=max(20, len(df.columns)))
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        if key_column not in header:
            # ensure key_column present as first column
            header.insert(0, key_column)

        # Ensure incoming df has the key_column (create empty column if missing)
        if key_column not in df.columns:
            df[key_column] = ""

        # Reorder df columns to match header (drop any extra cols not in header)
        ordered_cols = [c for c in header if c in df.columns]
        df_to_append = df[ordered_cols].astype(str)

        ws.update([header])
        # format distance if present before initial append
        if 'distance' in df_to_append.columns:
            df_to_append['distance'] = format_distance_series(df_to_append['distance'])
        # append rows (stringified) in the same column order as header
        ws.append_rows(df_to_append.values.tolist())
        logging.info("Created sheet %s with headers: %s", tab_name, header)
        return
    

    # read existing rows robustly
    existing = safe_get_all_records_manual(ws)

    if existing:
        existing_df = pd.DataFrame(existing)
        # Als key_column ontbreekt, probeer een beste match of val terug op lege set
        if key_column not in existing_df.columns:
            logging.warning("Key column '%s' not found in sheet headers: %s", key_column, existing_df.columns.tolist())
            # probeer heuristiek: zoek kolom die 'activity' of 'id' in naam heeft
            candidates = [c for c in existing_df.columns if re.search(r'activity|id', c, re.IGNORECASE)]
            if candidates:
                guessed = candidates[0]
                logging.info("Using guessed key column '%s' for existing rows", guessed)
                existing_keys = set(existing_df[guessed].astype(str))
            else:
                logging.warning("No suitable key column found; treating sheet as empty for dedupe")
                existing_keys = set()
        else:
            existing_keys = set(existing_df[key_column].astype(str))
    else:
        existing_keys = set()

    # determine new rows (those whose key is not in existing_keys)
    new_rows = df[~df[key_column].isin(existing_keys)]

    if new_rows.empty:
        logging.info("No new rows for %s", tab_name)
        return

    # format distance column for new rows if present
    if 'distance' in new_rows.columns:
        new_rows['distance'] = format_distance_series(new_rows['distance'])

    # append new rows
    ws.append_rows(new_rows.astype(str).values.tolist())
    logging.info("Added %d new rows to %s", len(new_rows), tab_name)



# -----------------------------
# Helpers (module level)
# -----------------------------
def find_in_structure(obj, target_keys):
    """
    Recursively search dicts/lists for the first value whose key matches any of target_keys.
    Returns the value or None.
    """
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
    """
    Try multiple strategies to extract a sensible value for sheet column col_name from rec (a dict).
    """
    if rec is None:
        return ""
    # direct exact match
    if col_name in rec:
        return rec[col_name] if rec[col_name] is not None else ""
    # case-insensitive direct match
    lower_map = {str(k).lower(): v for k, v in rec.items()}
    if col_name.lower() in lower_map:
        return lower_map[col_name.lower()] if lower_map[col_name.lower()] is not None else ""

    # uitgebreide alternative names mapping
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

    # substring match: zoek keys die de kolomnaam bevatten
    for k, v in rec.items():
        if col_name.lower() in str(k).lower():
            return v if v is not None else ""

    # recursive search
    found = find_in_structure(rec, {col_name.lower(), key_norm})
    if found is not None:
        return found

    return ""


# -----------------------------
# UPSERT for Health (gspread) with robust date matching (no backup)
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
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        ws = sh.add_worksheet(title=ws_title, rows=2000, cols=max(20, len(header)))
        ws.update([header])
        df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")
        ws.append_rows(df[header].astype(str).values.tolist())
        logging.info("Created Health sheet and inserted %d rows", len(df))
        return

    # Normalize incoming df dates to 'yyyy-mm-dd'
    df["calendarDate"] = pd.to_datetime(df["calendarDate"]).dt.strftime("%Y-%m-%d")

    # Read header and existing rows
    all_values = ws.get_all_values()
    if not all_values:
        header = df.columns.tolist()
        header = _make_unique_headers(header)
        ws.update([header])
        existing_rows = []
    else:
        header = all_values[0]
        existing_rows = all_values[1:]

    logging.debug("sheet header: %s", header)
    logging.debug("first 5 existing rows: %s", existing_rows[:5])

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
            try:
                date_to_row[str(row[date_col_idx])[:10]] = i
            except Exception:
                continue

    logging.debug("date_to_row map (sample): %s", dict(list(date_to_row.items())[:10]))

    # Debug sample of incoming record keys
    if len(df) > 0:
        logging.debug("sample incoming health record keys (first record): %s", df.iloc[0].to_dict())

    # Upsert each incoming row (build row_values using helper, then update/append)
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

        # post-processing: sleepMinutes -> sleepHours conversion (heuristic)
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

        # post-processing: weight parsing
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

        logging.debug("processing incoming date %s", target_date)
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
# Cleanup (robust header handling)
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

    # direct na sh = sheets_client()
    try:
        ws_sport = sh.worksheet("Sport")
        header_vals = ws_sport.get_all_values()
        if header_vals and header_vals[0] and header_vals[0][0].startswith("col_"):
            # sheet heeft gegenereerde headers; zet correcte header uit df_activities (indien beschikbaar)
            if 'df_activities' in locals() and not df_activities.empty:
                new_header = _make_unique_headers(df_activities.columns.tolist())
                ws_sport.update([new_header])
                logging.info("Replaced generic Sport header with: %s", new_header)
            else:
                logging.warning("Sport sheet has generic header but df_activities not available to set header.")
    except Exception as e:
        logging.debug("Header-fix check skipped or failed: %s", e)

    
    # Sport (incremental append)
    # Run a cleanup first to normalize headers and dedupe existing rows,
    # so append_new_rows can rely on a sane header row.
    try:
        cleanup_sheet(sh, "Sport", key_column="activityId", sort_column="startTimeLocal")
    except Exception as e:
        logging.warning("cleanup before append failed: %s", e)

    df_activities = fetch_activities(client)
    append_new_rows(sh, "Sport", df_activities, key_column="activityId")
    # run cleanup again to normalize after append
    try:
        cleanup_sheet(sh, "Sport", key_column="activityId", sort_column="startTimeLocal")
    except Exception as e:
        logging.warning("cleanup after append failed: %s", e)

    # Health (UPSERT) - fetch last 7 days and upsert
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
