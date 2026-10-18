"""Synthetic Fleet check (owner API 1.8). It prints the number in args.value, and uses no network, file or AI."""
import json
import sys

value = (json.loads(sys.argv[1]).get("args") or {}).get("value", 0)
print(json.dumps({"value": value, "summary": f"synthetic value {value}"}))
