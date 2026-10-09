"""Move API keys out of this folder, into %USERPROFILE%\\.nnc-reporter\\keys.json.

Use this if the project folder is synced (OneDrive) or shared with anyone. The app
reads the external file automatically and prefers it over config.json.
Run: move-keys-out.bat
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nncr import config  # noqa: E402

dest_dir = Path.home() / ".nnc-reporter"
dest = dest_dir / "keys.json"

raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
keys = {k: v for k, v in (raw.get("keys") or {}).items()
        if isinstance(v, str) and v and "PUT_YOUR" not in v.upper()}

# The highways account password is a secret too.
pw = (raw.get("highways_login") or {}).get("password")
if isinstance(pw, str) and pw and "PUT_YOUR" not in pw.upper():
    keys["highways_password"] = pw

if not keys:
    print("No keys found in config.json - nothing to move.")
    sys.exit(0)

dest_dir.mkdir(exist_ok=True)
existing = {}
if dest.exists():
    try:
        existing = json.loads(dest.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        pass
existing.update(keys)
dest.write_text(json.dumps(existing, indent=2), encoding="utf-8")

for k in keys:
    if k == "highways_password":
        raw["highways_login"]["password"] = ""
    else:
        raw["keys"][k] = ""
config.CONFIG_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")

print(f"Moved {', '.join(keys)} to:\n  {dest}\n")
print("config.json no longer holds any secrets, so the synced copy is safe.")
print("Keep a note of that file - it is not backed up by OneDrive.")
print("\nChecking the app still sees them...")
cfg = config.load()
for k in keys:
    if k == "highways_password":
        ok = bool(cfg["highways_login"].get("password"))
    else:
        ok = bool(cfg["keys"].get(k))
    print(f"  {k}: {'found' if ok else 'MISSING'}")
