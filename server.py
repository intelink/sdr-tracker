import json
import math
import os
import subprocess
import threading
import time
import logging
from datetime import datetime, timezone, date
from pathlib import Path

import numpy as np
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from skyfield.api import EarthSatellite, load, wgs84

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
BASE = Path(__file__).parent
KNOWN_STATIONS_FILE = BASE / "known_stations.json"

# ---------------------------------------------------------------------------
# Band label + recommended SDR bandwidth helper
# ---------------------------------------------------------------------------
_BANDS = [
    (0.135,   0.1375,  "2200m"), (0.472,  0.479,   "630m"),
    (1.8,     2.0,     "160m"),  (3.5,    4.0,     "80m"),
    (5.3515,  5.3665,  "60m"),   (7.0,    7.3,     "40m"),
    (10.1,    10.15,   "30m"),   (14.0,   14.35,   "20m"),
    (18.068,  18.168,  "17m"),   (21.0,   21.45,   "15m"),
    (24.89,   24.99,   "12m"),   (28.0,   29.7,    "10m"),
    (50.0,    54.0,    "6m"),    (70.0,   70.5,    "4m"),
    (144.0,   148.0,   "2m"),    (222.0,  225.0,   "1.25m"),
    (430.0,   440.0,   "70cm"),  (902.0,  928.0,   "33cm"),
    (1240.0,  1300.0,  "23cm"),  (2300.0, 2450.0,  "13cm"),
    (3300.0,  3500.0,  "9cm"),   (5650.0, 5925.0,  "6cm"),
    (10000.0, 10500.0, "3cm"),   (24000.0,24250.0, "1.2cm"),
]

_BW_BY_MODE = {
    "SSB":    ("2.4 kHz",  "SSB USB/LSB (2.4 kHz)"),
    "CW":     ("500 Hz",   "CW (500 Hz sau mai îngust)"),
    "FM":     ("15 kHz",   "FM Narrow (15–25 kHz)"),
    "APRS":   ("15 kHz",   "FM Narrow (15 kHz) pentru APRS/packet"),
    "BPSK":   ("3 kHz",    "USB/BPSK (3 kHz)"),
    "FSK":    ("20 kHz",   "FM/FSK (20–25 kHz)"),
    "AFSK":   ("15 kHz",   "FM (15 kHz) pentru AFSK"),
    "SSTV":   ("3 kHz",    "USB (3 kHz) pentru SSTV"),
    "DATV":   ("8 MHz",    "Wideband (necesită hardware special)"),
}

def freq_to_band(freq_mhz: float) -> str:
    for lo, hi, name in _BANDS:
        if lo <= freq_mhz <= hi:
            return name
    if freq_mhz < 0.1:
        return "VLF"
    if freq_mhz < 30:
        return "HF"
    if freq_mhz < 300:
        return "VHF"
    if freq_mhz < 3000:
        return "UHF"
    return "SHF/EHF"

def mode_to_bw(mode: str) -> tuple:
    """Returns (bandwidth_str, sdr_hint_str) for the given mode."""
    mode_up = mode.upper()
    for key, val in _BW_BY_MODE.items():
        if key in mode_up:
            return val
    if "SSB" in mode_up or "LINEAR" in mode_up or "INVERSAT" in mode_up:
        return _BW_BY_MODE["SSB"]
    if "FM" in mode_up:
        return _BW_BY_MODE["FM"]
    if "CW" in mode_up:
        return _BW_BY_MODE["CW"]
    return ("—", "Consultați documentația satelitului")

def enrich_freq(entry: dict) -> dict:
    """Add band and SDR bandwidth hint to a frequency entry."""
    f = entry.copy()
    freq = f.get("freq", 0)
    mode = f.get("mode", "")
    f["band"] = freq_to_band(freq)
    bw, hint = mode_to_bw(mode)
    f["sdr_bw"] = bw
    f["sdr_hint"] = hint
    return f

# ---------------------------------------------------------------------------
# New stations tracking (persistent JSON)
# ---------------------------------------------------------------------------
_known_stations: dict = {}   # url -> {"first_seen": ISO date str}
_known_lock = threading.Lock()

def _load_known_stations():
    global _known_stations
    try:
        if KNOWN_STATIONS_FILE.exists():
            _known_stations = json.loads(KNOWN_STATIONS_FILE.read_text())
    except Exception as e:
        log.warning(f"Could not load known_stations.json: {e}")
        _known_stations = {}

def _save_known_stations():
    try:
        KNOWN_STATIONS_FILE.write_text(json.dumps(_known_stations, indent=2))
    except Exception as e:
        log.warning(f"Could not save known_stations.json: {e}")

def _mark_known(stations: list) -> list:
    """Tag each station with is_new (first seen today) and first_seen date."""
    today = date.today().isoformat()
    changed = False
    with _known_lock:
        for st in stations:
            url = st["url"]
            if url not in _known_stations:
                _known_stations[url] = {"first_seen": today}
                changed = True
            st["first_seen"] = _known_stations[url]["first_seen"]
            st["is_new"] = (_known_stations[url]["first_seen"] == today)
        if changed:
            _save_known_stations()
    return stations

def count_new_today(stations: list) -> int:
    today = date.today().isoformat()
    return sum(1 for s in stations if s.get("first_seen") == today)

# ---------------------------------------------------------------------------
# Country capital coordinates (fallback for websdr.org scrape)
# ---------------------------------------------------------------------------
COUNTRY_COORDS = {
    "US": (38.9, -77.0), "USA": (38.9, -77.0), "United States": (38.9, -77.0),
    "UK": (51.5, -0.1), "GB": (51.5, -0.1), "United Kingdom": (51.5, -0.1), "England": (51.5, -0.1),
    "DE": (52.5, 13.4), "Germany": (52.5, 13.4), "Deutschland": (52.5, 13.4),
    "NL": (52.4, 4.9), "Netherlands": (52.4, 4.9), "Holland": (52.4, 4.9),
    "FR": (48.9, 2.3), "France": (48.9, 2.3),
    "IT": (41.9, 12.5), "Italy": (41.9, 12.5),
    "ES": (40.4, -3.7), "Spain": (40.4, -3.7),
    "PL": (52.2, 21.0), "Poland": (52.2, 21.0),
    "CZ": (50.1, 14.4), "Czech": (50.1, 14.4),
    "SK": (48.1, 17.1), "Slovakia": (48.1, 17.1),
    "HU": (47.5, 19.0), "Hungary": (47.5, 19.0),
    "RO": (44.4, 26.1), "Romania": (44.4, 26.1),
    "BG": (42.7, 23.3), "Bulgaria": (42.7, 23.3),
    "RU": (55.8, 37.6), "Russia": (55.8, 37.6),
    "UA": (50.4, 30.5), "Ukraine": (50.4, 30.5),
    "SE": (59.3, 18.1), "Sweden": (59.3, 18.1),
    "NO": (59.9, 10.7), "Norway": (59.9, 10.7),
    "FI": (60.2, 25.0), "Finland": (60.2, 25.0),
    "DK": (55.7, 12.6), "Denmark": (55.7, 12.6),
    "BE": (50.8, 4.4), "Belgium": (50.8, 4.4),
    "AT": (48.2, 16.4), "Austria": (48.2, 16.4),
    "CH": (46.9, 7.4), "Switzerland": (46.9, 7.4),
    "PT": (38.7, -9.1), "Portugal": (38.7, -9.1),
    "GR": (37.9, 23.7), "Greece": (37.9, 23.7),
    "JP": (35.7, 139.7), "Japan": (35.7, 139.7),
    "CN": (39.9, 116.4), "China": (39.9, 116.4),
    "AU": (-35.3, 149.1), "Australia": (-35.3, 149.1),
    "NZ": (-41.3, 174.8), "New Zealand": (-41.3, 174.8),
    "CA": (45.4, -75.7), "Canada": (45.4, -75.7),
    "BR": (-15.8, -47.9), "Brazil": (-15.8, -47.9),
    "ZA": (-25.7, 28.2), "South Africa": (-25.7, 28.2),
    "IN": (28.6, 77.2), "India": (28.6, 77.2),
    "KR": (37.6, 127.0), "South Korea": (37.6, 127.0),
    "TR": (39.9, 32.9), "Turkey": (39.9, 32.9),
    "IL": (31.8, 35.2), "Israel": (31.8, 35.2),
    "IR": (35.7, 51.4), "Iran": (35.7, 51.4),
    "MX": (19.4, -99.1), "Mexico": (19.4, -99.1),
    "AR": (-34.6, -58.4), "Argentina": (-34.6, -58.4),
    "CL": (-33.5, -70.7), "Chile": (-33.5, -70.7),
    "LT": (54.7, 25.3), "Lithuania": (54.7, 25.3),
    "LV": (56.9, 24.1), "Latvia": (56.9, 24.1),
    "EE": (59.4, 24.7), "Estonia": (59.4, 24.7),
    "HR": (45.8, 16.0), "Croatia": (45.8, 16.0),
    "RS": (44.8, 20.5), "Serbia": (44.8, 20.5),
    "SI": (46.1, 14.5), "Slovenia": (46.1, 14.5),
    "BA": (43.8, 18.4), "Bosnia": (43.8, 18.4),
    "MK": (42.0, 21.4), "Macedonia": (42.0, 21.4),
    "ME": (42.4, 19.3), "Montenegro": (42.4, 19.3),
    "AL": (41.3, 19.8), "Albania": (41.3, 19.8),
    "MD": (47.0, 28.9), "Moldova": (47.0, 28.9),
    "BY": (53.9, 27.6), "Belarus": (53.9, 27.6),
    "GE": (41.7, 44.8), "Georgia": (41.7, 44.8),
    "AZ": (40.4, 49.9), "Azerbaijan": (40.4, 49.9),
    "AM": (40.2, 44.5), "Armenia": (40.2, 44.5),
    "KZ": (51.2, 71.4), "Kazakhstan": (51.2, 71.4),
    "TH": (13.8, 100.5), "Thailand": (13.8, 100.5),
    "MY": (3.1, 101.7), "Malaysia": (3.1, 101.7),
    "SG": (1.3, 103.8), "Singapore": (1.3, 103.8),
    "ID": (-6.2, 106.8), "Indonesia": (-6.2, 106.8),
    "PH": (14.6, 121.0), "Philippines": (14.6, 121.0),
    "VN": (21.0, 105.8), "Vietnam": (21.0, 105.8),
    "TW": (25.0, 121.5), "Taiwan": (25.0, 121.5),
    "HK": (22.3, 114.2), "Hong Kong": (22.3, 114.2),
    "PK": (33.7, 73.1), "Pakistan": (33.7, 73.1),
    "BD": (23.7, 90.4), "Bangladesh": (23.7, 90.4),
    "LK": (6.9, 79.9), "Sri Lanka": (6.9, 79.9),
    "EG": (30.0, 31.2), "Egypt": (30.0, 31.2),
    "NG": (9.1, 7.4), "Nigeria": (9.1, 7.4),
    "KE": (-1.3, 36.8), "Kenya": (-1.3, 36.8),
    "MA": (34.0, -6.8), "Morocco": (34.0, -6.8),
    "DZ": (36.7, 3.0), "Algeria": (36.7, 3.0),
    "TN": (36.8, 10.2), "Tunisia": (36.8, 10.2),
    "SA": (24.7, 46.7), "Saudi Arabia": (24.7, 46.7),
    "AE": (24.5, 54.4), "UAE": (24.5, 54.4),
}

DEFAULT_COORD = (0.0, 0.0)

# ---------------------------------------------------------------------------
# Satellite database
# ---------------------------------------------------------------------------
SAT_DB = {
    # ── Populare / Recomandate ───────────────────────────────────────────────
    "ISS": {
        "norad": 25544, "name": "ISS — Stația Spațială Internațională",
        "agency": "NASA/ESA/JAXA/Roscosmos", "orbit_type": "LEO ~408 km",
        "description": "Prima stație spațială internațională cu echipament radio amator ARISS. Organizează QSO-uri cu școli.",
        "downlink": [
            {"freq": 145.800, "mode": "FM", "bandwidth": "15 kHz", "info": "Voce FM — contact astronauți"},
            {"freq": 145.825, "mode": "APRS/AX.25 1200bd", "bandwidth": "15 kHz", "info": "Telemetrie, mesaje APRS"},
        ],
        "uplink": [{"freq": 144.490, "mode": "APRS", "info": "APRS uplink"}, {"freq": 145.200, "mode": "FM", "info": "Voce FM uplink"}],
        "beacon": [],
        "notes": "Repetor VHF/UHF cross-band activ intermitent. Verificați ARISS pentru programul QSO.",
    },
    "SO-50": {
        "norad": 27607, "name": "SO-50 (SaudiSat-1C)",
        "agency": "SAUDISAT", "orbit_type": "LEO ~670 km",
        "description": "Unul din cei mai vechi sateliți FM amatori, activ din 2002. Ideal pentru primul QSO.",
        "downlink": [{"freq": 436.795, "mode": "FM", "bandwidth": "15 kHz", "info": "Downlink principal"}],
        "uplink": [{"freq": 145.850, "mode": "FM", "info": "Ton 67 Hz CTCSS obligatoriu"}],
        "beacon": [],
        "notes": "Necesită ton 67 Hz. Foarte popular pentru primele QSO prin satelit.",
    },
    "AO-91": {
        "norad": 43017, "name": "AO-91 / RadFxSat (Fox-1B)",
        "agency": "AMSAT", "orbit_type": "LEO ~520 km",
        "description": "Satelit Fox-1B operat de AMSAT cu transponder FM. Conține experiment de radiații.",
        "downlink": [{"freq": 145.960, "mode": "FM", "bandwidth": "15 kHz", "info": "Downlink principal"}],
        "uplink": [{"freq": 435.250, "mode": "FM", "info": "Ton 67 Hz CTCSS necesar"}],
        "beacon": [{"freq": 145.960, "mode": "DUV 200 bps", "info": "Telemetrie sub voce, permanent"}],
        "notes": "Full duplex posibil. Verificați AMSAT pentru starea transponderului.",
    },
    "AO-85": {
        "norad": 40967, "name": "AO-85 / Fox-1A",
        "agency": "AMSAT", "orbit_type": "LEO ~670 km",
        "description": "Primul satelit din seria Fox-1. Transponder FM și telemetrie DUV.",
        "downlink": [{"freq": 145.980, "mode": "FM", "bandwidth": "15 kHz", "info": "Downlink FM principal"}],
        "uplink": [{"freq": 435.180, "mode": "FM", "info": "Ton 67.0 Hz CTCSS necesar"}],
        "beacon": [{"freq": 145.980, "mode": "DUV 200 bps", "info": "Telemetrie sub voce"}],
        "notes": "Activ. Ton CTCSS 67.0 Hz pe uplink.",
    },
    "AO-95": {
        "norad": 43770, "name": "AO-95 / Fox-1Cliff",
        "agency": "AMSAT", "orbit_type": "LEO ~500 km",
        "description": "Satelit Fox-1Cliff cu transponder FM și experiment University of Iowa.",
        "downlink": [{"freq": 145.920, "mode": "FM", "bandwidth": "15 kHz", "info": "Downlink FM"}],
        "uplink": [{"freq": 435.350, "mode": "FM", "info": "Ton 67.0 Hz CTCSS necesar"}],
        "beacon": [{"freq": 145.920, "mode": "DUV 200 bps", "info": "Telemetrie DUV"}],
        "notes": "Ton CTCSS 67.0 Hz pe uplink. Verificați AMSAT pentru stare.",
    },
    "RS-44": {
        "norad": 44909, "name": "RS-44 (DOSAAF-85)",
        "agency": "Roscosmos / DOSAAF", "orbit_type": "LEO ~1000 km",
        "description": "Satelit rusesc cu transponder linear SSB/CW. Orbita mai înaltă oferă ferestre de contact mai lungi.",
        "downlink": [{"freq": 435.610, "mode": "SSB/CW inversat", "bandwidth": "100 kHz", "info": "435.610–435.640 MHz"}],
        "uplink": [{"freq": 145.935, "mode": "SSB/CW", "info": "145.935–145.965 MHz (inversat față de downlink)"}],
        "beacon": [{"freq": 435.605, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Transponder linear inversat. Putere max 5W pe uplink. Fereastră ~15 min.",
    },
    "FO-29": {
        "norad": 24278, "name": "FO-29 (Fuji-OSCAR 29)",
        "agency": "JARL / Japan", "orbit_type": "LEO ~800 km",
        "description": "Satelit japonez cu transponder linear SSB/CW. Activ intermitent.",
        "downlink": [{"freq": 435.800, "mode": "SSB/CW", "bandwidth": "100 kHz", "info": "435.800–435.900 MHz"}],
        "uplink": [{"freq": 145.900, "mode": "SSB/CW", "info": "145.900–145.980 MHz"}],
        "beacon": [{"freq": 435.795, "mode": "CW/BPSK", "info": "Beacon digital"}],
        "notes": "Activ în mod JA (digital) și FM alternativ. Verificați JARL pentru program.",
    },
    "AO-73": {
        "norad": 39444, "name": "AO-73 (FUNcube-1)",
        "agency": "AMSAT-UK", "orbit_type": "LEO ~580 km",
        "description": "Satelit educațional cu transponder linear și telemetrie BPSK. Proiect FUNcube.",
        "downlink": [
            {"freq": 145.935, "mode": "BPSK 1200bd", "bandwidth": "5 kHz", "info": "Telemetrie în eclipsă"},
            {"freq": 145.950, "mode": "SSB/CW", "bandwidth": "30 kHz", "info": "Transponder în lumina soarelui"},
        ],
        "uplink": [{"freq": 435.150, "mode": "SSB/CW", "info": "Uplink linear"}],
        "beacon": [{"freq": 145.935, "mode": "BPSK", "info": "Telemetrie continuă"}],
        "notes": "Comută automat transponder ↔ telemetrie în funcție de lumina solară.",
    },
    "JO-97": {
        "norad": 43803, "name": "JO-97 / JY1SAT",
        "agency": "AMSAT / Royal Sci. Soc. Jordan", "orbit_type": "LEO ~575 km",
        "description": "Satelit iordanian cu transponder FUNcube linear și cameră video.",
        "downlink": [
            {"freq": 145.840, "mode": "BPSK 1200bd", "bandwidth": "5 kHz", "info": "Telemetrie FUNcube"},
            {"freq": 145.855, "mode": "FM", "bandwidth": "15 kHz", "info": "Voce FM (mod repetor)"},
        ],
        "uplink": [{"freq": 435.100, "mode": "SSB/CW", "info": "Transponder linear uplink"}],
        "beacon": [{"freq": 145.840, "mode": "BPSK", "info": "Telemetrie permanentă"}],
        "notes": "Are și cameră foto la bord. Transponder activ conform programului AMSAT.",
    },
    "IO-117": {
        "norad": 53109, "name": "IO-117 / GreenCube",
        "agency": "AMSAT-Italia / Univ. Roma", "orbit_type": "LEO ~600 km",
        "description": "Satelit italian cu digipeater digital pentru voce (Codec2) și APRS. Foarte activ.",
        "downlink": [{"freq": 435.310, "mode": "4k8 FSK / Codec2 1400bps", "bandwidth": "20 kHz", "info": "Voce digitală + APRS digipeater"}],
        "uplink": [{"freq": 435.310, "mode": "4k8 FSK", "info": "Același canal — half duplex"}],
        "beacon": [{"freq": 435.310, "mode": "FSK", "info": "Telemetrie în preamble"}],
        "notes": "Necesită software DR-Linux/GreenCube. Cel mai popular digipeater digital LEO în 2024.",
    },
    "XW-3": {
        "norad": 50466, "name": "XW-3 / CAS-9",
        "agency": "CAMSAT China", "orbit_type": "LEO ~500 km",
        "description": "Satelit CAMSAT cu transponder linear SSB/CW și repetor FM.",
        "downlink": [
            {"freq": 145.870, "mode": "SSB/CW inversat", "bandwidth": "30 kHz", "info": "145.870–145.895 MHz linear"},
            {"freq": 145.900, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"},
        ],
        "uplink": [
            {"freq": 435.430, "mode": "SSB/CW", "info": "435.430–435.455 MHz linear"},
            {"freq": 435.485, "mode": "FM", "info": "Repetor FM uplink"},
        ],
        "beacon": [{"freq": 145.860, "mode": "CW", "info": "Beacon CW"}, {"freq": 435.400, "mode": "CW", "info": "Beacon UHF"}],
        "notes": "Lansat în 2021. Transponder activ regulat.",
    },
    # ── Sateliți FM clasici ──────────────────────────────────────────────────
    "AO-7": {
        "norad": 7530, "name": "AO-7 (OSCAR 7)",
        "agency": "AMSAT", "orbit_type": "LEO ~1460 km",
        "description": "Lansat în 1974, cel mai vechi satelit amator funcțional. Funcționează fără baterie, doar în lumina soarelui.",
        "downlink": [
            {"freq": 29.502, "mode": "SSB/CW", "bandwidth": "100 kHz", "info": "Mod A — 29.400–29.500 MHz"},
            {"freq": 145.975, "mode": "SSB/CW", "bandwidth": "100 kHz", "info": "Mod B — 145.975–146.000 MHz"},
        ],
        "uplink": [
            {"freq": 145.875, "mode": "SSB/CW", "info": "Mod A — 145.850–145.950 MHz"},
            {"freq": 432.125, "mode": "SSB/CW", "info": "Mod B — 432.125 MHz"},
        ],
        "beacon": [{"freq": 29.502, "mode": "CW", "info": "Beacon mod A"}, {"freq": 145.975, "mode": "CW", "info": "Beacon mod B"}],
        "notes": "Funcționează doar în lumina soarelui (bateria moartă din 1981). Alternează între Mod A și Mod B.",
    },
    "LILACSAT-2": {
        "norad": 40908, "name": "LILACSAT-2",
        "agency": "Harbin Inst. Tech.", "orbit_type": "LEO ~500 km",
        "description": "Satelit universitar chinezesc cu FM și transponder linear. Cameră HD la bord.",
        "downlink": [
            {"freq": 437.200, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"},
            {"freq": 437.200, "mode": "BPSK 9600bd", "bandwidth": "20 kHz", "info": "Telemetrie digitală"},
        ],
        "uplink": [{"freq": 144.350, "mode": "FM/SSB", "info": "Uplink VHF"}],
        "beacon": [{"freq": 437.225, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Are cameră HD la bord.",
    },
    "PO-101": {
        "norad": 43678, "name": "PO-101 / DIWATA-2B",
        "agency": "Univ. Tohoku + Filipine", "orbit_type": "LEO ~595 km",
        "description": "Microsatelit filipinez cu transponder FM și experimente meteorologice.",
        "downlink": [{"freq": 145.900, "mode": "FM", "bandwidth": "15 kHz", "info": "Downlink FM"}],
        "uplink": [{"freq": 437.500, "mode": "FM", "info": "Ton 141.3 Hz CTCSS necesar"}],
        "beacon": [{"freq": 145.900, "mode": "AFSK 1200bd", "bandwidth": "5 kHz", "info": "Telemetrie AX.25"}],
        "notes": "Necesită ton CTCSS 141.3 Hz pe uplink.",
    },
    "IO-86": {
        "norad": 40931, "name": "IO-86 / LAPAN-A2",
        "agency": "LAPAN Indonesia", "orbit_type": "LEO ~650 km",
        "description": "Satelit indonezian cu repetor FM și cameră de supraveghere maritimă. Transmite SSTV.",
        "downlink": [
            {"freq": 145.880, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM voce"},
            {"freq": 437.325, "mode": "SSTV", "bandwidth": "15 kHz", "info": "Imagini SSTV (Robot36)"},
        ],
        "uplink": [{"freq": 435.880, "mode": "FM", "info": "Repetor FM uplink"}],
        "beacon": [{"freq": 145.880, "mode": "AFSK 9600bd", "info": "Telemetrie digitală"}],
        "notes": "Transmite imagini SSTV Robot36 pe 437.325 MHz. Repetor FM activ.",
    },
    "NO-44": {
        "norad": 26931, "name": "NO-44 / PCSAT",
        "agency": "USNA / AMSAT", "orbit_type": "LEO ~800 km",
        "description": "Satelit Naval Academy SUA. Digipeater APRS pentru urmărire nave.",
        "downlink": [{"freq": 145.825, "mode": "AFSK 1200bd AX.25", "bandwidth": "15 kHz", "info": "APRS downlink"}],
        "uplink": [{"freq": 145.825, "mode": "APRS 1200bd", "info": "APRS uplink"}],
        "beacon": [{"freq": 145.825, "mode": "APRS", "info": "Telemetrie APRS"}],
        "notes": "Digipeater APRS. Activ intermitent.",
    },
    # ── Tevel 2 — constelaţie israeliană 2024 ───────────────────────────────
    "TEVEL2-1": {
        "norad": 63217, "name": "TEVEL2-1 (Israel)",
        "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — 9 sateliți educaționali israelieni cu repetor FM. Lansat 2024.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie AX.25"}],
        "notes": "Repetor FM activ. Ton 100 Hz CTCSS pe uplink.",
    },
    "TEVEL2-2": {"norad": 63219, "name": "TEVEL2-2", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie AX.25"}], "notes": "Ton 100 Hz pe uplink."},
    "TEVEL2-3": {"norad": 63218, "name": "TEVEL2-3", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": "Ton 100 Hz pe uplink."},
    "TEVEL2-4": {"norad": 63213, "name": "TEVEL2-4", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    "TEVEL2-5": {"norad": 63214, "name": "TEVEL2-5", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    "TEVEL2-6": {"norad": 63215, "name": "TEVEL2-6", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    "TEVEL2-7": {"norad": 63238, "name": "TEVEL2-7", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    "TEVEL2-8": {"norad": 63239, "name": "TEVEL2-8", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    "TEVEL2-9": {"norad": 63237, "name": "TEVEL2-9", "agency": "Israel — Edu.", "orbit_type": "LEO ~550 km",
        "description": "Constelaţie TEVEL2 — repetor FM educațional.",
        "downlink": [{"freq": 436.400, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.970, "mode": "FM", "info": "Ton 100 Hz CTCSS"}],
        "beacon": [{"freq": 436.400, "mode": "AFSK 1200bd", "info": "Telemetrie"}], "notes": ""},
    # ── Beacon / telemetrie ──────────────────────────────────────────────────
    "SALSAT": {
        "norad": 46495, "name": "SALSAT (TU Berlin)",
        "agency": "TU Berlin", "orbit_type": "LEO ~560 km",
        "description": "Satelit german de analiză spectrum radio. Mapează interferenţele RF globale.",
        "downlink": [
            {"freq": 435.750, "mode": "CW + FM", "bandwidth": "15 kHz", "info": "Beacon CW + telemetrie FM"},
            {"freq": 2401.000, "mode": "BPSK", "bandwidth": "—", "info": "Date spectru S-band"},
        ],
        "uplink": [], "beacon": [{"freq": 435.750, "mode": "CW", "info": "Beacon permanent"}],
        "notes": "Misiune ştiinţifică. Nu are transponder vocal.",
    },
    "SONATE-2": {
        "norad": 59112, "name": "SONATE-2 (JMU Würzburg)",
        "agency": "Univ. Würzburg", "orbit_type": "LEO ~550 km",
        "description": "Satelit german de AI on-board și experiment propulsie. Beacon amator.",
        "downlink": [{"freq": 437.175, "mode": "FSK 9600bd", "bandwidth": "20 kHz", "info": "Telemetrie + ADS-B"}],
        "uplink": [], "beacon": [{"freq": 437.175, "mode": "FSK", "info": "Beacon periodic"}],
        "notes": "Lansat 2023. Experiment AI on-board și propulsie.",
    },
    "MESAT1": {
        "norad": 60209, "name": "MESAT1 (Mongolia)",
        "agency": "Mongolia", "orbit_type": "LEO ~550 km",
        "description": "Primul satelit amator mongol. Beacon pe UHF.",
        "downlink": [{"freq": 437.050, "mode": "CW + FSK 9600bd", "bandwidth": "15 kHz", "info": "Beacon + telemetrie"}],
        "uplink": [], "beacon": [{"freq": 437.050, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Primul satelit mongol. Lansat 2023.",
    },
    "HADES-ICM": {
        "norad": 63492, "name": "HADES-ICM (Spania)",
        "agency": "AMSAT-EA / Spania", "orbit_type": "LEO ~550 km",
        "description": "Satelit spaniol cu transponder SSB linear. Lansat 2024.",
        "downlink": [{"freq": 145.925, "mode": "SSB/CW inversat", "bandwidth": "40 kHz", "info": "145.925–145.875 MHz"}],
        "uplink": [{"freq": 435.950, "mode": "SSB/CW", "info": "435.950–436.100 MHz"}],
        "beacon": [{"freq": 437.350, "mode": "CW/FSK", "info": "Beacon CW"}],
        "notes": "Transponder linear. Lansat 2024.",
    },
    "ASRTU-1": {
        "norad": 61781, "name": "AO-123 / ASRTU-1",
        "agency": "China / universități", "orbit_type": "LEO ~550 km",
        "description": "Satelit educațional chinezesc cu repetor FM și experiment CW.",
        "downlink": [
            {"freq": 145.855, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"},
            {"freq": 435.355, "mode": "CW", "bandwidth": "—", "info": "Beacon CW"},
        ],
        "uplink": [{"freq": 435.180, "mode": "FM", "info": "Repetor FM uplink"}],
        "beacon": [{"freq": 435.355, "mode": "CW", "info": "Beacon permanent"}],
        "notes": "Desemnat OSCAR-123. Lansat 2024.",
    },
    "KNACKSAT-2": {
        "norad": 67683, "name": "KNACKSAT-2 (Thailand)",
        "agency": "KMITL Thailand", "orbit_type": "LEO ~550 km",
        "description": "Satelit universitar tailandez cu repetor FM și experiment AIS.",
        "downlink": [{"freq": 435.500, "mode": "FM + FSK", "bandwidth": "15 kHz", "info": "Repetor FM + telemetrie"}],
        "uplink": [{"freq": 145.980, "mode": "FM", "info": "Repetor FM uplink"}],
        "beacon": [{"freq": 435.500, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Lansat 2025. Cel mai recent satelit amator tailandez.",
    },
    # ── Geostaţionar ────────────────────────────────────────────────────────
    "QO-100": {
        "norad": 43700, "name": "QO-100 / Es'hail-2 (GEO 26°E)",
        "agency": "Es'hailSat / AMSAT-DL", "orbit_type": "GEO 35786 km — 26°E",
        "description": "Primul satelit amator geostaționar! Acoperă Europa, Africa, Orientul Mijlociu, Asia de Vest. Necesită antenă parabolică și downconverter 10 GHz.",
        "downlink": [
            {"freq": 10489.750, "mode": "SSB/CW (NB)", "bandwidth": "250 kHz", "info": "Narrowband — 10489.550–10489.800 MHz"},
            {"freq": 10491.000, "mode": "DATV (WB)", "bandwidth": "8 MHz", "info": "Wideband DATV — 10491–10499 MHz"},
        ],
        "uplink": [
            {"freq": 2400.175, "mode": "SSB/CW (NB)", "bandwidth": "250 kHz", "info": "Narrowband — 2400.050–2400.300 MHz"},
            {"freq": 2401.500, "mode": "DATV (WB)", "bandwidth": "8 MHz", "info": "Wideband — 2401.5–2409.5 MHz"},
        ],
        "beacon": [
            {"freq": 10489.550, "mode": "CW", "info": "Beacon lower edge NB"},
            {"freq": 10489.800, "mode": "CW", "info": "Beacon upper edge NB"},
        ],
        "notes": "GEO — poziție fixă pe cer! Necesită parabolă 60cm+ și downconverter LNB 10 GHz. Vizibil din România la elevație ~25° spre sud-vest.",
    },
    # ── Sateliți mai vechi / istorici ───────────────────────────────────────
    "UO-11": {
        "norad": 14781, "name": "UO-11 / UOSAT-2",
        "agency": "Univ. Surrey UK", "orbit_type": "LEO ~685 km",
        "description": "Lansat în 1984. Unul din primele microsatelite. Transmite periodic date telemetrie.",
        "downlink": [{"freq": 145.825, "mode": "AFSK 1200bd", "bandwidth": "15 kHz", "info": "Telemetrie digitală"}],
        "uplink": [], "beacon": [{"freq": 145.825, "mode": "AFSK", "info": "Beacon periodic"}],
        "notes": "40+ ani pe orbită. Transmite intermitent.",
    },
    "LO-19": {
        "norad": 20442, "name": "LO-19 / LUSAT",
        "agency": "AMSAT-LU / Argentina", "orbit_type": "LEO ~795 km",
        "description": "Satelit argentinian din 1990. Beacon CW activ intermitent.",
        "downlink": [{"freq": 437.125, "mode": "CW + BPSK 1200bd", "bandwidth": "—", "info": "Beacon + telemetrie"}],
        "uplink": [], "beacon": [{"freq": 437.125, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Beacon intermitent. Nu are transponder vocal activ.",
    },
    "AO-27": {
        "norad": 22825, "name": "AO-27 / EYESAT-A",
        "agency": "AMSAT-NA", "orbit_type": "LEO ~820 km",
        "description": "Satelit FM amator din 1993. Repetor activ în unele ferestre.",
        "downlink": [{"freq": 436.795, "mode": "FM", "bandwidth": "15 kHz", "info": "Repetor FM"}],
        "uplink": [{"freq": 145.850, "mode": "FM", "info": "Ton 67 Hz CTCSS"}],
        "beacon": [], "notes": "Activ intermitent. Aceeași frecvență downlink cu SO-50.",
    },
    "STRAND-1": {
        "norad": 39090, "name": "STRAND-1 (Surrey UK)",
        "agency": "SSTL / Surrey UK", "orbit_type": "LEO ~785 km",
        "description": "Primul satelit controlat parțial de smartphone. Beacon FSK.",
        "downlink": [{"freq": 437.568, "mode": "FSK 9600bd", "bandwidth": "20 kHz", "info": "Telemetrie + beacon"}],
        "uplink": [], "beacon": [{"freq": 437.568, "mode": "FSK", "info": "Beacon periodic"}],
        "notes": "Experiment cu smartphone la bord (Nexus One).",
    },
    "UNISAT-6": {
        "norad": 40012, "name": "UNISAT-6 (Italia)",
        "agency": "GAUSS / Roma", "orbit_type": "LEO ~600 km",
        "description": "Microsatelit italian cu FM și transmisie SSTV.",
        "downlink": [
            {"freq": 437.175, "mode": "FM", "bandwidth": "15 kHz", "info": "Voce FM + SSTV"},
            {"freq": 437.175, "mode": "FSK 9600bd", "bandwidth": "20 kHz", "info": "Telemetrie digitală"},
        ],
        "uplink": [{"freq": 145.980, "mode": "FM", "info": "Uplink FM"}],
        "beacon": [{"freq": 437.175, "mode": "CW", "info": "Beacon CW"}],
        "notes": "Transmite SSTV periodic.",
    },
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_stations_cache = {"data": None, "ts": 0, "ttl": 300}   # 5 min
_tle_cache = {}  # norad -> {"tle_lines": [...], "ts": float}
_tle_ttl = 6 * 3600  # 6 hours

_ts_skyfield = load.timescale()

# ---------------------------------------------------------------------------
# TLE fetch
# ---------------------------------------------------------------------------

def _fetch_tle_lines_for_norad(norad_id: int):
    """Fetch TLE from SatNOGS (primary) with Celestrak fallback."""
    # Primary: SatNOGS DB API (reliably accessible)
    try:
        r = requests.get(
            f"https://db.satnogs.org/api/tle/?format=json&norad_cat_id={norad_id}",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list) and data[0].get("tle1"):
            d = data[0]
            name = d.get("tle0", f"NORAD-{norad_id}").lstrip("0 ").strip()
            return name, d["tle1"], d["tle2"]
    except Exception as e:
        log.warning(f"SatNOGS TLE fetch failed for NORAD {norad_id}: {e}")

    # Fallback: Celestrak
    try:
        r = requests.get(
            f"https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=tle",
            timeout=8,
        )
        r.raise_for_status()
        lines = [l.strip() for l in r.text.strip().splitlines() if l.strip()]
        if len(lines) >= 3:
            return lines[0], lines[1], lines[2]
        if len(lines) == 2 and lines[0].startswith("1 "):
            return f"NORAD-{norad_id}", lines[0], lines[1]
    except Exception as e:
        log.warning(f"Celestrak TLE fetch failed for NORAD {norad_id}: {e}")

    return None


def get_tle(sat_id: str):
    """Return (name, line1, line2) for sat_id, using cache."""
    info = SAT_DB.get(sat_id)
    if not info:
        return None
    norad = info["norad"]
    cached = _tle_cache.get(norad)
    if cached and (time.time() - cached["ts"]) < _tle_ttl:
        return cached["tle_lines"]
    result = _fetch_tle_lines_for_norad(norad)
    if result:
        _tle_cache[norad] = {"tle_lines": result, "ts": time.time()}
        return result
    # Return stale if available
    if cached:
        return cached["tle_lines"]
    return None


def _prefetch_all_tle():
    """Background prefetch TLEs for all satellites."""
    for sat_id in SAT_DB:
        get_tle(sat_id)
        time.sleep(0.5)
    log.info("TLE prefetch complete.")


# ---------------------------------------------------------------------------
# SDR Station scraping
# ---------------------------------------------------------------------------

def _get_country_coords(text: str):
    """Try to find country coords from text."""
    for key, coords in COUNTRY_COORDS.items():
        if key.lower() in text.lower():
            return coords
    return None


def _infer_freqs(label: str, sdr_type: str) -> list:
    """Infer frequency ranges from station label and SDR platform type.

    Platform defaults (when label has no explicit range):
      KiwiSDR   → HF only (hardware-limited 0–32 MHz)
      OpenWebRX → VHF + UHF (RTL-SDR dongles; HF added only if label says so)
      WebSDR    → HF (historically HF-focused installations)
      other     → VHF + UHF (most modern SDR dongles cover these bands)
    """
    import re
    lbl = label.lower()
    freqs = []

    # Explicit numeric MHz-MHz range: "0.5-30 MHz", "118-174MHz"
    for lo, hi in re.findall(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*mhz', lbl):
        freqs.append({"low": float(lo), "high": float(hi)})

    # kHz-MHz cross-unit range: "100kHz - 30MHz" → treat as HF 0-30 MHz
    if not freqs:
        for lo_khz, hi_mhz in re.findall(r'(\d+(?:\.\d+)?)\s*khz\s*[-–]\s*(\d+(?:\.\d+)?)\s*mhz', lbl):
            freqs.append({"low": 0.0, "high": float(hi_mhz)})

    if not freqs:
        vhf = any(x in lbl for x in ["vhf", "2m", " 144", " 145", " 146", "2m/70"])
        uhf = any(x in lbl for x in ["uhf", "70cm", " 430", " 431", " 432", " 435",
                                      " 436", " 437", " 438", " 439", "70 cm"])
        hf_explicit = any(x in lbl for x in ["hf", "shortwave", "sw ", "lf", "mf",
                                              "0.5-30", "0-30", "0-32", "longwave",
                                              "am broadcast"])

        if sdr_type == "KiwiSDR":
            # KiwiSDR is hardware-limited to HF
            freqs.append({"low": 0.0, "high": 32.0})

        elif sdr_type == "OpenWebRX":
            # OpenWebRX is primarily used with RTL-SDR → VHF/UHF by default
            freqs.append({"low": 118.0, "high": 174.0})
            freqs.append({"low": 400.0, "high": 480.0})
            if hf_explicit:
                freqs.append({"low": 0.0, "high": 32.0})

        elif sdr_type == "WebSDR":
            # WebSDR is historically HF-focused; add VHF/UHF only if label says so
            if vhf:
                freqs.append({"low": 118.0, "high": 174.0})
            if uhf:
                freqs.append({"low": 400.0, "high": 480.0})
            if hf_explicit or (not vhf and not uhf):
                freqs.append({"low": 0.0, "high": 32.0})

        else:
            # Unknown platform: use label keywords; default to VHF+UHF if nothing found
            if vhf:
                freqs.append({"low": 118.0, "high": 174.0})
            if uhf:
                freqs.append({"low": 400.0, "high": 480.0})
            if hf_explicit:
                freqs.append({"low": 0.0, "high": 32.0})
            if not vhf and not uhf and not hf_explicit:
                freqs.append({"low": 118.0, "high": 174.0})
                freqs.append({"low": 400.0, "high": 480.0})

    return freqs or [{"low": 0.0, "high": 32.0}]


def _fetch_receiverbook() -> list:
    """Fetch SDR stations from receiverbook.de (1400+ real stations with GPS coords).
    Uses simple non-backtracking regex to avoid catastrophic slowdowns on large HTML."""
    import re
    stations = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        r = requests.get("https://www.receiverbook.de/map", timeout=25, headers=headers)
        r.raise_for_status()
        html = r.text

        # ── Step 1: extract all coordinates [lon, lat] in order ──────────────
        # Simple non-backtracking pattern
        coords_raw = re.findall(r'"coordinates":\[(-?[0-9.]+),(-?[0-9.]+)\]', html)

        # ── Step 2: extract all receiver objects in order ─────────────────────
        # Pattern: "url":"VALUE","type":"VALUE" — no backtracking, no .*
        # The label comes before url but may contain HTML; we grab url+type which are safe
        receivers_raw = re.findall(
            r'"url":"(https?://[^"]{4,200})","type":"([^"]{1,30})"',
            html
        )
        # Also grab labels (preceding "label" key)
        labels_raw = re.findall(r'"label":"([^"]{0,200})"', html)

        if not receivers_raw:
            log.warning("receiverbook.de: no receivers found in page")
            return stations

        # ── Step 3: pair each receiver with the nearest preceding coordinate ──
        # Find char positions of each coordinate and receiver in the HTML
        coord_positions = []
        for m in re.finditer(r'"coordinates":\[(-?[0-9.]+),(-?[0-9.]+)\]', html):
            coord_positions.append((m.start(), float(m.group(1)), float(m.group(2))))

        # Walk receivers in document order, assigning the last-seen coordinate
        cur_lon, cur_lat = 0.0, 0.0
        coord_idx = 0
        idx = 0
        for m in re.finditer(r'"url":"(https?://[^"]{4,200})","type":"([^"]{1,30})"', html):
            pos = m.start()
            url = m.group(1)
            sdr_type = m.group(2)

            # Advance coordinate pointer to the last coord before this receiver
            while coord_idx < len(coord_positions) and coord_positions[coord_idx][0] < pos:
                cur_lon = coord_positions[coord_idx][1]
                cur_lat = coord_positions[coord_idx][2]
                coord_idx += 1

            # Try to find label just before this url in a small window
            window_start = max(0, pos - 300)
            window = html[window_start:pos]
            lbl_m = re.findall(r'"label":"([^"]{0,150})"', window)
            label = lbl_m[-1] if lbl_m else url
            clean_label = re.sub(r'<[^>]+>', '', label).strip()[:80]

            jlat = (hash(url + "a") % 100 - 50) * 0.002
            jlon = (hash(url + "b") % 100 - 50) * 0.002
            stations.append({
                "id": f"rb_{idx}",
                "name": clean_label or url,
                "url": url,
                "lat": round(cur_lat + jlat, 5),
                "lon": round(cur_lon + jlon, 5),
                "freqs": _infer_freqs(clean_label, sdr_type),
                "online": True,
                "source": sdr_type,
                "type": sdr_type,
            })
            idx += 1

        log.info(f"receiverbook.de: parsed {len(stations)} stations")
    except Exception as e:
        log.warning(f"receiverbook.de fetch failed: {e}")
    return stations


def _build_fallback_stations():
    """Verified-working WebSDR/KiwiSDR stations as last-resort fallback."""
    return [
        {"id": "fb_0",  "name": "WebSDR Univ. Twente NL (HF)",
         "url": "http://websdr.ewi.utwente.nl:8901/",
         "lat": 52.238, "lon": 6.856, "freqs": [{"low": 0.0, "high": 30.0}],
         "online": True, "source": "WebSDR", "type": "WebSDR"},
        {"id": "fb_1",  "name": "WebSDR Hack Green UK (HF)",
         "url": "http://hackgreensdr.org:8901/",
         "lat": 53.040, "lon": -2.540, "freqs": [{"low": 0.0, "high": 30.0}],
         "online": True, "source": "WebSDR", "type": "WebSDR"},
        {"id": "fb_2",  "name": "KiwiSDR New Zealand (HF)",
         "url": "http://kiwisdr.owdjim.gen.nz:8073/",
         "lat": -41.006, "lon": 173.010, "freqs": [{"low": 0.0, "high": 32.0}],
         "online": True, "source": "KiwiSDR", "type": "KiwiSDR"},
        {"id": "fb_3",  "name": "KiwiSDR Berlin DE (HF)",
         "url": "http://thomas0177.ddns.net:8074/",
         "lat": 52.419, "lon": 13.306, "freqs": [{"low": 0.0, "high": 32.0}],
         "online": True, "source": "KiwiSDR", "type": "KiwiSDR"},
        {"id": "fb_4",  "name": "OpenWebRX Bedford UK (HF/VHF/UHF)",
         "url": "http://remoteradio.changeip.org:8077/",
         "lat": 52.117, "lon": -0.450,
         "freqs": [{"low": 0.0, "high": 32.0}, {"low": 118.0, "high": 174.0}, {"low": 400.0, "high": 480.0}],
         "online": True, "source": "OpenWebRX", "type": "OpenWebRX"},
        {"id": "fb_5",  "name": "KiwiSDR Marahau NZ 2 (HF)",
         "url": "http://kiwisdr2.owdjim.gen.nz:8075/",
         "lat": -41.008, "lon": 173.012, "freqs": [{"low": 0.0, "high": 32.0}],
         "online": True, "source": "KiwiSDR", "type": "KiwiSDR"},
        {"id": "fb_6",  "name": "SDR Esparreguera ES (VHF/UHF)",
         "url": "http://ea3rkeuhf.sytes.net/",
         "lat": 41.550, "lon": 1.856,
         "freqs": [{"low": 118.0, "high": 174.0}, {"low": 400.0, "high": 480.0}],
         "online": True, "source": "OpenWebRX", "type": "OpenWebRX"},
        {"id": "fb_7",  "name": "SDR Collbato ES (HF/VHF)",
         "url": "http://ed3ybk.sytes.net:8073/",
         "lat": 41.550, "lon": 1.820,
         "freqs": [{"low": 0.0, "high": 32.0}, {"low": 118.0, "high": 174.0}],
         "online": True, "source": "OpenWebRX", "type": "OpenWebRX"},
    ]


def _refresh_stations_cache():
    """Background thread: fetch stations and populate cache."""
    rb_stations = _fetch_receiverbook()
    fallback = _build_fallback_stations()
    seen_urls = set()
    merged = []
    for st in rb_stations + fallback:
        url_key = st["url"].rstrip("/").lower()
        if url_key not in seen_urls and url_key:
            seen_urls.add(url_key)
            merged.append(st)
    if not merged:
        merged = fallback
    merged = _mark_known(merged)
    _stations_cache["data"] = merged
    _stations_cache["ts"] = time.time()
    new_today = count_new_today(merged)
    log.info(f"Station cache updated: {len(merged)} total, {new_today} new today")


def get_stations():
    """Return list of SDR stations. Returns fallback instantly if cache not ready."""
    now = time.time()
    if _stations_cache["data"] is not None and (now - _stations_cache["ts"]) < _stations_cache["ttl"]:
        return _stations_cache["data"]
    # Cache stale or empty — trigger background refresh and return what we have
    if _stations_cache["data"] is None:
        # First call: return fallback immediately, populate in background
        threading.Thread(target=_refresh_stations_cache, daemon=True).start()
        return _build_fallback_stations()
    # Cache is stale: refresh in background, serve stale data for now
    threading.Thread(target=_refresh_stations_cache, daemon=True).start()
    return _stations_cache["data"]


# ---------------------------------------------------------------------------
# Satellite position calculation
# ---------------------------------------------------------------------------

def compute_satellite_position(sat_id: str):
    """
    Returns dict with lat, lon, alt_km, footprint_radius_deg, footprint_radius_km,
    or None on failure.
    """
    tle = get_tle(sat_id)
    if not tle:
        return None

    name, line1, line2 = tle
    try:
        satellite = EarthSatellite(line1, line2, name, _ts_skyfield)
        t = _ts_skyfield.now()
        geocentric = satellite.at(t)
        subpoint = wgs84.subpoint(geocentric)
        lat = subpoint.latitude.degrees
        lon = subpoint.longitude.degrees
        alt_km = subpoint.elevation.km

        # Footprint radius in degrees
        if alt_km > 0:
            cos_val = 6371.0 / (6371.0 + alt_km)
            cos_val = max(-1.0, min(1.0, cos_val))
            footprint_deg = math.degrees(math.acos(cos_val))
        else:
            footprint_deg = 0.0

        # In km (arc length on sphere)
        footprint_km = math.radians(footprint_deg) * 6371.0

        return {
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "alt_km": round(alt_km, 2),
            "footprint_deg": round(footprint_deg, 4),
            "footprint_km": round(footprint_km, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.warning(f"Position computation error for {sat_id}: {e}")
        return None


def _great_circle_deg(lat1, lon1, lat2, lon2):
    """Great-circle distance in degrees between two points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return math.degrees(c)


def get_active_stations(sat_id: str, stations: list):
    """
    Returns list of station ids that are:
    1. Within the satellite's footprint
    2. Have at least one frequency range that covers a downlink frequency
    """
    pos = compute_satellite_position(sat_id)
    if not pos:
        return []

    sat_info = SAT_DB.get(sat_id, {})
    downlink_freqs = [dl["freq"] for dl in sat_info.get("downlink", [])]

    active = []
    sat_lat = pos["lat"]
    sat_lon = pos["lon"]
    fp_deg = pos["footprint_deg"]

    for st in stations:
        # Distance check
        dist = _great_circle_deg(sat_lat, sat_lon, st["lat"], st["lon"])
        if dist > fp_deg:
            continue

        # Frequency check - does any downlink freq fall in any station freq range?
        freq_match = False
        for dl_freq in downlink_freqs:
            for fr in st.get("freqs", []):
                if fr["low"] <= dl_freq <= fr["high"]:
                    freq_match = True
                    break
            if freq_match:
                break

        if freq_match:
            active.append(st["id"])

    return active


# ---------------------------------------------------------------------------
# Observer location (persistent)
# ---------------------------------------------------------------------------
OBSERVER_FILE = BASE / "observer.json"
_observer = {"lat": 44.43, "lon": 26.1, "name": "București, România"}


def _load_observer():
    global _observer
    try:
        if OBSERVER_FILE.exists():
            data = json.loads(OBSERVER_FILE.read_text())
            if "lat" in data and "lon" in data:
                _observer = data
    except Exception as e:
        log.warning(f"Could not load observer.json: {e}")


def _save_observer():
    try:
        OBSERVER_FILE.write_text(json.dumps(_observer, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning(f"Could not save observer.json: {e}")


# ---------------------------------------------------------------------------
# Observer geometry per satellite (az/el/distance/Doppler)
# ---------------------------------------------------------------------------

def compute_observer_geometry(sat_id: str, obs_lat: float, obs_lon: float):
    """Return az, el, dist_km, range_rate_km_s, above_horizon for sat from observer."""
    tle = get_tle(sat_id)
    if not tle:
        return None
    name, line1, line2 = tle
    try:
        satellite = EarthSatellite(line1, line2, name, _ts_skyfield)
        observer = wgs84.latlon(obs_lat, obs_lon)
        t = _ts_skyfield.now()
        difference = satellite - observer
        topocentric = difference.at(t)
        alt, az, dist = topocentric.altaz()
        pos = topocentric.position.km
        vel = topocentric.velocity.km_per_s
        dist_km = dist.km
        if dist_km > 0:
            range_rate = sum(p * v for p, v in zip(pos, vel)) / dist_km
        else:
            range_rate = 0.0
        return {
            "az": round(float(az.degrees), 1),
            "el": round(float(alt.degrees), 1),
            "dist_km": round(float(dist_km), 1),
            "range_rate_km_s": round(float(range_rate), 4),
            "above_horizon": bool(alt.degrees > 0),
        }
    except Exception as e:
        log.warning(f"Observer geometry error for {sat_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Pass predictions
# ---------------------------------------------------------------------------

def compute_passes(sat_id: str, obs_lat: float, obs_lon: float, n: int = 5, horizon: float = 5.0):
    """Return up to n upcoming passes for sat_id visible from observer."""
    tle = get_tle(sat_id)
    if not tle:
        return []
    name, line1, line2 = tle
    try:
        satellite = EarthSatellite(line1, line2, name, _ts_skyfield)
        observer = wgs84.latlon(obs_lat, obs_lon)
        t0 = _ts_skyfield.now()
        t1 = _ts_skyfield.tt_jd(t0.tt + 2.0)  # 48 hours window

        times, events = satellite.find_events(observer, t0, t1, altitude_degrees=horizon)

        passes = []
        aos_t = None
        aos_az = 0
        tca_t = None
        tca_el = 0
        tca_az = 0
        for ti, event in zip(times, events):
            ev = int(event)
            if ev == 0:  # AOS
                aos_t = ti
                tca_t = None
                diff = satellite - observer
                topo = diff.at(ti)
                _, az_aos, _ = topo.altaz()
                aos_az = round(az_aos.degrees, 1)
            elif ev == 1 and (aos_t is not None):  # TCA
                tca_t = ti
                diff = satellite - observer
                topo = diff.at(ti)
                alt_tca, az_tca, _ = topo.altaz()
                tca_el = round(alt_tca.degrees, 1)
                tca_az = round(az_tca.degrees, 1)
            elif ev == 2 and (aos_t is not None):  # LOS
                diff = satellite - observer
                topo = diff.at(ti)
                _, az_los, _ = topo.altaz()
                aos_dt = aos_t.utc_datetime()
                tca_dt = (tca_t if tca_t is not None else ti).utc_datetime()
                los_dt = ti.utc_datetime()
                duration = (los_dt - aos_dt).total_seconds()
                passes.append({
                    "aos": aos_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "aos_az": aos_az,
                    "tca": tca_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tca_el": tca_el,
                    "tca_az": tca_az,
                    "los": los_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "los_az": round(az_los.degrees, 1),
                    "duration_s": round(duration),
                })
                aos_t = None
                tca_t = None
                if len(passes) >= n:
                    break

        return passes
    except Exception as e:
        log.warning(f"Pass prediction error for {sat_id}: {e}")
        return []


# ---------------------------------------------------------------------------
# Ground track
# ---------------------------------------------------------------------------

def compute_ground_track(sat_id: str, past_min: int = 30, future_min: int = 90):
    """Return past and future ground track split at antimeridian."""
    tle = get_tle(sat_id)
    if not tle:
        return {"past": [], "future": []}
    name, line1, line2 = tle
    try:
        satellite = EarthSatellite(line1, line2, name, _ts_skyfield)
        now_jd = _ts_skyfield.now().tt

        offsets = np.linspace(-past_min / 1440.0, future_min / 1440.0, (past_min + future_min) * 2)
        times = _ts_skyfield.tt_jd(now_jd + offsets)
        geocentric = satellite.at(times)
        subpoints = wgs84.subpoint_of(geocentric)
        lats = subpoints.latitude.degrees
        lons = subpoints.longitude.degrees

        split_idx = past_min * 2  # past portion length

        def split_antimeridian(lat_arr, lon_arr):
            """Split track at antimeridian jumps >180 degrees."""
            segments = []
            seg = [[float(lat_arr[0]), float(lon_arr[0])]]
            for i in range(1, len(lat_arr)):
                if abs(float(lon_arr[i]) - float(lon_arr[i - 1])) > 180:
                    segments.append(seg)
                    seg = []
                seg.append([float(lat_arr[i]), float(lon_arr[i])])
            segments.append(seg)
            return segments

        past_segs = split_antimeridian(lats[:split_idx], lons[:split_idx])
        future_segs = split_antimeridian(lats[split_idx:], lons[split_idx:])

        return {"past": past_segs, "future": future_segs}
    except Exception as e:
        log.warning(f"Ground track error for {sat_id}: {e}")
        return {"past": [], "future": []}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/observer", methods=["GET", "POST"])
def api_observer():
    global _observer
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "lat" in data and "lon" in data:
            _observer = {
                "lat": float(data["lat"]),
                "lon": float(data["lon"]),
                "name": data.get("name", ""),
            }
            _save_observer()
        return jsonify(_observer)
    return jsonify(_observer)


@app.route("/api/geocode")
def api_geocode():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "No query"}), 400
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 5},
            headers={"User-Agent": "SDR-Tracker/1.0 (sdr_tracker@local)"},
            timeout=8,
        )
        r.raise_for_status()
        results = r.json()
        simplified = [
            {
                "name": item.get("display_name", "")[:80],
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
            }
            for item in results
        ]
        return jsonify(simplified)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/satellite/<sat_id>/passes")
def api_satellite_passes(sat_id):
    sat_id = sat_id.upper()
    if sat_id not in SAT_DB:
        return jsonify({"error": "Satelit necunoscut"}), 404
    n = request.args.get("n", 5, type=int)
    horizon = request.args.get("horizon", 5.0, type=float)
    obs = _observer
    passes = compute_passes(sat_id, obs["lat"], obs["lon"], n=n, horizon=horizon)
    return jsonify({"sat_id": sat_id, "observer": obs, "passes": passes})


@app.route("/api/satellite/<sat_id>/groundtrack")
def api_satellite_groundtrack(sat_id):
    sat_id = sat_id.upper()
    if sat_id not in SAT_DB:
        return jsonify({"error": "Satelit necunoscut"}), 404
    past = request.args.get("past", 30, type=int)
    future = request.args.get("future", 90, type=int)
    track = compute_ground_track(sat_id, past_min=past, future_min=future)
    return jsonify({"sat_id": sat_id, **track})


@app.route("/")
def index():
    enriched = {}
    for sat_id, info in SAT_DB.items():
        enriched[sat_id] = dict(info)
        enriched[sat_id]["downlink"] = [enrich_freq(f) for f in info.get("downlink", [])]
        enriched[sat_id]["uplink"]   = [enrich_freq(f) for f in info.get("uplink", [])]
        enriched[sat_id]["beacon"]   = [enrich_freq(f) for f in info.get("beacon", [])]
    return render_template("index.html", satellites=enriched)


@app.route("/api/stations")
def api_stations():
    stations = get_stations()
    new_today = count_new_today(stations)
    return jsonify({"stations": stations, "count": len(stations), "new_today": new_today})


@app.route("/api/satellites")
def api_satellites():
    result = {}
    for sat_id, info in SAT_DB.items():
        result[sat_id] = {
            "id": sat_id,
            "name": info["name"],
            "agency": info["agency"],
            "orbit_type": info["orbit_type"],
            "description": info["description"],
            "norad": info["norad"],
            "downlink": [enrich_freq(f) for f in info["downlink"]],
            "uplink":   [enrich_freq(f) for f in info["uplink"]],
            "beacon":   [enrich_freq(f) for f in info["beacon"]],
            "notes": info["notes"],
        }
    return jsonify(result)


@app.route("/api/satellite/<sat_id>/position")
def api_satellite_position(sat_id):
    sat_id = sat_id.upper()
    if sat_id not in SAT_DB:
        return jsonify({"error": "Satelit necunoscut"}), 404

    pos = compute_satellite_position(sat_id)
    if not pos:
        return jsonify({"error": "Nu s-a putut calcula poziția (TLE lipsă)"}), 503

    stations = get_stations()
    active = get_active_stations(sat_id, stations)
    obs = _observer
    obs_geo = compute_observer_geometry(sat_id, obs["lat"], obs["lon"])

    return jsonify({
        "sat_id": sat_id,
        "position": pos,
        "active_stations": active,
        "observer": obs_geo,
    })


@app.route("/api/satellite/<sat_id>/tle")
def api_satellite_tle(sat_id):
    sat_id = sat_id.upper()
    if sat_id not in SAT_DB:
        return jsonify({"error": "Satelit necunoscut"}), 404

    tle = get_tle(sat_id)
    if not tle:
        return jsonify({"error": "TLE indisponibil"}), 503

    name, line1, line2 = tle
    return jsonify({
        "sat_id": sat_id,
        "norad": SAT_DB[sat_id]["norad"],
        "tle_name": name,
        "line1": line1,
        "line2": line2,
    })


CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
OLLAMA_URL = "http://localhost:11434"

CLAUDE_MODELS = [
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "provider": "claude"},
    {"id": "claude-sonnet-4-6",         "name": "Claude Sonnet 4.6", "provider": "claude"},
    {"id": "claude-opus-4-7",           "name": "Claude Opus 4.7",   "provider": "claude"},
]


@app.route("/api/models")
def api_models():
    ollama_models = []
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if r.ok:
            skip = {"all-minilm", "mxbai-embed", "nomic-embed"}
            for m in r.json().get("models", []):
                name = m["name"]
                if not any(s in name for s in skip):
                    ollama_models.append({"id": name, "name": name, "provider": "ollama"})
    except Exception:
        pass
    return jsonify({"claude": CLAUDE_MODELS, "ollama": ollama_models})


def _search_stations(query: str, sat_info: dict, limit: int = 20) -> list:
    """Search stations by query — extracts freq/location/type hints and scores matches."""
    import re
    q = query.lower()
    stations = get_stations()
    if not stations:
        return []

    # Extract frequency numbers from query
    freq_hints = []
    for m in re.findall(r'(\d+(?:\.\d+)?)\s*(?:mhz)?', q):
        f = float(m)
        if 0.5 <= f <= 10000:
            freq_hints.append(f)
    # Also use satellite downlink freqs if query mentions satellite
    if sat_info:
        for dl in sat_info.get("downlink", []):
            freq_hints.append(dl["freq"])

    # Country/location keywords from COUNTRY_COORDS keys
    loc_hints = [k.lower() for k in COUNTRY_COORDS if k.lower() in q]

    # SDR type hints
    type_hints = []
    for kw in ["kiwisdr", "kiwi", "websdr", "openwebrx"]:
        if kw in q:
            type_hints.append(kw)

    # Band hints
    band_vhf = any(x in q for x in ["vhf", "2m", "144", "145", "146"])
    band_uhf = any(x in q for x in ["uhf", "70cm", "430", "435", "436", "437"])
    band_hf  = any(x in q for x in ["hf", "shortwave", "40m", "20m", "80m", "10m"])

    scored = []
    for st in stations:
        score = 0
        name_lc = st["name"].lower()
        stype_lc = st.get("type", "").lower()

        # Frequency match (high value)
        for fh in freq_hints:
            for fr in st.get("freqs", []):
                if fr["low"] <= fh <= fr["high"]:
                    score += 12
                    break

        # Location match
        for loc in loc_hints:
            if loc in name_lc:
                score += 8

        # Type match
        for th in type_hints:
            if th in stype_lc or th in name_lc:
                score += 6

        # Band match
        if band_vhf:
            for fr in st.get("freqs", []):
                if fr["low"] <= 145.8 <= fr["high"]:
                    score += 5; break
        if band_uhf:
            for fr in st.get("freqs", []):
                if fr["low"] <= 437.0 <= fr["high"]:
                    score += 5; break
        if band_hf:
            for fr in st.get("freqs", []):
                if fr["high"] <= 32:
                    score += 5; break

        # Generic word match
        for word in q.split():
            if len(word) > 3 and word in name_lc:
                score += 3

        if score > 0:
            scored.append((score, st))

    scored.sort(key=lambda x: (-x[0], not x[1].get("online", False)))
    return [s[1] for s in scored[:limit]]


def _station_line(st: dict, sat_info: dict) -> str:
    """One-line compact station description with tuned URL."""
    freqs = ", ".join(f"{f['low']:.0f}-{f['high']:.0f} MHz" for f in st.get("freqs", []))
    best_freq = None
    if sat_info:
        for dl in sat_info.get("downlink", []):
            for fr in st.get("freqs", []):
                if fr["low"] <= dl["freq"] <= fr["high"]:
                    best_freq = dl["freq"]; break
            if best_freq:
                break
    tune = f"?tune={int(best_freq * 1000)}" if best_freq else ""
    url = st.get("url", "").rstrip("/") + tune
    return f"{st['name'][:45]} | {st.get('type','?')} | {freqs} | {url}"


def _build_context(sat_id: str, active_ids: list, user_msg: str) -> str:
    sat_info = SAT_DB.get(sat_id, {})
    sat_name = sat_info.get("name", sat_id)

    dl_lines = [f"  - {d['freq']} MHz {d['mode']} ({d.get('info','')})"
                for d in sat_info.get("downlink", [])]
    ul_lines = [f"  - {u['freq']} MHz {u['mode']}"
                for u in sat_info.get("uplink", [])]

    stations = get_stations()
    st_map = {s["id"]: s for s in stations}
    active_lines = [_station_line(st_map[sid], sat_info)
                    for sid in active_ids[:10] if sid in st_map]

    # Inject search results when query seems station-related
    search_lines = []
    search_kw = ["stați", "sdr", "caută", "găsești", "găsesc", "deschide", "link",
                 "locați", "frecven", "unde", "european", "km", "ascult", "recepț",
                 "online", "kiwi", "websdr", "openwebrx"]
    if any(k in user_msg.lower() for k in search_kw):
        found = _search_stations(user_msg, sat_info, limit=15)
        search_lines = [_station_line(s, sat_info) for s in found]

    NL = "\n"
    dl_str = ", ".join("{} MHz {}".format(d["freq"], d["mode"]) for d in sat_info.get("downlink", []))
    ul_str = ", ".join("{} MHz".format(u["freq"]) for u in sat_info.get("uplink", []))
    ctx = (
        "Satelit: {} | NORAD {} | {}\n".format(sat_name, sat_info.get("norad","?"), sat_info.get("orbit_type","?"))
        + "Downlink: {}\n".format(dl_str or "(niciuna)")
        + "Uplink: {}\n".format(ul_str or "(niciuna)")
        + "Note: {}\n\n".format(sat_info.get("notes",""))
        + "STATII ACTIVE ACUM IN FOOTPRINT ({}):\n".format(len(active_lines))
        + (NL.join(active_lines) if active_lines else "  (niciuna)") + "\n"
    )
    if search_lines:
        ctx += "\nREZULTATE CAUTARE STATII ({}):\n".format(len(search_lines)) + NL.join(search_lines)
    return ctx


@app.route("/api/stations/search")
def api_stations_search():
    q     = request.args.get("q", "")
    freq  = request.args.get("freq", type=float)
    stype = request.args.get("type", "")
    limit = request.args.get("limit", 20, type=int)

    # Build a synthetic query
    query = q
    if freq:
        query += f" {freq} mhz"
    if stype:
        query += f" {stype}"

    sat_info = {}  # no satellite context for generic search
    results = _search_stations(query, sat_info, limit=limit)

    # Filter by type if explicit
    if stype:
        results = [r for r in results if stype.lower() in r.get("type", "").lower()]

    return jsonify({"results": results, "count": len(results)})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data       = request.get_json(silent=True) or {}
    user_msg   = (data.get("message") or "").strip()
    sat_id     = (data.get("sat_id") or "ISS").upper()
    active_ids = data.get("active_stations", [])
    model_id   = data.get("model", "claude-haiku-4-5-20251001")
    provider   = data.get("provider", "claude")

    if not user_msg:
        return jsonify({"error": "Mesaj gol"}), 400

    sat_ctx = _build_context(sat_id, active_ids, user_msg)
    sat_name = SAT_DB.get(sat_id, {}).get("name", sat_id)

    system_prompt = (
        "Ești un asistent expert în radioamatorism și SDR (Software Defined Radio), integrat în platforma SDR Tracker.\n"
        "Cunoști: protocoale amatore (FM, SSB, CW, APRS, BPSK, FSK, SSTV, Codec2), sateliți amatori, "
        "software SDR (WebSDR, KiwiSDR, OpenWebRX, SDR#, GQRX, SDR++), antene VHF/UHF.\n"
        "Răspunzi în română, concis. Folosești markdown (bold, liste, linkuri).\n"
        "IMPORTANT: Când menționezi o stație SDR, redă URL-ul ca link markdown: [Nume stație](URL). "
        "Include ?tune=frecvență_hz în URL când e relevant. "
        "Când explici setări: frecvența exactă, BW, modulație, CTCSS dacă e necesar.\n\n"
        f"CONTEXT CURENT:\n{sat_ctx}"
    )

    prompt = f"[Satelit activ: {sat_name}]\n\n{user_msg}"

    def sse(text=None, done=False, error=None):
        if error:
            return f"data: {json.dumps({'error': error})}\n\n"
        if done:
            return f"data: {json.dumps({'done': True})}\n\n"
        return f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"

    if provider == "ollama":
        def gen_ollama():
            try:
                r = requests.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": model_id,
                        "stream": True,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": prompt},
                        ],
                    },
                    stream=True, timeout=120,
                )
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    txt = ev.get("message", {}).get("content", "")
                    if txt:
                        yield sse(txt)
                    if ev.get("done"):
                        break
            except Exception as e:
                yield sse(error=str(e))
            finally:
                yield sse(done=True)

        return Response(stream_with_context(gen_ollama()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Claude CLI
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", model_id,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--append-system-prompt", system_prompt,
        prompt,
    ]

    def gen_claude():
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=os.environ.copy(),
            )
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "stream_event":
                    se = ev.get("event", {})
                    if se.get("type") == "content_block_delta":
                        d = se.get("delta", {})
                        if d.get("type") == "text_delta":
                            yield sse(d.get("text", ""))
            proc.wait(timeout=5)
        except Exception as e:
            yield sse(error=str(e))
        finally:
            yield sse(done=True)

    return Response(stream_with_context(gen_claude()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/stream")
def stream():
    sat_id = request.args.get("sat", "ISS").upper()
    if sat_id not in SAT_DB:
        sat_id = "ISS"

    def generate():
        while True:
            try:
                pos = compute_satellite_position(sat_id)
                if pos:
                    stations = get_stations()
                    active = get_active_stations(sat_id, stations)
                    obs = _observer
                    obs_geo = compute_observer_geometry(sat_id, obs["lat"], obs["lon"])
                    payload = json.dumps({
                        "sat_id": sat_id,
                        "position": pos,
                        "active_stations": active,
                        "observer": obs_geo,
                    })
                    yield f"data: {payload}\n\n"
                else:
                    yield f"data: {json.dumps({'error': 'no_tle', 'sat_id': sat_id})}\n\n"
            except Exception as e:
                log.error(f"SSE error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _background_tle_refresh():
    """Periodically refresh TLEs every 6 hours."""
    while True:
        time.sleep(_tle_ttl)
        log.info("Background TLE refresh starting...")
        _prefetch_all_tle()


def _periodic_stations_refresh():
    """Refresh stations every 5 minutes in background."""
    while True:
        time.sleep(_stations_cache["ttl"])
        log.info("Periodic station refresh...")
        _refresh_stations_cache()


if __name__ == "__main__":
    _load_known_stations()
    _load_observer()
    threading.Thread(target=_prefetch_all_tle, daemon=True).start()
    threading.Thread(target=_background_tle_refresh, daemon=True).start()
    threading.Thread(target=_refresh_stations_cache, daemon=True).start()
    threading.Thread(target=_periodic_stations_refresh, daemon=True).start()
    log.info("SDR Tracker starting on port 8810...")
    app.run(host="0.0.0.0", port=8810, debug=False, threaded=True)
