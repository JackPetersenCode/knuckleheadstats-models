"""Smoke-test the whole pipeline before launching. Verifies each piece works
without needing real money, a paid API key, or external Discord posts.

Usage:
  python smoke_test.py
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PASS = "[ OK ]"
FAIL = "[FAIL]"
SKIP = "[skip]"


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def check(condition, label, error=None):
    if condition:
        print(f"  {PASS}  {label}")
    else:
        print(f"  {FAIL}  {label}")
        if error:
            print(f"        {error}")
    return condition


def main():
    fails = 0

    section("1. Required files exist")
    files = [
        "v5_seedavg_model.pkl",
        "daily_picker.py",
        "save_seedavg_model.py",
        "fetch_today_odds.py",
        "picks_service/aggregate_picks.py",
        "picks_service/discord_post.py",
        "picks_service/ig_image.py",
        "picks_service/settler.py",
        "picks_service/record.py",
        "line_shopper/poll.py",
        "line_shopper/detect.py",
        "line_shopper/alert.py",
        "landing_page/index.html",
        "landing_page/style.css",
        "landing_page/update_record.py",
    ]
    for f in files:
        ok = (ROOT / f).exists()
        if not check(ok, f, error="missing"): fails += 1

    section("2. Python imports")
    for mod in ("psycopg2", "pandas", "numpy", "sklearn", "xgboost", "lightgbm",
                "requests", "PIL"):
        try:
            __import__(mod)
            print(f"  {PASS}  import {mod}")
        except ImportError as e:
            print(f"  {FAIL}  import {mod}")
            print(f"        run: pip install {mod}")
            fails += 1

    section("3. Postgres reachable")
    try:
        import psycopg2
        pg = psycopg2.connect(host="localhost", user="postgres",
                              dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
        with pg.cursor() as c:
            c.execute("SELECT COUNT(*) FROM mlb_features_v5")
            n = c.fetchone()[0]
        print(f"  {PASS}  Postgres connected, mlb_features_v5 has {n:,} rows")
        pg.close()
    except Exception as e:
        check(False, "Postgres", error=str(e)[:120])
        fails += 1

    section("4. v5 model loads")
    try:
        import pickle
        with open(ROOT / "v5_seedavg_model.pkl", "rb") as f:
            m = pickle.load(f)
        assert "features" in m and "seed_models" in m
        print(f"  {PASS}  v5 model loaded, {len(m['features'])} features, "
              f"{len(m['seed_models'])} seeds, trained through {m['trained_through']}")
    except Exception as e:
        check(False, "v5 model load", error=str(e)[:120])
        fails += 1

    section("5. MLB API reachable (no key needed)")
    try:
        import requests
        r = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2025-08-15",
            timeout=15)
        r.raise_for_status()
        n = sum(len(d.get("games", [])) for d in r.json().get("dates", []))
        print(f"  {PASS}  MLB API responded, {n} games on test date")
    except Exception as e:
        check(False, "MLB API", error=str(e)[:120])
        fails += 1

    section("6. Odds API key (optional)")
    if os.environ.get("ODDS_API_KEY"):
        print(f"  {PASS}  ODDS_API_KEY set")
    else:
        print(f"  {SKIP}  ODDS_API_KEY not set (line shopper won't run)")
        print(f"          Get one free at https://the-odds-api.com if you want it")

    section("7. Discord webhooks (optional)")
    for var in ("DISCORD_FREE_WEBHOOK", "DISCORD_VIP_WEBHOOK",
                "LINE_SHOPPER_DISCORD_FREE", "LINE_SHOPPER_DISCORD_PRO"):
        if os.environ.get(var):
            print(f"  {PASS}  {var} set")
        else:
            print(f"  {SKIP}  {var} not set")

    section("8. Ensure picks_published table exists (first-run setup)")
    try:
        # Re-use aggregate_picks.ensure_table without running the rest
        sys.path.insert(0, str(ROOT / "picks_service"))
        from aggregate_picks import ensure_table
        import psycopg2
        pg = psycopg2.connect(host="localhost", user="postgres",
                              dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
        ensure_table(pg)
        pg.close()
        print(f"  {PASS}  picks_published table verified/created")
    except Exception as e:
        check(False, "ensure_table", error=str(e)[:120])
        fails += 1

    section("9. Test run: ig_image.py on a past date")
    try:
        candidates = [ROOT / "ig_post_2025-08-15.png",
                      ROOT / "picks_service" / "ig_post_2025-08-15.png"]
        for c in candidates:
            if c.exists(): c.unlink()
        result = subprocess.run(
            [sys.executable, str(ROOT / "picks_service" / "ig_image.py"),
             "2025-08-15"],
            cwd=str(ROOT),
            capture_output=True, text=True, timeout=60)
        out_file = next((c for c in candidates if c.exists()), None)
        if result.returncode == 0 and out_file is not None:
            sz = out_file.stat().st_size
            print(f"  {PASS}  ig_image.py produced {out_file.name} ({sz:,} bytes)")
        else:
            print(f"  {FAIL}  ig_image.py: rc={result.returncode}")
            print(f"        stderr: {result.stderr[:200]}")
            fails += 1
    except Exception as e:
        check(False, "ig_image.py run", error=str(e)[:120])
        fails += 1

    section("10. Test run: record.py")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "picks_service" / "record.py"),
             "--days", "365"],
            capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"  {PASS}  record.py runs")
            for line in result.stdout.split("\n")[:6]:
                print(f"        {line}")
        else:
            print(f"  {FAIL}  record.py: rc={result.returncode}")
            fails += 1
    except Exception as e:
        check(False, "record.py run", error=str(e)[:120])
        fails += 1

    section("SUMMARY")
    if fails == 0:
        print(f"  {PASS}  ALL GREEN")
        print("\n  Next steps:")
        print("    1. Set ODDS_API_KEY env var (https://the-odds-api.com — free tier)")
        print("    2. Create Discord server, set DISCORD_* env vars to webhook URLs")
        print("    3. Deploy landing_page/ to Netlify (drop folder)")
        print("    4. Start posting daily picks!")
    else:
        print(f"  {FAIL}  {fails} issue(s) — fix before launching")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
