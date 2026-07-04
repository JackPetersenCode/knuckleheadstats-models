"""Probe the MLB Stats API directly (no dependency on statsapi wrapper).

Endpoints we care about:
  - /api/v1.1/game/{game_pk}/feed/live
    -> gameData.weather (condition, temp, wind)
    -> gameData.officials (plate umpire)
    -> liveData.boxscore.teams.{home,away}.battingOrder (player IDs in lineup)
    -> liveData.boxscore.teams.{home,away}.players[ID].seasonStats / batSide
"""
import json
import requests

BASE = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"

# pick a known 2024 game from our DB (Dodgers at Padres opener)
GAME_PK = 745444

r = requests.get(BASE.format(GAME_PK), timeout=30)
r.raise_for_status()
data = r.json()

print("=== gameData.weather ===")
print(json.dumps(data["gameData"].get("weather", {}), indent=2))

print("\n=== gameData.gameInfo (firstPitch, attendance, time) ===")
print(json.dumps(data["gameData"].get("gameInfo", {}), indent=2))

print("\n=== officials (umpires) ===")
officials = data["liveData"]["boxscore"].get("officials", [])
for o in officials:
    print(f"  {o.get('officialType')}: {o['official'].get('fullName')} (id {o['official'].get('id')})")

print("\n=== home batting order (player ids) ===")
home = data["liveData"]["boxscore"]["teams"]["home"]
print("  battingOrder:", home.get("battingOrder"))
# Look up first batter to see what fields are available
players = home.get("players", {})
if home.get("battingOrder"):
    pid = home["battingOrder"][0]
    key = f"ID{pid}"
    p = players.get(key, {})
    print(f"\n  first batter ({p.get('person', {}).get('fullName')}):")
    print(f"    batSide: {p.get('batSide')}")
    print(f"    seasonStats batting (keys): {list(p.get('seasonStats', {}).get('batting', {}).keys())[:15]}")
    bs = p.get("seasonStats", {}).get("batting", {})
    print(f"    avg/obp/slg/ops: {bs.get('avg')} / {bs.get('obp')} / {bs.get('slg')} / {bs.get('ops')}")

# Probable pitchers
print("\n=== probable pitchers ===")
gd = data["gameData"]
print(f"  home: {gd.get('probablePitchers',{}).get('home',{}).get('fullName')}")
print(f"  away: {gd.get('probablePitchers',{}).get('away',{}).get('fullName')}")
