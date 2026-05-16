from typing import Optional

STOPS = [
    {"name": "KRB",          "caption": "Kent Ridge Bus Terminal"},
    {"name": "LT13",         "caption": "LT 13"},
    {"name": "AS5",          "caption": "AS 5"},
    {"name": "BIZ2",         "caption": "BIZ 2"},
    {"name": "TCOMS-OPP",    "caption": "Opp TCOMS"},
    {"name": "PGP",          "caption": "Prince George's Park"},
    {"name": "KR-MRT",       "caption": "Kent Ridge MRT"},
    {"name": "LT27",         "caption": "LT 27"},
    {"name": "UHALL",        "caption": "University Hall"},
    {"name": "UHC-OPP",      "caption": "Opp University Health Centre"},
    {"name": "YIH",          "caption": "Yusof Ishak House"},
    {"name": "CLB",          "caption": "Central Library"},
    {"name": "SDE3-OPP",     "caption": "Opp SDE 3"},
    {"name": "JP-SCH-16151", "caption": "The Japanese Primary School"},
    {"name": "KV",           "caption": "Kent Vale"},
    {"name": "MUSEUM",       "caption": "Museum"},
    {"name": "UHC",          "caption": "University Health Centre"},
    {"name": "UHALL-OPP",    "caption": "Opp University Hall"},
    {"name": "S17",          "caption": "S 17"},
    {"name": "KR-MRT-OPP",   "caption": "Opp Kent Ridge MRT"},
    {"name": "PGPR",         "caption": "Prince George's Park Foyer"},
    {"name": "COM3",         "caption": "COM 3"},
    {"name": "UTOWN",        "caption": "University Town"},
    {"name": "TCOMS",        "caption": "TCOMS"},
    {"name": "HSSML-OPP",    "caption": "Opp HSSML"},
    {"name": "NUSS-OPP",     "caption": "Opp NUSS"},
    {"name": "LT13-OPP",     "caption": "Ventus"},
    {"name": "IT",           "caption": "Information Technology"},
    {"name": "YIH-OPP",      "caption": "Opp Yusof Ishak House"},
    {"name": "RAFFLES",      "caption": "Raffles Hall"},
    {"name": "CG",           "caption": "College Green"},
    {"name": "OTH",          "caption": "Oei Tiong Ham Building"},
    {"name": "BG-MRT",       "caption": "Botanic Gardens MRT (PUDO)"},
]


def find_stop(query: str) -> Optional[dict]:
    """Return the first stop matching query by exact name, then partial name/caption."""
    q = query.strip().upper()
    for stop in STOPS:
        if stop["name"].upper() == q:
            return stop
    for stop in STOPS:
        if q in stop["name"].upper() or q in stop["caption"].upper():
            return stop
    return None
