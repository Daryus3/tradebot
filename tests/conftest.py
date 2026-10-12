"""Testler için ortak yardımcılar: proje kökünü import yoluna ekler, sahte Binance istemcisi sağlar."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from config import load_config  # noqa: E402


class FakeClient:
    """BinanceClient'ın test için basitleştirilmiş hali (internet gerektirmez)."""

    def __init__(self, prices=None, spread=0.0):
        self.prices = dict(prices or {})
        self.spread = spread
        self.time_offset_ms = 0

    def sync_time(self):
        return 0

    def book_ticker(self, symbol):
        p = self.prices[symbol]
        return {"bidPrice": str(p * (1 - self.spread)), "askPrice": str(p * (1 + self.spread))}

    def price(self, symbol):
        return self.prices[symbol]

    def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        p = self.prices.get(symbol, 1.0)
        return [[i * 60_000, str(p), str(p), str(p), str(p), "0", i * 60_000 + 59_999, "0", 0, "0", "0", "0"]
                for i in range(limit)]

    def exchange_info(self):
        return {"symbols": [
            {
                "symbol": sym, "status": "TRADING", "baseAsset": sym[:-4], "quoteAsset": "USDT",
                "isSpotTradingAllowed": True, "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"],
                "quoteOrderQtyMarketAllowed": True,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00010000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01000000", "minQty": "0.01000000",
                     "maxQty": "9000000.00000000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0.00000000",
                     "maxQty": "100000.00000000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True},
                ],
            }
            for sym in self.prices
        ]}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    # Kullanıcının .env dosyası testleri etkilemesin
    for key in ["TRADING_MODE", "START_CAPITAL_USDT", "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "MAX_HOLD_MINUTES",
                "TRAILING_ACTIVATION_PCT", "TRAILING_FLOOR_PCT", "TRAILING_DYNAMIC", "FEE_RATE",
                "MAX_TOTAL_LOSS_USDT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        monkeypatch.delenv(key, raising=False)
    return load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")


@pytest.fixture
def fake_client():
    return FakeClient
