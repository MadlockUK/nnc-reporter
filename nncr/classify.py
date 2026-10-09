"""Look at the photo, pick a real NNC category, draft the report wording.

Uses the Anthropic API (key in config.json). If no key is set, everything still
works - you just pick the category yourself in the UI.
"""
import base64
import json
import re
from pathlib import Path

from . import categories

MAX_EDGE = 1400  # downscale before upload: cheaper, and plenty for recognition

SYSTEM = """You triage photographs of street, highway and local environment \
issues for a resident reporting them to North Northamptonshire Council (UK). As \
well as highway faults, the council takes reports of fly-tipping, litter, dead \
animals, graffiti, fly-posting, dog fouling, waste accumulations, overgrown \
gardens, open or derelict properties, and smoke from bonfires - each has its own \
category in the allowed list.

You will be given one photo and the location it was taken. Reply with ONLY a JSON \
object, no prose, no code fence:

{
  "category": "<one line copied EXACTLY from the allowed list, or \"\" if none fits>",
  "confidence": <0.0-1.0>,
  "title": "<short summary, max 90 chars, e.g. '30cm pothole in carriageway near junction'>",
  "detail": "<2-4 sentences describing what is wrong, factual and neutral>",
  "severity": "low|medium|high",
  "notes": "<optional caveat for the human reviewer, or empty string>",
  "bin_type": "<Litter bin | Dog bin | \"\">",
  "bin_issues": ["<Bin is damaged | Bin is missing | Requires emptying | Other>"]
}

Only fill "bin_type" and "bin_issues" when the photo shows a PUBLIC litter or dog \
waste bin - the sort fixed to a post in a street or park. Leave them empty otherwise. \
Household wheelie bins and grit bins are not these. As a guide in this area, litter \
bins are usually black or blue and dog waste bins are usually red, but trust any \
lettering, symbols or shape over the colour. Copy the wording above exactly - it is \
the council's own, and anything the list does not cover (graffiti, for one) is \
"Other", described in "detail". List every issue you can see: a bin can \
be both overflowing and damaged.

Choosing between the categories that look alike - get these right, because each one sends the report to a different council form:
- Loose, scattered litter - bottles, cans, cups, wrappers, food packaging, small bits of rubbish spread over a verge, path, park, car park or roadside - is "Street Cleansing". This is the commonest case and it is NOT fly-tipping.
- "Flytipping" is a deliberately dumped LOAD: bagged or piled waste, furniture, mattresses, appliances, building rubble, tyres - one or a few large items or sacks left in one spot, plainly tipped rather than dropped.
- "Rubbish (refuse and recycling)" is about the council's own collections - a missed bin round, household wheelie bins left out or not emptied. It is not loose litter in the street.
- "Parks/landscapes" is damage to park furniture, fencing or planting, not litter that happens to be lying in a park.
- Sweepings, mud, leaf fall, broken glass and a street that simply needs sweeping are also "Street Cleansing"; graffiti is "Graffiti" and posters and stickers are "Flyposting", whatever they are stuck to.
- If it is a mixture, choose the one that describes the bulk of what is in the photo, and say what else is there in "notes".

Rules:
- "category" MUST be one line copied verbatim from the allowed list, including the
  "Group > " prefix where the line has one. Do not add anything to it, do not
  reformat it, do not append the group in brackets, do not invent a category.
- If no line in the list genuinely fits, return "" for category, set confidence to
  0, and say what it shows in "notes". A blank category is far more useful than a
  wrong one - the human will pick.
- Judge only what is visible. Do not speculate about causes or blame anyone.
- Never describe people, faces, number plates, or house numbers. If a number \
plate or a person is visible, say so in "notes" so the human can crop it.
- If the photo does not show a reportable fault (a pet, a screenshot, a selfie, \
scenery), set category to "", confidence 0, and explain in "notes".
- Use "high" severity only for a genuine hazard to traffic or pedestrians.
- Do not mention the location in "title" beyond a generic hint - the form adds it.
- Write in plain British English. Be polite and specific. Do not exaggerate.
"""


def available(cfg: dict) -> bool:
    return bool(cfg.get("classify", {}).get("enabled")) and bool(
        (cfg.get("keys") or {}).get("anthropic"))


def _encode(path: Path) -> tuple[str, str]:
    from PIL import Image
    import io
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > MAX_EDGE:
            im.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"


def _allowed_list() -> str:
    """One category per line, exactly as it must be copied back to us."""
    return "\n".join(c["label"] for c in categories.load()
                     if c["name"] not in categories.NEVER_AUTO)


def classify(photo: Path, place: dict, cfg: dict) -> dict:
    """Returns dict with category/title/detail/confidence, or {'error': ...}."""
    if not available(cfg):
        return {"error": "classification off (no Anthropic key in config.json)"}
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic package not installed"}

    client = anthropic.Anthropic(api_key=cfg["keys"]["anthropic"])
    model = cfg.get("classify", {}).get("model") or "claude-sonnet-5"
    b64, media = _encode(photo)

    where = place.get("where") or "unknown location"
    w3w = f" What3Words: ///{place['w3w']}." if place.get("w3w") else ""

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=700,
            system=SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media, "data": b64}},
                {"type": "text", "text": (
                    f"Location: {where}.{w3w}\n\n"
                    f"Allowed categories:\n{_allowed_list()}\n\n"
                    "Return the JSON object now.")},
            ]}],
        )
    except Exception as e:
        return {"error": f"API call failed: {e}"}

    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _parse_json(text)
    if data is None:
        return {"error": f"could not parse model reply: {text[:200]}"}

    raw_cat = (data.get("category") or "").strip()
    match = categories.find(raw_cat)
    if match is None:
        if raw_cat:
            data["notes"] = (data.get("notes") or "") + \
                f" [suggested '{raw_cat}', which is not a council category - " \
                f"please pick one yourself]"
        data["confidence"] = 0.0
    data["category"] = match["name"] if match else ""
    data["group"] = match["group"] if match else ""
    data["bin_type"] = normalise_bin_type(data.get("bin_type"))
    data["bin_issues"] = normalise_bin_issues(data.get("bin_issues"))
    data["title"] = (data.get("title") or "")[:120].strip()
    data["detail"] = (data.get("detail") or "").strip()
    try:
        data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        data["confidence"] = 0.0
    return data


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# Words that suggest each kind of tipping. The council's own dropdown wording is
# unknown until the page loads, so each key is matched against the OPTION text and
# its words are matched against our description of the photo.
# "strong" words name the waste itself; "weak" ones often describe the surroundings
# instead - a trolley dumped on grass is not garden waste - so they score far less.
WASTE_WORDS = {
    "household": {"strong": ["household waste", "domestic waste", "household rubbish",
                             "bin bag", "black bag", "refuse sack", "bin liner"],
                  "weak": ["household", "domestic", "rubbish", "refuse"]},
    "garden": {"strong": ["garden waste", "green waste", "grass clippings",
                          "hedge cutting", "hedge trimming", "tree branches", "turf"],
               "weak": ["garden", "grass", "hedge", "soil", "leaves", "branch"]},
    "construction": {"strong": ["construction waste", "building waste", "rubble",
                                "plasterboard", "hardcore", "bricks", "concrete"],
                     "weak": ["brick", "timber", "diy", "paving", "slab", "builder"]},
    "tyre": {"strong": ["tyre", "tyres", "tire"], "weak": ["wheel"]},
    "appliance": {"strong": ["fridge", "freezer", "washing machine", "white goods",
                             "cooker", "dishwasher", "microwave"],
                  "weak": ["appliance"]},
    "electric": {"strong": ["television", "monitor", "computer"],
                 "weak": ["tv", "electrical", "cable"]},
    "furniture": {"strong": ["sofa", "settee", "mattress", "wardrobe", "armchair",
                             "furniture", "carpet"],
                  "weak": ["chair", "table", "bed"]},
    "commercial": {"strong": ["commercial waste", "trade waste", "pallet"],
                   "weak": ["business", "industrial", "shop", "catering"]},
    "hazardous": {"strong": ["asbestos", "gas cylinder", "clinical waste", "syringe",
                             "chemical"],
                  "weak": ["oil", "battery", "paint"]},
    "vehicle": {"strong": ["vehicle part", "car part", "bumper", "exhaust",
                           "chassis", "car seat"], "weak": []},
    "metal": {"strong": ["scrap metal", "shopping trolley"],
              "weak": ["metal", "trolley", "pipe", "steel"]},
    "black bag": {"strong": ["black bag", "bin bag", "refuse sack", "bin liner"],
                  "weak": []},
}

STRONG, WEAK, MIN_SCORE = 3, 1, 3

# The same word tables for the other council forms' "type of issue" dropdowns.
# Each key is matched against the OPTION text the form actually offers, and its
# words against our description of the report - exactly as WASTE_WORDS above.
FORM_TYPE_WORDS = {
    "streetcare": {
        "litter": {"strong": ["litter", "littering", "rubbish on the"],
                   "weak": ["rubbish", "debris", "mess"]},
        "dead animal": {"strong": ["dead animal", "dead badger", "dead fox",
                                   "dead deer", "dead cat", "dead dog", "dead bird",
                                   "roadkill", "carcass"],
                        "weak": ["dead"]},
        "street cleaning": {"strong": ["street cleaning", "street cleansing",
                                       "road sweeping", "needs sweeping"],
                            "weak": ["sweeping", "cleansing", "dirty"]},
        "graffiti": {"strong": ["graffiti", "tagged", "spray paint", "spray-paint"],
                     "weak": ["tag", "paint"]},
        "fly posting": {"strong": ["fly posting", "fly-posting", "flyposting",
                                   "illegal poster"],
                        "weak": ["poster", "sticker", "flyer"]},
        "leaves": {"strong": ["leaf fall", "fallen leaves"], "weak": ["leaves"]},
        "broken glass": {"strong": ["broken glass", "smashed glass"],
                         "weak": ["glass"]},
    },
    "dog": {
        "dog fouling": {"strong": ["dog fouling", "dog mess", "dog waste",
                                   "dog foul", "dog poo", "dog dirt"],
                        "weak": ["fouling", "mess"]},
        "stray": {"strong": ["stray dog", "loose dog"], "weak": ["stray"]},
        "barking": {"strong": ["barking", "dog noise"], "weak": ["bark"]},
    },
    "grounds": {
        "grass": {"strong": ["grass cutting", "grass needs cutting", "uncut grass",
                             "overgrown grass", "long grass"],
                  "weak": ["grass", "mowing", "verge"]},
        "tree": {"strong": ["overhanging tree", "tree branch", "dangerous tree",
                            "damaged tree"],
                 "weak": ["tree", "branch"]},
        "hedge": {"strong": ["overgrown hedge", "hedge needs cutting",
                             "overhanging hedge"],
                  "weak": ["hedge", "shrub", "bush"]},
        "weed": {"strong": ["weeds", "weed growth"], "weak": ["weed"]},
    },
    "env": {
        "accumulation": {"strong": ["accumulation", "accumulated waste",
                                    "waste in garden", "rubbish in garden",
                                    "overgrown garden"],
                         "weak": ["garden", "waste", "rubbish", "vermin", "rats"]},
        "noise": {"strong": ["noise nuisance", "loud music", "noisy"],
                  "weak": ["noise", "music", "alarm"]},
        "odour": {"strong": ["odour", "bad smell", "foul smell", "sewage smell",
                             "drain smell"],
                  "weak": ["smell", "drain", "sewage"]},
        "smoke": {"strong": ["bonfire", "smoke nuisance", "burning waste"],
                  "weak": ["smoke", "burning", "fire"]},
        "light": {"strong": ["light nuisance", "security light", "floodlight"],
                  "weak": ["light", "glare"]},
        "dust": {"strong": ["dust nuisance", "vibration", "construction dust",
                            "demolition"],
                 "weak": ["dust", "site"]},
    },
    "vehicle": {
        "abandoned": {"strong": ["abandoned vehicle", "abandoned car",
                                 "burnt out", "burnt-out", "untaxed"],
                      "weak": ["abandoned", "vehicle", "car", "van"]},
    },
}

CHOOSE_TYPE = """You are filling in a council report form.

Choose the ONE option from the list that best describes the issue being reported. \
Reply with the option text copied exactly, nothing else. If none fits, reply with \
the option that means "other" if there is one, otherwise reply NONE."""


# The council's own wording, copied from the bins form, and in its order. Holding
# the same words the form offers means what we show you is what gets clicked -
# and note the form has no graffiti option, so graffiti is "Other" with the
# details in the description.
BIN_TYPES = {
    "Litter bin": ["litter", "black", "blue", "general waste", "rubbish"],
    "Dog bin": ["dog", "red"],
}
BIN_ISSUES = {
    # "lid" alone is not damage - "graffiti on the bin lid" is about the graffiti.
    "Bin is damaged": ["damage", "broken", "vandal", "burnt", "burned", "smashed",
                       "cracked", "hanging off", "loose lid", "broken lid"],
    "Bin is missing": ["missing", "removed", "gone", "stolen"],
    "Requires emptying": ["empty", "emptying", "full", "overflow", "overflowing"],
    "Other": ["other", "graffiti", "sticker", "tagged"],
}

# Words too vague to spot an issue from a description, though they are fine as an
# answer someone has chosen.
_VAGUE = {"other"}

# For report titles, where "Litter bin Requires emptying" reads badly.
BIN_ISSUE_TITLE = {
    "Bin is damaged": "is damaged",
    "Bin is missing": "is missing",
    "Requires emptying": "needs emptying",
    "Other": "needs attention",
}


def bin_words(kind: str, name: str) -> list:
    """The words that identify one bin type or issue, so a form that words its
    options slightly differently is still matched rather than left blank."""
    table = BIN_TYPES if kind == "type" else BIN_ISSUES
    return [name] + list(table.get(name, []))


def best_option(options: list, words: list) -> str:
    """Pick the option that best matches a set of words. Blank if none do."""
    best, score = "", 0
    for opt in options or []:
        low = " ".join(str(opt).lower().split())
        hit = max((len(w) for w in words if w and w.lower() in low), default=0)
        if hit > score:
            best, score = opt, hit
    return best


def normalise_bin_type(value) -> str:
    """Anything that means a bin type - ours, the model's, or an older report's -
    turned into the council's own wording."""
    low = str(value or "").strip().lower()
    if not low:
        return ""
    for name, words in BIN_TYPES.items():
        if name.lower() in low or any(w in low for w in words):
            return name
    return ""


def normalise_bin_issues(value) -> list:
    """A tidy list, in a sensible order, with no repeats.

    Accepts a list, a single phrase, or the JSON list the database stores.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = [text]
        else:
            value = [text]
    found = []
    for item in (value or []):
        low = str(item or "").strip().lower()
        if not low:
            continue
        for name, words in BIN_ISSUES.items():
            if name.lower() in low or any(w in low for w in words):
                if name not in found:
                    found.append(name)
                break
    order = list(BIN_ISSUES)
    return sorted(found, key=order.index)


def bin_details_from_text(text: str) -> dict:
    """Fall back to the wording when there is no AI answer to go on."""
    low = " ".join((text or "").lower().split())
    kind = ""
    if any(w in low for w in ("dog waste", "dog bin", "dog-waste", "red bin")):
        kind = "Dog bin"
    elif any(w in low for w in ("litter bin", "litterbin", "waste bin", "bin")):
        kind = "Litter bin"
    issues = []
    for name, words in BIN_ISSUES.items():
        # A word like "other" says nothing about what is wrong, so it is only ever
        # an answer someone picked, never something read out of a description.
        if any(w in low for w in words if w not in _VAGUE):
            issues.append(name)
    return {"bin_type": kind, "bin_issues": issues}


def pick_from_list(options: list, report: dict, cfg: dict,
                   form: str = "flytipping") -> tuple:
    """Choose the closest option for a dropdown we only see at run time.

    Returns (choice, how). Tries plain word matching first - free and instant -
    and only asks the model when that is inconclusive. `form` picks the word
    table: the fly-tipping form's dropdown describes kinds of waste, the street
    care form's kinds of mess, and so on.
    """
    if not options:
        return None, "no options"
    table = WASTE_WORDS if form in ("flytipping", None, "") \
        else FORM_TYPE_WORDS.get(form, {})
    title = str(report.get("title") or "").lower()
    text = " ".join(str(report.get(k) or "") for k in
                    ("title", "detail", "category", "notes")).lower()

    def score_against(source: str):
        best, best_score = None, 0
        for opt in options:
            low = opt.lower()
            score = 0
            for key, words in table.items():
                if key not in low:
                    continue
                score += sum(STRONG for w in words["strong"] if w in source)
                score += sum(WEAK for w in words["weak"] if w in source)
            score += sum(WEAK for w in re.findall(r"[a-z]{5,}", low)
                         if w in source and w not in ("waste", "other", "rubbish"))
            if score > best_score:
                best, best_score = opt, score
        return best, best_score

    # Decide locally only when the summary - which names the thing that is wrong -
    # supports it. The longer description often mentions the surroundings ("on a
    # grassed area"), which is not evidence about the waste.
    best, best_score = score_against(title)
    if best and best_score >= MIN_SCORE:
        return best, "matched locally"

    choice = _ask_model_to_choose(options, text, cfg)
    if choice:
        return choice, "chosen by Claude"

    for opt in options:                       # last resort
        if "other" in opt.lower():
            return opt, "fell back to 'other'"
    return None, "no match"


def _ask_model_to_choose(options: list, text: str, cfg: dict) -> str | None:
    """One small text-only call - a fraction of a penny."""
    if not available(cfg) or not text.strip():
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["keys"]["anthropic"])
        msg = client.messages.create(
            model=cfg.get("classify", {}).get("model") or "claude-sonnet-5",
            max_tokens=60, system=CHOOSE_TYPE,
            messages=[{"role": "user", "content":
                       f"Waste described as: {text[:600]}\n\n"
                       f"Options:\n" + "\n".join(f"- {o}" for o in options)}],
        )
        reply = "".join(b.text for b in msg.content
                        if getattr(b, "type", "") == "text").strip()
    except Exception:
        return None
    if not reply or reply.upper().startswith("NONE"):
        return None
    for opt in options:                        # only ever return a real option
        if opt.strip().lower() == reply.strip().lower():
            return opt
    for opt in options:
        if opt.strip().lower() in reply.strip().lower():
            return opt
    return None


def compose_detail(detail: str, place: dict, taken_at: str | None) -> str:
    """Add the location + date the council needs, without bloating the text."""
    parts = [detail.strip()] if detail else []
    loc = []
    if place.get("street"):
        loc.append(f"Location: {place['street']}"
                   + (f", {place['locality']}" if place.get("locality") else ""))
    if place.get("postcode"):
        loc.append(f"postcode {place['postcode']}")
    if place.get("w3w"):
        loc.append(f"What3Words ///{place['w3w']}")
    if place.get("lat") is not None:
        loc.append(f"coordinates {place['lat']:.5f}, {place['lon']:.5f}")
    if loc:
        parts.append(". ".join(loc) + ".")
    if taken_at:
        parts.append(f"Photographed {taken_at[:10]} at {taken_at[11:16]}.")
    return "\n\n".join(parts)
