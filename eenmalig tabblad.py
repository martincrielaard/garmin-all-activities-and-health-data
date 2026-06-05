import os, json, gspread
from google.oauth2.service_account import Credentials

gc = gspread.authorize(Credentials.from_service_account_info(
    json.loads(os.environ['GOOGLE_CREDENTIALS']),
    scopes=['https://www.googleapis.com/auth/spreadsheets']
))
sh = gc.open_by_key(os.environ['SHEET_ID'])

header = ["startTimeLocal","activityName","activityType_key","distance_km","duration_hms",
          "calories","averageHR","maxHR","value_col_i","elevationGain",
          "activityId","averageSpeed","pace_run","speed_bike","pace_swim"]

# maak nieuw blad aan als het nog niet bestaat
title = "SportTemplate"
try:
    ws = sh.worksheet(title)
    ws.clear()
    ws.update([header])
    print("Header updated in existing sheet", title)
except Exception:
    sh.add_worksheet(title=title, rows=2000, cols=len(header))
    ws = sh.worksheet(title)
    ws.update([header])
    print("Created sheet", title)
