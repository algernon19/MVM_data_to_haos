"""CSV parsing and long-term statistics import for MVM Next Energy Import."""
from __future__ import annotations

import calendar
import csv
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    CONF_ANNUAL_THRESHOLD,
    CONF_COST_ENABLED,
    CONF_IMPORT_DIR,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    COST_STATISTIC_ID,
    COST_STATISTIC_NAME,
    CSV_DIRECTION,
    CSV_STATUS_OK,
    CSV_UNIT,
    DEFAULT_ANNUAL_THRESHOLD,
    DEFAULT_COST_ENABLED,
    DEFAULT_PRICE_HIGH,
    DEFAULT_PRICE_LOW,
    DOMAIN,
    SIGNAL_UPDATE,
    STATISTIC_ID,
    STATISTIC_NAME,
    STATISTIC_UNIT,
    STATISTIC_UNIT_CLASS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TIME_ZONE,
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


def _push_statistics(hass: HomeAssistant, merged_hourly: dict[str, float]) -> float:
    """Push the full recomputed hourly series as external long-term statistics.

    The whole series is recomputed from the earliest known hour every time and
    fully replaces the stored statistic, so the cumulative "sum" stays
    consistent after a correction or a removed month.
    """

    metadata: StatisticMetaData = {
        "statistic_id": STATISTIC_ID,
        "source": STATISTIC_ID.split(":", 1)[0],
        "name": STATISTIC_NAME,
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

    _clear_then_add(hass, STATISTIC_ID, metadata, statistics)
    return round(running_total, 3)


def _compute_cost_hourly(
    merged_hourly: dict[str, float],
    price_low: float,
    price_high: float,
    annual_threshold: float,
) -> dict[str, float]:
    """Apply the tiered household tariff to each hour of the series.

    Within a calendar year the consumption up to ``annual_threshold`` kWh is
    billed at ``price_low``; every kWh above it at ``price_high``. Hours are
    processed in chronological order so an hour that straddles the threshold is
    split proportionally. Year boundaries use Europe/Budapest local time.
    """
    year_used: dict[int, float] = {}
    cost_hourly: dict[str, float] = {}

    for start_iso in sorted(merged_hourly):
        kwh = merged_hourly[start_iso]
        year = datetime.fromisoformat(start_iso).astimezone(BUDAPEST_TZ).year
        used = year_used.get(year, 0.0)

        low_part = max(0.0, min(kwh, annual_threshold - used))
        high_part = kwh - low_part
        cost_hourly[start_iso] = round(
            low_part * price_low + high_part * price_high, 4
        )
        year_used[year] = used + kwh

    return cost_hourly


def _push_cost_statistics(
    hass: HomeAssistant, currency: str, cost_hourly: dict[str, float]
) -> float:
    """Push the derived cost series as a second external statistic (currency)."""
    metadata: StatisticMetaData = {
        "statistic_id": COST_STATISTIC_ID,
        "source": COST_STATISTIC_ID.split(":", 1)[0],
        "name": COST_STATISTIC_NAME,
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

    _clear_then_add(hass, COST_STATISTIC_ID, metadata, statistics)
    return round(running_total, 2)


class MvmImportCoordinator:
    """Owns the import directory scan, on-disk cache and statistics push."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._files: dict[str, ParsedFile] = {}
        self.attributes: dict[str, object] = {}
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
        self.state_last_quarter = stored.get("state_last_quarter")

    async def _async_save_cache(self) -> None:
        await self._store.async_save(
            {
                "files": {name: pf.__dict__ for name, pf in self._files.items()},
                "attributes": self.attributes,
                "state_last_quarter": self.state_last_quarter,
            }
        )

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
            cost_hourly = _compute_cost_hourly(
                merged_hourly,
                self.price_low,
                self.price_high,
                self.annual_threshold,
            )
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
        }

        await self._async_save_cache()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
