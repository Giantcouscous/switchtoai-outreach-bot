"""
Debug script — run once on Railway to print raw Notion row structure.
Shows exact property names, types, and values as Notion returns them.
python debug.py
"""
import os, json, requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "3765462bed8480e9bd86fde3dcb6b6de")

r = requests.post(
    f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
    headers={
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    },
    json={"page_size": 1}
)
r.raise_for_status()
rows = r.json().get("results", [])
if not rows:
    print("No rows returned")
else:
    props = rows[0]["properties"]
    print("=== PROPERTY NAMES AND TYPES ===")
    for name, val in props.items():
        t = val.get("type", "?")
        # Print the raw value so we can see what's inside
        print(f"\n[{name}]  type={t}")
        print(json.dumps(val, indent=2))
