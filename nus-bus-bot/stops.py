import math
from typing import Optional

# Coordinates sourced from NUS NextBus API /BusStops endpoint (authoritative)
STOPS = [
    {"name": "AS5",          "caption": "AS 5",                        "lat": 1.293619, "lng": 103.771475},
    {"name": "BG-MRT",       "caption": "Botanic Gardens MRT (PUDO)",  "lat": 1.322614, "lng": 103.815914},
    {"name": "BIZ2",         "caption": "BIZ 2",                       "lat": 1.293223, "lng": 103.775068},
    {"name": "CG",           "caption": "College Green",               "lat": 1.323337, "lng": 103.816276},
    {"name": "CLB",          "caption": "Central Library",             "lat": 1.296544, "lng": 103.772569},
    {"name": "COM3",         "caption": "COM 3",                       "lat": 1.294431, "lng": 103.775217},
    {"name": "HSSML-OPP",   "caption": "Opp HSSML",                   "lat": 1.292798, "lng": 103.774978},
    {"name": "IT",           "caption": "Information Technology",      "lat": 1.297204, "lng": 103.772688},
    {"name": "JP-SCH-16151", "caption": "The Japanese Primary School", "lat": 1.300770, "lng": 103.769904},
    {"name": "KR-MRT",       "caption": "Kent Ridge MRT",              "lat": 1.294820, "lng": 103.784413},
    {"name": "KR-MRT-OPP",  "caption": "Opp Kent Ridge MRT",          "lat": 1.294962, "lng": 103.784556},
    {"name": "KRB",          "caption": "Kent Ridge Bus Terminal",     "lat": 1.294430, "lng": 103.769997},
    {"name": "KV",           "caption": "Kent Vale",                   "lat": 1.301899, "lng": 103.769455},
    {"name": "LT13",         "caption": "LT 13",                      "lat": 1.294552, "lng": 103.770635},
    {"name": "LT13-OPP",    "caption": "Ventus",                      "lat": 1.295340, "lng": 103.770617},
    {"name": "LT27",         "caption": "LT 27",                      "lat": 1.297421, "lng": 103.780941},
    {"name": "MUSEUM",       "caption": "Museum",                     "lat": 1.301081, "lng": 103.773690},
    {"name": "NUSS-OPP",    "caption": "Opp NUSS",                    "lat": 1.293208, "lng": 103.772618},
    {"name": "OTH",          "caption": "Oei Tiong Ham Building",     "lat": 1.319796, "lng": 103.817774},
    {"name": "PGP",          "caption": "Prince George's Park",       "lat": 1.291765, "lng": 103.780419},
    {"name": "PGPR",         "caption": "Prince George's Park Foyer", "lat": 1.290994, "lng": 103.781153},
    {"name": "RAFFLES",      "caption": "Raffles Hall",               "lat": 1.300946, "lng": 103.772703},
    {"name": "S17",          "caption": "S 17",                       "lat": 1.297488, "lng": 103.780707},
    {"name": "SDE3-OPP",    "caption": "Opp SDE 3",                   "lat": 1.297799, "lng": 103.769603},
    {"name": "TCOMS",        "caption": "TCOMS",                      "lat": 1.293654, "lng": 103.776898},
    {"name": "TCOMS-OPP",   "caption": "Opp TCOMS",                   "lat": 1.293789, "lng": 103.776715},
    {"name": "UHALL",        "caption": "University Hall",            "lat": 1.297372, "lng": 103.778075},
    {"name": "UHALL-OPP",   "caption": "Opp University Hall",         "lat": 1.297574, "lng": 103.778088},
    {"name": "UHC",          "caption": "University Health Centre",   "lat": 1.298910, "lng": 103.776103},
    {"name": "UHC-OPP",     "caption": "Opp University Health Centre","lat": 1.298788, "lng": 103.775612},
    {"name": "UTOWN",        "caption": "University Town",            "lat": 1.303876, "lng": 103.774621},
    {"name": "YIH",          "caption": "Yusof Ishak House",          "lat": 1.298885, "lng": 103.774377},
    {"name": "YIH-OPP",     "caption": "Opp Yusof Ishak House",       "lat": 1.298904, "lng": 103.774118},
]

_SKIP_WORDS = {"NUS", "THE", "OF", "AT", "IN", "AND", "A", "AN",
               "NATIONAL", "UNIVERSITY", "SINGAPORE"}


def find_stop(query: str) -> Optional[dict]:
    q = query.strip().upper()

    # 1. Exact name match
    for stop in STOPS:
        if stop["name"].upper() == q:
            return stop

    # 2. Full substring match
    for stop in STOPS:
        if q in stop["name"].upper() or q in stop["caption"].upper():
            return stop

    # 3. Token match — strip non-distinctive words like "NUS", then require all
    #    remaining tokens to appear in the stop name or caption.
    tokens = [w for w in q.split() if w not in _SKIP_WORDS and len(w) > 1]
    if tokens:
        for stop in STOPS:
            caption = stop["caption"].upper()
            name    = stop["name"].upper()
            if all(t in caption or t in name for t in tokens):
                return stop

    return None


def nearby_stops(lat: float, lng: float, radius_m: int = 500) -> list[dict]:
    def _dist(s):
        R = 6_371_000
        p1, p2 = math.radians(lat), math.radians(s["lat"])
        dp = math.radians(s["lat"] - lat)
        dl = math.radians(s["lng"] - lng)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

    results = [{"dist": _dist(s), **s} for s in STOPS if _dist(s) <= radius_m]
    return sorted(results, key=lambda x: x["dist"])
