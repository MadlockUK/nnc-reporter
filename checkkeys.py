"""Confirm the API keys in config.json actually work. Run: check-keys.bat"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nncr import classify, config, geo  # noqa: E402

cfg = config.load()
LAT, LON = 52.39620, -0.72590  # Rockingham Road, Kettering

print("\nWhat3Words")
if not cfg["keys"].get("what3words"):
    print("  no key in config.json - reports will use street + postcode instead")
else:
    r = geo.what3words(LAT, LON, cfg)
    if r.get("words"):
        print(f"  working: {LAT}, {LON}  ->  ///{r['words']}")
        if r.get("nearest_place"):
            print(f"  nearest place: {r['nearest_place']}")
    else:
        print(f"  NOT working: {r.get('error')}")
        print("  Check the key at https://developer.what3words.com/public-api - "
              "convert-to-3wa needs to be included in your plan.")

print("\nStreet lookup (OpenStreetMap, no key needed)")
rg = geo.reverse_geocode(LAT, LON, cfg)
if rg.get("error"):
    print(f"  NOT working: {rg['error']}")
else:
    print(f"  working: {rg.get('street')}, {rg.get('locality')} {rg.get('postcode')}")

print("\nPhoto categorisation (Anthropic)")
if not classify.available(cfg):
    print("  off - add keys.anthropic to config.json to have categories suggested")
else:
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=cfg["keys"]["anthropic"])
        m = c.messages.create(model=cfg["classify"]["model"], max_tokens=5,
                              messages=[{"role": "user", "content": "Reply OK"}])
        print(f"  working: model {cfg['classify']['model']} responded")
    except Exception as e:
        print(f"  NOT working: {e}")

print()
