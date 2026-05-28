import os
import json
import time
import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

def get_garmin_client():
    client = Garmin(os.environ.get('GARMIN_EMAIL'), os.environ.get('GARMIN_PASSWORD'))
    client.login()
    return client

def seconds_to_hms(seconds):
    if not seconds: return "00:00:00"
    return str(timedelta(seconds=int(float(seconds))))

def format_run_pace(ms):
    if not ms or ms <= 0: return ""
    seconds_per_km = 1000 / ms
    return f"{int(seconds_per_km // 60):02d}:{int(seconds_per_km % 60):02d}"

def format_swim_pace(ms):
    if not ms or ms <= 0: return ""
    seconds_100m = 100 / ms
    return f"{int(seconds_100m // 60):02d}:{int(seconds_100m % 60):02d}"

def sync_activities():
    # VOLL-SYNC: Wir gehen zurück bis 2010
    stop_date = datetime(2010, 1, 1)
    
    print(f"🚀 Starte VOLL-SYNC Sport (Historie ab {stop_date.strftime('%Y')})...")
    try:
        client = get_garmin_client()
    except Exception as e:
        print(f"❌ Login fehlgeschlagen: {e}")
        return

    gc = gspread.authorize(Credentials.from_service_account_info(
        json.loads(os.environ.get('GOOGLE_CREDENTIALS')), 
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    ))
    sh = gc.open_by_key(os.environ.get('SHEET_ID'))
    sport_sheet = sh.worksheet("Sport")

    # IDs laden, um nichts doppelt zu schreiben
    all_rows = sport_sheet.get_all_values()
    existing_ids = [row[10] for row in all_rows if len(row) > 10]
    
    batch_size = 50  # Größere Batches für schnelleren Voll-Sync
    start_index = 0
    should_continue = True
    
    while should_continue:
        print(f"📦 Lade Batch ab Index {start_index}...")
        activities = client.get_activities(start_index, batch_size)
        
        if not activities: 
            print("🏁 Ende der Garmin-Historie erreicht.")
            break
        
        new_rows = []
        for act in activities:
            start_time_str = act.get('startTimeLocal', '')
            if not start_time_str: continue
            
            act_date = datetime.strptime(start_time_str[:10], '%Y-%m-%d')
            
            # Prüfen, ob wir das Zieljahr erreicht haben
            if act_date < stop_date:
                print(f"🛑 Ziel-Datum {stop_date.strftime('%Y')} erreicht. Synchronisation beendet.")
                should_continue = False
                break

            act_id = str(act.get('activityId'))
            if act_id in existing_ids:
                continue

            type_key = act.get('activityType', {}).get('typeKey', '').lower()
            avg_speed_ms = round(act.get('averageSpeed', 0), 3)
            
            # Release 2: Multisport-Cadence & SWOLF für Spalte I
            swolf = act.get('averageSwolf')
            cadence = (
                act.get('averageRunningCadenceInStepsPerMinute') or 
                act.get('averageCadence') or 
                act.get('averageBikingCadenceInRevPerMinute') or 
                0
            )
            
            if "swimming" in type_key and swolf:
                value_col_i = round(swolf, 0)
            else:
                value_col_i = round(cadence, 0) if cadence else 0

            pace_run = ""
            speed_bike = ""
            pace_swim = ""

            if any(x in type_key for x in ["cycling", "biking", "virtual_ride"]):
                speed_bike = round(avg_speed_ms * 3.6, 2)
            elif "running" in type_key:
                pace_run = format_run_pace(avg_speed_ms)
            elif "swimming" in type_key:
                pace_swim = format_swim_pace(avg_speed_ms)

            new_rows.append([
                start_time_str[:10],                # A: Datum (Release 2)
                act.get('activityName', '-'),       # B: Name
                type_key,                           # C: Typ
                round(act.get('distance', 0) / 1000, 2), # D: Distanz km
                seconds_to_hms(act.get('duration', 0)),  # E: Zeit
                act.get('calories', 0),             # F: Kalorien
                act.get('averageHR', 0),            # G: Ø Puls
                act.get('maxHR', 0),                # H: Max Puls
                value_col_i,                        # I: Cadence/SWOLF (Release 2)
                round(act.get('elevationGain', 0), 0), # J: HM
                act_id,                             # K: ID
                avg_speed_ms,                       # L: m/s
                pace_run,                           # M: Pace Run
                speed_bike,                         # N: km/h Bike
                pace_swim                           # O: Pace Swim
            ])
            existing_ids.append(act_id)

        if new_rows:
            new_rows.reverse() 
            # Release 2: USER_ENTERED für Google Sheets Erkennung
            sport_sheet.append_rows(new_rows, value_input_option='USER_ENTERED')
            print(f"  ✅ {len(new_rows)} Aktivitäten hinzugefügt.")
        
        if should_continue:
            time.sleep(1.5) # Kurz warten, um API-Limits zu respektieren
            start_index += batch_size

    print("🎉 Vollständiger Historien-Sync abgeschlossen!")

if __name__ == "__main__":
    sync_activities()
