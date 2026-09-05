import threading
import time
from datetime import datetime, timezone

import requests


COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
CACHE_TTL_SECONDS = 4
REQUEST_TIMEOUT_SECONDS = 4
ASSETS = (
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("solana", "SOL"),
    ("cardano", "ADA"),
    ("ripple", "XRP"),
)


class CryptoProviderError(RuntimeError):
    """The external provider did not return usable market data."""


_cache = {"payload": None, "expires_at": 0.0}
_cache_lock = threading.Lock()


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value, field, symbol):
    if value is None or isinstance(value, bool):
        raise CryptoProviderError(f"Missing {field} for {symbol}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CryptoProviderError(f"Invalid {field} for {symbol}") from error
    if number != number or number in (float("inf"), float("-inf")):
        raise CryptoProviderError(f"Invalid {field} for {symbol}")
    return number


def _normalize_assets(rows):
    rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    normalized = []
    for coin_id, symbol in ASSETS:
        row = rows_by_id.get(coin_id)
        if row is None:
            raise CryptoProviderError(f"Missing market data for {symbol}")
        normalized.append({
            "symbol": symbol,
            "price_usd": _number(row.get("current_price"), "price", symbol),
            "change_24h_percent": _number(row.get("price_change_percentage_24h"), "24h change", symbol),
            "volume_24h_usd": _number(row.get("total_volume"), "24h volume", symbol),
            "market_cap_usd": _number(row.get("market_cap"), "market cap", symbol),
            "updated_at": row.get("last_updated") or _utc_timestamp(),
        })
    return normalized


def _fetch_market_data():
    response = requests.get(
        COINGECKO_MARKETS_URL,
        params={
            "vs_currency": "usd",
            "ids": ",".join(coin_id for coin_id, _ in ASSETS),
            "price_change_percentage": "24h",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        rows = response.json()
    except ValueError as error:
        raise CryptoProviderError("Crypto provider returned invalid JSON") from error
    if not isinstance(rows, list):
        raise CryptoProviderError("Crypto provider returned an invalid payload")
    return {
        "status": "success",
        "source": "coingecko",
        "updated_at": _utc_timestamp(),
        "assets": _normalize_assets(rows),
    }


def get_crypto_market_data():
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and now < _cache["expires_at"]:
            return _cache["payload"]

    try:
        payload = _fetch_market_data()
    except Exception as error:
        with _cache_lock:
            stale_payload = _cache["payload"]
        if stale_payload is not None:
            return {
                **stale_payload,
                "status": "stale",
                "message": "Market data temporarily unavailable; showing the last successful update.",
            }
        raise CryptoProviderError("Crypto provider request failed") from error

    with _cache_lock:
        _cache["payload"] = payload
        _cache["expires_at"] = time.monotonic() + CACHE_TTL_SECONDS
    return payload


def clear_cache():
    with _cache_lock:
        _cache["payload"] = None
        _cache["expires_at"] = 0.0
