#!/usr/bin/env python3
"""Turn the raw SWCP GPX + tracker spreadsheet into a small GeoJSON for the website.

Usage:  python3 tools/build_swcp_data.py

Reads   south_west/uploads_2026_04_South_West_Coast_Path_Elev.gpx   (30 MB, not committed)
        south_west/South West Coast Path Tracker.xlsx
Writes  data/swcp.geojson    one LineString feature per official stage, simplified
        data/swcp.js         the same thing as `window.SWCP = {...}`, which is what the
                             page actually loads -- fetch() of a local .geojson is blocked
                             by CORS when you open walking.html straight off disk.

Stage list comes from the workbook's OfficialStages sheet (Cicerone 3rd ed., Paddy
Dillon). Progress comes from the status column of MyDays, matched to official stages
through its guidebook_stage_ref column, so the spreadsheet stays the single source of
truth -- re-run this after editing it.
"""
import bisect
import json
import math
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GPX = ROOT / "south_west" / "uploads_2026_04_South_West_Coast_Path_Elev.gpx"
XLSX = ROOT / "south_west" / "South West Coast Path Tracker.xlsx"
OUT = ROOT / "data" / "swcp.geojson"

GNS = {"g": "http://www.topografix.com/GPX/1/1"}
XNS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

SIMPLIFY_M = 25          # Douglas-Peucker tolerance, metres
COORD_DP = 5             # ~1 m precision, keeps the file small
SEARCH_WINDOW_M = 8000   # how far either side of the expected split to look

# Stages that aren't part of the 630-mile line: 42 is the optional Isle of Portland
# circuit, '44 (alt)' is an alternative to stage 44. Both would double back on
# themselves and break the single continuous route.
SKIP_STAGES = {"42", "44 (alt)"}   # 42 is re-added below from the GPX loop itself


# ── geometry helpers ────────────────────────────────────────────────────────
def haversine(a, b):
    """Distance in metres between two (lon, lat) pairs."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def perp_m(p, a, b):
    """Perpendicular distance in metres from p to segment a-b, in a local flat projection."""
    k = math.cos(math.radians(a[1])) * 111320.0
    px, py = p[0] * k, p[1] * 110574.0
    ax, ay = a[0] * k, a[1] * 110574.0
    bx, by = b[0] * k, b[1] * 110574.0
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(pts, tol):
    """Iterative Douglas-Peucker (recursion would blow the stack on 100k points)."""
    if len(pts) < 3:
        return pts[:]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        worst, idx = -1.0, lo
        for i in range(lo + 1, hi):
            d = perp_m(pts[i], pts[lo], pts[hi])
            if d > worst:
                worst, idx = d, i
        if worst > tol:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    return [p for p, k in zip(pts, keep) if k]


# ── read the trail itself ───────────────────────────────────────────────────
FERRYBRIDGE = (-2.4569, 50.5714)   # where the Isle of Portland circuit leaves and rejoins


def cumulative(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + haversine(pts[i - 1], pts[i]))
    return cum


def read_trail():
    """Parts 1-11 concatenated Minehead -> Poole, with the Portland loop pulled out.

    The GPX's main parts run the Isle of Portland circuit inline, but the guidebook
    treats it as optional stage 42. Left in, it inflates the running distance by
    ~16 km and shoves every boundary after Abbotsbury out of place, so it comes out
    as its own line and the main route jumps straight across Ferrybridge.
    """
    root = ET.parse(GPX).getroot()
    parts = {}
    for trk in root.findall("g:trk", GNS):
        name = trk.findtext("g:name", default="", namespaces=GNS)
        if not name.startswith("South West Coast Path Part "):
            continue
        n = int(name.rsplit(" ", 1)[1])
        if n in parts:                       # the file lists every track twice
            continue
        parts[n] = [(round(float(p.get("lon")), 7), round(float(p.get("lat")), 7))
                    for p in trk.findall(".//g:trkpt", GNS)]

    pts = []
    for n in sorted(parts):
        for p in parts[n]:
            if not pts or p != pts[-1]:      # the GPX repeats each point 3x
                pts.append(p)

    # The route passes within a few hundred metres of Ferrybridge twice: on the way
    # out to Portland and on the way back. Everything between is the circuit.
    near = [i for i, p in enumerate(pts) if haversine(p, FERRYBRIDGE) < 600]
    passes = []
    for i in near:
        if passes and i - passes[-1][-1] <= 50:
            passes[-1].append(i)
        else:
            passes.append([i])

    loop = []
    if len(passes) >= 2:
        a, z = passes[0][-1], passes[-1][0]
        loop = pts[a:z + 1]
        pts = pts[:a + 1] + pts[z:]
        print(f"  lifted a {cumulative(loop)[-1]/1000:.1f} km Portland circuit out of the main line")

    return pts, cumulative(pts), loop


# ── spreadsheet ─────────────────────────────────────────────────────────────
def read_workbook():
    z = zipfile.ZipFile(XLSX)
    shared = [
        "".join(t.text or "" for t in si.iter(f"{{{XNS['m']}}}t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", XNS)
    ]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target").lstrip("/")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}

    def rows(sheet_name):
        rid = next(s.get(RID) for s in wb.findall(".//m:sheet", XNS)
                   if s.get("name") == sheet_name)
        tgt = rels[rid]
        sh = ET.fromstring(z.read(tgt if tgt.startswith("xl/") else "xl/" + tgt))
        out = []
        for r in sh.findall(".//m:row", XNS):
            cells = {}
            for c in r.findall("m:c", XNS):
                ref = "".join(ch for ch in c.get("r") if ch.isalpha())
                v = c.find("m:v", XNS)
                val = (v.text or "") if v is not None else ""
                if c.get("t") == "s" and val:
                    val = shared[int(val)]
                cells[ref] = val.strip()
            out.append(cells)
        return out

    # MyDays: status per guidebook stage. A stage counts as walked only if a day
    # logged against it says Completed; 'Partially completed' is kept separate so
    # its distance isn't claimed as done.
    status = {}
    for r in rows("MyDays")[2:]:
        ref, st = r.get("F", ""), r.get("J", "").lower()
        if not ref or not st:
            continue
        if st.startswith("completed"):
            status[ref] = "done"
        elif st.startswith("partial") and status.get(ref) != "done":
            status[ref] = "partial"

    stages = []
    for r in rows("OfficialStages")[2:]:
        ref = r.get("A", "")
        if not ref or ref in SKIP_STAGES or not r.get("I"):
            continue
        stages.append({
            "ref": ref,
            "n": int(ref),
            "start": r.get("B", ""),
            "end": r.get("C", ""),
            "km": float(r["D"]),
            "ascent": int(float(r["E"])) if r.get("E") else None,
            "time": r.get("F", ""),
            "end_ll": (float(r["J"]), float(r["I"])),
            "state": status.get(ref, "todo"),
        })
    stages.sort(key=lambda s: s["n"])
    return stages


# ── regions ─────────────────────────────────────────────────────────────────
# The four official SWCP administrative sections, as cumulative km from Minehead.
REGIONS = [
    (220, "Somerset & North Devon"),   # Minehead - Marsland Mouth
    (703, "Cornwall"),                 # Marsland Mouth - Cremyll
    (890, "South Devon"),              # Cremyll - Lyme Regis
    (10_000, "Dorset"),                # Lyme Regis - South Haven Point
]


def region_at(km):
    return next(name for limit, name in REGIONS if km < limit)


# ── stitch the two together ─────────────────────────────────────────────────
ON_TRAIL_M = 1500        # a boundary coordinate further than this isn't really on the path
AGREE_M = 6000           # ...nor is one this far from where the book's mileage puts it


def split_index(pts, cum, expected_m, target_ll, floor_idx):
    """Where does a stage boundary fall on the trail?

    `expected_m` -- the book's cumulative distance rescaled to the GPX's own length --
    says roughly where to look, and the sheet's coordinate picks the exact vertex
    inside that window.

    The sheet's coordinates were themselves derived by scaling against the GPX while
    the Portland circuit was still inline, so the ones past Abbotsbury sit several km
    short of the places they're named after (its 'Swanage' is nearer Kimmeridge). A
    coordinate is only trusted if it lands both on the trail and near where the book's
    mileage expects it; otherwise the mileage alone decides. Returns (index, trusted).
    """
    fallback = max(floor_idx + 1, min(len(pts) - 1, bisect.bisect_left(cum, expected_m)))

    lo = max(floor_idx + 1, bisect.bisect_left(cum, expected_m - SEARCH_WINDOW_M))
    hi = min(len(pts) - 1, bisect.bisect_right(cum, expected_m + SEARCH_WINDOW_M))
    if lo > hi:
        return fallback, False

    best, best_d = lo, float("inf")
    for i in range(lo, hi + 1):
        d = haversine(pts[i], target_ll)
        if d < best_d:
            best, best_d = i, d

    if best_d <= ON_TRAIL_M and abs(cum[best] - expected_m) <= AGREE_M:
        return best, True
    return fallback, False


def main():
    print("reading GPX ...")
    trail, cum, portland = read_trail()
    trail_m = cum[-1]
    print(f"  {len(trail):,} unique trail points, {trail_m/1000:.0f} km")

    print("reading spreadsheet ...")
    stages = read_workbook()
    book_m = sum(s["km"] for s in stages) * 1000
    counts = {k: sum(1 for s in stages if s["state"] == k) for k in ("done", "partial", "todo")}
    print(f"  {len(stages)} official stages, {book_m/1000:.0f} km per the book")
    print(f"  {counts['done']} completed, {counts['partial']} partial, {counts['todo']} to go")

    scale = trail_m / book_m
    features, idx, run_book_km, run_km = [], 0, 0.0, 0.0
    untrusted = []

    for k, s in enumerate(stages):
        run_book_km += s["km"]
        expected = run_book_km * 1000 * scale
        if k == len(stages) - 1:
            end = len(trail) - 1                       # always finish at Poole
        else:
            end, trusted = split_index(trail, cum, expected, s["end_ll"], idx)
            if not trusted:
                untrusted.append(s["ref"])

        seg = trail[idx:end + 1]
        idx = end
        length_km = sum(haversine(seg[i], seg[i + 1]) for i in range(len(seg) - 1)) / 1000
        simple = simplify(seg, SIMPLIFY_M)
        run_km += length_km

        features.append({
            "type": "Feature",
            "properties": {
                "seq": s["n"],
                "start": s["start"],
                "end": s["end"],
                "km": round(length_km, 1),
                "book_km": s["km"],
                "ascent": s["ascent"],
                "time": s["time"],
                "region": region_at(run_km - length_km / 2),
                "state": s["state"],
                "optional": False,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(x, COORD_DP), round(y, COORD_DP)] for x, y in simple],
            },
        })

    # Stage 42 rides along as its own optional line rather than part of the route.
    if portland:
        simple = simplify(portland, SIMPLIFY_M)
        features.append({
            "type": "Feature",
            "properties": {
                "seq": 42,
                "start": "Ferrybridge",
                "end": "Isle of Portland circuit",
                "km": round(cumulative(portland)[-1] / 1000, 1),
                "book_km": 22.0,
                "ascent": 510,
                "time": "7:00",
                "region": "Dorset",
                "state": "todo",
                "optional": True,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(x, COORD_DP), round(y, COORD_DP)] for x, y in simple],
            },
        })

    if untrusted:
        print("  boundary placed from book mileage alone (sheet coordinate rejected): "
              + ", ".join(untrusted))

    # Segment length is measured off the GPX, so it runs ~0.5 km over the book on a
    # typical stage. Anything much wider than that is the book and the track genuinely
    # disagreeing about the route -- stage 41, where the track takes the long way
    # inland around the Fleet -- and is worth seeing rather than quietly averaging out.
    odd = [(f["properties"]["seq"], f["properties"]["km"] - f["properties"]["book_km"])
           for f in features if abs(f["properties"]["km"] - f["properties"]["book_km"]) > 3]
    for seq, diff in odd:
        print(f"  ! stage {seq} is {diff:+.1f} km against the book's figure")

    OUT.parent.mkdir(exist_ok=True)
    blob = json.dumps({"type": "FeatureCollection", "features": features},
                      separators=(",", ":"))
    OUT.write_text(blob)
    OUT.with_suffix(".js").write_text(f"window.SWCP = {blob};\n")
    pts_out = sum(len(f["geometry"]["coordinates"]) for f in features)
    print(f"wrote {OUT.relative_to(ROOT)} + swcp.js  "
          f"{OUT.stat().st_size/1024:.0f} KB  {pts_out:,} points  {run_km:.0f} km")


if __name__ == "__main__":
    main()
