"""Which council form a category has to go to.

North Northamptonshire lists some categories on the highways site that it will not
actually accept there - choosing Flytipping just shows a notice pointing at the
council's separate online form. Those live here so the rule is applied everywhere:
on intake, when you change a category in the app, and again just before a report is
filled in, in case it was queued before the rule existed.

The council's "Report an environmental issue" pages signpost each scenario to its
own Granicus form, so those categories are routed the same way:
  granicus              fly-tipping form
  granicus_bins         public litter / dog waste bin form
  granicus_streetcare   street care & cleaning (litter, dead animals, street
                        cleaning, graffiti, fly-posting)
  granicus_dog          dog issues (dog fouling)
  granicus_grounds      grass cutting, trees or hedges (Corby, Kettering and
                        Wellingborough areas only - East Northants goes to town
                        and parish councils)
  granicus_derelict     open or derelict properties
  granicus_vehicle      abandoned vehicles
  granicus_env          "Tell us about an environmental issue" (accumulations and
                        overgrown gardens, noise, odours/drains, smoke and
                        bonfires, light, dust and vibration)
"""

# category name (lowercase) -> the service it must use
FORCED = {
    "flytipping": "granicus",
    "litter or dog waste bin": "granicus_bins",
    # Street care & cleaning form
    "graffiti": "granicus_streetcare",
    "flyposting": "granicus_streetcare",
    "street cleansing": "granicus_streetcare",
    "dead animal": "granicus_streetcare",
    # Dedicated forms
    "dog fouling": "granicus_dog",
    "abandoned vehicles": "granicus_vehicle",
    "open or derelict property": "granicus_derelict",
    "grass, trees or hedges (grounds)": "granicus_grounds",
    # "Tell us about an environmental issue" - one form, several scenarios
    "accumulation or overgrown garden": "granicus_env",
    "noise nuisance": "granicus_env",
    "odour or drain nuisance": "granicus_env",
    "smoke or bonfire": "granicus_env",
    "light nuisance": "granicus_env",
    "dust or vibration": "granicus_env",
}

DEFAULT = "fixmystreet"

# The grounds form covers the former Corby, Kettering and Wellingborough council
# areas; East Northamptonshire grass/tree/hedge issues go to town and parish
# councils instead. Localities here trigger a warning, never a block - boundaries
# do not follow locality names exactly.
EAST_NORTHANTS_LOCALITIES = (
    "rushden", "higham ferrers", "irthlingborough", "raunds", "thrapston",
    "oundle", "ringstead", "stanwick", "woodford", "islip", "kings cliffe",
    "king's cliffe", "nassington", "warmington", "titchmarsh", "brigstock",
    "easton on the hill",
)


def grounds_area_warning(report: dict) -> str | None:
    """A warning when a grounds report looks to be in the former East Northants
    district, whose grass/tree/hedge issues the council's form does not take."""
    if route_for_report(report) != "granicus_grounds":
        return None
    low = (report.get("locality") or "").strip().lower()
    if low and low in EAST_NORTHANTS_LOCALITIES:
        return (f"{report.get('locality')} is in the former East Northamptonshire "
                f"area - the council's grounds form only covers Corby, Kettering "
                f"and Wellingborough; East Northants issues go to the town or "
                f"parish council")
    return None


# A dead animal on the street goes to the street care & cleaning form, and the
# highways list has no category for it, so it is recognised from the wording too.
DEAD_ANIMAL_WORDS = (
    "dead animal", "dead badger", "dead fox", "dead deer", "dead cat",
    "dead dog", "dead bird", "dead rabbit", "dead hedgehog", "roadkill",
    "animal carcass", "carcass",
)


def looks_like_dead_animal(text: str) -> bool:
    low = " ".join((text or "").lower().split())
    return bool(low) and any(w in low for w in DEAD_ANIMAL_WORDS)

# Loose litter belongs on the street care & cleaning form. The highways list has
# no "litter" line - the nearest is "Street Cleansing" - so a report that plainly
# describes scattered litter is recognised from the wording too, and only when
# nothing about it suggests a tipped load, which is a different form again.
LITTER_WORDS = (
    "scattered litter", "litter scattered", "loose litter", "general litter",
    "litter strewn", "strewn with litter", "litter across", "litter on the",
    "litter is spread", "litter spread", "littering", "litter picking",
    "cans and bottles", "bottles and cans", "food packaging", "drinks cans",
    "sweet wrappers", "takeaway packaging", "needs sweeping", "street sweeping",
)
NOT_LITTER_WORDS = (
    "fly-tip", "flytip", "fly tip", "dumped", "dumping", "tipped",
    "bin bag", "black bag", "refuse sack", "bin liner", "mattress", "sofa",
    "settee", "fridge", "freezer", "washing machine", "rubble", "plasterboard",
    "tyre", "furniture", "white goods", "builders waste", "building waste",
    "garden waste", "trolley", "abandoned vehicle",
)


def looks_like_loose_litter(text: str) -> bool:
    """Scattered litter rather than a tipped load or a bin that needs emptying."""
    low = " ".join((text or "").lower().split())
    if not low:
        return False
    if any(w in low for w in NOT_LITTER_WORDS):
        return False
    return any(w in low for w in LITTER_WORDS)


# Public litter and dog waste bins have their own form. Grit bins belong to
# highways, and household wheelie bins are refuse collection - neither goes here.
BIN_WORDS = (
    "litter bin", "litterbin", "dog waste bin", "dog bin", "dog-waste bin",
    "waste bin", "bin is full", "bin full", "overflowing bin", "bin overflowing",
    "bin is overflowing", "bin needs emptying", "damaged bin", "bin damaged",
    "broken bin", "bin is broken", "missing bin", "bin missing", "bin has been removed",
)
NOT_BIN_WORDS = (
    "grit bin", "salt bin", "wheelie bin", "wheeled bin", "household bin",
    "recycling bin", "black bin", "blue bin", "green bin", "brown bin",
    "grey bin", "food bin", "garden bin", "bin collection", "missed collection",
    "bin lorry", "bin men", "bin day",
)


def looks_like_bin(text: str) -> bool:
    """Does this describe a public litter or dog waste bin?"""
    low = " ".join((text or "").lower().split())
    if not low:
        return False
    if any(w in low for w in NOT_BIN_WORDS):
        return False
    return any(w in low for w in BIN_WORDS)


def route_for(category: str, cfg: dict | None = None) -> str:
    """Not configurable: the council enforces this, so an override could only
    produce a report the highways site refuses. `cfg` is accepted so callers need
    not care, and is deliberately ignored for forced categories."""
    return FORCED.get((category or "").strip().lower(), DEFAULT)


def route_for_report(report: dict, cfg: dict | None = None) -> str:
    """Which form a whole report belongs on - category first, then what it says.

    Nothing in the council's highways list is a public litter bin, and the nearest
    it has to loose litter is "Street Cleansing", so both are recognised from the
    description as well as the category.
    """
    report = report or {}
    forced = FORCED.get((report.get("category") or "").strip().lower())
    if forced:
        return forced
    text = " ".join(str(report.get(k) or "") for k in ("title", "detail", "notes"))
    if looks_like_bin(text):
        return "granicus_bins"
    if looks_like_dead_animal(text):
        return "granicus_streetcare"
    if looks_like_loose_litter(text):
        return "granicus_streetcare"
    return DEFAULT


def is_forced(category: str) -> bool:
    return (category or "").strip().lower() in FORCED


def must_move(report: dict, cfg: dict | None = None) -> str | None:
    """The service this report has to use, if that is not the one it is on."""
    want = route_for_report(report, cfg)
    return want if want != (report.get("service") or DEFAULT) else None


def restate_bins(store) -> list:
    """Bring older bin reports up to the council's own wording.

    They were saved as "needs emptying" and "dog waste"; the form says "Requires
    emptying" and "Dog bin", and holding its words is what keeps the tick-boxes
    and the form in step.
    """
    import json

    from . import classify
    changed = []
    for r in store.all_reports():
        if r.get("status") in store.DONE_STATUSES:
            continue
        was_type, was_issues = r.get("bin_type") or "", r.get("bin_issues") or ""
        if not was_type and not was_issues:
            continue
        now_type = classify.normalise_bin_type(was_type)
        now_issues = classify.normalise_bin_issues(was_issues)
        packed = json.dumps(now_issues) if now_issues else ""
        if now_type == was_type and packed == was_issues:
            continue
        store.update(r["id"], {"bin_type": now_type, "bin_issues": packed})
        changed.append((r["id"], was_issues or was_type, packed or now_type))
    return changed


def fix_existing(store, cfg: dict | None = None) -> list:
    """Correct reports queued before the rules existed. Returns what changed."""
    changed = []
    for r in store.all_reports():
        if r.get("status") in ("submitted", "mapped", "completed"):
            continue                       # already gone, leave the record alone
        want = must_move(r, cfg)
        if want:
            store.update(r["id"], {"service": want})
            changed.append((r["id"], r.get("category") or "?", r.get("service"), want))
    return changed
