"""CSV parsing and long-term statistics import for MVM Next Energy Import."""
from __future__ import annotations

import csv
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    CSV_DIRECTION,
    CSV_STATUS_OK,
    CSV_UNIT,
    DEFAULT_IMPORT_DIR,
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
    hourly: dict[str, float] = field(default_factory=dict)  # UTC isoformat -> kWh
    quarter_count: int = 0
    meter_serial: str | None = None
    last_quarter_local: str | None = None  # naive local isoformat, e.g. 2026-08-31T23:45:00


def _parse_csv_file(path: Path) -> ParsedFile:
    """Parse one MVM Next CSV export file (blocking, run in executor)."""
    stat = path.stat()
    result = ParsedFile(mtime=stat.st_mtime, size=stat.st_size)
    hourly: dict[datetime, float] = {}
    seen_naive: dict[datetime, int] = {}
    last_quarter_naive: datetime | None = None

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        next(reader, None)  # header row

        for row in reader:
            if len(row) < 6:
                continue
            serial, idopont, adat_tipus, statusz, ertek, mertekegyseg = (
                cell.strip() for cell in row[:6]
            )

            if adat_tipus != CSV_DIRECTION:
                continue
            if statusz != CSV_STATUS_OK:
                continue
            if mertekegyseg != CSV_UNIT:
                continue

            try:
                naive = datetime.strptime(idopont, "%Y. %m. %d. %H:%M")
                value = float(ertek)
            except ValueError:
                _LOGGER.debug("Skipping unparsable row in %s: %s", path.name, row)
                continue

            # Disambiguate the repeated local hour on the autumn DST fall-back
            # night (fold=1 for the second, post-transition occurrence).
            occurrence = seen_naive.get(naive, 0)
            seen_naive[naive] = occurrence + 1
            local_dt = naive.replace(tzinfo=BUDAPEST_TZ, fold=min(occurrence, 1))
            hour_start_local = local_dt.replace(minute=0, second=0, microsecond=0)

            hourly[hour_start_local] = hourly.get(hour_start_local, 0.0) + value
            result.quarter_count += 1
            result.meter_serial = serial

            if last_quarter_naive is None or naive > last_quarter_naive:
                last_quarter_naive = naive

    result.hourly = {
        dt.astimezone(timezone.utc).isoformat(): round(value, 3)
        for dt, value in hourly.items()
    }
    if last_quarter_naive is not None:
        result.last_quarter_local = last_quarter_naive.isoformat()

    return result


def _push_statistics(hass: HomeAssistant, merged_hourly: dict[str, float]) -> float:
    """Push the full recomputed hourly series as external long-term statistics.

    Home Assistant upserts external statistics by (statistic_id, start), so
    calling this again with corrected values simply overwrites the affected
    hours. The whole series is recomputed from the earliest known hour every
    time so the cumulative "sum" stays consistent after a correction.
    """
    # Local import: homeassistant.components.recorder is only importable
    # once the recorder component is loaded.
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

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

    async_add_external_statistics(hass, metadata, statistics)
    return round(running_total, 3)


class MvmImportCoordinator:
    """Owns the import directory scan, on-disk cache and statistics push."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, import_dir: str) -> None:
        self.hass = hass
        self.entry = entry
        self.import_dir = Path(import_dir or DEFAULT_IMPORT_DIR)
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

    async def async_load_cache(self) -> None:
        stored = await self._store.async_load()
        if not stored:
            return
        self._files = {
            name: ParsedFile(**data) for name, data in stored.get("files", {}).items()
        }
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

    async def async_upload(self, file_id: str, filename: str | None) -> None:
        """Store a browser-uploaded CSV in the import directory and import it.

        file_id is the token produced by the frontend file selector; Home
        Assistant keeps the uploaded payload in a temporary location until
        process_uploaded_file() hands us its path (and cleans it up on exit).
        """
        from homeassistant.components.file_upload import process_uploaded_file

        def _store() -> str:
            with process_uploaded_file(self.hass, file_id) as src:
                target_name = (filename or src.name).strip() or src.name
                target_name = Path(target_name).name
                if not target_name.lower().endswith(".csv"):
                    target_name += ".csv"
                self.import_dir.mkdir(parents=True, exist_ok=True)
                dest = self.import_dir / target_name
                shutil.copyfile(src, dest)
            return target_name

        stored_name = await self.hass.async_add_executor_job(_store)
        _LOGGER.info("MVM Next: feltöltött fájl mentve: %s", stored_name)
        await self.async_import(stored_name)

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

        csv_files = await self.hass.async_add_executor_job(
            lambda: sorted(self.import_dir.glob("*.csv"))
        )

        current_names = {p.name for p in csv_files}
        for stale_name in set(self._files) - current_names:
            _LOGGER.warning(
                "MVM Next importált fájl eltűnt a könyvtárból: %s (a hozzá tartozó "
                "korábban importált adatok a statisztikában megmaradnak)",
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
            _LOGGER.info(
                "MVM Next: nincs feldolgozható CSV a(z) %s könyvtárban", self.import_dir
            )
            return

        merged_hourly: dict[str, float] = {}
        latest_quarter: str | None = None
        latest_source_file: str | None = None
        latest_meter_serial: str | None = None
        imported_quarters = 0

        for name in sorted(self._files):
            parsed = self._files[name]
            merged_hourly.update(parsed.hourly)
            imported_quarters += parsed.quarter_count
            if parsed.last_quarter_local and (
                latest_quarter is None or parsed.last_quarter_local > latest_quarter
            ):
                latest_quarter = parsed.last_quarter_local
                latest_source_file = name
                latest_meter_serial = parsed.meter_serial

        # async_add_external_statistics is a @callback: it must run on the
        # event loop, not in an executor thread.
        total_energy = _push_statistics(self.hass, merged_hourly)

        now_local = datetime.now(BUDAPEST_TZ)
        self.state_last_quarter = (
            latest_quarter.replace("T", " ") if latest_quarter else None
        )
        self.attributes = {
            "last_import": now_local.strftime("%Y-%m-%d %H:%M"),
            "imported_quarters": imported_quarters,
            "imported_hours": len(merged_hourly),
            "total_energy": total_energy,
            "meter_serial": latest_meter_serial,
            "source_file": latest_source_file,
            "import_dir": str(self.import_dir),
            "statistic_id": STATISTIC_ID,
        }

        await self._async_save_cache()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
