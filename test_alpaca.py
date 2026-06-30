# test_alpaca.py
import requests

KEY    = "PK372JXIHFTRGTBTYGHMYJNMZS"
SECRET = "2QgdZAChPk9e2f71CnfwVzhW4JcvszHKNWiVexPqeAw2"

headers = {
    "APCA-API-KEY-ID":     KEY,
    "APCA-API-SECRET-KEY": SECRET,
}

# test 1: account access
r1 = requests.get(
    "https://paper-api.alpaca.markets/v2/account",
    headers=headers
)
print(f"Account: {r1.status_code}")
print(r1.text[:300])

# test 2: data access
r2 = requests.get(
    "https://data.alpaca.markets/v2/stocks/SPY/bars"
    "?timeframe=1Day&start=2026-06-01&limit=5",
    headers=headers
)
print(f"\nData: {r2.status_code}")
print(r2.text[:300])