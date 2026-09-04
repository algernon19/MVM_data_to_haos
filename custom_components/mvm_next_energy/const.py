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

# Split out when a "contract start date" (felhasználóváltás) is set: the
# consumption from that date on is "yours", everything before it belonged to
# the previous account holder. STATISTIC_ID above always keeps the full,
# unsplit meter history regardless of this setting.
STATISTIC_OWN_ID = "mvm_next:imported_consumption_own"
STATISTIC_OWN_NAME = "MVM Next – Saját fogyasztás (felhasználóváltás óta)"
STATISTIC_PREVIOUS_ID = "mvm_next:imported_consumption_previous"
STATISTIC_PREVIOUS_NAME = "MVM Next – Előző felhasználó fogyasztása"

# Hypothetical: what the previous account holder's own consumption would have
# cost under your A1 / D tariff settings. Purely informational - never feeds
# into the real allowance/cost calculation.
COST_PREVIOUS_STATISTIC_ID = "mvm_next:imported_cost_previous"
COST_PREVIOUS_STATISTIC_NAME = "MVM Next – Előző lakó fogyasztásának becsült költsége (A1)"
COST_D_PREVIOUS_STATISTIC_ID = "mvm_next:imported_cost_d_previous"
COST_D_PREVIOUS_STATISTIC_NAME = (
    "MVM Next – Előző lakó fogyasztásának becsült költsége (D tarifa)"
)

# Second, derived statistic: the cost of the imported consumption, computed
# from the Hungarian tiered household tariff. Its unit is the HA-configured
# currency so the Energy dashboard accepts it under "total cost".
COST_STATISTIC_ID = "mvm_next:imported_cost"
COST_STATISTIC_NAME = "MVM Next – Vételezett fogyasztás költsége"

# Tiered household electricity price (gross HUF/kWh), 2024/2025 "rezsicsökkentés":
# consumption up to the annual allowance is billed at the reduced price, the
# part above it at the market price. The allowance window is the calendar year.
CONF_COST_ENABLED = "cost_enabled"
CONF_PRICE_LOW = "price_low"
CONF_PRICE_HIGH = "price_high"
CONF_ANNUAL_THRESHOLD = "annual_threshold_kwh"
# Date the current account holder's contract started. Consumption before it
# belongs to the previous user; for the calendar year it falls in, the yearly
# allowance is prorated by the number of days remaining in that year.
CONF_START_DATE = "start_date"
# How the reduced-price allowance is accounted: "monthly" (the ~210 kWh/month
# that shows up on each invoice, the yearly 2523 kWh split by day count) or
# "yearly" (one 2523 kWh bucket from 1 January - the figure the year-end
# reconciliation settles to).
CONF_ALLOWANCE_PERIOD = "allowance_period"
DEFAULT_ALLOWANCE_PERIOD = "monthly"
DEFAULT_COST_ENABLED = True
DEFAULT_PRICE_LOW = 36.39
DEFAULT_PRICE_HIGH = 70.104
DEFAULT_ANNUAL_THRESHOLD = 2523.0

# --- MVM "D" (dynamic, HUPX-based) tariff ---------------------------------
# What-if comparison: what the same consumption would cost on the announced
# dynamic tariff. Per-kWh gross price for an hour:
#   ( hupx_eur_mwh * eur_huf / 1000 + merchant + transmission + distribution )
#   * (1 + vat/100)
# Historical hourly day-ahead prices come from api.energy-charts.info (opt-in,
# needs internet). Fee defaults match the ha-mvm-d-tariff project.
COST_D_STATISTIC_ID = "mvm_next:imported_cost_d"
COST_D_STATISTIC_NAME = "MVM Next – Vételezett fogyasztás költsége (D tarifa)"
ENERGY_CHARTS_PRICE_URL = "https://api.energy-charts.info/price"
# The MNB legacy SOAP service is only served over plain HTTP (the https URL 404s).
MNB_SOAP_URL = "http://www.mnb.hu/arfolyamok.asmx"

CONF_D_ENABLED = "d_enabled"
CONF_D_MERCHANT_FEE = "d_merchant_fee_huf_kwh"
CONF_D_TRANSMISSION_FEE = "d_transmission_fee_huf_kwh"
CONF_D_DISTRIBUTION_FEE = "d_distribution_fee_huf_kwh"
CONF_D_VAT_PERCENT = "d_vat_percent"
CONF_D_EUR_HUF = "d_eur_huf"
DEFAULT_D_ENABLED = False
DEFAULT_D_MERCHANT_FEE = 13.70
DEFAULT_D_TRANSMISSION_FEE = 4.84
DEFAULT_D_DISTRIBUTION_FEE = 18.56
DEFAULT_D_VAT_PERCENT = 27.0
DEFAULT_D_EUR_HUF = 0.0  # 0 = MNB daily rate, fetched automatically

D_PRICE_STORAGE_KEY = f"{DOMAIN}.d_prices"

SERVICE_IMPORT = "import"
SERVICE_UPLOAD = "upload"
ATTR_FILE_PATH = "file_path"
ATTR_FILE = "file"

SIGNAL_UPDATE = f"{DOMAIN}_update"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.import_state"

# Only rows matching all three of these are imported, per the MVM Next CSV
# export format ("Gyári szám;Időpont;Adatpont típus;Státusz;Érték;Mértékegység").
CSV_DIRECTION = "Vételezett"
CSV_STATUS_OK = "Mért"
CSV_UNIT = "kWh"

TIME_ZONE = "Europe/Budapest"
