"""
Binance USDⓈ-M vadeli işlem bağlantısı (CCXT, 'defaultType': 'future').

  - Açılışta hesabın tek yönlü (One-Way) modda olduğu API'den teyit edilir; Hedge modundaysa ve açık pozisyon
    yoksa tek yönlü moda alınır, alınamazsa bot işlem açmaz.
  - Her girişten HEMEN ÖNCE set_margin_mode('isolated') ve set_leverage(kaldıraç) çağrılır.
  - Miktar ve fiyatlar CCXT'nin amount_to_precision / price_to_precision fonksiyonlarıyla Binance'in adım
    kurallarına yuvarlanır; emir tutarı Binance'in minimum emir tutarının (notional) altındaysa emir hiç
    gönderilmez (MinNotionalError), Binance bu hatayı yine de verirse (-4164) aynı hata olarak yakalanır.
  - Zarar kes Binance'te STOP_MARKET (closePosition=true, son işlem fiyatı) olarak bekler: bot kapansa bile
    pozisyon korunur. Kâr Al limit (reduceOnly, maker komisyon) ya da TAKE_PROFIT_MARKET emridir.

PaperFuturesExchange aynı arayüzle test modudur: piyasa verisi gerçek (herkese açık uçlar, API anahtarı gerekmez),
emirler ve bakiye sanaldır; borsadaki stop / hedef emirlerinin yerine bot her kontrolde fiyatı karşılaştırır.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

try:
    import ccxt
except ImportError:  # pragma: no cover - kurulum durumuna bağlı
    ccxt = None

from logger import get_logger

log = get_logger()

QUOTE = "USDT"
KLINE_FIELDS = ("t", "o", "h", "l", "c", "v", "qv", "n", "tb")
_KLINE_COLS = (0, 1, 2, 3, 4, 5, 7, 8, 9)
DEFAULT_MIN_NOTIONAL = 5.0


class FuturesError(Exception):
    """Vadeli işlem emri / hesap hatası (mesaj Telegram'a gider)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class MinNotionalError(FuturesError):
    """Emir Binance'in minimum emir tutarının altında: işlem pas geçilir, bot çökmez."""


@dataclass
class FFill:
    price: float
    qty: float
    fee: float
    order_id: str = ""


@dataclass
class ExchangePosition:
    symbol: str
    side: str            # "long" | "short"
    qty: float
    entry_price: float
    leverage: float
    liq_price: float
    margin: float
    unrealized: float
    mark_price: float


def need_ccxt() -> None:
    if ccxt is None:
        raise FuturesError("Vadeli işlem modu CCXT gerektirir: .venv/bin/pip install -r requirements.txt", "NO_CCXT")


def ccxt_symbol(symbol: str) -> str:
    """BTCUSDT → BTC/USDT:USDT (CCXT'nin USDⓈ-M sürekli vadeli işlem sembolü)."""
    base = symbol[:-len(QUOTE)] if symbol.endswith(QUOTE) else symbol
    return f"{base}/{QUOTE}:{QUOTE}"


def make_ccxt(cfg, with_keys: bool = True):
    need_ccxt()
    params = {"enableRateLimit": True, "options": {"defaultType": "future", "adjustForTimeDifference": True}}
    if with_keys:
        params["apiKey"] = cfg.binance_api_key
        params["secret"] = cfg.binance_api_secret
    return ccxt.binance(params)


def _is_network(exc: Exception) -> bool:
    return ccxt is not None and isinstance(exc, (ccxt.NetworkError,)) and not isinstance(exc, ccxt.InvalidNonce)


def _code_of(exc: Exception) -> str:
    text = str(exc)
    for code in ("-4164", "-2019", "-4046", "-4047", "-4048", "-4059", "-2021", "-2022", "-4061", "-1111", "-4005",
                 "-4003", "-2015", "-1021"):
        if code in text:
            return code
    return ""


class FuturesExchange:
    """Canlı Binance USDⓈ-M bağlantısı (CCXT)."""
    is_paper = False
    rules: Dict[str, object] = {}     # Spot parite kuralları (TradingBot uyumluluğu; vadeli işlemde kullanılmaz)
    rules_loaded_at = float("inf")

    def __init__(self, cfg, exchange=None, time_fn=time.time):
        self.cfg = cfg
        self.ex = exchange if exchange is not None else make_ccxt(cfg, with_keys=True)
        self.time_fn = time_fn
        self.markets_loaded_at = 0.0
        self.time_offset_ms = 0

    # TradingBot / Telegram menüsüyle uyumluluk (spot'a özgü çağrılar vadeli işlemde anlamsızdır)
    @property
    def client(self):
        return self              # /menu → Gecikme: client.ping()

    def refresh_rules(self) -> int:
        return self.load_markets()

    def typical_min_notional(self) -> float:
        return DEFAULT_MIN_NOTIONAL

    def dust_assets(self) -> list:
        return []

    # ------------------------------------------------------------------ yardımcılar
    def _call(self, what: str, fn, *args, **kwargs):
        """CCXT çağrısı: ağ hataları requests.ConnectionError olarak yükselir (ana döngü bunu "ağ kesintisi"
        sayar ve bekleyip tekrar dener); diğer hatalar FuturesError."""
        try:
            return fn(*args, **kwargs)
        except FuturesError:
            raise
        except Exception as exc:  # noqa: BLE001 - CCXT'nin hata türleri çok çeşitli
            if _is_network(exc):
                raise requests.ConnectionError(f"{what}: {exc}") from exc
            code = _code_of(exc)
            if code == "-4164" or (ccxt is not None and isinstance(exc, ccxt.InvalidOrder)
                                   and "notional" in str(exc).lower()):
                raise MinNotionalError(f"{what}: emir tutarı Binance'in minimum emir tutarının altında ({exc})",
                                       "-4164") from exc
            raise FuturesError(f"{what}: {exc}", code) from exc

    def load_markets(self) -> int:
        markets = self._call("Vadeli pariteler yüklenemedi", self.ex.load_markets, True)
        self.markets_loaded_at = self.time_fn()
        return sum(1 for m in markets.values() if m.get("swap") and m.get("linear") and m.get("quote") == QUOTE)

    def market(self, symbol: str) -> dict:
        try:
            return self.ex.market(ccxt_symbol(symbol))
        except Exception as exc:  # noqa: BLE001
            raise FuturesError(f"{symbol} Binance vadeli işlemlerde bulunamadı ({exc})", "NO_MARKET") from exc

    def has_market(self, symbol: str) -> bool:
        try:
            m = self.market(symbol)
        except FuturesError:
            return False
        return bool(m.get("active", True)) and bool(m.get("swap"))

    def amount_to_precision(self, symbol: str, qty: float) -> float:
        """Miktarı Binance'in adımına AŞAĞI yuvarlar (CCXT amount_to_precision, TRUNCATE)."""
        try:
            return float(self.ex.amount_to_precision(ccxt_symbol(symbol), qty))
        except Exception as exc:  # noqa: BLE001 - miktar en küçük adımın altında
            if ccxt is not None and isinstance(exc, ccxt.InvalidOrder):
                return 0.0
            raise

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.ex.price_to_precision(ccxt_symbol(symbol), price))

    def min_notional(self, symbol: str) -> float:
        limits = (self.market(symbol).get("limits") or {}).get("cost") or {}
        return float(limits.get("min") or DEFAULT_MIN_NOTIONAL)

    def min_qty(self, symbol: str) -> float:
        limits = (self.market(symbol).get("limits") or {}).get("amount") or {}
        return float(limits.get("min") or 0.0)

    def check_order_size(self, symbol: str, qty: float, price: float) -> None:
        """Yuvarlanmış miktar Binance'in en küçük miktar / emir tutarı kurallarını sağlamıyorsa MinNotionalError."""
        need = self.min_notional(symbol)
        if qty <= 0 or qty < self.min_qty(symbol) or qty * price < need:
            raise MinNotionalError(f"{symbol}: emir tutarı {qty * price:.2f} USDT, Binance'in en küçük emir tutarı "
                                   f"{need:g} USDT (bakiye x kaldıraç yetmiyor)", "MIN_NOTIONAL")

    # ------------------------------------------------------------------ hesap
    def check_one_way(self, has_positions: bool = False) -> str:
        """Hesap tek yönlü (One-Way) modda mı? Hedge modundaysa (açık pozisyon yokken) tek yönlü moda alır.
        Dönen yazı bilgilendirme notudur; geçilemezse FuturesError."""
        mode = self._call("Pozisyon modu okunamadı", self.ex.fetch_position_mode)
        if not mode.get("hedged"):
            return "✅ Hesap tek yönlü (One-Way) modda"
        if has_positions:
            raise FuturesError("Binance vadeli hesabınız Hedge (çift yönlü) modda ve açık pozisyon var. Pozisyonları "
                               "kapatıp Binance → Vadeli → Ayarlar → Pozisyon Modu'ndan 'Tek Yönlü' seçin.", "HEDGE")
        try:
            self._call("Tek yönlü moda geçilemedi", self.ex.set_position_mode, False)
        except FuturesError as exc:
            raise FuturesError(f"Hesap Hedge (çift yönlü) modda ve tek yönlü moda alınamadı: {exc}. Binance → "
                               f"Vadeli → Ayarlar → Pozisyon Modu'ndan 'Tek Yönlü' seçin.", "HEDGE") from exc
        return "🔁 Hesap Hedge modundaydı; tek yönlü (One-Way) moda alındı"

    def free_balance(self, asset: str = QUOTE) -> float:
        """Vadeli (USDⓈ-M) cüzdandaki serbest USDT (yeni pozisyon için kullanılabilir teminat)."""
        bal = self._call("Vadeli bakiye okunamadı", self.ex.fetch_balance)
        return float((bal.get("free") or {}).get(asset) or 0.0)

    def total_balance(self, asset: str = QUOTE) -> float:
        bal = self._call("Vadeli bakiye okunamadı", self.ex.fetch_balance)
        return float((bal.get("total") or {}).get(asset) or 0.0)

    def prepare_symbol(self, symbol: str, leverage: int) -> None:
        """İşleme girmeden HEMEN ÖNCE: yalıtılmış (ISOLATED) teminat ve kaldıraç."""
        sym = ccxt_symbol(symbol)
        try:
            self._call(f"{symbol} teminat modu ISOLATED yapılamadı", self.ex.set_margin_mode, "isolated", sym)
        except FuturesError as exc:
            if exc.code != "-4046" and "no need to change" not in str(exc).lower():
                raise                                     # -4046: zaten isolated
        self._call(f"{symbol} kaldıraç {leverage}x yapılamadı", self.ex.set_leverage, int(leverage), sym)

    # ------------------------------------------------------------------ emirler
    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> FFill:
        """Piyasa emri (side: 'buy' | 'sell'). Miktar adım kuralına yuvarlanır."""
        amount = self.amount_to_precision(symbol, qty)
        if amount <= 0:
            raise MinNotionalError(f"{symbol}: miktar en küçük adımın altında", "MIN_QTY")
        params = {"reduceOnly": True} if reduce_only else {}
        order = self._call(f"{symbol} piyasa emri ({side})", self.ex.create_order, ccxt_symbol(symbol), "market",
                           side, amount, None, params)
        return self._fill(symbol, order, amount)

    def _fill(self, symbol: str, order: dict, amount: float) -> FFill:
        oid = str(order.get("id") or "")
        price = float(order.get("average") or order.get("price") or 0.0)
        filled = float(order.get("filled") or 0.0)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)
        if (not price or not filled) and oid:            # Binance bazı yanıtlarda dolum bilgisini sonra verir
            try:
                order = self.ex.fetch_order(oid, ccxt_symbol(symbol))
                price = float(order.get("average") or order.get("price") or price or 0.0)
                filled = float(order.get("filled") or filled or 0.0)
                fee = float((order.get("fee") or {}).get("cost") or fee or 0.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s emir ayrıntısı okunamadı: %s", symbol, exc)
        if not price:
            price = self.last_price(symbol)
        if not filled:
            filled = amount
        if not fee:
            fee = filled * price * self.cfg.futures_taker_fee
        return FFill(price, filled, fee, oid)

    def place_stop(self, symbol: str, position_side: str, stop_price: float) -> str:
        """Zarar kes / başabaş stopu: STOP_MARKET, closePosition (miktar gerekmez), son işlem fiyatıyla tetiklenir."""
        close_side = "sell" if position_side == "long" else "buy"
        price = self.price_to_precision(symbol, stop_price)
        order = self._call(f"{symbol} zarar kes emri", self.ex.create_order, ccxt_symbol(symbol), "STOP_MARKET",
                           close_side, None, None, {"stopPrice": price, "closePosition": True,
                                                    "workingType": "CONTRACT_PRICE"})
        return str(order.get("id") or "")

    def place_take_profit(self, symbol: str, position_side: str, qty: float, tp_price: float,
                          limit: bool = True) -> str:
        """Kâr Al: reduceOnly LIMIT (maker) ya da TAKE_PROFIT_MARKET (closePosition)."""
        close_side = "sell" if position_side == "long" else "buy"
        price = self.price_to_precision(symbol, tp_price)
        if limit:
            amount = self.amount_to_precision(symbol, qty)
            order = self._call(f"{symbol} kâr al emri", self.ex.create_order, ccxt_symbol(symbol), "limit", close_side,
                               amount, price, {"reduceOnly": True, "timeInForce": "GTC"})
        else:
            order = self._call(f"{symbol} kâr al emri", self.ex.create_order, ccxt_symbol(symbol),
                               "TAKE_PROFIT_MARKET", close_side, None, None,
                               {"stopPrice": price, "closePosition": True, "workingType": "CONTRACT_PRICE"})
        return str(order.get("id") or "")

    def cancel(self, symbol: str, order_id: str, conditional: bool = False) -> None:
        if not order_id:
            return
        params = {"trigger": True} if conditional else {}
        try:
            self._call(f"{symbol} emri iptal edilemedi", self.ex.cancel_order, order_id, ccxt_symbol(symbol), params)
        except FuturesError as exc:
            if not any(k in str(exc).lower() for k in ("unknown order", "-2011", "not found", "does not exist")):
                raise

    def cancel_all(self, symbol: str) -> None:
        """Paritedeki tüm bekleyen emirleri (normal + koşullu) iptal eder; hata yalnızca loglanır."""
        for params in ({}, {"trigger": True}):
            try:
                self.ex.cancel_all_orders(ccxt_symbol(symbol), params)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s bekleyen emirler iptal edilemedi (%s): %s", symbol, params or "normal", exc)

    def order_filled(self, symbol: str, order_id: str, conditional: bool = False) -> Optional[FFill]:
        """Emir dolduysa dolum bilgisi, değilse None."""
        if not order_id:
            return None
        params = {"trigger": True} if conditional else {}
        try:
            order = self._call(f"{symbol} emir durumu", self.ex.fetch_order, order_id, ccxt_symbol(symbol), params)
        except FuturesError:
            return None
        if str(order.get("status")) not in ("closed", "filled") or not float(order.get("filled") or 0):
            return None
        price = float(order.get("average") or order.get("price") or 0.0)
        qty = float(order.get("filled") or 0.0)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)
        return FFill(price, qty, fee, order_id)

    def position(self, symbol: str) -> Optional[ExchangePosition]:
        rows = self._call(f"{symbol} pozisyonu okunamadı", self.ex.fetch_positions, [ccxt_symbol(symbol)])
        for p in rows or []:
            qty = abs(float(p.get("contracts") or 0.0))
            if qty <= 0:
                continue
            side = str(p.get("side") or ("long" if float((p.get("info") or {}).get("positionAmt", 0)) > 0
                                         else "short"))
            return ExchangePosition(symbol, side, qty, float(p.get("entryPrice") or 0.0),
                                    float(p.get("leverage") or 0.0), float(p.get("liquidationPrice") or 0.0),
                                    float(p.get("initialMargin") or p.get("collateral") or 0.0),
                                    float(p.get("unrealizedPnl") or 0.0), float(p.get("markPrice") or 0.0))
        return None

    def all_positions(self) -> List[ExchangePosition]:
        """Hesaptaki tüm açık vadeli pozisyonlar (tek istek)."""
        rows = self._call("Vadeli pozisyonlar okunamadı", self.ex.fetch_positions)
        out = []
        for p in rows or []:
            qty = abs(float(p.get("contracts") or 0.0))
            if qty <= 0:
                continue
            market_id = str((p.get("info") or {}).get("symbol") or "")
            out.append(ExchangePosition(market_id or str(p.get("symbol")), str(p.get("side") or ""), qty,
                                        float(p.get("entryPrice") or 0.0), float(p.get("leverage") or 0.0),
                                        float(p.get("liquidationPrice") or 0.0), 0.0,
                                        float(p.get("unrealizedPnl") or 0.0), float(p.get("markPrice") or 0.0)))
        return out

    def closing_fill(self, symbol: str, since_ms: int, position_side: str) -> Optional[FFill]:
        """Pozisyonu kapatan işlemler (botun görmediği stop / hedef / likidasyon): ortalama fiyat, miktar, komisyon."""
        close_side = "sell" if position_side == "long" else "buy"
        try:
            trades = self._call(f"{symbol} işlem geçmişi", self.ex.fetch_my_trades, ccxt_symbol(symbol), since_ms)
        except FuturesError as exc:
            log.warning("%s", exc)
            return None
        rows = [t for t in trades or [] if t.get("side") == close_side]
        qty = sum(float(t.get("amount") or 0) for t in rows)
        if qty <= 0:
            return None
        cost = sum(float(t.get("amount") or 0) * float(t.get("price") or 0) for t in rows)
        fee = sum(float((t.get("fee") or {}).get("cost") or 0) for t in rows)
        return FFill(cost / qty, qty, fee, str(rows[-1].get("order") or ""))

    def funding_paid(self, symbol: str, since_ms: int) -> float:
        """Pozisyon süresince ödenen (+) / alınan (-) fonlama (USDT)."""
        try:
            rows = self.ex.fetch_funding_history(ccxt_symbol(symbol), since_ms)
        except Exception as exc:  # noqa: BLE001 - bilgi amaçlı
            log.warning("%s fonlama geçmişi okunamadı: %s", symbol, exc)
            return 0.0
        return -sum(float(r.get("amount") or 0) for r in rows or [])

    # ------------------------------------------------------------------ piyasa verisi (herkese açık)
    def klines(self, symbol: str, interval: str, limit: int) -> Dict[str, "object"]:
        """KAPANMIŞ mumlar (son satır bitmemiş mumsa atılır): futures_data.FIELDS dizileri. Taker buy hacmi CCXT'nin
        birleşik fetch_ohlcv'sinde olmadığı için Binance'in ham klines ucu kullanılır."""
        import numpy as np
        rows = self._call(f"{symbol} mumları alınamadı", self.ex.fapiPublicGetKlines,
                          {"symbol": symbol, "interval": interval, "limit": int(limit)})
        now_ms = int(self.time_fn() * 1000)
        rows = [r for r in rows or [] if int(r[6]) < now_ms]
        if not rows:
            out = {k: np.zeros(0) for k in KLINE_FIELDS}
            out["t"] = np.zeros(0, dtype=np.int64)
            return out
        data = np.array([[float(r[i]) for i in _KLINE_COLS] for r in rows], dtype=np.float64)
        out = {k: data[:, i] for i, k in enumerate(KLINE_FIELDS)}
        out["t"] = data[:, 0].astype(np.int64)
        return out

    def bid_ask(self, symbol: str) -> Tuple[float, float]:
        book = self._call(f"{symbol} fiyat alınamadı", self.ex.fetch_order_book, ccxt_symbol(symbol), 5)
        bid = float(book["bids"][0][0]) if book.get("bids") else 0.0
        ask = float(book["asks"][0][0]) if book.get("asks") else 0.0
        if not bid or not ask:
            last = self.last_price(symbol)
            return last, last
        return bid, ask

    def last_price(self, symbol: str) -> float:
        ticker = self._call(f"{symbol} fiyat alınamadı", self.ex.fetch_ticker, ccxt_symbol(symbol))
        return float(ticker.get("last") or ticker.get("close") or 0.0)

    def ping(self) -> bool:
        self._call("Binance vadeli işlemlere ulaşılamadı", self.ex.fetch_time)
        return True


class PaperFuturesExchange(FuturesExchange):
    """Test modu: gerçek piyasa verisi, sanal bakiye ve emirler. Bakiye BotState.paper_balances['USDT']'de tutulur
    (bot kapanıp açılınca kaldığı yerden devam eder)."""
    is_paper = True

    def __init__(self, cfg, balances: Dict[str, float], exchange=None, time_fn=time.time):
        super().__init__(cfg, exchange if exchange is not None else make_ccxt(cfg, with_keys=False), time_fn)
        self.balances = balances
        self.positions: Dict[str, dict] = {}    # Açık sanal pozisyonlar: parite → {side, qty, entry, margin, lev}
        self.orders: Dict[str, dict] = {}       # Sanal stop / hedef emirleri: id → {...}
        self._seq = 0

    def _id(self) -> str:
        self._seq += 1
        return f"paper-{int(self.time_fn())}-{self._seq}"

    def check_one_way(self, has_positions: bool = False) -> str:
        return "🧪 Test modu: pozisyon modu kontrolü atlandı (canlıda One-Way teyit edilir)"

    def free_balance(self, asset: str = QUOTE) -> float:
        used = sum(p["margin"] for p in self.positions.values())
        return max(0.0, self.balances.get(asset, 0.0) - used)

    def total_balance(self, asset: str = QUOTE) -> float:
        return self.balances.get(asset, 0.0)

    def all_positions(self) -> List[ExchangePosition]:
        return [p for p in (self.position(s) for s in self.positions) if p is not None]

    def prepare_symbol(self, symbol: str, leverage: int) -> None:
        self.market(symbol)        # Parite yoksa hata

    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> FFill:
        amount = self.amount_to_precision(symbol, qty)
        if amount <= 0:
            raise MinNotionalError(f"{symbol}: miktar en küçük adımın altında", "MIN_QTY")
        bid, ask = self.bid_ask(symbol)
        slip = self.cfg.futures_slippage_pct / 100
        price = ask * (1 + slip) if side == "buy" else bid * (1 - slip)
        if not reduce_only:
            self.check_order_size(symbol, amount, price)
        fee = amount * price * self.cfg.futures_taker_fee
        self.balances[QUOTE] = round(self.balances.get(QUOTE, 0.0) - fee, 8)
        return FFill(price, amount, fee, self._id())

    def open_paper(self, symbol: str, side: str, qty: float, entry: float, margin: float, leverage: int) -> None:
        self.positions[symbol] = {"side": side, "qty": qty, "entry": entry, "margin": margin, "lev": leverage}

    def close_paper(self, symbol: str, exit_price: float, qty: float, fee: float) -> float:
        """Sanal pozisyonu kapatır; gerçekleşen K/Z (komisyon hariç) bakiyeye eklenir."""
        p = self.positions.pop(symbol, None)
        if p is None:
            return 0.0
        sign = 1 if p["side"] == "long" else -1
        gross = sign * qty * (exit_price - p["entry"])
        self.balances[QUOTE] = round(self.balances.get(QUOTE, 0.0) + gross - fee, 8)
        for oid in [k for k, o in self.orders.items() if o["symbol"] == symbol]:
            self.orders.pop(oid, None)
        return gross

    def place_stop(self, symbol: str, position_side: str, stop_price: float) -> str:
        oid = self._id()
        self.orders[oid] = {"symbol": symbol, "kind": "stop", "side": position_side,
                            "price": self.price_to_precision(symbol, stop_price)}
        return oid

    def place_take_profit(self, symbol: str, position_side: str, qty: float, tp_price: float,
                          limit: bool = True) -> str:
        oid = self._id()
        self.orders[oid] = {"symbol": symbol, "kind": "tp", "side": position_side, "limit": limit,
                            "price": self.price_to_precision(symbol, tp_price)}
        return oid

    def cancel(self, symbol: str, order_id: str, conditional: bool = False) -> None:
        self.orders.pop(order_id, None)

    def cancel_all(self, symbol: str) -> None:
        for oid in [k for k, o in self.orders.items() if o["symbol"] == symbol]:
            self.orders.pop(oid, None)

    def triggered(self, symbol: str, price: float) -> Optional[Tuple[str, dict]]:
        """Fiyat sanal stop / hedef emrine ulaştı mı? (emir kimliği, emir) — önce stop (temkinli)."""
        found = None
        for oid, o in self.orders.items():
            if o["symbol"] != symbol:
                continue
            long_ = o["side"] == "long"
            if o["kind"] == "stop" and (price <= o["price"] if long_ else price >= o["price"]):
                return oid, o
            if o["kind"] == "tp" and (price >= o["price"] if long_ else price <= o["price"]):
                found = (oid, o)
        return found

    def order_filled(self, symbol: str, order_id: str, conditional: bool = False) -> Optional[FFill]:
        return None

    def position(self, symbol: str) -> Optional[ExchangePosition]:
        p = self.positions.get(symbol)
        if p is None:
            return None
        return ExchangePosition(symbol, p["side"], p["qty"], p["entry"], p["lev"], 0.0, p["margin"], 0.0, 0.0)

    def closing_fill(self, symbol: str, since_ms: int, position_side: str) -> Optional[FFill]:
        return None

    def funding_paid(self, symbol: str, since_ms: int) -> float:
        return 0.0


def make_exchange(cfg, state, exchange=None):
    """Mod'a göre canlı / test vadeli bağlantısı."""
    if cfg.is_paper:
        state.paper_balances.setdefault(QUOTE, state.start_capital)
        return PaperFuturesExchange(cfg, state.paper_balances, exchange=exchange)
    return FuturesExchange(cfg, exchange=exchange)


def describe_error(exc: Exception) -> str:
    """Kullanıcıya gösterilecek kısa açıklama (anahtar / izin / IP hatalarında ne yapılacağı)."""
    text = str(exc)
    if "-2015" in text or "Invalid API-key" in text:
        return (text + "\nAPI anahtarında 'Enable Futures' izni açık mı, IP kısıtlamasında sunucunun IP adresi var mı? "
                "(Binance → API Yönetimi)")
    if "-2019" in text:
        return text + "\nVadeli cüzdanda yeterli serbest USDT yok (Spot → USDⓈ-M Vadeli transferi yapın)."
    return text


def summarize_positions(rows: List[ExchangePosition]) -> str:
    return ", ".join(f"{p.symbol} {p.side.upper()} {p.qty:g}" for p in rows) or "yok"
