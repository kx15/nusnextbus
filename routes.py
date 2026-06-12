"""Static NUS shuttle domain data: route stop-sequences, operating schedules,
campus-entry gateways, Bukit Timah transfer hubs, and companion-stop pairs.

Hand-maintained reference data (mirrors the convention of ``stops.py``). Stop
membership is verified against the NUS NextBus API; stop order is best-estimate
geographic and may be ±1 for edge cases.
"""

# NUS campus entry points: (stop_name, MRT station address for Directions API)
_GATEWAYS = [
    ("KR-MRT", "Kent Ridge MRT Station, Singapore"),
    ("BG-MRT", "Botanic Gardens MRT Station, Singapore"),
]

# Ordered stop sequences for each NUS ISB route.
# Stop membership verified against NUS NextBus API (/ShuttleService per stop).
# Direction order is best-estimate geographic; count may be ±1 for edge cases.
_NUS_ROUTES: dict[str, list[str]] = {
    # All routes rebuilt from NUS NextBus API — every stop membership verified.
    # Stop order is best-estimate geographic; counts may be ±1 for edge cases.

    # A1 API-confirmed: AS5, BIZ2, CLB, KR-MRT, KRB, LT13, LT27, PGP,
    #                   TCOMS-OPP, UHALL, UHC-OPP, YIH
    # Route order confirmed from live timing: UHALL→UHC-OPP→YIH→CLB→KRB (2 min gap)
    # KRB is the official start; bus goes south campus first, then north back to KRB.
    "A1": [
        "KRB", "LT13", "AS5", "BIZ2", "TCOMS-OPP", "PGP",
        "KR-MRT", "LT27", "UHALL", "UHC-OPP", "YIH", "CLB", "KRB",
    ],

    # A2 confirmed route (user-verified):
    "A2": [
        "KRB", "IT", "YIH-OPP", "MUSEUM", "UHC", "UHALL-OPP",
        "S17", "KR-MRT-OPP", "PGPR", "TCOMS",
        "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "KRB",
    ],

    # D1 confirmed route (user-verified):
    "D1": [
        "COM3", "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "IT",
        "YIH-OPP", "MUSEUM", "UTOWN",
        "YIH", "CLB", "LT13", "AS5", "BIZ2", "COM3",
    ],

    # D2 confirmed route (user-verified):
    "D2": [
        "COM3", "TCOMS-OPP", "PGP", "KR-MRT",
        "LT27", "UHALL", "UHC-OPP", "MUSEUM", "UTOWN", "UHC",
        "UHALL-OPP", "S17", "KR-MRT-OPP", "PGP", "TCOMS", "COM3",
    ],

    # K confirmed route (user-verified):
    "K": [
        "PGP", "KR-MRT", "LT27", "UHALL", "UHC-OPP",
        "YIH", "CLB", "SDE3-OPP", "JP-SCH-16151", "KV",
        "MUSEUM", "UHC", "UHALL-OPP", "S17", "KR-MRT-OPP", "PGP",
    ],

    # P confirmed route (user-verified):
    "P": [
        "KV", "CG", "OTH", "BG-MRT", "KR-MRT", "UHC-OPP", "UTOWN",
    ],

    # R1 confirmed route (user-verified):
    "R1": [
        "KV", "MUSEUM", "UTOWN", "YIH", "CLB", "LT13", "AS5", "BIZ2", "PGP",
    ],

    # R2 confirmed route (user-verified):
    "R2": [
        "PGP", "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "IT",
        "YIH-OPP", "UTOWN", "RAFFLES", "KV",
    ],
}

# Stops at Bukit Timah campus — only reachable via Bus P
_BUKIT_TIMAH_STOPS = {"CG", "BG-MRT", "OTH"}
# Transfer hubs for arriving at BT campus
# KV is the natural P boarding point for OTH/CG (KV→CG→OTH on Bus P)
_BUS_P_HUBS_ARRIVAL   = ["UTOWN", "KR-MRT", "KV", "MUSEUM"]
# Transfer hubs for departing BT campus (KR-MRT first — only 2 stops from OTH on P)
_BUS_P_HUBS_DEPARTURE = ["KR-MRT", "UTOWN", "MUSEUM"]
# Companion stops — same physical location, opposite side of road.
# Used to find shorter connecting routes (e.g. D2 from KR-MRT-OPP is 3 stops to COM3
# vs 12 stops from KR-MRT itself).
_COMPANION_STOPS: dict[str, str] = {
    # Verified companion pairs (opposite sides of road, complementary bus sets)
    "KR-MRT":    "KR-MRT-OPP",  "KR-MRT-OPP": "KR-MRT",   # 22m
    "LT27":      "S17",          "S17":         "LT27",       # 27m
    "TCOMS":     "TCOMS-OPP",   "TCOMS-OPP":  "TCOMS",      # 25m
    "UHALL":     "UHALL-OPP",   "UHALL-OPP":  "UHALL",      # 22m
    "UHC":       "UHC-OPP",     "UHC-OPP":    "UHC",        # 56m
    "YIH":       "YIH-OPP",     "YIH-OPP":    "YIH",        # 29m
    "LT13":      "LT13-OPP",    "LT13-OPP":   "LT13",       # 87m
    "BIZ2":      "HSSML-OPP",   "HSSML-OPP":  "BIZ2",       # 48m
    "CLB":       "IT",           "IT":          "CLB",        # 74m
}

# Bus operating schedules.
# Keys: "mon_fri", "saturday", "mon_sat" (Mon–Sat), "sun_ph" (Sun/PH), "weekday" (legacy Mon–Sat).
# A value of None for "sun_ph" means no service on Sunday/PH.
_BUS_SCHEDULE: dict[str, dict] = {
    "A1": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:07", "23:00"),
    },
    "A2": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:00", "23:00"),
    },
    "D1": {
        "mon_fri":  ("07:15 _(term)_ / 07:20 _(vac)_", "23:00"),
        "saturday": ("07:20", "23:00"),
        "sun_ph":   ("09:10", "23:00"),
    },
    "D2": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:00", "23:00"),
    },
    "K": {
        "mon_fri":  ("07:04", "23:04"),
        "saturday": ("07:04", "19:44"),
        "sun_ph":   None,  # No service 💀
    },
    "P": {
        "mon_fri":  ("08:20", "17:25"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
    "R1": {
        "mon_fri":  ("07:40", "19:30"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
    "R2": {
        "mon_fri":  ("08:20", "19:30"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
}
