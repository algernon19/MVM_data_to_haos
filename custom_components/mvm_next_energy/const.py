"""Constants for the MVM Next Energy Import integration."""
from __future__ import annotations

DOMAIN = "mvm_next_energy"

CONF_IMPORT_DIR = "import_dir"
DEFAULT_IMPORT_DIR = "/config/mvm_next"

# Statistic id used for the imported long-term statistic series. Intentionally
# NOT keyed by meter serial number, so a physical meter exchange does not
# start a new, disconnected statistics series.
STATISTIC_ID = "mvm_next:imported_consumption"
STATISTIC_NAME = "MVM Next – Vételezett fogyasztás"
STATISTIC_UNIT = "kWh"
STATISTIC_UNIT_CLASS = "energy"

SERVICE_IMPORT = "import"
ATTR_FILE_PATH = "file_path"

SIGNAL_UPDATE = f"{DOMAIN}_update"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.import_state"

# Only rows matching all three of these are imported, per the MVM Next CSV
# export format ("Gyári szám;Időpont;Adatpont típus;Státusz;Érték;Mértékegység").
CSV_DIRECTION = "Vételezett"
CSV_STATUS_OK = "Mért"
CSV_UNIT = "kWh"

TIME_ZONE = "Europe/Budapest"
