from typing import Optional

STOPS = [
    {"name": "AS5",          "caption": "AS 5"},
    {"name": "BG-MRT",       "caption": "Botanic Gardens MRT (PUDO)"},
    {"name": "BIZ2",         "caption": "BIZ 2"},
    {"name": "CG",           "caption": "College Green"},
    {"name": "CLB",          "caption": "Central Library"},
    {"name": "COM3",         "caption": "COM 3"},
    {"name": "HSSML-OPP",    "caption": "Opp HSSML"},
    {"name": "IT",           "caption": "Information Technology"},
    {"name": "JP-SCH-16151", "caption": "The Japanese Primary School"},
    {"name": "KR-MRT",       "caption": "Kent Ridge MRT"},
    {"name": "KR-MRT-OPP",   "caption": "Opp Kent Ridge MRT"},
    {"name": "KRB",          "caption": "Kent Ridge Bus Terminal"},
    {"name": "KV",           "caption": "Kent Vale"},
    {"name": "LT13",         "caption": "LT 13"},
    {"name": "LT13-OPP",     "caption": "Ventus"},
    {"name": "LT27",         "caption": "LT 27"},
    {"name": "MUSEUM",       "caption": "Museum"},
    {"name": "NUSS-OPP",     "caption": "Opp NUSS"},
    {"name": "OTH",          "caption": "Oei Tiong Ham Building"},
    {"name": "PGP",          "caption": "Prince George's Park"},
    {"name": "PGPR",         "caption": "Prince George's Park Foyer"},
    {"name": "RAFFLES",      "caption": "Raffles Hall"},
    {"name": "S17",          "caption": "S 17"},
    {"name": "SDE3-OPP",     "caption": "Opp SDE 3"},
    {"name": "TCOMS",        "caption": "TCOMS"},
    {"name": "TCOMS-OPP",    "caption": "Opp TCOMS"},
    {"name": "UHALL",        "caption": "University Hall"},
    {"name": "UHALL-OPP",    "caption": "Opp University Hall"},
    {"name": "UHC",          "caption": "University Health Centre"},
    {"name": "UHC-OPP",      "caption": "Opp University Health Centre"},
    {"name": "UTOWN",        "caption": "University Town"},
    {"name": "YIH",          "caption": "Yusof Ishak House"},
    {"name": "YIH-OPP",      "caption": "Opp Yusof Ishak House"},
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
