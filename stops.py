import math
from typing import Optional

STOPS = [
    {"name": "AS5",          "caption": "AS 5",                        "lat": 1.2952, "lng": 103.7826},
    {"name": "BG-MRT",       "caption": "Botanic Gardens MRT (PUDO)",  "lat": 1.3222, "lng": 103.8153},
    {"name": "BIZ2",         "caption": "BIZ 2",                       "lat": 1.2935, "lng": 103.7737},
    {"name": "CG",           "caption": "College Green",               "lat": 1.3041, "lng": 103.7742},
    {"name": "CLB",          "caption": "Central Library",             "lat": 1.2966, "lng": 103.7798},
    {"name": "COM3",         "caption": "COM 3",                       "lat": 1.2941, "lng": 103.7740},
    {"name": "HSSML-OPP",   "caption": "Opp HSSML",                   "lat": 1.2983, "lng": 103.7800},
    {"name": "IT",           "caption": "Information Technology",      "lat": 1.2946, "lng": 103.7735},
    {"name": "JP-SCH-16151", "caption": "The Japanese Primary School", "lat": 1.3044, "lng": 103.7793},
    {"name": "KR-MRT",       "caption": "Kent Ridge MRT",              "lat": 1.2975, "lng": 103.7847},
    {"name": "KR-MRT-OPP",  "caption": "Opp Kent Ridge MRT",          "lat": 1.2972, "lng": 103.7848},
    {"name": "KRB",          "caption": "Kent Ridge Bus Terminal",     "lat": 1.2949, "lng": 103.7817},
    {"name": "KV",           "caption": "Kent Vale",                   "lat": 1.3014, "lng": 103.7762},
    {"name": "LT13",         "caption": "LT 13",                      "lat": 1.2966, "lng": 103.7806},
    {"name": "LT13-OPP",    "caption": "Ventus",                      "lat": 1.2968, "lng": 103.7807},
    {"name": "LT27",         "caption": "LT 27",                      "lat": 1.2967, "lng": 103.7833},
    {"name": "MUSEUM",       "caption": "Museum",                     "lat": 1.3010, "lng": 103.7777},
    {"name": "NUSS-OPP",    "caption": "Opp NUSS",                    "lat": 1.2979, "lng": 103.7804},
    {"name": "OTH",          "caption": "Oei Tiong Ham Building",     "lat": 1.2984, "lng": 103.7823},
    {"name": "PGP",          "caption": "Prince George's Park",       "lat": 1.2913, "lng": 103.7810},
    {"name": "PGPR",         "caption": "Prince George's Park Foyer", "lat": 1.2913, "lng": 103.7814},
    {"name": "RAFFLES",      "caption": "Raffles Hall",               "lat": 1.3050, "lng": 103.7763},
    {"name": "S17",          "caption": "S 17",                       "lat": 1.2969, "lng": 103.7797},
    {"name": "SDE3-OPP",    "caption": "Opp SDE 3",                   "lat": 1.2948, "lng": 103.7719},
    {"name": "TCOMS",        "caption": "TCOMS",                      "lat": 1.2975, "lng": 103.7866},
    {"name": "TCOMS-OPP",   "caption": "Opp TCOMS",                   "lat": 1.2974, "lng": 103.7868},
    {"name": "UHALL",        "caption": "University Hall",            "lat": 1.2980, "lng": 103.7820},
    {"name": "UHALL-OPP",   "caption": "Opp University Hall",         "lat": 1.2984, "lng": 103.7818},
    {"name": "UHC",          "caption": "University Health Centre",   "lat": 1.2985, "lng": 103.7831},
    {"name": "UHC-OPP",     "caption": "Opp University Health Centre","lat": 1.2991, "lng": 103.7831},
    {"name": "UTOWN",        "caption": "University Town",            "lat": 1.3047, "lng": 103.7741},
    {"name": "YIH",          "caption": "Yusof Ishak House",          "lat": 1.2971, "lng": 103.7807},
    {"name": "YIH-OPP",     "caption": "Opp Yusof Ishak House",       "lat": 1.2971, "lng": 103.7808},
]


def find_stop(query: str) -> Optional[dict]:
    q = query.strip().upper()
    for stop in STOPS:
        if stop["name"].upper() == q:
            return stop
    for stop in STOPS:
        if q in stop["name"].upper() or q in stop["caption"].upper():
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
