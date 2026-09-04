"""MVM 'D' (dynamic, HUPX-based) tariff: historical price fetch and cost.

Opt-in what-if comparison. Historical hourly day-ahead prices for the
Hungarian bidding zone come from api.energy-charts.info (free, no token).
The per-kWh gross price of an hour is

    ( hupx_eur_mwh * eur_huf / 1000 + merchant + transmission + distribution )
    * (1 + vat / 100)

with the fees and the EUR/HUF rate configurable. Fetched prices are cached
on disk keyed by UTC hour, so a re-import does not re-download them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import ENERGY_CHARTS_PRICE_URL

_LOGGER = logging.getLogger(__name__)

_CHUNK_DAYS = 45
_BIDDING_ZONE = "HU"


@dataclass(frozen=True)
class DTariffConfig:
    """User-tunable parts of the dynamic tariff price formula."""

    merchant_fee: float
    transmission_fee: float
    distribution_fee: float
    vat_percent: float
    eur_huf: float

    def gross_price(self, eur_mwh: float) -> float:
        """Gross HUF/kWh for a raw day-ahead price."""
        net = (
            eur_mwh * self.eur_huf / 1000.0
            + self.merchant_fee
            + self.transmission_fee
            + self.distribution_fee
        )
        return net * (1.0 + self.vat_percent / 100.0)


class DPriceStore:
    """On-disk cache of hourly day-ahead prices (UTC hour ISO -> EUR/MWh)."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._hass = hass
        self._store: Store = Store(hass, 1, key)
        self._prices: dict[str, float] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        stored = await self._store.async_load()
        if stored:
            self._prices = {str(k): float(v) for k, v in stored.get("prices", {}).items()}
        self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save({"prices": self._prices})

    async def async_prices_for(self, hours_utc: list[datetime]) -> dict[str, float]:
        """Return {hour ISO: EUR/MWh} for the requested UTC hours, fetching gaps."""
        await self.async_load()
        wanted = {h.replace(minute=0, second=0, microsecond=0) for h in hours_utc}
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        missing = sorted(
            h
            for h in wanted
            if h <= now and h.isoformat() not in self._prices
        )
        if missing:
            await self._async_fetch_range(missing[0].date(), missing[-1].date())
            await self._async_save()

        return {
            h.isoformat(): self._prices[h.isoformat()]
            for h in wanted
            if h.isoformat() in self._prices
        }

    async def _async_fetch_range(self, start, end) -> None:
        session = async_get_clientsession(self._hass)
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=_CHUNK_DAYS))
            params = {
                "bzn": _BIDDING_ZONE,
                "start": cursor.isoformat(),
                "end": (chunk_end + timedelta(days=1)).isoformat(),
            }
            try:
                async with session.get(
                    ENERGY_CHARTS_PRICE_URL, params=params, timeout=30
                ) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            except (ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning(
                    "MVM Next: D tarifa árlekérés sikertelen (%s .. %s): %s",
                    cursor,
                    chunk_end,
                    err,
                )
                cursor = chunk_end + timedelta(days=1)
                continue

            seconds = payload.get("unix_seconds") or []
            prices = payload.get("price") or []
            hour_acc: dict[str, list[float]] = {}
            for sec, price in zip(seconds, prices):
                if price is None:
                    continue
                hour = datetime.fromtimestamp(sec, timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                hour_acc.setdefault(hour.isoformat(), []).append(float(price))
            for hour_iso, values in hour_acc.items():
                self._prices[hour_iso] = round(sum(values) / len(values), 4)

            _LOGGER.info(
                "MVM Next: D tarifa – %d óra ára letöltve (%s .. %s)",
                len(hour_acc),
                cursor,
                chunk_end,
            )
            cursor = chunk_end + timedelta(days=1)


async def async_d_gross_prices(
    hass: HomeAssistant,
    store_key: str,
    hour_isos: list[str],
    config: DTariffConfig,
) -> tuple[dict[str, float], dict[str, object]]:
    """Return ({consumption hour ISO: gross HUF/kWh}, meta).

    The MVM "D" tariff prices only the consumption *above* the yearly
    allowance at this dynamic price; the tiering itself is applied by the
    caller (``_compute_cost_hourly``). Keys mirror the consumption series so
    the caller can look them up directly.
    """
    if not hour_isos:
        return {}, {"hours_priced": 0, "hours_total": 0}

    store = DPriceStore(hass, store_key)
    hours_utc = [
        datetime.fromisoformat(iso).astimezone(timezone.utc) for iso in hour_isos
    ]
    raw = await store.async_prices_for(hours_utc)

    gross: dict[str, float] = {}
    for iso in hour_isos:
        hour_utc = (
            datetime.fromisoformat(iso)
            .astimezone(timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
        )
        eur_mwh = raw.get(hour_utc.isoformat())
        if eur_mwh is None:
            continue
        gross[iso] = round(config.gross_price(eur_mwh), 4)

    meta = {
        "hours_priced": len(gross),
        "hours_total": len(hour_isos),
        "eur_huf": config.eur_huf,
    }
    return gross, meta
