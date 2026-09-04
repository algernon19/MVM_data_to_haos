"""CSV parsing and long-term statistics import for MVM Next Energy Import."""
from __future__ import annotations

import calendar
import csv
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ALLOWANCE_PERIOD,
    CONF_ANNUAL_THRESHOLD,
    CONF_COST_ENABLED,
    CONF_D_DISTRIBUTION_FEE,
    CONF_D_ENABLED,
    CONF_D_EUR_HUF,
    CONF_D_MERCHANT_FEE,
    CONF_D_TRANSMISSION_FEE,
    CONF_D_VAT_PERCENT,
    CONF_IMPORT_DIR,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    CONF_START_DATE,
    COST_D_PREVIOUS_STATISTIC_ID,
    COST_D_PREVIOUS_STATISTIC_NAME,
    COST_D_STATISTIC_ID,
    COST_D_STATISTIC_NAME,
    COST_PREVIOUS_STATISTIC_ID,
    COST_PREVIOUS_STATISTIC_NAME,
    COST_STATISTIC_ID,
    COST_STATISTIC_NAME,
    D_PRICE_STORAGE_KEY,
    CSV_DIRECTION,
    CSV_STATUS_OK,
    CSV_UNIT,
    DEFAULT_ALLOWANCE_PERIOD,
    DEFAULT_ANNUAL_THRESHOLD,
    DEFAULT_D_DISTRIBUTION_FEE,
    DEFAULT_D_ENABLED,
    DEFAULT_D_EUR_HUF,
    DEFAULT_D_MERCHANT_FEE,
    DEFAULT_D_TRANSMISSION_FEE,
    DEFAULT_D_VAT_PERCENT,
    DEFAULT_COST_ENABLED,
    DEFAULT_PRICE_HIGH,
    DEFAULT_PRICE_LOW,
    DOMAIN,
    SIGNAL_UPDATE,
    STATISTIC_ID,
    STATISTIC_NAME,
    STATISTIC_OWN_ID,
    STATISTIC_OWN_NAME,
    STATISTIC_PREVIOUS_ID,
    STATISTIC_PREVIOUS_NAME,
    STATISTIC_UNIT,
    STATISTIC_UNIT_CLASS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TIME_ZONE,
)
from .dynamic import (
    DTariffConfig,
    async_current_d_price,
    async_d_gross_prices,
    async_d_price_forecast,
)

_LOGGER = logging.getLogger(__name__)

BUDAPEST_TZ = ZoneInfo(TIME_ZONE)

try:  # Home Assistant >= 2025.x exposes StatisticMeanType, replacing has_mean.
    from homeassistant.components.recorder.models import StatisticMeanType

    _HAS_MEAN_TYPE = True
except ImportError:  # pragma: no cover - older HA core
    _HAS_MEAN_TYPE = False


@dataclass
class ParsedFile:
    """Result of parsing a single MVM Next CSV export."""

    mtime: float
    size: int
    quarters: dict[str, float] = field(default_factory=dict)  # UTC quarter iso -> kWh
    meter_serial: str | None = None
    last_quarter_local: str | None = None  # naive local isoformat, e.g. 2026-08-31T23:45:00


_CANON_RE = re.compile(r"^mvm_\d{4}-\d{2}(-\d{2}(_\d{4}-\d{2}-\d{2})?)?\.csv$")


def _csv_date_span(path: Path) -> tuple[date, date] | None:
    """Return the (first, last) calendar date covered by a CSV (blocking)."""
    first: date | None = None
    last: date | None = None
    parsed = 0
    header: list[str] | None = None
    first_data: list[str] | None = None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        header = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            if first_data is None:
                first_data = row
            try:
                day = datetime.strptime(row[1].strip(), "%Y. %m. %d. %H:%M").date()
            except ValueError:
                continue
            parsed += 1
            if first is None or day < first:
                first = day
            if last is None or day > last:
                last = day
    _LOGGER.debug(
        "MVM Next: %s – fejléc=%r, első adatsor=%r, dátumos sorok=%d, span=%s..%s",
        path.name,
        header,
        first_data,
        parsed,
        first,
        last,
    )
    if first is None or last is None:
        return None
    return first, last


def _canonical_name(path: Path) -> str | None:
    """Deterministic file name for the period a CSV covers.

    MVM Next always exports with the same file name (``meresi_adatok_<serial>``),
    so importing a new month would overwrite the previous export on disk. Naming
    each file after the period it contains keeps every export as a separate file.
    """
    span = _csv_date_span(path)
    if span is None:
        return None
    start, end = span
    if start == end:
        return f"mvm_{start.isoformat()}.csv"
    if (
        start.year == end.year
        and start.month == end.month
        and start.day == 1
        and end.day == calendar.monthrange(end.year, end.month)[1]
    ):
        return f"mvm_{start.strftime('%Y-%m')}.csv"
    return f"mvm_{start.isoformat()}_{end.isoformat()}.csv"


def _parse_csv_file(path: Path) -> ParsedFile:
    """Parse one MVM Next CSV export file (blocking, run in executor).

    Deduplication rules, so a doubled or overlapping export never inflates a
    reading:

    * Within one file each 15-minute timestamp maps to exactly ONE value. If
      the same local timestamp shows up again it is either the repeated hour of
      the autumn DST fall-back night (at most four quarter-hours, kept as the
      post-transition ``fold=1`` occurrence) or an accidentally duplicated /
      concatenated row (every extra occurrence is dropped, with a warning).

    The result is a ``{UTC quarter-hour ISO: kWh}`` map with one value per
    slot; hourly aggregation and cross-file merging happen in the coordinator.
    """
    stat = path.stat()
    result = ParsedFile(mtime=stat.st_mtime, size=stat.st_size)

    rows: list[tuple[datetime, float, str]] = []
    total = 0
    rejected: dict[str, int] = {}
    sample_row: list[str] | None = None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        next(reader, None)  # header row

        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            total += 1
            if sample_row is None:
                sample_row = row
            if len(row) < 6:
                rejected["too_few_columns"] = rejected.get("too_few_columns", 0) + 1
                continue
            serial, idopont, adat_tipus, statusz, ertek, mertekegyseg = (
                cell.strip() for cell in row[:6]
            )

            if adat_tipus != CSV_DIRECTION:
                rejected["adat_tipus"] = rejected.get("adat_tipus", 0) + 1
                continue
            if statusz != CSV_STATUS_OK:
                rejected["statusz"] = rejected.get("statusz", 0) + 1
                continue
            if mertekegyseg != CSV_UNIT:
                rejected["mertekegyseg"] = rejected.get("mertekegyseg", 0) + 1
                continue

            try:
                naive = datetime.strptime(idopont, "%Y. %m. %d. %H:%M")
                value = float(ertek)
            except ValueError:
                rejected["unparsable"] = rejected.get("unparsable", 0) + 1
                _LOGGER.debug("Skipping unparsable row in %s: %s", path.name, row)
                continue

            rows.append((naive, value, serial))

    if not rows:
        _LOGGER.warning(
            "MVM Next: %s – egyetlen feldolgozható sor sincs (%d adatsorból). "
            "Elutasítva: %s. Első sor mintája: %r. Elvárt oszlopok pontosvesszővel "
            "elválasztva: 'Gyári szám;Időpont;Adatpont típus;Státusz;Érték;"
            "Mértékegység', ahol Adatpont típus=%r, Státusz=%r, Mértékegység=%r.",
            path.name,
            total,
            rejected or "nincs adatsor",
            sample_row,
            CSV_DIRECTION,
            CSV_STATUS_OK,
            CSV_UNIT,
        )
    else:
        _LOGGER.debug(
            "MVM Next: %s – %d/%d sor elfogadva (elutasítva: %s)",
            path.name,
            len(rows),
            total,
            rejected or "nincs",
        )

    counts: dict[datetime, int] = {}
    for naive, _value, _serial in rows:
        counts[naive] = counts.get(naive, 0) + 1

    repeated = [ts for ts, count in counts.items() if count > 1]
    # A single monthly export can contain at most one autumn DST fall-back,
    # i.e. four repeated quarter-hours, each appearing exactly twice. Anything
    # beyond that means the file itself carries duplicated rows.
    dst_fallback = 0 < len(repeated) <= 4 and all(
        counts[ts] == 2 for ts in repeated
    )
    if repeated and not dst_fallback:
        _LOGGER.warning(
            "MVM Next: %s ismétlődő időpontokat tartalmaz (%d db); minden "
            "időponthoz csak az első mérési érték kerül feldolgozásra",
            path.name,
            len(repeated),
        )

    quarters: dict[datetime, float] = {}  # UTC quarter start -> kWh
    seen_naive: dict[datetime, int] = {}
    last_quarter_naive: datetime | None = None

    for naive, value, serial in rows:
        occurrence = seen_naive.get(naive, 0)
        seen_naive[naive] = occurrence + 1

        if occurrence and not dst_fallback:
            continue  # duplicated row - keep only the first value for this ts
        fold = 1 if (occurrence and dst_fallback) else 0

        local_dt = naive.replace(tzinfo=BUDAPEST_TZ, fold=fold)
        utc_quarter = local_dt.astimezone(timezone.utc).replace(
            second=0, microsecond=0
        )
        quarters[utc_quarter] = value  # assign, never add: one value per slot
        result.meter_serial = serial

        if last_quarter_naive is None or naive > last_quarter_naive:
            last_quarter_naive = naive

    result.quarters = {
        utc_quarter.isoformat(): round(value, 3)
        for utc_quarter, value in quarters.items()
    }
    if last_quarter_naive is not None:
        result.last_quarter_local = last_quarter_naive.isoformat()

    return result


def _clear_then_add(
    hass: HomeAssistant,
    statistic_id: str,
    metadata: "StatisticMetaData",
    statistics: list["StatisticData"],
) -> None:
    """Replace a statistic's entire history with the given series.

    The imported CSVs are the single source of truth. Clearing first (the
    task is queued before the re-add, so the recorder runs them in order)
    drops any rows outside the current range - e.g. a month that was
    imported earlier from a CSV that is no longer in the directory - which
    would otherwise leave the cumulative ``sum`` discontinuous and produce
    wild spikes on the Energy dashboard.
    """
    # Local import: homeassistant.components.recorder is only importable
    # once the recorder component is loaded.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    get_instance(hass).async_clear_statistics([statistic_id])
    async_add_external_statistics(hass, metadata, statistics)


def _push_statistics(
    hass: HomeAssistant,
    merged_hourly: dict[str, float],
    statistic_id: str = STATISTIC_ID,
    name: str = STATISTIC_NAME,
) -> float:
    """Push a recomputed hourly energy series as an external long-term statistic.

    The whole series is recomputed from the earliest known hour every time and
    fully replaces the stored statistic, so the cumulative "sum" stays
    consistent after a correction or a removed month.
    """

    metadata: StatisticMetaData = {
        "statistic_id": statistic_id,
        "source": statistic_id.split(":", 1)[0],
        "name": name,
        "unit_of_measurement": STATISTIC_UNIT,
        "has_sum": True,
        "unit_class": STATISTIC_UNIT_CLASS,
    }
    if _HAS_MEAN_TYPE:
        metadata["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover - older HA core
        metadata["has_mean"] = False

    running_total = 0.0
    statistics: list[StatisticData] = []
    for start_iso in sorted(merged_hourly):
        value = merged_hourly[start_iso]
        running_total += value
        statistics.append(
            {
                "start": datetime.fromisoformat(start_iso),
                "state": value,
                "sum": round(running_total, 3),
            }
        )

    _clear_then_add(hass, statistic_id, metadata, statistics)
    return round(running_total, 3)


def _year_allowance(
    year: int, base_threshold: float, start_date: date | None
) -> float:
    """Yearly reduced-price allowance, prorated for a mid-year contract start."""
    if start_date is None or year != start_date.year:
        return base_threshold
    days_in_year = 366 if calendar.isleap(year) else 365
    covered = days_in_year - start_date.timetuple().tm_yday + 1
    return round(base_threshold * covered / days_in_year, 1)


def _period_bucket(
    local: datetime,
    base_threshold: float,
    start_date: date | None,
    period: str,
) -> tuple[tuple, float]:
    """(accounting-bucket key, allowance for that bucket) for a timestamp.

    "monthly": the yearly allowance split by day count into each month (this
    is what a monthly invoice shows); "yearly": one bucket per calendar year.
    """
    if period == "monthly":
        year, month = local.year, local.month
        days_in_year = 366 if calendar.isleap(year) else 365
        days_in_month = calendar.monthrange(year, month)[1]
        if start_date and (year, month) == (start_date.year, start_date.month):
            days_in_month -= start_date.day - 1
        return (year, month), round(base_threshold * days_in_month / days_in_year, 1)
    return (local.year,), _year_allowance(local.year, base_threshold, start_date)


def _compute_cost_hourly(
    merged_hourly: dict[str, float],
    price_low: float,
    price_high: float,
    annual_threshold: float,
    start_date: date | None = None,
    dynamic_high: dict[str, float] | None = None,
    period: str = "yearly",
) -> dict[str, float]:
    """Apply the tiered household tariff to each slot of the series.

    Consumption up to the allowance for its accounting bucket (a month or the
    whole year, per ``period``) is billed at ``price_low``; every kWh above it
    at ``price_high`` - or, for the MVM "D" tariff, at ``dynamic_high[slot]``
    (that slot's historical dynamic price) when a mapping is given. Slots are
    processed in chronological order so one that straddles the threshold is
    split proportionally. Boundaries use Europe/Budapest local time.

    Consumption before ``start_date`` (the account holder's contract start)
    belongs to the previous user and is left out; the bucket the start date
    falls in has its allowance prorated by day count.
    """
    used: dict[tuple, float] = {}
    cost_hourly: dict[str, float] = {}

    for start_iso in sorted(merged_hourly):
        local = datetime.fromisoformat(start_iso).astimezone(BUDAPEST_TZ)
        if start_date is not None and local.date() < start_date:
            continue
        kwh = merged_hourly[start_iso]
        key, allowance = _period_bucket(local, annual_threshold, start_date, period)
        so_far = used.get(key, 0.0)

        low_part = max(0.0, min(kwh, allowance - so_far))
        high_part = kwh - low_part
        hi_price = price_high
        if dynamic_high is not None:
            hi_price = dynamic_high.get(start_iso, price_high)
        cost_hourly[start_iso] = round(
            low_part * price_low + high_part * hi_price, 4
        )
        used[key] = so_far + kwh

    return cost_hourly


def _push_cost_statistics(
    hass: HomeAssistant,
    currency: str,
    cost_hourly: dict[str, float],
    statistic_id: str = COST_STATISTIC_ID,
    name: str = COST_STATISTIC_NAME,
) -> float:
    """Push a derived cost series as an external statistic (currency unit)."""
    metadata: StatisticMetaData = {
        "statistic_id": statistic_id,
        "source": statistic_id.split(":", 1)[0],
        "name": name,
        "unit_of_measurement": currency,
        "has_sum": True,
        "unit_class": None,
    }
    if _HAS_MEAN_TYPE:
        metadata["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover - older HA core
        metadata["has_mean"] = False

    running_total = 0.0
    statistics: list[StatisticData] = []
    for start_iso in sorted(cost_hourly):
        value = cost_hourly[start_iso]
        running_total += value
        statistics.append(
            {
                "start": datetime.fromisoformat(start_iso),
                "state": value,
                "sum": round(running_total, 4),
            }
        )

    _clear_then_add(hass, statistic_id, metadata, statistics)
    return round(running_total, 2)


def _sum_to_hourly(per_slot: dict[str, float]) -> dict[str, float]:
    """Roll a per-15-minute (or any sub-hour) series up to whole hours."""
    hourly: dict[str, float] = {}
    for iso, value in per_slot.items():
        hour = (
            datetime.fromisoformat(iso)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )
        hourly[hour] = round(hourly.get(hour, 0.0) + value, 4)
    return hourly


def _monthly_totals(cost_hourly: dict[str, float]) -> dict[str, float]:
    """{YYYY-MM (Budapest): summed cost} for a cost-per-hour series."""
    months: dict[str, float] = {}
    for start_iso, value in cost_hourly.items():
        month = (
            datetime.fromisoformat(start_iso)
            .astimezone(BUDAPEST_TZ)
            .strftime("%Y-%m")
        )
        months[month] = round(months.get(month, 0.0) + value, 2)
    return months


def _restrict_from(
    merged_hourly: dict[str, float], start_date: date | None
) -> dict[str, float]:
    """Drop hours before start_date (the account holder's contract start)."""
    if start_date is None:
        return merged_hourly
    return {
        iso: kwh
        for iso, kwh in merged_hourly.items()
        if datetime.fromisoformat(iso).astimezone(BUDAPEST_TZ).date() >= start_date
    }


def _restrict_before(
    merged_hourly: dict[str, float], start_date: date | None
) -> dict[str, float]:
    """Only the hours before start_date - the previous account holder's."""
    if start_date is None:
        return {}
    return {
        iso: kwh
        for iso, kwh in merged_hourly.items()
        if datetime.fromisoformat(iso).astimezone(BUDAPEST_TZ).date() < start_date
    }


def _compute_year_summary(
    merged_hourly: dict[str, float],
    cost_hourly: dict[str, float],
    annual_threshold: float,
    start_date: date | None = None,
    period: str = "yearly",
) -> dict[str, object]:
    """Current-calendar-year figures for the allowance dashboard.

    The tiered allowance resets on 1 January, so the dashboard tracks where
    this year stands: kWh used, kWh left at the reduced price, which price
    tier is active now and - from the average daily use so far this year -
    an estimate of when the market price kicks in. Consumption before
    ``start_date`` (a mid-year contract start) is ignored and the allowance
    is prorated for that year.
    """
    now_local = datetime.now(BUDAPEST_TZ)
    year = now_local.year
    allowance = _year_allowance(year, annual_threshold, start_date)

    year_hours: dict[date, float] = {}
    year_kwh = 0.0
    for start_iso, kwh in merged_hourly.items():
        local = datetime.fromisoformat(start_iso).astimezone(BUDAPEST_TZ)
        if local.year != year:
            continue
        if start_date is not None and local.date() < start_date:
            continue
        year_kwh += kwh
        day = local.date()
        year_hours[day] = year_hours.get(day, 0.0) + kwh

    year_cost = round(
        sum(
            cost
            for start_iso, cost in cost_hourly.items()
            if datetime.fromisoformat(start_iso).astimezone(BUDAPEST_TZ).year == year
        ),
        2,
    )

    remaining = round(allowance - year_kwh, 1)
    used_pct = round(year_kwh / allowance * 100, 1) if allowance else 0.0
    tier = "piaci" if remaining <= 0 else "kedvezmenyes"

    crossover = "atlepve" if remaining <= 0 else "ismeretlen"
    if year_hours and remaining > 0:
        first_day = min(year_hours)
        last_day = max(year_hours)
        span_days = max(1, (last_day - first_day).days + 1)
        daily_avg = year_kwh / span_days
        if daily_avg > 0:
            hit = last_day + timedelta(days=remaining / daily_avg)
            crossover = (
                f"{year}. utan" if hit.year > year else hit.isoformat()
            )

    return {
        "year": year,
        "year_allowance": round(allowance, 1),
        "year_consumption": round(year_kwh, 2),
        "year_cost": year_cost,
        "allowance_remaining": remaining,
        "allowance_used_pct": used_pct,
        "price_tier": tier,
        "tier_crossover_estimate": crossover,
        "data_through": max(year_hours).isoformat() if year_hours else None,
        "contract_start": start_date.isoformat() if start_date else None,
        "allowance_period": period,
    }


class MvmImportCoordinator:
    """Owns the import directory scan, on-disk cache and statistics push."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._files: dict[str, ParsedFile] = {}
        self.attributes: dict[str, object] = {}
        self.year_summary: dict[str, object] = {}
        self.current_d: dict[str, float] = {}
        self.state_last_quarter: str | None = None

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="MVM Next Energy Import",
            manufacturer="MVM Next",
            model="CSV Import",
        )

    def _opt(self, key: str, default):
        """Read a setting from entry.options, falling back to entry.data."""
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def import_dir(self) -> Path:
        configured = self._opt(CONF_IMPORT_DIR, "") or ""
        return Path(str(configured).strip() or self.hass.config.path("mvm_next"))

    @property
    def cost_enabled(self) -> bool:
        return bool(self._opt(CONF_COST_ENABLED, DEFAULT_COST_ENABLED))

    @property
    def price_low(self) -> float:
        return float(self._opt(CONF_PRICE_LOW, DEFAULT_PRICE_LOW))

    @property
    def price_high(self) -> float:
        return float(self._opt(CONF_PRICE_HIGH, DEFAULT_PRICE_HIGH))

    @property
    def annual_threshold(self) -> float:
        return float(self._opt(CONF_ANNUAL_THRESHOLD, DEFAULT_ANNUAL_THRESHOLD))

    @property
    def allowance_period(self) -> str:
        value = str(self._opt(CONF_ALLOWANCE_PERIOD, DEFAULT_ALLOWANCE_PERIOD))
        return value if value in ("monthly", "yearly") else DEFAULT_ALLOWANCE_PERIOD

    @property
    def d_enabled(self) -> bool:
        return bool(self._opt(CONF_D_ENABLED, DEFAULT_D_ENABLED))

    @property
    def d_config(self) -> DTariffConfig:
        return DTariffConfig(
            merchant_fee=float(self._opt(CONF_D_MERCHANT_FEE, DEFAULT_D_MERCHANT_FEE)),
            transmission_fee=float(
                self._opt(CONF_D_TRANSMISSION_FEE, DEFAULT_D_TRANSMISSION_FEE)
            ),
            distribution_fee=float(
                self._opt(CONF_D_DISTRIBUTION_FEE, DEFAULT_D_DISTRIBUTION_FEE)
            ),
            vat_percent=float(self._opt(CONF_D_VAT_PERCENT, DEFAULT_D_VAT_PERCENT)),
            eur_huf=float(self._opt(CONF_D_EUR_HUF, DEFAULT_D_EUR_HUF)),
        )

    @property
    def contract_start(self) -> date | None:
        raw = str(self._opt(CONF_START_DATE, "") or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            _LOGGER.warning("MVM Next: érvénytelen felhasználóváltás dátum: %s", raw)
            return None

    async def async_load_cache(self) -> None:
        stored = await self._store.async_load()
        if not stored:
            return
        for name, data in stored.get("files", {}).items():
            try:
                self._files[name] = ParsedFile(**data)
            except TypeError:
                # Cache written by an older version - drop it so the file is
                # re-parsed from disk on the next import.
                _LOGGER.debug("MVM Next: elavult gyorsítótár-bejegyzés eldobva: %s", name)
        self.attributes = stored.get("attributes", {})
        self.year_summary = stored.get("year_summary", {})
        self.state_last_quarter = stored.get("state_last_quarter")

    async def _async_save_cache(self) -> None:
        await self._store.async_save(
            {
                "files": {name: pf.__dict__ for name, pf in self._files.items()},
                "attributes": self.attributes,
                "year_summary": self.year_summary,
                "state_last_quarter": self.state_last_quarter,
            }
        )

    async def async_refresh_current(self) -> None:
        """Update the 'current D price' sensors (called on a timer)."""
        if not self.d_enabled:
            if self.current_d:
                self.current_d = {}
                async_dispatcher_send(self.hass, SIGNAL_UPDATE)
            return
        store_key = f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}"
        try:
            data = await async_current_d_price(self.hass, store_key, self.d_config)
        except Exception:  # noqa: BLE001 - a timer callback must not raise
            _LOGGER.debug("MVM Next: aktuális D ár lekérés hiba", exc_info=True)
            return
        if not data:
            return
        try:
            data["forecast"] = await async_d_price_forecast(
                self.hass, store_key, self.d_config
            )
        except Exception:  # noqa: BLE001 - a timer callback must not raise
            _LOGGER.debug("MVM Next: D ár előrejelzés hiba", exc_info=True)
            data["forecast"] = self.current_d.get("forecast", [])
        self.current_d = data
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def async_upload(self, file_id: str) -> None:
        """Store a browser-uploaded CSV in the import directory and import it.

        file_id is the token produced by the frontend file selector; Home
        Assistant keeps the uploaded payload in a temporary location until
        process_uploaded_file() hands us its path (and cleans it up on exit).
        The file lands under a temporary name; async_import() then renames it
        to the period it covers, so uploads never overwrite each other.
        """
        from homeassistant.components.file_upload import process_uploaded_file

        def _store() -> None:
            with process_uploaded_file(self.hass, file_id) as src:
                self.import_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                shutil.copyfile(src, self.import_dir / f"mvm_upload_{stamp}.csv")

        _LOGGER.debug("MVM Next: feltöltés indul, file_id=%s", file_id)
        await self.hass.async_add_executor_job(_store)
        await self.async_import(None)
        _LOGGER.info(
            "MVM Next: feltöltés feldolgozva, %s óra a statisztikában",
            self.attributes.get("imported_hours"),
        )

    async def async_import(self, file_path: str | None) -> None:
        """Scan the import directory and (re)push updated statistics.

        If file_path is given, that single file is force-reparsed even if it
        looks unchanged (used for the explicit "import this file" action);
        every other already-known file is still reused from cache unless it
        changed on disk too.
        """
        await self.hass.async_add_executor_job(
            lambda: self.import_dir.mkdir(parents=True, exist_ok=True)
        )

        force_reparse: str | None = None
        if file_path:
            path = Path(file_path)
            if not path.is_absolute():
                path = self.import_dir / path
            if not await self.hass.async_add_executor_job(path.is_file):
                raise HomeAssistantError(f"Fájl nem található: {path}")
            force_reparse = path.name

        def _scan_and_normalize() -> tuple[list[Path], dict[str, str]]:
            """Glob the *.csv files, renaming each to the period it covers.

            MVM Next exports all share one file name, so without this a new
            month's export would overwrite the previous one on disk.
            """
            found = sorted(
                p
                for p in self.import_dir.glob("*")
                if p.is_file() and p.suffix.lower() == ".csv"
            )
            result: list[Path] = []
            renamed: dict[str, str] = {}
            spans: dict[str, str] = {}
            for path in found:
                span = _csv_date_span(path)
                spans[path.name] = f"{span[0]} … {span[1]}" if span else "nincs dátum"
                if _CANON_RE.match(path.name):
                    result.append(path)
                    continue
                canon = _canonical_name(path)
                if not canon or canon == path.name:
                    result.append(path)
                    continue
                target = self.import_dir / canon
                path.replace(target)  # atomic; overwrites a same-period re-export
                renamed[path.name] = canon
                result.append(target)
            # Two source files may map to the same period name.
            return sorted(set(result)), renamed, spans

        csv_files, renamed, spans = await self.hass.async_add_executor_job(
            _scan_and_normalize
        )
        for name, span in spans.items():
            _LOGGER.info("MVM Next: fájl %s – felismert időszak: %s", name, span)

        for old_name, new_name in renamed.items():
            _LOGGER.info(
                "MVM Next: fájl átnevezve a benne lévő időszak szerint: %s -> %s",
                old_name,
                new_name,
            )
            self._files.pop(old_name, None)
            if force_reparse == old_name:
                force_reparse = new_name
        _LOGGER.info(
            "MVM Next: import indul, könyvtár=%s, talált CSV=%d %s",
            self.import_dir,
            len(csv_files),
            [p.name for p in csv_files],
        )

        current_names = {p.name for p in csv_files}
        for stale_name in set(self._files) - current_names:
            _LOGGER.warning(
                "MVM Next importált fájl eltűnt a könyvtárból: %s – a hozzá tartozó "
                "adatok kikerülnek a statisztikából (a könyvtárban lévő CSV-k az "
                "egyetlen forrás). Ha meg akarod tartani, tedd vissza a fájlt.",
                stale_name,
            )
            self._files.pop(stale_name, None)

        for path in csv_files:
            stat = await self.hass.async_add_executor_job(path.stat)
            cached = self._files.get(path.name)
            unchanged = (
                cached is not None
                and cached.mtime == stat.st_mtime
                and cached.size == stat.st_size
            )
            if unchanged and path.name != force_reparse:
                continue

            _LOGGER.debug("MVM Next CSV feldolgozása: %s", path.name)
            parsed = await self.hass.async_add_executor_job(_parse_csv_file, path)
            self._files[path.name] = parsed

        if not self._files:
            _LOGGER.warning(
                "MVM Next: nincs feldolgozható CSV a(z) %s könyvtárban – az import "
                "nem csinál semmit. Ellenőrizd, hogy a CSV fájlok ebben a "
                "könyvtárban vannak-e (a HaOS legújabb verzióin a config mappa "
                "elérése /homeassistant, nem /config). A könyvtár a "
                "Beállítás → Import könyvtár menüben módosítható.",
                self.import_dir,
            )
            return

        merged_quarters: dict[str, float] = {}
        latest_quarter: str | None = None
        latest_source_file: str | None = None
        latest_meter_serial: str | None = None

        # Oldest file first, newest last, so that when two exports cover the
        # same quarter-hour the value from the more recently written file wins.
        # dict assignment (not addition) means an overlapping period is never
        # counted twice - one timestamp keeps exactly one value.
        for name, parsed in sorted(
            self._files.items(), key=lambda kv: (kv[1].mtime, kv[0])
        ):
            merged_quarters.update(parsed.quarters)
            if parsed.last_quarter_local and (
                latest_quarter is None or parsed.last_quarter_local > latest_quarter
            ):
                latest_quarter = parsed.last_quarter_local
                latest_source_file = name
                latest_meter_serial = parsed.meter_serial

        # Sum the four quarter-hours of each hour - the only place values add up.
        merged_hourly: dict[str, float] = {}
        for quarter_iso, value in merged_quarters.items():
            hour_iso = datetime.fromisoformat(quarter_iso).replace(minute=0).isoformat()
            merged_hourly[hour_iso] = round(merged_hourly.get(hour_iso, 0.0) + value, 3)

        if not merged_hourly:
            _LOGGER.warning(
                "MVM Next: a(z) %d CSV fájlból egyetlen mérési adat sem jött ki – "
                "a statisztika nem frissül. Nézd meg a fenti figyelmeztetéseket a "
                "CSV formátumáról.",
                len(self._files),
            )
            return

        _LOGGER.info(
            "MVM Next: %d negyedóra, %d óra kerül a statisztikába (utolsó: %s)",
            len(merged_quarters),
            len(merged_hourly),
            latest_quarter,
        )

        # async_add_external_statistics is a @callback: it must run on the
        # event loop, not in an executor thread.
        total_energy = _push_statistics(self.hass, merged_hourly)

        # If a contract-start date is set, also split the raw meter history
        # into "yours" and "the previous account holder's" as their own
        # statistics - so both remain visible/chartable on their own, while
        # only the "own" part (and everything, if no date is set) feeds the
        # cost/allowance calculation below.
        previous_total_kwh = 0.0
        previous_monthly: dict[str, float] = {}
        previous_quarters: dict[str, float] = {}
        if self.contract_start is not None:
            own_consumption = _restrict_from(merged_hourly, self.contract_start)
            previous_consumption = _restrict_before(merged_hourly, self.contract_start)
            previous_quarters = _restrict_before(merged_quarters, self.contract_start)
            _push_statistics(
                self.hass, own_consumption, STATISTIC_OWN_ID, STATISTIC_OWN_NAME
            )
            _push_statistics(
                self.hass,
                previous_consumption,
                STATISTIC_PREVIOUS_ID,
                STATISTIC_PREVIOUS_NAME,
            )
            previous_total_kwh = round(sum(previous_consumption.values()), 2)
            previous_monthly = _monthly_totals(previous_consumption)

        # Always compute the tiered cost series: the cost statistic push is
        # optional, but the allowance dashboard needs the yearly figures.
        # Tiering runs at 15-minute resolution (the raw meter granularity, and
        # what the "D" tariff uses); the result is rolled up to hourly for the
        # long-term statistic.
        cost_quarterly = _compute_cost_hourly(
            merged_quarters,
            self.price_low,
            self.price_high,
            self.annual_threshold,
            self.contract_start,
            period=self.allowance_period,
        )
        cost_hourly = _sum_to_hourly(cost_quarterly)

        total_cost: float | None = None
        _LOGGER.info(
            "MVM Next: költségszámítás %s (pénznem=%s, árak=%.2f / %.3f, keret=%.0f kWh)",
            "BE" if self.cost_enabled else "KI",
            self.hass.config.currency,
            self.price_low,
            self.price_high,
            self.annual_threshold,
        )
        cost_error: str | None = None
        if self.cost_enabled:
            currency = self.hass.config.currency or "HUF"
            try:
                total_cost = _push_cost_statistics(self.hass, currency, cost_hourly)
            except Exception as err:  # noqa: BLE001 - keep the consumption import intact
                cost_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception(
                    "MVM Next: a(z) %s költség-statisztika feltöltése nem sikerült",
                    COST_STATISTIC_ID,
                )
            else:
                _LOGGER.info(
                    "MVM Next: költség statisztika frissítve, összesen %.0f %s "
                    "(%.2f / %.3f %s per kWh, éves sáv %.0f kWh)",
                    total_cost,
                    currency,
                    self.price_low,
                    self.price_high,
                    currency,
                    self.annual_threshold,
                )

        self.year_summary = _compute_year_summary(
            merged_hourly,
            cost_hourly,
            self.annual_threshold,
            self.contract_start,
            self.allowance_period,
        )

        # --- MVM "D" (dynamic) tariff what-if comparison ----------------------
        # The "D" tariff keeps the tiered A1 allowance: consumption up to the
        # yearly limit stays on the reduced price, only the overage is priced
        # at the hourly HUPX-based dynamic price.
        d_error: str | None = None
        if self.d_enabled:
            year = self.year_summary["year"]
            own_quarters = _restrict_from(merged_quarters, self.contract_start)
            try:
                d_prices, d_meta = await async_d_gross_prices(
                    self.hass,
                    f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}",
                    list(own_quarters),
                    self.d_config,
                )
                d_cost_quarterly = _compute_cost_hourly(
                    own_quarters,
                    self.price_low,
                    self.price_high,
                    self.annual_threshold,
                    self.contract_start,
                    dynamic_high=d_prices,
                    period=self.allowance_period,
                )
                d_cost_hourly = _sum_to_hourly(d_cost_quarterly)
                currency = self.hass.config.currency or "HUF"
                d_total = _push_cost_statistics(
                    self.hass,
                    currency,
                    d_cost_hourly,
                    COST_D_STATISTIC_ID,
                    COST_D_STATISTIC_NAME,
                )
                a1_monthly = _monthly_totals(
                    _restrict_from(cost_quarterly, self.contract_start)
                )
                d_monthly = _monthly_totals(d_cost_quarterly)
                months = sorted(set(a1_monthly) | set(d_monthly))
                d_year = round(
                    sum(
                        c
                        for iso, c in d_cost_quarterly.items()
                        if datetime.fromisoformat(iso).astimezone(BUDAPEST_TZ).year
                        == year
                    ),
                    2,
                )
                a1_year = self.year_summary.get("year_cost", 0.0)
                self.year_summary.update(
                    {
                        "d_enabled": True,
                        "d_total_cost": d_total,
                        "year_cost_d": d_year,
                        "year_cost_a1_vs_d": round(d_year - a1_year, 2),
                        "d_slots_priced": d_meta.get("slots_priced"),
                        "d_slots_total": d_meta.get("slots_total"),
                        "d_eur_huf_source": d_meta.get("eur_huf"),
                        "monthly_comparison": [
                            {
                                "month": m,
                                "a1": a1_monthly.get(m, 0.0),
                                "d": d_monthly.get(m, 0.0),
                            }
                            for m in months
                        ],
                    }
                )
                _LOGGER.info(
                    "MVM Next: D tarifa – %d/%d negyedóra beárazva (%s árfolyam), "
                    "idei D költség %.0f, A1 %.0f (különbség %.0f)",
                    d_meta.get("slots_priced", 0),
                    d_meta.get("slots_total", 0),
                    d_meta.get("eur_huf"),
                    d_year,
                    a1_year,
                    d_year - a1_year,
                )
            except Exception as err:  # noqa: BLE001 - keep the import intact
                d_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("MVM Next: D tarifa számítás hiba")
        self.year_summary["d_error"] = d_error

        # --- Hypothetical "what if the previous tenant had used A1 / D"? ---
        # Purely informational: applies your price settings and (for D) the
        # actual historical HUPX prices to the previous account holder's own
        # consumption, as if that consumption had been yours - it never feeds
        # into the real allowance/cost figures above.
        previous_cost_error: str | None = None
        previous_cost_summary: dict[str, object] = {}
        if self.contract_start is not None and previous_quarters and self.cost_enabled:
            try:
                prev_cost_quarterly = _compute_cost_hourly(
                    previous_quarters,
                    self.price_low,
                    self.price_high,
                    self.annual_threshold,
                    None,
                    period=self.allowance_period,
                )
                prev_cost_hourly = _sum_to_hourly(prev_cost_quarterly)
                currency = self.hass.config.currency or "HUF"
                prev_a1_total = _push_cost_statistics(
                    self.hass,
                    currency,
                    prev_cost_hourly,
                    COST_PREVIOUS_STATISTIC_ID,
                    COST_PREVIOUS_STATISTIC_NAME,
                )
                prev_monthly_a1 = _monthly_totals(prev_cost_quarterly)
                prev_monthly_d: dict[str, float] = {}
                prev_d_total: float | None = None

                if self.d_enabled:
                    prev_d_prices, prev_d_meta = await async_d_gross_prices(
                        self.hass,
                        f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}",
                        list(previous_quarters),
                        self.d_config,
                    )
                    prev_cost_quarterly_d = _compute_cost_hourly(
                        previous_quarters,
                        self.price_low,
                        self.price_high,
                        self.annual_threshold,
                        None,
                        dynamic_high=prev_d_prices,
                        period=self.allowance_period,
                    )
                    prev_cost_hourly_d = _sum_to_hourly(prev_cost_quarterly_d)
                    prev_d_total = _push_cost_statistics(
                        self.hass,
                        currency,
                        prev_cost_hourly_d,
                        COST_D_PREVIOUS_STATISTIC_ID,
                        COST_D_PREVIOUS_STATISTIC_NAME,
                    )
                    prev_monthly_d = _monthly_totals(prev_cost_quarterly_d)
                    _LOGGER.info(
                        "MVM Next: előző lakó – D tarifa %d/%d negyedóra beárazva",
                        prev_d_meta.get("slots_priced", 0),
                        prev_d_meta.get("slots_total", 0),
                    )

                months = sorted(set(prev_monthly_a1) | set(prev_monthly_d))
                previous_cost_summary = {
                    "previous_cost_a1_total": prev_a1_total,
                    "previous_cost_d_total": prev_d_total,
                    "previous_monthly_comparison": [
                        {
                            "month": m,
                            "a1": prev_monthly_a1.get(m, 0.0),
                            "d": prev_monthly_d.get(m, 0.0),
                        }
                        for m in months
                    ],
                }
            except Exception as err:  # noqa: BLE001 - keep the import intact
                previous_cost_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception(
                    "MVM Next: előző lakó hipotetikus költségszámítás hiba"
                )

        _LOGGER.info("MVM Next: idei összegzés: %s", self.year_summary)

        now_local = datetime.now(BUDAPEST_TZ)
        self.state_last_quarter = (
            latest_quarter.replace("T", " ") if latest_quarter else None
        )
        self.attributes = {
            "last_import": now_local.strftime("%Y-%m-%d %H:%M"),
            "imported_quarters": len(merged_quarters),
            "imported_hours": len(merged_hourly),
            "total_energy": total_energy,
            "total_cost": total_cost,
            "cost_enabled": self.cost_enabled,
            "cost_currency": self.hass.config.currency,
            "cost_error": cost_error,
            "meter_serial": latest_meter_serial,
            "source_file": latest_source_file,
            "import_dir": str(self.import_dir),
            "statistic_id": STATISTIC_ID,
            "cost_statistic_id": COST_STATISTIC_ID if self.cost_enabled else None,
            "own_consumption_statistic_id": (
                STATISTIC_OWN_ID if self.contract_start else None
            ),
            "previous_consumption_statistic_id": (
                STATISTIC_PREVIOUS_ID if self.contract_start else None
            ),
            "previous_tenant_kwh": previous_total_kwh,
            "previous_tenant_monthly": previous_monthly or None,
            "previous_cost_a1_total": previous_cost_summary.get("previous_cost_a1_total"),
            "previous_cost_d_total": previous_cost_summary.get("previous_cost_d_total"),
            "previous_monthly_comparison": previous_cost_summary.get(
                "previous_monthly_comparison"
            ),
            "previous_cost_error": previous_cost_error,
            "previous_cost_statistic_id": (
                COST_PREVIOUS_STATISTIC_ID
                if previous_cost_summary.get("previous_cost_a1_total") is not None
                else None
            ),
            "previous_cost_d_statistic_id": (
                COST_D_PREVIOUS_STATISTIC_ID
                if previous_cost_summary.get("previous_cost_d_total") is not None
                else None
            ),
        }

        await self._async_save_cache()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
