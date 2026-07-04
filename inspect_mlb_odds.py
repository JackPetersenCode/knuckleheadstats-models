"""Inspect the ArnavSaraogi MLB odds JSON to understand structure."""
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)

print(f"Top-level type: {type(data).__name__}")
if isinstance(data, dict):
    keys = list(data.keys())
    print(f"  num top keys: {len(keys)}")
    print(f"  first 5 keys: {keys[:5]}")
    print(f"  last 5 keys: {keys[-5:]}")
    first_key = keys[0]
    v = data[first_key]
    print(f"\nValue type for '{first_key}': {type(v).__name__}")
    print(json.dumps(v, indent=2)[:2000] if isinstance(v, (dict, list)) else v)
elif isinstance(data, list):
    print(f"  num items: {len(data)}")
    print(f"  first item:\n{json.dumps(data[0], indent=2)[:2000]}")
