"""
Vadeli işlem motoru (STRATEGY=futures): Binance USDⓈ-M'de kaldıraçlı long / short scalping.

FuturesBot, TradingBot'un Telegram komutlarını, menüleri, canlı panoyu, raporları, kill-switch'i ve durum kaydını
aynen kullanır; yalnızca tarama, pozisyon açma / yönetme / kapatma kısımları vadeli işleme göre değişir:

  Döngü (her 5 dk mum kapanışından ~3 sn sonra, FUTURES_FAST_ENTRY=true ise 1 sn sonra; açık pozisyon yokken):
    1. FUTURES_COINS'teki coinlerin son 300 kapanmış 5 dk mumu + BTC'nin 1 saatlik mumları alınır.
    2. strategy.FuturesScalpStrategy.evaluate 4'lü onayı arar (likidasyon + hacim + RSI uyumsuzluğu + BTC trendi).
    3. En güçlü sinyalde: set_margin_mode(ISOLATED) + set_leverage(FUTURES_LEVERAGE) → serbest bakiyenin
       FUTURES_MARGIN_USAGE_PCT'si teminat (bileşik) → piyasa emri → Binance'e STOP_MARKET zarar kes (konamazsa
       pozisyon hemen kapatılır) ve Kâr Al emri.
  Giriş gecikmesi her zaman ölçülür: taramanın aşamaları ve her girişin mum kapanışından kaç ms sonra dolduğu tarama
  kaydına (futures_scans_<mod>.jsonl), pozisyona ve açılış mesajına yazılır; backtest raporu ortalamasını gösterir.
  Hızlı giriş (FUTURES_FAST_ENTRY, varsayılan kapalı) aynı kurallarla yolu kısaltır: tarama 1 sn sonra (yanıtta yeni
  mum da görünmeli, yoksa kısa aralıklarla yeniden denenir; en geç eski tarama anında eldekiyle devam edilir), mumlar
  tek dalgada paralel istenir, teminat modu + kaldıraç önceden ayarlanıp her taramadan hemen önce doğrulanır.
  Açık pozisyonda (~2 sn'de bir):
    - Binance'te stop / hedef çalıştıysa kapanış kaydedilir (bot kapalıyken çalıştıysa açılışta).
    - Başabaş kalkanı: fiyat girişten +BREAKEVEN_TRIGGER_PCT'ye gelince Binance'teki stop girişin
      +BREAKEVEN_FLOOR_PCT üstüne taşınır (short'ta altına).
    - Zaman stopu: MAX_HOLD_MINUTES dolunca piyasa emriyle kapatılır.
    - Yedek: fiyat stopun ötesine geçtiği hâlde Binance'teki emir çalışmadıysa bot piyasa emriyle kapatır.
Yüzdeler (Kâr Al, Zarar Kes, başabaş) FİYAT hareketidir; 10x'te fiyat +%1.2 ≈ teminatın +%12'si (ROE).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import requests

from config import (MINUTE_SCAN_STEP_MS, futures_be_trigger, futures_breadth_on, futures_close_only, futures_coin_list,
                    futures_hold_minutes, scan_log_path,
                    futures_crash_rules, futures_funding_rules,
                    futures_liq_distance_pct, futures_minute_limit, futures_modules, futures_pump_rules, futures_scan_shifts,
                    futures_slot_share, futures_wave_cfg, futures_wave_rules)
from futures_data import resample
from futures_exchange import FFill, FuturesError, MinNotionalError, describe_error
from logger import get_logger
from risk import ExitReason, Position, cooldown_minutes, kill_switch_hit
from strategy import (FUT_BAR_MS, FUT_BREADTH_DROP_PCT, FUT_BREADTH_HOURS, FUT_BTC_1H_LIMIT, FUT_H1_LIMIT, FUT_HOUR_MS,
                      FuturesSignal,
                      ScanResult, funding_per_bar, futures_kline_limit, isolated_factor, market_breadth)
from telegram_bot import CRITICAL_EXITS, Messages, esc, fmt_duration, fmt_price, fmt_signed, fmt_usdt, futures_tag
from trader import ALIVE_SAVE_SEC, POSITION_LOG_SEC, RULES_REFRESH_SEC, Mark, TradingBot

log = get_logger()

SCAN_DELAY_SEC = 3            # 5 dk mum kapandıktan bu kadar sonra taranır (Binance'te yerleşsin)
POSITION_POLL_SEC = 2.0       # Açık pozisyon kontrol aralığı
MAX_SCAN_LAG_SEC = 90         # Mum kapanışından bu kadar sonra (kesinti / yavaşlık) o mumun sinyali alınmaz
MARGIN_RETRY_FACTOR = 0.9     # "Margin is insufficient" (-2019): miktar %10 küçültülüp bir kez daha denenir
SOFT_STOP_GRACE_PCT = 0.15    # Fiyat stopu bu kadar (fiyat %) geçtiği hâlde Binance emri çalışmadıysa bot kapatır
BE_RETRY_SEC = 30             # Başabaş stopu taşınamadıysa bu kadar sonra tekrar denenir
STOP_WOULD_TRIGGER = "-2021"  # Binance: "Order would immediately trigger" (fiyat stop seviyesini çoktan geçmiş)
TRAIL_MOVE_SEC = 10           # İzleyen stop: Binance'teki stop emri en sık bu aralıkla taşınır (bot arada kendisi izler)
TRAIL_MIN_STEP_PCT = 0.1      # ...ve yeni seviye eskisinden en az bu kadar (fiyat %) iyiyse
PREFILTER_SLACK_PCT = 1.0     # En hacimli N + çöküş: 24 saatlik tepeye göre ön elemede pay (fiyat puanı)
KLINE_PARALLEL_MIN = 16       # Bundan çok coinin mumları paralel istenir (120-150 coinlik evrende tarama gecikmesin)
KLINE_WORKERS = 6
UNIVERSE_REFRESH_SEC = 24 * 3600   # En hacimli N köklü coin sıralaması günde bir yenilenir (parite başına 1 istek)
UNIVERSE_RETRY_SEC = 600           # Sıralama alınamazsa bu kadar sonra yeniden denenir
SCAN_LOG_DAYS = 30                 # Canlı tarama kaydı (futures_scans_<mod>.jsonl) bu kadar gün tutulur
UNIVERSE_WARN_SEC = 6 * 3600       # Sıralama alınamıyor uyarısı en sık bu aralıkla (Telegram)
PAPER_LIMIT_THROUGH_PCT = 0.02     # Test modu: limit emir fiyat limitin bu kadar (%) ötesine geçince dolar (backtestle aynı)
LIMIT_REQUOTE_MAX = 2              # Post-only emir anında eşleşeceği için reddedilirse (-5022) yeni fiyatla yeniden dizilir
LIMIT_CANCEL_GRACE_SEC = 5         # Süre dolunca iptal; test modunda son 1 dk mumun kapanması da beklenir
FUNDING_LIMIT = 5                  # Fonlama modülü: coin başına istenen son ödenmiş fonlama sayısı
# Hızlı giriş (FUTURES_FAST_ENTRY=true). Kurallar ve pozisyon boyları aynıdır; yalnızca mum kapanışı → emir süresi kısalır.
FAST_SCAN_DELAY_SEC = 1.0          # Tarama mum kapanışından bu kadar sonra (kapalıyken SCAN_DELAY_SEC)
FAST_KLINE_WORKERS = 24            # Mumlar tek dalgada istenir (~24 coin × ağırlık 2 = 48; sınır dakikada 2.400)
FAST_SETTLE_WAIT_SEC = 0.25        # Yanıtta yeni mum yoksa o coin bu kadar sonra yeniden istenir
FAST_SETTLE_MAX_TRIES = 12         # Her durumda en fazla bu kadar deneme (normalde eski tarama anı önce gelir)
# Sakin coin kuralı: yeni mumu henüz görünmeyen coinin az önce kapanan mumu kesinleşmemiş olabilir; ama kapanışı bu
# kadar puan oynasa bile hiçbir kararı (çöküş eşiği, genişlik sayımı) değiştiremeyecekse o coin beklenmez
FAST_NEAR_CRASH_PCT = 1.0          # Düşüşü bir çöküş / dalga eşiğine bu kadar puan yakın (ya da üstünde) coin bekler
FAST_NEAR_BREADTH_PCT = 0.5        # 1 saatlik düşüşü genişlik eşiğine (%3) bu kadar puan yakın coin bekler
FAST_PREFILTER_MIN = 40            # Bundan az coinde 24 saatlik ön eleme yapılmaz (tek istek ama 2 sn'ye kadar bekletir)
FAST_WARM_LEAD_SEC = 2.5           # Taramadan bu kadar önce: bağlantılar ısıtılır, teminat modu / kaldıraç doğrulanır
FAST_WARM_MIN_SEC = 1.0            # Taramaya bundan az kaldıysa ön hazırlık atlanır (taramayı geciktirmesin)
FAST_VERIFY_MAX_AGE_SEC = 30       # Doğrulama bundan eskiyse girişte teminat modu + kaldıraç yeniden ayarlanır (eski yol)
FAST_VERIFY_EVERY_SEC = 300        # Ön hazırlık (taramalar arasında) için doğrulama aralığı
FAST_PREPARE_BATCH = 2             # Ön hazırlık: döngü adımı başına en fazla bu kadar coin (2 istek / coin)
FAST_PREPARE_GUARD_SEC = 20        # Taramaya bu kadar kala ön hazırlık / parite yenilemesi yapılmaz
FAST_PREPARE_RETRY_SEC = 3600      # Önceden ayarlanamayan coin bu kadar sonra yeniden denenir (girişte eski yol denenir)
FAST_PREPARE_POLL_SEC = 1.0        # Hazırlanacak coin varken döngü bu aralıkla döner
FAST_MIN_SLEEP_SEC = 0.02
CLOCK_WARN_MS = 750                # Sunucu saati Binance'ten bu kadar farklıysa uyarı (günlükte, saatte bir)
ACCOUNT_WARN_EVERY_SEC = 3600      # Hesap modu engeli (Çoklu Varlık, -4168) Telegram'a en fazla bu aralıkla yazılır
MAX_LAG_MS = 10 * 60_000           # Bundan büyük "gecikme" ölçüm sayılmaz (saat atlaması / elle çağrı)


@dataclass
class FMark(Mark):
    roe: float = 0.0          # Binance'in gösterdiği ROE (%): gerçekleşmemiş K/Z ÷ teminat (komisyonsuz)


def _sign(pos) -> int:
    return 1 if pos.side == "long" else -1


def _worse(pos, price: float, level: float) -> bool:
    """Fiyat seviyenin zarar tarafında mı (long: altında, short: üstünde)?"""
    return price <= level if pos.side == "long" else price >= level


def _stop_reason(pos) -> ExitReason:
    """Binance'teki stop emri çalıştığında kapanış nedeni: izleyen stop > başabaş > zarar kes."""
    if pos.trailing_active:
        return ExitReason.TRAILING_STOP
    return ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP_LOSS


def _reached(pos, price: float, level: float) -> bool:
    """Fiyat lehte seviyeye ulaştı mı (long: üstünde, short: altında)?"""
    return price >= level if pos.side == "long" else price <= level


class FuturesBot(TradingBot):
    is_futures = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_scan_at = self._next_close(self.time_fn()) + self._scan_delay()
        self.last_signals: List[FuturesSignal] = []
        self._be_retry_at: Dict[str, float] = {}
        self._stop_passed: Set[str] = set()    # Stop taşınırken "emir hemen tetiklenir" reddi alan pariteler
        self._trail_at: Dict[str, float] = {}
        self.universe: List[str] = []          # FUTURES_UNIVERSE: en hacimli N köklü parite (sıralamanın ilk N'i)
        self._ranking: List[str] = []          # Tüm köklü pariteler, ortalama hacme göre
        self._ranking_at = 0.0
        self._ranking_key: tuple = ()
        self._ranking_source = ""              # "avg": ayardaki ortalama hacim | "24h": geçici (24 saatlik hacim)
        self._ranking_error = ""               # Son sıralama denemesi başarısızsa nedeni (/tarama ve uyarı)
        self._ranking_retry_at = 0.0           # Başarısız denemeden sonra bu ana kadar yeniden denenmez
        self._universe_warned_at = 0.0
        # Canlı tarama kaydı: her 5 dk mum için bir satır (taranan / analiz edilen coinler, sinyaller ve sonuçları ya da
        # taramanın neden yapılmadığı). Vadeli backtest raporu bunu backtestin işlemleriyle karşılaştırır.
        self.scan_log_path = scan_log_path(self.store.path.parent, self.cfg.trading_mode)
        self._journal_universe: Optional[List[str]] = None
        self._prune_scan_log()
        self._h1_cache: Dict[str, Tuple[int, dict]] = {}   # Düzeltme modülü: parite → (saat, 1 saatlik mumlar)
        self.last_breadth: Optional[int] = None    # Dalga modu: son taramada son 1 saatte %3+ düşen coin sayısı
        # Giriş gecikmesi ölçümü (her zaman) ve hızlı giriş (FUTURES_FAST_ENTRY) durumu
        self.sleep_fn = time.sleep                 # Kısa beklemeler (testlerde sahte saat ilerletilir)
        self.last_timing: Optional[dict] = None    # Son taramanın zamanlaması (/tarama)
        self._entry_lags: Dict[str, dict] = {}     # Bu taramada açılan pozisyonların gecikmesi (tarama kaydına)
        self._prepared: Set[str] = set()           # Teminat modu + kaldıracı Binance'ten doğrulanmış pariteler
        self._prepared_lev = 0
        self._verified_at = 0.0                    # Son doğrulama anı (symbol_settings)
        self._prepare_retry: Dict[str, float] = {}
        self._self_prepared: Set[str] = set()      # Botun önceden ayarladığı, henüz Binance'ten teyit edilmemiş pariteler
        self._prepare_todo = 0                     # Önceden ayarlanmayı bekleyen parite sayısı (döngü hızı için)
        self._warm_for = 0.0                       # Ön hazırlığı yapılmış tarama anı (next_scan_at)
        self._clock_offset_ms: Optional[float] = None   # Binance saati − sunucu saati (ms; ısıtma isteklerinden)
        self._clock_warned_at = 0.0
        self._quiet_for, self._quiet_val = None, None   # Sakin coin kuralının eşikleri (ayar nesnesi başına bir kez)
        self._last_quiet = 0                       # Son taramada yeni mumu beklenmeden kabul edilen coin sayısı
        # Hesap düzeyinde giriş engeli (Çoklu Varlık modu, -4168): hiçbir pozisyon açılamaz; /tarama'da görünür
        self.account_block = ""
        self._account_warned_at = 0.0

    # ================================================================ kapasite
    def slots(self) -> int:
        """Tek pozisyon (tüm bakiye, bileşik); dalga modunda FUTURES_MAX_POSITIONS (her biri bakiyenin 1/N'i)."""
        return max(1, int(self.cfg.futures_max_positions))

    def target_slots(self) -> int:
        return self.slots()

    def free_slots(self) -> int:
        return max(0, self.slots() - len(self.state.positions) - (1 if self.state.pending_entry else 0))

    def _cascade_on(self) -> bool:
        """Genişlik gerekiyor mu: dalga modu / yalnız çöküş küçültme (Faz 5) ya da dalgada yeniden giriş / dalga
        eşikleri (Faz 6)."""
        return futures_breadth_on(self.cfg)

    def _reentry_bypass(self, breadth: Optional[int], now: float) -> Set[str]:
        """Faz 6 (FUTURES_REENTRY_BREADTH): genişlik eşiği aşıldıysa zarar sonrası beklemesi atlanabilecek coinler.
        Yalnızca o zararla başlayan bekleme sürüyorsa (kara liste / onay ertelemesiyle uzatılmadıysa), SYMBOL_COOLDOWN
        bittiyse ve coinde açık pozisyon / bekleyen emir yoksa (backtest: futures_backtest.cooldown_block)."""
        cfg, state = self.cfg, self.state
        need = int(getattr(cfg, "futures_reentry_breadth", 0))
        if need <= 0 or breadth is None or breadth < need or not state.wave_reentry:
            return set()
        busy = (set(state.positions) | {p["symbol"] for p in state.pending_orders.values()} | set(self.smart.entries)
                | ({state.pending_entry.get("symbol")} if state.pending_entry else set()))
        if self.fee_info is not None and self.fee_info.bnb_enabled:
            busy.add(f"BNB{cfg.quote_asset}")
        out = set()
        for symbol, (after, until) in state.wave_reentry.items():
            if symbol not in busy and now >= after and abs(state.cooldowns.get(symbol, -1.0) - until) < 1e-6:
                out.add(symbol)
        return out

    def _mark_reentry(self, symbol: str, now: float, pnl: float, prev_until: float) -> None:
        """Kapanışta (state.cooldowns yazıldıktan sonra; prev_until = kapanıştan önceki bekleme bitişi): zarar sonrası
        bekleme dalgada atlanabilir mi (Faz 6). Dalgada yeniden girilen işlem kârla kapanırsa önceki zararın beklemesi
        sonuna kadar sürer: o süre içinde coine yine yalnızca dalgada girilir (backtest: futures_backtest.set_cooldown)."""
        cfg, state = self.cfg, self.state
        loss_cd = int(getattr(cfg, "loss_cooldown_minutes", 0))
        prev = state.wave_reentry.get(symbol)
        after = now + cfg.symbol_cooldown_minutes * 60
        if int(getattr(cfg, "futures_reentry_breadth", 0)) <= 0:
            state.wave_reentry.pop(symbol, None)
        elif pnl < 0 and loss_cd > cfg.symbol_cooldown_minutes:
            state.wave_reentry[symbol] = [after, state.cooldowns[symbol]]
        elif prev is not None and prev_until > now and abs(prev_until - prev[1]) < 1e-6:   # Önceki zarar sürüyor
            until = max(prev[1], state.cooldowns[symbol])
            state.cooldowns[symbol] = until
            state.wave_reentry[symbol] = [max(prev[0], after), until]
        else:
            state.wave_reentry.pop(symbol, None)

    def _scan_step_ms(self) -> int:
        """Taramalar arası adım (ms): 5 dk mum; dakikalık taramada (FUTURES_MINUTE_SCAN, destekleniyorsa) 1 dk."""
        return MINUTE_SCAN_STEP_MS if futures_scan_shifts(self.cfg) else FUT_BAR_MS

    def _next_close(self, now: float) -> float:
        bar = self._scan_step_ms() // 1000
        return (int(now) // bar + 1) * bar

    def _fast(self) -> bool:
        """Hızlı giriş açık mı (FUTURES_FAST_ENTRY)."""
        return bool(getattr(self.cfg, "futures_fast_entry", False))

    def _scan_delay(self) -> float:
        """Tarama 5 dk mum kapanışından kaç sn sonra başlar."""
        return FAST_SCAN_DELAY_SEC if self._fast() else SCAN_DELAY_SEC

    def _since_ms(self, closed_ms: int) -> int:
        """Şu an, verilen mum kapanışından kaç ms sonra (sunucu saati)."""
        return int(round(self.time_fn() * 1000 - closed_ms))

    # ================================================================ açılış
    def startup(self) -> None:
        cfg, ex = self.cfg, self.exchange
        ex.load_markets()
        if cfg.futures_universe > 0:
            self._refresh_universe(self.time_fn())
            coins = [s[:-4] for s in self._universe_symbols()]
            missing: List[str] = []
        else:
            coins = list(futures_coin_list(cfg))
            missing = [c for c in coins if not ex.has_market(c + "USDT")]
        mode_note = ex.check_one_way(has_positions=bool(self.state.positions))
        free = None if ex.is_paper else ex.free_balance()
        now = self.time_fn()
        if not self._announced:
            self.notifier.send(Messages.futures_started(
                cfg, self.state.capital, self.state.realized_pnl, len(coins) - len(missing),
                [c for c in coins if c not in missing], self.loss_limit, self.loss_pct, mode_note, free))
            for note in cfg.notes:
                self.notifier.send(Messages.warning(note))
            if not ex.is_paper:
                try:
                    asset_note = ex.check_single_asset()
                except (FuturesError, requests.ConnectionError) as exc:
                    log.warning("Varlık modu denetlenemedi: %s", exc)
                    asset_note = str(exc) if getattr(exc, "code", "") == "MULTI_ASSETS" else None
                    if asset_note:
                        self.account_block, self._account_warned_at = asset_note, now
                if asset_note:
                    self.notifier.send(Messages.warning(asset_note))
            if not ex.is_paper and not self.state.positions:
                try:
                    wallet = self._wallet_note(ex.total_balance())
                except (FuturesError, requests.ConnectionError) as exc:
                    log.warning("Vadeli cüzdan bakiyesi okunamadı: %s", exc)
                    wallet = None
                if wallet:
                    self.notifier.send(Messages.warning(wallet))
            if missing:
                self.notifier.send(Messages.warning("Binance vadeli işlemlerde bulunamayan (atlanan) coinler: "
                                                    + ", ".join(missing)))
            if self.state.paused:
                self.notifier.send("⏸️ Yeni işlemler daha önce /durdur ile durdurulmuştu. Devam için /devam yazın.")
            if self.state.last_alive and now - self.state.last_alive > cfg.outage_alert_sec:
                self.notifier.send(Messages.outage((now - self.state.last_alive) / 60, list(self.state.positions),
                                                   startup=True), critical=bool(self.state.positions))
            self._announced = True
        log.info("Vadeli bot başladı: %s | %d coin | %s", "test" if ex.is_paper else "CANLI", len(coins), mode_note)
        self._reconcile_positions_on_startup()
        if self.state.pending_entry:
            log.info("Bekleyen limit giriş emri: %s", self.state.pending_entry.get("symbol"))
            self._check_pending_entry(now)
        self._init_report_markers(now)
        self.state.last_alive = now
        self.store.save(self.state)
        if self.command_listener is not None:
            self.command_listener.start()
        if self.ip_monitor is not None:
            self.ip_monitor.start()
        self._sync_liq_feed()

    def _sync_liq_feed(self) -> None:
        """Likidasyon akışı: FUTURES_LIQ_SOURCE=feed / both (giriş şartı) veya LIQ_FEED (bildirim) için."""
        smart = self.smart
        want = self.cfg.liq_feed or self.cfg.futures_liq_source != "proxy"
        if want and smart.liq_feed is None:
            from liquidations import LiquidationFeed
            feed = LiquidationFeed()
            if feed.available:
                smart.liq_feed = feed
                feed.start()
            else:
                log.warning("websocket-client kurulu değil; likidasyon akışı başlatılamadı")
        elif not want and smart.liq_feed is not None:
            smart.liq_feed.stop()
            smart.liq_feed = None

    def apply_config(self, cfg) -> None:
        """Telegram'dan değişen ayarlar: borsa katmanı da (komisyon / kayma varsayımları) yeni ayarları kullanır."""
        old_delay, old_step = self._scan_delay(), self._scan_step_ms()
        super().apply_config(cfg)
        self.exchange.cfg = cfg
        new_delay = self._scan_delay()
        if self._scan_step_ms() != old_step:       # Dakikalık tarama açıldı / kapandı: sıradaki tarama yeni adımla
            self.next_scan_at = self._next_close(self.time_fn()) + new_delay
            self._warm_for = 0.0
        elif new_delay != old_delay:               # Hızlı giriş açıldı / kapandı: sıradaki tarama yeni gecikmeyle
            self.next_scan_at += new_delay - old_delay
            self._prepared, self._verified_at, self._prepare_todo, self._warm_for = set(), 0.0, 0, 0.0
            self._prepare_retry, self._self_prepared = {}, set()

    def _check_commission(self) -> None:
        return   # Spot BNB / komisyon kontrolü vadeli işlemde yok

    def fee_status_line(self) -> str:
        cfg = self.cfg
        return (f"💸 Vadeli komisyon (ayar): piyasa %{cfg.futures_taker_fee * 100:.3f} / limit "
                f"%{cfg.futures_maker_fee * 100:.3f}")

    # ================================================================ ana adım
    def tick(self) -> None:
        now = self.time_fn()
        ex = self.exchange
        fast = self._fast()
        # Hızlı girişte taramaya az kala yavaş bakım işleri (parite kuralları, ön hazırlık) yapılmaz: tarama gecikmesin
        quiet = fast and self.next_scan_at - now < FAST_PREPARE_GUARD_SEC
        if now - ex.markets_loaded_at > RULES_REFRESH_SEC and not quiet:
            try:
                ex.load_markets()
            except Exception as exc:  # noqa: BLE001 - eski kurallarla devam
                log.warning("Vadeli pariteler yenilenemedi: %s", exc)
        self.process_commands()
        if not self.running:
            return
        if self.state.pending_entry:
            self._check_pending_entry(now)                   # Limit giriş emri: doldu mu / süresi doldu mu
        if self.state.positions:
            self.manage_positions(now)
        elif self.state.killed or kill_switch_hit(self.state.realized_pnl, 0.0, self.loss_limit):
            self.trigger_kill(self.state.realized_pnl)
        if not self.running:
            return
        if fast and now < self.next_scan_at:
            if self._warm_for != self.next_scan_at and now >= self.next_scan_at - FAST_WARM_LEAD_SEC:
                self._warm_for = self.next_scan_at
                if self.next_scan_at - now >= FAST_WARM_MIN_SEC:
                    self._pre_scan(now)                      # Bağlantıları ısıt, teminat modu / kaldıracı doğrula
            elif not quiet:
                self._prepare_ahead(now)                     # Taramalar arasında: eksik coinleri önceden ayarla
        if now >= self.next_scan_at:
            self.next_scan_at = self._next_close(now) + self._scan_delay()
            blocked = self._scan_block_reason(now)
            step = self._scan_step_ms()
            bar_ms = int(now * 1000) // step * step
            if blocked is None:
                try:
                    self.scan_and_enter(now)
                except Exception as exc:  # noqa: BLE001 - kayda yazılır, ana döngü her zamanki gibi ele alır
                    self._journal({"t": bar_ms, "skip": f"tarama hatası ({type(exc).__name__}: {exc})"[:200]})
                    raise
            else:
                self._journal({"t": bar_ms, "skip": blocked})
        self._periodic(now)

    def _scan_block_reason(self, now: float) -> Optional[str]:
        """Bu mum kapanışında neden taranmıyor (None = taranır)."""
        state = self.state
        if state.killed:
            return "kill-switch"
        if state.paused:
            return "yeni işlemler durdurulmuş (/durdur)"
        if self.entries_frozen(now):
            return f"alımlar donduruldu ({state.freeze_reason})"
        if state.pending_entry:
            return "limit giriş emri bekliyor"
        if self.free_slots() <= 0:
            return f"boş yer yok ({len(state.positions)}/{self.slots()} pozisyon açık)"
        return None

    # ================================================================ canlı tarama kaydı
    def _journal(self, entry: dict) -> None:
        """Tarama kaydına bir satır (JSON). Yazılamazsa bot çalışmaya devam eder."""
        if self.scan_log_path is None:
            return
        try:
            with open(self.scan_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            log.debug("Tarama kaydı yazılamadı: %s", exc)

    def _journal_scan(self, closed_ms: int, universe: List[str], analyzed: List[str], breadth: Optional[int],
                      outcome: Dict[str, str], errors: int, excluded: List[str],
                      timing: Optional[dict] = None) -> None:
        """Yapılan taramanın kaydı: evren (yalnızca değişince), analiz edilen coinler, genişlik, sinyaller ve sonuçları,
        beklemedeki / açık pozisyondaki coinler, mumu alınamayan coin sayısı. `timing` (giriş gecikmesi ölçümü):
        "ms" = [tarama başladı, mumlar alındı, sinyaller hazır] (mum kapanışından sonra ms, sunucu saati); "f" = açılan
        her pozisyon için {"o": emir gönderildi, "r": yanıt geldi, "x": Binance'in işlem zamanı (borsa saati; varsa),
        "p": dolum fiyatı sinyal mumunun kapanışına göre % (+ = pahalıya)}; "kt" = mumlar kaç denemede alındı (> 1 ise);
        "co" = Binance saati − sunucu saati (ms); "fast" = 1: hızlı giriş açıktı."""
        short = [s[:-4] for s in universe]
        row: dict = {"t": closed_ms}
        if short != self._journal_universe:
            row["u"] = short
            self._journal_universe = short
        row["a"] = [s[:-4] for s in analyzed]
        if breadth is not None:
            row["b"] = breadth
        if outcome:
            row["s"] = {k[:-4]: v for k, v in outcome.items()}
        if excluded:
            row["x"] = [s[:-4] for s in excluded]
        if errors:
            row["e"] = errors
        if timing:
            row.update(timing)
        self._journal(row)

    def _prune_scan_log(self) -> None:
        """SCAN_LOG_DAYS günden eski satırları siler (açılışta)."""
        path = self.scan_log_path
        try:
            if path is None or not path.exists():
                return
            cutoff = (self.time_fn() - SCAN_LOG_DAYS * 86400) * 1000
            lines = path.read_text(encoding="utf-8").splitlines()
            keep, universe, need_u = [], None, True
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if "u" in row:
                    universe = row["u"]
                if row.get("t", 0) < cutoff:
                    continue
                if need_u and "skip" not in row:                    # Evren yalnızca değişince yazılır: kalan ilk
                    if "u" not in row and universe is not None:     # tarama satırı silinenlerdeki evreni taşısın
                        row["u"] = universe
                    need_u = False
                keep.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            if len(keep) < len(lines):
                path.write_text("".join(k + "\n" for k in keep), encoding="utf-8")
        except OSError as exc:
            log.debug("Tarama kaydı temizlenemedi: %s", exc)

    def _periodic(self, now: float) -> None:
        if now >= self.next_status_at:
            self.next_status_at = now + self.cfg.status_report_minutes * 60
            self.send_status(routine=True)
        self._maybe_reports(now)
        self._check_freeze_end(now)
        self._check_ip()
        self.smart.update_board(now)
        self.smart.check_liquidations(now)
        self.backtests.poll(now)
        if self.equity_log is not None and now >= self.next_equity_at:
            self.next_equity_at = now + self.cfg.equity_log_minutes * 60
            self.equity_log.append(now, self.state.capital, self.unrealized(), len(self.state.positions))
        if now - self._last_alive_save >= ALIVE_SAVE_SEC:
            self._last_alive_save = now
            self.trade_log.flush_pending()
            self.state.last_alive = now
            self.store.save(self.state)

    def _next_sleep(self) -> float:
        busy = bool(self.state.positions or self.state.pending_entry)
        if not self._fast():
            if busy:
                return POSITION_POLL_SEC
            return max(1.0, min(self.next_scan_at - self.time_fn(), 30.0))
        # Hızlı giriş: döngü tam tarama anında (ve ondan önce ön hazırlık anında) uyanır; eski hâlinde tarama, uykunun
        # denk gelişine göre 1 sn'ye (açık pozisyon varken 2 sn'ye) kadar geç başlıyordu
        wake = self.next_scan_at
        if self._warm_for != self.next_scan_at:
            wake -= FAST_WARM_LEAD_SEC
        cap = POSITION_POLL_SEC if busy else FAST_PREPARE_POLL_SEC if self._prepare_todo else 30.0
        return max(FAST_MIN_SLEEP_SEC, min(wake - self.time_fn(), cap))

    def bid_ask(self, symbol: str):
        return self.exchange.bid_ask(symbol)

    # ================================================================ tarama ve giriş
    def scan_and_enter(self, now: float) -> None:
        ex = self.exchange
        now_ms = int(now * 1000)
        step = self._scan_step_ms()
        closed_ms = now_ms // step * step                      # Az önce kapanan mumun kapanış zamanı
        # Dakikalık tarama: 5 dk sınırı dışındaki dakikada, o dakikada kapanan (kaydırılmış) 5 dk mumlar 1 dk mumlardan
        # üretilir ve aynı kural uygulanır (backtest: find_signals). 5 dk sınırında her şey eskisi gibidir.
        shift = closed_ms % FUT_BAR_MS
        if now_ms - closed_ms > MAX_SCAN_LAG_SEC * 1000:
            lag = (now_ms - closed_ms) / 1000
            self._journal({"t": closed_ms, "skip": f"gecikme (mum kapanışından {lag:.0f} sn sonra)"})
            return
        fast = self._fast()
        timing: dict = {"ms": [self._since_ms(closed_ms)]}     # Giriş gecikmesi ölçümü: tarama başladı
        self._entry_lags = {}
        state = self.state
        state.cooldowns = {s: t for s, t in state.cooldowns.items() if t > now}   # Süresi dolan beklemeler
        state.wave_reentry = {s: v for s, v in state.wave_reentry.items() if s in state.cooldowns}
        symbols, skipped = self._scan_symbols()
        limit = futures_kline_limit(self.cfg)       # En uzun pencere + izleme süresi + oynaklık geçmişi
        excluded = self._scan_excluded()
        cascade = self._cascade_on()
        # Dalga modu: genişlik tüm taranan coinlerden ölçülür (backtestteki evrenle aynı); beklemedeki / açık
        # pozisyondaki coinlerin mumları da istenir ama onlarda sinyal aranmaz
        targets = [s for s in symbols if ex.has_market(s) and (cascade or s not in excluded)]
        klines, errors, tries = self._fetch_klines(targets, limit, closed_ms, fast, shift)
        timing["ms"].append(self._since_ms(closed_ms))         # ...mumlar alındı
        if tries > 1:
            timing["kt"] = tries
        if self._last_quiet:
            timing["kq"] = self._last_quiet                    # Yeni mumu beklenmeden kabul edilen sakin coin sayısı
        breadth = market_breadth(klines) if cascade else None
        self.last_breadth = breadth
        reentry = self._reentry_bypass(breadth, now)       # Faz 6: dalgada zarar sonrası bekleme atlanır
        if reentry:
            excluded = excluded - reentry
            log.info("Dalgada yeniden giriş: %s coin düşüyor, zarar sonrası beklemesi atlanan: %s", breadth,
                     ", ".join(sorted(reentry)))
        klines = {s: k for s, k in klines.items() if s not in excluded}
        self._attach_extras(klines, closed_ms)       # Faz 4: fonlama oranları, 1 saatlik mumlar (modül açıksa)
        btc_1h = ex.klines("BTCUSDT", "1h", FUT_BTC_1H_LIMIT) if self.strategy.needs_btc_trend else None
        signals, rejects = self.strategy.evaluate(klines, btc_1h, now_ms, feed=self.smart.liq_feed, breadth=breadth)
        timing["ms"].append(self._since_ms(closed_ms))         # ...sinyaller hazır
        for sig in signals:
            if sig.symbol in reentry:
                sig.metrics["reentry"] = 1.0
        if errors:
            log.warning("Vadeli tarama: %d coinin mumları alınamadı (%s)", len(errors), "; ".join(errors[:3]))
            rejects["veri alınamadı"] += len(errors)
        if skipped:
            rejects["çöküş / pompa yok (24 saatlik aralık dar)" if futures_pump_rules(self.cfg)
                    else "çöküş yok (24 saatlik tepeye yakın)"] += skipped
        result = ScanResult(signals=list(signals), candidates=len(symbols) + skipped, analyzed=len(klines),
                            rejects=rejects)
        self._record_scan(result, now)
        self.last_signals = list(signals)
        outcome: Dict[str, str] = {}
        stop = "boş yer yok"
        for sig in signals:              # En güçlüden: boş yer oldukça (dalga modunda ek pozisyon yalnızca dalgada)
            if self.free_slots() <= 0 or self.state.pending_entry:
                stop = "boş yer yok" if self.free_slots() <= 0 else "limit giriş emri bekliyor"
                break
            if sig.symbol in self.state.positions:
                outcome[sig.symbol] = "bu coinde pozisyon zaten açık"
                continue
            if self.state.positions and (breadth or 0) < self.cfg.futures_extra_breadth:
                log.info("Ek pozisyon açılmadı: dalga yok (%s coin düşüyor, gereken %d)", breadth,
                         self.cfg.futures_extra_breadth)
                stop = f"ek pozisyon için dalga yok ({breadth} coin düşüyor, gereken {self.cfg.futures_extra_breadth})"
                break
            log.info("Vadeli sinyal: %s %s | %s", sig.symbol, sig.side, sig.note())
            try:
                self.open_futures(sig, now)
            except MinNotionalError as exc:
                log.warning("Minimum emir tutarı: %s", exc)
                self.notifier.send(Messages.warning(f"{sig.symbol} {sig.side.upper()} sinyali pas geçildi: "
                                                    f"{describe_error(exc)}"))
                outcome[sig.symbol] = "minimum emir tutarının altında"
                continue                 # Sıradaki sinyal denenir (backtest de minimum emrin altındakini atlar)
            except FuturesError as exc:
                log.error("Vadeli pozisyon açılamadı: %s", exc)
                if self._note_account_block(exc, now, sig.symbol):   # Hesap modu engeli: saatte bir kritik uyarı
                    outcome[sig.symbol] = f"açılamadı: {exc}"[:120]
                    stop = "hesap modu engeli (Çoklu Varlık, -4168)"
                    break
                self.notifier.send(Messages.error(f"{sig.symbol} {sig.side.upper()} pozisyonu açılamadı: "
                                                  f"{describe_error(exc)}"))
                outcome[sig.symbol] = f"açılamadı: {describe_error(exc)}"[:120]
                stop = "önceki girişte hata"
                break
            if sig.symbol in self.state.positions:
                self.account_block = ""
            outcome[sig.symbol] = ("açıldı" if sig.symbol in self.state.positions
                                   else "limit emir kondu" if self.state.pending_entry else "açılmadı (koruma)")
        for sig in signals:
            outcome.setdefault(sig.symbol, stop)
        full = (self._universe_symbols() if self.cfg.futures_universe > 0
                else [c + "USDT" for c in futures_coin_list(self.cfg)])
        if self._entry_lags:
            timing["f"] = {k[:-4]: v for k, v in self._entry_lags.items()}
        if fast:
            timing["fast"] = 1
            if self._clock_offset_ms is not None:
                timing["co"] = int(round(self._clock_offset_ms))
        self.last_timing = dict(timing, t=closed_ms)
        self._journal_scan(closed_ms, full, sorted(klines), breadth, outcome, len(errors),
                           sorted(s for s in excluded if s in set(full)), timing)

    def _fetch_klines(self, targets: List[str], limit: int, closed_ms: int,
                      fast: bool, shift: int = 0) -> Tuple[Dict[str, dict], List[str], int]:
        """Taranan coinlerin 5 dk mumları: (parite → mumlar; yalnızca son mumu az önce kapanan olanlar, hata metinleri,
        deneme sayısı).

        Hızlı girişte tarama mum kapanışından 1 sn sonra başlar: yanıtta az önce kapanan mumun ARDINDAN yeni (süren)
        mum da görünmelidir. Bu, Binance'in o mumu kapattığını gösterir; sunucunun saati ilerideyse ya da veri henüz
        yerleşmediyse görünmez. Yeni mumu görünmeyen HER coin kısa aralıklarla yeniden istenir (yalnızca o coinler);
        en geç eski tarama anında (kapanış + SCAN_DELAY_SEC) eski davranışa dönülür: az önce kapanan mum yanıttaysa
        yeterlidir. Ölçüm (Binance spot veri ucu, Kasım 2026, 22 coin × 3 sınır, 632 yanıt): kapanan mum sınırdan
        ortanca 0.3 sn, en geç 2.5 sn sonra son hâlini aldı (ETH, SOL, BNB gibi likit coinlerde de); yeni mum ortanca
        1-1.5 sn, en geç 2.5 sn sonra göründü ve göründüğü hiçbir yanıtta kapanan mum eksik değildi. Coinlerin bir
        kısmında yeni mum göründü diye gecikenleri beklemeden kabul etmek kesinleşmemiş mumu taramaya sokabiliyordu;
        bu yüzden kararı etkileyebilecek her coin kendi yeni mumunu bekler.

        Sakin coin kuralı (rapor 18 ölçümü: 103 canlı taramanın 61'i tek tük coinin yeni mumu için eski tarama anına
        kadar bekledi, mumlar ortanca 3.7 sn'de alındı): yeni mumu görünmeyen coinin kapanışı FAST_NEAR_* puan oynasa
        bile ne bir çöküş / dalga eşiğine ne de genişlik eşiğine ulaşamıyorsa o coin beklenmez; kesinleşmemiş mumu
        taramanın sonucunu değiştiremez. Eşiğe yakın ya da eşiğin üstündeki (sinyal adayı) coinler eskisi gibi bekler.
        Sinyal yalnızca kapanışlara bağlı değilse (ek modül, likidasyon şartı, oynaklık eşiği...) kural uygulanmaz."""
        ex = self.exchange
        quiet_rules = self._quiet_rules() if fast else None
        quiet: Set[str] = set()
        # Dakikalık taramanın ara dakikası (shift > 0): 1 dk mumlar istenir; az önce kapanan 1 dk mum yanıttaysa
        # o dakikada kapanan 5 dk mumlar üretilir (futures_data.resample, backtestle aynı fonksiyon)
        interval, bar_ms = ("1m", MINUTE_SCAN_STEP_MS) if shift else ("5m", FUT_BAR_MS)
        if shift:
            limit = futures_minute_limit(self.cfg)

        def fetch(symbol: str):
            try:
                if fast:
                    k, open_ms = ex.klines_state(symbol, interval, limit)
                    return symbol, k, open_ms, None
                return symbol, ex.klines(symbol, interval, limit), 0, None
            except FuturesError as exc:
                return symbol, None, 0, exc

        def burst(symbols: List[str]):                # Ağ hatası (ConnectionError) sıralı sürümdeki gibi yükselir
            if fast:                                  # Tek dalga, CCXT'nin istekler arası beklemesi olmadan
                with ex.unthrottled():
                    if len(symbols) == 1:
                        return [fetch(symbols[0])]
                    with ThreadPoolExecutor(max_workers=min(FAST_KLINE_WORKERS, len(symbols))) as pool:
                        return list(pool.map(fetch, symbols))
            if len(symbols) > KLINE_PARALLEL_MIN:
                with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as pool:
                    return list(pool.map(fetch, symbols))
            return [fetch(s) for s in symbols]

        klines: Dict[str, dict] = {}
        failed: Dict[str, str] = {}
        settled: Set[str] = set()
        pending, tries = list(targets), 0
        deadline = closed_ms / 1000 + SCAN_DELAY_SEC
        while pending:
            tries += 1
            again = []
            for symbol, k, open_ms, exc in burst(pending):
                if exc is not None:
                    if symbol not in klines:          # Yeniden denemede hata: önceki yanıt (aynı mum) kullanılır
                        failed[symbol] = f"{symbol}: {exc}"
                    continue
                failed.pop(symbol, None)
                fresh = bool(len(k["t"])) and int(k["t"][-1]) + bar_ms == closed_ms
                if fresh and shift:                   # 1 dk mumlar → bu dakikada kapanan 5 dk mumlar
                    k = resample(k, FUT_BAR_MS, shift_ms=shift)
                    fresh = bool(len(k["t"])) and int(k["t"][-1]) + FUT_BAR_MS == closed_ms
                if fresh:                             # Yalnızca az önce kapanan mum
                    klines[symbol] = k
                if fresh and open_ms >= closed_ms:
                    settled.add(symbol)
                    quiet.discard(symbol)
                elif fresh and quiet_rules is not None and not self._near_decision(k, quiet_rules):
                    quiet.add(symbol)                 # Kesinleşmemiş olsa da kararı değiştiremez: beklenmez
                else:
                    again.append(symbol)
            if not fast or not again:
                break
            left = deadline - self.time_fn()
            if left <= 0 or tries >= FAST_SETTLE_MAX_TRIES:
                break                                 # Eski tarama anı: eldeki (az önce kapanan) mumlarla devam
            self.sleep_fn(min(FAST_SETTLE_WAIT_SEC, left))
            pending = again
        self._last_quiet = len(quiet)
        if fast and (tries > 1 or quiet):
            log.info("Hızlı giriş: mumlar %d denemede alındı (yeni mumu görünen %d / %d coin; beklenmeyen sakin coin %d)",
                     tries, len(settled), len(targets), len(quiet))
        return klines, list(failed.values()), tries

    def _quiet_rules(self) -> Optional[Tuple[Tuple[int, float], ...]]:
        """Sakin coin kuralının eşikleri: ((mum sayısı, düşüş eşiği %), ...), ana + ek + dalga kurallarının saat başına
        en düşüğü. Sinyal yalnızca kapanışlara bağlı değilse None (kural uygulanmaz: her coin yeni mumunu bekler)."""
        cfg = self.cfg
        if self._quiet_for is cfg:
            return self._quiet_val
        rules = None
        if futures_close_only(cfg):
            merged: Dict[int, float] = {}
            for c in (cfg, futures_wave_cfg(cfg)):
                for hours, pct in (futures_crash_rules(c) if c is not None else ()):
                    merged[int(hours)] = min(float(pct), merged.get(int(hours), float(pct)))
            rules = tuple(sorted((h * 12, pct) for h, pct in merged.items()))
        self._quiet_for, self._quiet_val = cfg, rules
        return rules

    def _near_decision(self, k: dict, rules: Tuple[Tuple[int, float], ...]) -> bool:
        """Coinin az önce kapanan mumu bir kararı değiştirebilir mi: düşüşü bir çöküş / dalga eşiğine FAST_NEAR_CRASH_PCT
        puandan yakın (ya da üstünde), ya da 1 saatlik düşüşü genişlik eşiğine FAST_NEAR_BREADTH_PCT puandan yakın.
        Yetersiz geçmiş → True (temkinli). Hesap strategy.crash_drop_pct ile aynı: kapanıştan kapanışa düşüş."""
        c = k["c"]
        n = len(c)
        last = float(c[-1]) if n else 0.0
        if last <= 0:
            return True
        for bars, pct in rules:
            if n <= bars:
                return True
            ref = float(c[-1 - bars])
            if ref <= 0 or (1 - last / ref) * 100 >= pct - FAST_NEAR_CRASH_PCT:
                return True
        if self._cascade_on():
            bars = FUT_BREADTH_HOURS * 12
            if n <= bars:
                return True
            ref = float(c[-1 - bars])
            if ref <= 0 or abs((1 - last / ref) * 100 - FUT_BREADTH_DROP_PCT) <= FAST_NEAR_BREADTH_PCT:
                return True
        return False

    # ================================================================ hızlı giriş: ön hazırlık
    def _fast_symbols(self) -> List[str]:
        """Önceden hazırlanacak (teminat modu + kaldıraç) pariteler: taranan evrenin tamamı."""
        cfg = self.cfg
        symbols = (self._universe_symbols() if cfg.futures_universe > 0
                   else [c + "USDT" for c in futures_coin_list(cfg)])
        return [s for s in symbols if self.exchange.has_market(s)]

    def _verify_prepared(self, now: float) -> bool:
        """Binance'teki parite ayarlarını okur (tek istek): teminat modu ISOLATED ve kaldıracı ayardakiyle aynı olan
        pariteler "hazır" sayılır. Yalnızca bu yanıta güvenilir: bot kendi ayarladığını da buradan teyit eder, elle
        yapılan değişiklik en geç bir sonraki taramadan önce görülür. Okunamazsa hazır parite kalmaz (girişte eski yol:
        teminat modu + kaldıraç emirden hemen önce ayarlanır)."""
        ex = self.exchange
        lev = int(self.cfg.futures_leverage)
        self._prepared, self._prepared_lev = set(), lev
        try:
            settings = None if ex.is_paper else ex.symbol_settings()
        except (FuturesError, requests.ConnectionError) as exc:
            log.warning("Hızlı giriş: parite ayarları doğrulanamadı (girişte eski yol kullanılır): %s", exc)
            settings = None
        self._verified_at = now if settings is not None else 0.0
        if settings is None:
            self._prepare_todo = 0
            return False
        symbols = self._fast_symbols()
        self._prepared = {s for s in symbols if settings.get(s) == (True, lev)}
        for symbol in self._self_prepared - self._prepared:      # Ayarlandı ama Binance farklı gösteriyor: ısrar etme
            self._prepare_retry[symbol] = now + FAST_PREPARE_RETRY_SEC
            log.warning("Hızlı giriş: %s önceden ayarlandı ama Binance farklı gösteriyor (%s); bu paritede girişten "
                        "hemen önce ayarlanacak", symbol, settings.get(symbol))
        self._self_prepared = set()
        self._prepare_todo = sum(1 for s in symbols if s not in self._prepared and s not in self.state.positions
                                 and now >= self._prepare_retry.get(s, 0.0))
        return True

    def _is_prepared(self, symbol: str, lev: int, now: float) -> bool:
        """Parite için teminat modu + kaldıraç az önce (bu taramadan hemen önce) Binance'ten doğrulandı mı?"""
        return (self._fast() and symbol in self._prepared and self._prepared_lev == lev
                and 0 <= self.time_fn() - self._verified_at <= FAST_VERIFY_MAX_AGE_SEC)

    def _pre_scan(self, now: float) -> None:
        """Taramadan FAST_WARM_LEAD_SEC önce (mum kapanmadan): bağlantılar ısıtılır (paralel hafif istekler; aynı
        yanıtlardan Binance ile saat farkı ölçülür) ve canlı modda teminat modu / kaldıraç doğrulanır. Tarama zaten
        yapılmayacaksa (durdurulmuş, yer yok...) atlanır; hata taramayı engellemez."""
        if self._scan_block_reason(now) is not None:
            return
        ex = self.exchange
        try:
            offset = ex.warm(min(FAST_KLINE_WORKERS, max(1, len(self._fast_symbols()))))
        except Exception as exc:  # noqa: BLE001 - yalnızca hız; tarama her durumda yapılır
            log.debug("Hızlı giriş: bağlantılar ısıtılamadı: %s", exc)
            offset = None
        self._clock_offset_ms = offset
        if offset is not None and abs(offset) >= CLOCK_WARN_MS and now - self._clock_warned_at >= 3600:
            self._clock_warned_at = now
            log.warning("Sunucu saati Binance'in %.0f ms %s: mum kapanışı yanlış anda sanılır, hızlı giriş yeniden "
                        "denemelerle yavaşlar ya da geç tarar. Sunucuda saat eşitlemesini (NTP / chrony) denetleyin.",
                        abs(offset), "gerisinde" if offset > 0 else "ilerisinde")
        if not ex.is_paper:
            self._verify_prepared(self.time_fn())

    def _prepare_ahead(self, now: float) -> None:
        """Taramalar arasında (taramaya en az FAST_PREPARE_GUARD_SEC varken): teminat modu / kaldıracı henüz doğru
        olmayan pariteler azar azar ayarlanır, böylece sinyal anında bu iki istek beklenmez. Açık pozisyonlu pariteye
        dokunulmaz. Ayarlanamayan parite bir saat sonra yeniden denenir; o paritede sinyal gelirse eski yol (girişten
        hemen önce ayarlama) çalışır ve hata her zamanki gibi bildirilir."""
        ex = self.exchange
        if ex.is_paper or self._scan_block_reason(now) is not None:
            self._prepare_todo = 0                    # Tarama yapılmayacak (durdurulmuş, yer yok...): acele yok
            return
        lev = int(self.cfg.futures_leverage)
        if self._prepared_lev != lev or now - self._verified_at > FAST_VERIFY_EVERY_SEC:
            if not self._verify_prepared(now):
                return
        if not self._prepare_todo:
            return
        todo = [s for s in self._fast_symbols() if s not in self._prepared and s not in self.state.positions
                and now >= self._prepare_retry.get(s, 0.0)]
        for symbol in todo[:FAST_PREPARE_BATCH]:
            try:
                ex.prepare_symbol(symbol, lev)
                self._prepared.add(symbol)            # Sıradaki doğrulamada Binance'ten teyit edilir
                self._self_prepared.add(symbol)
                self.account_block = ""
                log.info("Hızlı giriş: %s teminat modu ISOLATED, kaldıraç %dx önceden ayarlandı", symbol, lev)
            except FuturesError as exc:
                self._prepare_retry[symbol] = now + FAST_PREPARE_RETRY_SEC
                log.warning("Hızlı giriş: %s önceden ayarlanamadı (sinyal gelirse girişte yeniden denenir): %s",
                            symbol, exc)
                if self._note_account_block(exc, now):          # Hesap düzeyinde: diğer coinler de aynı yanıtı alır
                    for other in todo:
                        self._prepare_retry[other] = now + FAST_PREPARE_RETRY_SEC
                    self._prepare_todo = 0
                    return
        self._prepare_todo = max(0, len(todo) - FAST_PREPARE_BATCH)

    def _note_account_block(self, exc: Exception, now: float, symbol: str = "") -> bool:
        """Hata hesap düzeyinde bir giriş engeliyse (Binance hesabı Çoklu Varlık modunda: yalıtılmış teminat reddedilir,
        -4168) kaydeder ve Telegram'a kritik uyarı yollar (en fazla saatte bir; sinyal her 5 dk'da yinelenebilir).
        Engel /tarama'da da görünür; teminat modu ayarlanabildiğinde ya da pozisyon açıldığında kalkar."""
        if getattr(exc, "code", "") != "MULTI_ASSETS":
            return False
        self.account_block = str(exc)
        if now - self._account_warned_at >= ACCOUNT_WARN_EVERY_SEC:
            self._account_warned_at = now
            head = f"{symbol} sinyali açılamadı. " if symbol else ""
            self.notifier.send(Messages.error(head + str(exc)), critical=True)
        return True

    def _refresh_universe(self, now: float, force: bool = False) -> None:
        """En hacimli N köklü parite listesini (gerekirse) yeniler: günde bir ya da ayar değişince. Sıralama durumda
        saklanır (yeniden başlatmada hemen hazır). Alınamazsa önceki sıralama, hiç yoksa geçici olarak 24 saatlik hacme
        göre sıralama kullanılır ve 10 dk sonra yeniden denenir; evren yalnız listeye düşerse Telegram'dan uyarılır."""
        cfg = self.cfg
        daily = getattr(cfg, "futures_universe_mode", "avg") == "daily"
        key = (cfg.futures_universe_min_age_days, 1 if daily else cfg.futures_universe_avg_days)

        def is_fresh(at: float) -> bool:
            return (now - at < UNIVERSE_REFRESH_SEC if not daily
                    else int(now // 86400) == int(at // 86400))                  # daily: her UTC gününde yeniden
        fresh = bool(self._ranking) and key == self._ranking_key and is_fresh(self._ranking_at)
        waiting = key == self._ranking_key and now < self._ranking_retry_at       # Son deneme başarısız: biraz bekle
        if not force and (fresh or waiting):
            self.universe = self._ranking[:cfg.futures_universe]
            return
        saved = self.state.futures_ranking or {}
        if (not force and not self._ranking and saved.get("symbols") and list(saved.get("key") or ()) == list(key)
                and is_fresh(float(saved.get("at") or 0.0))):
            self._ranking, self._ranking_key = list(saved["symbols"]), key        # Yeniden başlatma: kayıtlı sıralama
            self._ranking_at, self._ranking_source, self._ranking_error = float(saved["at"]), "avg", ""
            self.universe = self._ranking[:cfg.futures_universe]
            log.info("En hacimli %d köklü parite (kayıtlı sıralama): %s", len(self.universe),
                     ", ".join(s[:-4] for s in self.universe))
            return
        log.info("En hacimli köklü pariteler sıralanıyor (en az %d günlük, son %d gün ortalaması)...", *key)
        error = ""
        try:
            ranking = [s for s, _qv in self.exchange.seasoned_ranking(*key)]
        except (FuturesError, requests.ConnectionError) as exc:
            ranking, error = [], str(exc)
        if ranking:
            recovered = bool(self._ranking_error)
            self._ranking, self._ranking_key, self._ranking_at = ranking, key, now
            self._ranking_source, self._ranking_error, self._ranking_retry_at = "avg", "", 0.0
            self.state.futures_ranking = {"key": list(key), "at": now, "symbols": ranking}
            self.store.save(self.state)
            self.universe = self._ranking[:cfg.futures_universe]
            log.info("En hacimli %d köklü parite: %s", len(self.universe), ", ".join(s[:-4] for s in self.universe))
            if recovered and self._universe_warned_at:
                self._universe_warned_at = 0.0
                self.notifier.send(f"✅ En hacimli coin sıralaması alındı: {len(self._universe_symbols())} coin "
                                   f"taranıyor ({', '.join(s[:-4] for s in self.universe)}).")
            return
        error = error or "sıralama boş geldi (uygun parite ya da hacim bilgisi yok)"
        self._ranking_error = error
        self._ranking_retry_at = now + UNIVERSE_RETRY_SEC                         # 10 dk sonra tekrar dene
        if not self._ranking or key != self._ranking_key:
            fallback = self._ranking_by_24h(key[0])
            if fallback:
                self._ranking, self._ranking_source = fallback, "24h"
            elif key != self._ranking_key:
                self._ranking, self._ranking_source = [], ""
        self._ranking_key = key
        self.universe = self._ranking[:cfg.futures_universe]
        log.warning("Hacim sıralaması alınamadı (%s); %s", error, self.universe_label())
        degraded = not self.universe or self._ranking_source == "24h"     # Önceki iyi sıralama varsa uyarı yok
        if degraded and now - self._universe_warned_at >= UNIVERSE_WARN_SEC:
            self._universe_warned_at = now
            self.notifier.send(Messages.warning(
                f"En hacimli {cfg.futures_universe} coin sıralaması alınamadı: {error}. Şimdilik "
                f"{self.universe_label()}; 10 dk sonra yeniden denenecek. Backtest bu sıralamayla çalıştığı için "
                f"liste dışındaki coinlerdeki işlemler kaçabilir."))

    def _ranking_by_24h(self, min_age_days: float) -> List[str]:
        """Geçici evren: köklü paritelerin son 24 saatlik USDT hacmine göre sıralaması (tek istek)."""
        try:
            eligible = set(self.exchange.eligible_symbols(min_age_days))
            if not eligible:
                return []
            tickers = self.exchange.tickers_24h()
        except Exception as exc:  # noqa: BLE001 - geçici evren en iyi çaba: bot hiçbir durumda durmasın
            log.warning("24 saatlik hacimler de alınamadı: %s", exc)
            return []
        rows = sorted(((t.get("qv", 0.0), s) for s, t in tickers.items() if s in eligible and t.get("qv", 0.0) > 0),
                      reverse=True)
        return [s for _qv, s in rows]

    def universe_label(self) -> str:
        """Taranan evrenin kısa açıklaması (/tarama, uyarılar): kaç coin, nereden."""
        cfg = self.cfg
        n = len(self._universe_symbols())
        if cfg.futures_universe <= 0:
            return f"{n} coin (FUTURES_COINS listesi)"
        listed = " + liste" if cfg.futures_universe_with_list else ""
        if not self.universe:
            return f"yalnız liste: {n} coin (en hacimli {cfg.futures_universe} sıralaması yok)"
        if self._ranking_source == "24h":
            return f"{n} coin (geçici: 24 saatlik hacme göre en hacimli {cfg.futures_universe}{listed})"
        return f"{n} coin (en hacimli {cfg.futures_universe}{listed})"

    def _universe_symbols(self) -> List[str]:
        """En hacimli N köklü parite (+ FUTURES_UNIVERSE_WITH_LIST açıksa FUTURES_COINS listesindekiler)."""
        symbols = list(self.universe)
        if self.cfg.futures_universe_with_list:
            symbols += [c + "USDT" for c in futures_coin_list(self.cfg) if c + "USDT" not in symbols]
        return symbols

    def _scan_symbols(self) -> Tuple[List[str], int]:
        """Taranacak pariteler ve ön elemede atlananların sayısı.

        FUTURES_UNIVERSE=0: FUTURES_COINS listesi. N > 0: en hacimli N köklü parite (sıralama günde bir yenilenir).
        Çöküş taktiğinde, fiyatı 24 saatlik tepesinin (en küçük eşik - pay) kadar altında olmayan coinlerin mumları
        hiç istenmez: son H (≤ 24) saatte %X düşen coin, 24 saatlik tepesinin de en az %X altındadır. Teyit beklenirken
        (izleme modu) fiyat çöküşten sonra toparlanmış olabilir: o zaman 24 saatlik DİP tepenin %X altında mı diye
        bakılır (pencere + izleme süresi 24 saati aşarsa ön eleme yapılmaz). Pompa modülü açıksa fiyatı 24 saatlik
        dibinin en az %Y üstünde olanlar da taranır; Bollinger / kırılım modülleri her coinde sinyal verebileceği için
        onlar açıkken ön eleme yapılmaz."""
        cfg = self.cfg
        if cfg.futures_universe <= 0:
            return [c + "USDT" for c in futures_coin_list(cfg)], 0
        self._refresh_universe(self.time_fn())
        symbols = self._universe_symbols()
        if cfg.futures_tactic != "crash" or not symbols:
            return symbols, 0
        mods = futures_modules(cfg)
        if "bb" in mods or "breakout" in mods or "pullback" in mods:
            return symbols, 0
        rules = (futures_crash_rules(cfg) + futures_wave_rules(cfg)        # Faz 6 dalga eşikleri de (genişlik sonra)
                 + (futures_funding_rules(cfg) if "funding" in mods else ()))
        watch = cfg.futures_confirm != "close"
        reach_h = max(h for h, _p in rules) + (cfg.futures_confirm_bars / 12 if watch else 0)
        if reach_h > 24:
            return symbols, 0
        least = min(p for _h, p in rules)
        if cfg.futures_crash_z > 0:
            least = min([cfg.futures_crash_z_min_pct] + [p for _h, p in futures_funding_rules(cfg) if "funding" in mods])
        pump = futures_pump_rules(cfg)
        if self._fast() and len(symbols) <= FAST_PREFILTER_MIN:
            # Hızlı giriş: küçük evrende ön eleme atlanır. Kazandırdığı birkaç mum isteğine karşılık taramanın en
            # başında bir istek daha bekletir (CCXT bu ağır istekten önce 2 sn'ye kadar bekler). Ön eleme yalnızca
            # sinyal veremeyecek coinleri atladığı için sinyaller ve genişlik değişmez.
            return symbols, 0
        try:
            tickers = self.exchange.tickers_24h()
        except (FuturesError, requests.ConnectionError) as exc:
            log.warning("24 saatlik özetler alınamadı (ön eleme yapılmadı): %s", exc)
            return symbols, 0
        if self._cascade_on():           # Genişlik: son 1 saatte %3+ düşen coin 24 saatlik tepesinin en az %3 altında
            least = min(least, FUT_BREADTH_DROP_PCT)
        need = max(0.0, least - PREFILTER_SLACK_PCT) / 100
        need_up = max(0.0, min(p for _h, p in pump) - PREFILTER_SLACK_PCT) / 100 if pump else None
        keep = []
        for symbol in symbols:
            t = tickers.get(symbol)
            price = t.get("low", 0.0) if t is not None and watch else (t or {}).get("last", 0.0)
            if t is None or t["high"] <= 0 or price <= 0 or price <= t["high"] * (1 - need):
                keep.append(symbol)                  # Özet yoksa temkinli: mumlara bakılır
            elif need_up is not None and 0 < t.get("low", 0.0) and t["last"] >= t["low"] * (1 + need_up):
                keep.append(symbol)                  # Pompa adayı: 24 saatlik dibinin yeterince üstünde
        return keep, len(symbols) - len(keep)

    def open_futures(self, sig: FuturesSignal, now: float) -> Optional[Position]:
        cfg, ex = self.cfg, self.exchange
        symbol, side = sig.symbol, sig.side
        lev = int(cfg.futures_leverage)
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        if cfg.stop_loss_pct >= room - 0.5:   # Ayar doğrulaması da engeller; kaldıraç çalışırken değişebilir
            raise FuturesError(f"Zarar kes (%{cfg.stop_loss_pct:g}) {lev}x'te likidasyona (%{room:.1f}) çok yakın")
        # 1) İşleme girmeden HEMEN ÖNCE: yalıtılmış teminat + kaldıraç (her girişte, zorunlu). Hızlı girişte ikisi
        #    önceden ayarlanır ve bu taramadan hemen önce Binance'ten doğrulanır: doğrulanmışsa yeniden istenmez
        #    (2 istek eksik); doğrulanmamışsa (elle değişmiş, okunamamış, doğrulama eski) eski yol çalışır.
        fast = self._fast()
        if not self._is_prepared(symbol, lev, now):
            ex.prepare_symbol(symbol, lev)
        # 2) Büyüklük: serbest bakiyenin FUTURES_MARGIN_USAGE_PCT'si teminat × kaldıraç (bileşik); dalga modunda
        #    bakiyenin (serbest + açık pozisyon teminatları) 1/N'i, yalnız çöküşte × FUTURES_ISOLATED_SIZE_PCT
        with (ex.unthrottled() if fast else nullcontext()):     # Hızlı giriş: istekler arası CCXT beklemesi yok
            free = self._available_margin()
            margin = self._entry_margin(sig, free)
            bid, ask = ex.bid_ask(symbol)
            if cfg.futures_entry_order == "limit":
                self._place_limit_entry(sig, now, lev, margin, bid, ask)
                return None
            price = ask if side == "long" else bid
            qty = ex.amount_to_precision(symbol, margin * lev / price)
            ex.check_order_size(symbol, qty, price)                  # MinNotionalError → işlem pas geçilir
            order_side = "buy" if side == "long" else "sell"
            sent = self.time_fn()
            try:
                fill = ex.market_order(symbol, order_side, qty)
            except FuturesError as exc:
                if exc.code != "-2019":
                    raise
                qty = ex.amount_to_precision(symbol, qty * MARGIN_RETRY_FACTOR)   # Komisyon payı yetmedi: biraz küçült
                ex.check_order_size(symbol, qty, price)
                fill = ex.market_order(symbol, order_side, qty)
        try:
            self._note_entry_lag(sig, fill, now, sent, self.time_fn())
        except Exception as exc:  # noqa: BLE001 - ölçüm, dolmuş emrin korumasını (zarar kes) asla engellemesin
            log.debug("Giriş gecikmesi ölçülemedi: %s", exc)
        return self._open_from_fill(sig, fill, lev, now)

    def _closed_ms(self, now: float) -> int:
        """`now` anında en son kapanan tarama mumunun kapanışı (ms): 5 dk; dakikalık taramada 1 dk adımıyla."""
        step = self._scan_step_ms()
        return int(now * 1000) // step * step

    def _note_entry_lag(self, sig: FuturesSignal, fill: FFill, now: float, sent: float, done: float) -> None:
        """Giriş gecikmesi ölçümü (piyasa emriyle giriş): emrin mum kapanışından kaç ms sonra gönderildiği, yanıtın ne
        zaman geldiği, Binance'in işlem zamanı (borsa saati; sunucu saatinden bağımsız) ve dolum fiyatının sinyal
        mumunun kapanışına göre farkı (%; + = daha pahalıya). Tarama kaydına ve pozisyona yazılır (backtest girişte
        +%0.05 gecikme payı + kayma varsayar: futures_backtest.ENTRY_LAG_PCT)."""
        closed_ms = self._closed_ms(now)
        lag: Dict[str, float] = {"o": int(round(sent * 1000 - closed_ms)), "r": int(round(done * 1000 - closed_ms))}
        exch = int(fill.ts_ms) - closed_ms if fill.ts_ms else None
        if exch is not None and 0 <= exch <= MAX_LAG_MS:
            lag["x"] = exch
        if not 0 <= lag["r"] <= MAX_LAG_MS:
            return
        if sig.price > 0 and fill.price > 0:
            sign = 1 if sig.side == "long" else -1
            lag["p"] = round(float(sign * (fill.price / sig.price - 1) * 100), 4)
            sig.metrics["slip_pct"] = lag["p"]
        sig.metrics["lag_ms"] = float(lag.get("x", lag["r"]))
        self._entry_lags[sig.symbol] = lag

    @staticmethod
    def _lag_text(pos: Position) -> str:
        """Günlük satırı için ölçülen giriş gecikmesi (ölçüm yoksa boş)."""
        try:
            lag = float(pos.metrics["lag_ms"]) / 1000
            return (f" | mum kapanışından {lag:.1f} sn sonra, kapanışa göre "
                    f"{float(pos.metrics.get('slip_pct', 0.0)):+.3f}%")
        except (KeyError, TypeError, ValueError):
            return ""

    def _open_from_fill(self, sig: FuturesSignal, fill: FFill, lev: int, now: float,
                        opened_at: Optional[float] = None) -> Optional[Position]:
        """Giriş emri doldu (piyasa ya da limit): pozisyon kaydı, Binance'te zarar kes + Kâr Al, bildirim."""
        cfg, ex = self.cfg, self.exchange
        symbol, side = sig.symbol, sig.side
        entry, qty = fill.price, fill.qty
        sign = 1 if side == "long" else -1
        tp = ex.price_to_precision(symbol, entry * (1 + sign * cfg.take_profit_pct / 100))
        sl = ex.price_to_precision(symbol, entry * (1 - sign * cfg.stop_loss_pct / 100))
        margin_used = qty * entry / lev
        # Faz 8: başka pozisyon açıkken açılan (ek) pozisyon kendi başabaş tetiğiyle izlenir (backtest: simulate_slots)
        n_open = sum(1 for p in self.state.positions.values() if p.futures and p.symbol != symbol)
        be_trigger = futures_be_trigger(cfg, n_open)
        pos = Position(symbol=symbol, base_asset=symbol[:-4], qty=qty, entry_price=entry,
                       cost_usdt=margin_used + fill.fee, opened_at=now if opened_at is None else opened_at,
                       buy_fee_usdt=fill.fee,
                       signal_note=sig.note(), score=sig.score, metrics=dict(sig.metrics), futures=True, side=side,
                       leverage=lev, margin_usdt=margin_used,
                       liq_price=entry * (1 - sign * (1 / lev - cfg.futures_mmr_pct / 100)), sl_price=sl, tp_price=tp,
                       regime=f"{side.upper()} {lev}X")      # İşlem kaydında (CSV) yön ve kaldıraç görünsün
        if cfg.breakeven_enabled and be_trigger != cfg.breakeven_trigger_pct:
            pos.metrics["be_trigger"] = be_trigger
        # Faz 9: ek pozisyon / dalgada açılan ilk pozisyon kendi zaman stopuyla izlenir (backtest: hold_minutes)
        try:
            breadth = int(float(sig.metrics["breadth"])) if "breadth" in sig.metrics else None
        except (TypeError, ValueError):
            breadth = None
        hold = futures_hold_minutes(cfg, n_open, breadth)
        if hold != cfg.max_hold_minutes:
            pos.metrics["hold_min"] = float(hold)
        self.state.positions[symbol] = pos
        if ex.is_paper:
            ex.open_paper(symbol, side, qty, entry, margin_used, lev)
        self.store.save(self.state)
        # 3) Koruma: zarar kes Binance'te bekler (bot kapansa da çalışır); konamazsa pozisyon korumasız kalmaz
        try:
            pos.stop_order_id = ex.place_stop(symbol, side, sl)
        except FuturesError as exc:
            self.notifier.send(Messages.error(f"{symbol} zarar kes emri Binance'e konamadı: {describe_error(exc)}"),
                               critical=True)
            if cfg.futures_exchange_stop:
                self.notifier.send(Messages.warning(f"{symbol} korumasız kalmasın diye pozisyon hemen kapatılıyor "
                                                    f"(FUTURES_EXCHANGE_STOP=true)."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                return None
        try:
            pos.tp_order_id = ex.place_take_profit(symbol, side, qty, tp, limit=cfg.futures_tp_order == "limit")
        except FuturesError as exc:
            self.notifier.send(Messages.warning(f"{symbol} kâr al emri Binance'e konamadı ({exc}); bot hedefi "
                                                f"kendisi izleyecek."))
        self.store.save(self.state)
        self.day_stats.buys += 1
        log.info("Vadeli pozisyon açıldı: %s %s %gx | giriş %.8g | miktar %g | teminat %.4f | TP %.8g | SL %.8g%s",
                 symbol, side, lev, entry, qty, margin_used, tp, sl, self._lag_text(pos))
        self.notifier.notify(Messages.futures_open(pos, pos.signal_note, cfg, qty * entry), "open")
        return pos

    # ================================================================ limit (maker) giriş
    def _available_margin(self) -> float:
        """Yeni pozisyon için kullanılabilir teminat (USDT): vadeli cüzdandaki serbest bakiye; canlı modda botun
        sermayesiyle sınırlı (başlangıç + K/Z - açık pozisyonlara bağlı tutar). Bot yalnızca kendisine verilen sermayeyle
        işlem yapar: cüzdana eklenen para /sermayeekle ile tanıtılınca kullanılır, kill-switch limiti de onunla büyür
        (tanıtılmadan kullanılsaydı pozisyonlar büyür ama kill-switch eski küçük sermayeye göre kalırdı)."""
        free = self.exchange.free_balance()
        if self.exchange.is_paper:
            return free
        return max(0.0, min(free, self.state.capital - self.state.committed))

    def _wallet_note(self, total: float) -> Optional[str]:
        """Canlı modda vadeli cüzdan ile botun sermayesi belirgin farklıysa açılış uyarısı (yoksa None)."""
        capital, q = self.state.capital, self.cfg.quote_asset
        extra = total - capital
        if extra > max(5.0, capital * 0.05):
            return (f"Vadeli (USDⓈ-M) cüzdanda {fmt_usdt(total)} {q} var, bot {fmt_usdt(capital)} {q} sermayeyle "
                    f"işlem yapıyor: bota ayrılmamış {fmt_usdt(extra)} {q} kullanılmaz. Bu parayla da işlem yapılsın "
                    f"istiyorsanız Telegram'a /sermayeekle {extra:.2f} yazın (kill-switch limiti de sermayeyle büyür).")
        if -extra > max(1.0, capital * 0.05):
            return (f"Vadeli (USDⓈ-M) cüzdandaki bakiye ({fmt_usdt(total)} {q}) botun sermayesinden "
                    f"({fmt_usdt(capital)} {q}) az: pozisyonlar cüzdandaki serbest bakiyeyle (daha küçük) açılır. "
                    f"Para çektiyseniz ya da başka işlem açtıysanız normal; değilse Spot'tan vadeli cüzdana aktarın.")
        return None

    def _entry_margin(self, sig: FuturesSignal, free: float) -> float:
        """Girişin teminatı (USDT). Tek pozisyonda serbest bakiye × FUTURES_MARGIN_USAGE_PCT (eskisi gibi); dalga
        modunda (serbest + açık pozisyon teminatları) × kullanım / FUTURES_MAX_POSITIONS, serbest bakiyeyi aşmadan.
        Yalnız çöküşte (genişlik < FUTURES_ISOLATED_BREADTH) × FUTURES_ISOLATED_SIZE_PCT (backtest: simulate)."""
        cfg = self.cfg
        usage = cfg.futures_margin_usage_pct / 100
        factor = isolated_factor(cfg.futures_isolated_size_pct, cfg.futures_isolated_breadth,
                                 int(sig.metrics.get("breadth", cfg.futures_isolated_breadth)))
        if factor < 1:
            sig.metrics["size_pct"] = round(factor * 100, 1)
        slots = self.slots()
        if slots <= 1:
            return free * usage * factor
        opened = [p for p in self.state.positions.values() if p.futures]
        used = sum(p.margin_usdt for p in opened)
        share = futures_slot_share(cfg, len(opened))        # Faz 5: 1/N; Faz 6: ilk büyük, ekler küçük
        if opened and cfg.futures_extra_size_pct < 100:
            sig.metrics["size_pct"] = round(cfg.futures_extra_size_pct * factor, 1)
            sig.metrics["size_kind"] = "ek pozisyon, ilkinin yüzdesi"
        return min((free + used) * usage * share * factor, free * usage)

    def _place_limit_entry(self, sig: FuturesSignal, now: float, lev: int, margin: float, bid: float,
                           ask: float) -> None:
        """FUTURES_ENTRY_ORDER=limit: alışta bid'in FUTURES_LIMIT_OFFSET_PCT altına (satışta ask'ın üstüne) post-only
        limit emir; FUTURES_LIMIT_TTL_MIN dakika dolmayı bekler (state.pending_entry, bot yeniden başlasa da sürer).
        Post-only emir anında eşleşeceği için reddedilirse (fiyat limiti geçti) güncel fiyatla yeniden dizilir."""
        cfg, ex = self.cfg, self.exchange
        symbol, side = sig.symbol, sig.side
        sign = 1 if side == "long" else -1
        order_side = "buy" if side == "long" else "sell"
        oid, price, qty = "", 0.0, 0.0
        for attempt in range(LIMIT_REQUOTE_MAX + 1):
            ref = bid if side == "long" else ask
            price = ex.price_to_precision(symbol, ref * (1 - sign * cfg.futures_limit_offset_pct / 100))
            qty = ex.amount_to_precision(symbol, margin * lev / price)
            ex.check_order_size(symbol, qty, price)              # MinNotionalError → işlem pas geçilir
            try:
                oid = ex.limit_order(symbol, order_side, qty, price, post_only=True)
                break
            except FuturesError as exc:
                if exc.code == "-2019":                          # Komisyon payı yetmedi: biraz küçült
                    margin *= MARGIN_RETRY_FACTOR
                elif exc.code != "-5022" or attempt == LIMIT_REQUOTE_MAX:
                    raise
                bid, ask = ex.bid_ask(symbol)                    # -5022: fiyat limiti geçti, güncel fiyatla dene
        if not oid:
            raise FuturesError(f"{symbol} limit giriş emri dizilemedi")
        signal_ms = (int(now * 1000) // FUT_BAR_MS) * FUT_BAR_MS
        self.state.pending_entry = {
            "symbol": symbol, "side": side, "order_id": oid, "price": price, "qty": qty, "lev": lev,
            "placed_at": now, "expires_at": signal_ms / 1000 + cfg.futures_limit_ttl_min * 60,
            "signal_ms": signal_ms, "candle_ms": sig.candle_ms, "signal_price": sig.price, "score": sig.score,
            "metrics": dict(sig.metrics), "priority": sig.priority}
        self.store.save(self.state)
        log.info("Limit giriş emri: %s %s %g @ %.8g (bid %.8g / ask %.8g, %d dk)", symbol, side, qty, price, bid, ask,
                 cfg.futures_limit_ttl_min)

    def _on_shutdown(self) -> None:
        """Bot dururken bekleyen limit giriş emri iptal edilir (bot yokken dolup korumasız pozisyon açmasın); kısmen
        dolduysa dolan kadarı zarar kes + Kâr Al ile pozisyon olur. Test modunda kayıt kalır (açılışta bakılır)."""
        pe, ex = self.state.pending_entry, self.exchange
        if not pe or ex.is_paper:
            return
        log.info("Bot duruyor: bekleyen limit giriş emri iptal ediliyor (%s)", pe["symbol"])
        try:
            ex.cancel(pe["symbol"], str(pe["order_id"]))
            _status, filled, avg, fee = ex.order_state(pe["symbol"], str(pe["order_id"]))
        except (FuturesError, requests.ConnectionError) as exc:
            log.warning("%s limit emir iptal edilemedi (açılışta yeniden bakılacak): %s", pe["symbol"], exc)
            return
        if filled > 0:
            self._finish_pending(pe, FFill(avg or float(pe["price"]), filled, fee, str(pe["order_id"])), self.time_fn())
        else:
            self._drop_pending(pe, "bot kapanırken iptal edildi")

    def _pending_signal(self, pe: dict) -> FuturesSignal:
        return FuturesSignal(pe["symbol"], pe["side"], float(pe.get("signal_price", pe["price"])),
                             int(pe.get("candle_ms", 0)), float(pe.get("score", 0.0)), dict(pe.get("metrics") or {}),
                             int(pe.get("priority", 0)))

    def _check_pending_entry(self, now: float) -> None:
        """Bekleyen limit giriş emri: dolduysa pozisyon açılır (zarar kes + Kâr Al konur); süresi dolduysa iptal edilir
        (kısmen dolduysa dolan kadarı pozisyon olur). Test modunda dolumu fiyat belirler (backtestle aynı kural)."""
        pe, ex = self.state.pending_entry, self.exchange
        if not pe:
            return
        symbol = pe["symbol"]
        if ex.is_paper:
            fill_ms = self._paper_limit_hit(pe, now)
            if fill_ms is not None:
                fill = ex.paper_limit_fill(symbol, float(pe["qty"]), float(pe["price"]), str(pe["order_id"]))
                self._finish_pending(pe, fill, now, fill_ms / 1000)
            elif now >= float(pe["expires_at"]) + 60 + LIMIT_CANCEL_GRACE_SEC:    # Son 1 dk mum da kapandı
                self._drop_pending(pe, "dolmadı")
            return
        try:
            status, filled, avg, fee = ex.order_state(symbol, str(pe["order_id"]))
        except FuturesError as exc:
            log.warning("%s limit emir durumu okunamadı: %s", symbol, exc)
            return
        if status == "closed" and filled > 0:
            self._finish_pending(pe, FFill(avg or float(pe["price"]), filled, fee, str(pe["order_id"])), now)
            return
        if status == "open" and now < float(pe["expires_at"]) + LIMIT_CANCEL_GRACE_SEC:
            return
        if status == "open":
            try:
                ex.cancel(symbol, str(pe["order_id"]))
                status, filled, avg, fee = ex.order_state(symbol, str(pe["order_id"]))
            except FuturesError as exc:
                log.warning("%s limit emir iptal edilemedi: %s", symbol, exc)
                return
        if filled > 0:                                   # Kısmen doldu: dolan kadarı pozisyon (korumasıyla)
            self._finish_pending(pe, FFill(avg or float(pe["price"]), filled, fee, str(pe["order_id"])), now)
        else:
            self._drop_pending(pe, "dolmadı" if status in ("open", "canceled", "expired", "") else status)

    def _paper_limit_hit(self, pe: dict, now: float) -> Optional[int]:
        """Test modu: limitin dolduğu an (ms) ya da None. Fiyat limiti PAPER_LIMIT_THROUGH_PCT geçmeli: süre içindeki
        kapanmış 1 dk mumların ucu ya da şu anki fiyat (backtest: futures_backtest.limit_fill)."""
        ex = self.exchange
        sign = 1 if pe["side"] == "long" else -1
        need = float(pe["price"]) * (1 - sign * PAPER_LIMIT_THROUGH_PCT / 100)
        start_ms = int(pe["signal_ms"])
        end_ms = start_ms + int(self.cfg.futures_limit_ttl_min) * 60_000
        now_ms = int(now * 1000)
        try:
            if now_ms < end_ms:
                last = ex.last_price(pe["symbol"])
                if last > 0 and (last < need if sign > 0 else last > need):
                    return now_ms
            minute = now_ms // 60_000                     # Kapanmış 1 dk mumlara dakikada bir bakılır
            if pe.get("k_minute") == minute:
                return None
            pe["k_minute"] = minute
            k = ex.klines(pe["symbol"], "1m", int(self.cfg.futures_limit_ttl_min) + 3)
        except FuturesError as exc:
            log.warning("%s test modu limit kontrolü: %s", pe["symbol"], exc)
            return None
        for t, lo, hi in zip(k["t"], k["l"], k["h"]):
            if start_ms <= int(t) < end_ms and (lo < need if sign > 0 else hi > need):
                return int(t)
        return None

    def _finish_pending(self, pe: dict, fill: FFill, now: float, opened_at: Optional[float] = None) -> None:
        self.state.pending_entry = {}
        self.store.save(self.state)
        log.info("Limit giriş emri doldu: %s %g @ %.8g", pe["symbol"], fill.qty, fill.price)
        try:
            self._open_from_fill(self._pending_signal(pe), fill, int(pe.get("lev", self.cfg.futures_leverage)), now,
                                 opened_at)
        except FuturesError as exc:
            self.notifier.send(Messages.error(f"{pe['symbol']} limit girişi sonrası pozisyon kurulamadı: "
                                              f"{describe_error(exc)}"), critical=True)

    def _drop_pending(self, pe: dict, why: str) -> None:
        self.state.pending_entry = {}
        self.store.save(self.state)
        log.info("Limit giriş emri %s: %s (%d dk)", why, pe["symbol"], self.cfg.futures_limit_ttl_min)
        self.notifier.notify(f"⏳ {esc(pe['symbol'])} {str(pe['side']).upper()} limit giriş emri "
                             f"{self.cfg.futures_limit_ttl_min} dk içinde {esc(why)}; iptal edildi (fırsat kaçtı).",
                             "open")

    # ================================================================ Faz 4 ek verileri
    def _attach_extras(self, klines: Dict[str, dict], closed_ms: int) -> None:
        """Fonlama modülü açıksa çöküş şartını sağlayan coinlere son ödenmiş fonlama oranları (k["fr"]), düzeltme
        modülü açıksa 1 saatlik mumlar (k["h1"], saat başına bir kez istenir) eklenir. Hata: o coinde modül sinyal
        vermez (log)."""
        mods = futures_modules(self.cfg)
        ex = self.exchange
        if "funding" in mods:
            rules = futures_funding_rules(self.cfg)
            for symbol, k in klines.items():
                c = k["c"]
                hit = any(len(c) > h * 12 and c[-1 - h * 12] > 0 and (1 - c[-1] / c[-1 - h * 12]) * 100 >= pct
                          for h, pct in rules)
                if not hit:
                    continue
                try:
                    ft, fr = ex.funding_history(symbol, FUNDING_LIMIT)
                except (FuturesError, requests.ConnectionError) as exc:
                    log.warning("%s fonlama oranları alınamadı: %s", symbol, exc)
                    continue
                k["fr"] = funding_per_bar(k["t"] + FUT_BAR_MS, ft, fr)
        if "pullback" in mods:
            hour = closed_ms // FUT_HOUR_MS
            todo = [s for s in klines if self._h1_cache.get(s, (None,))[0] != hour]

            def fetch(symbol: str):
                try:
                    return symbol, ex.klines(symbol, "1h", FUT_H1_LIMIT), None
                except FuturesError as exc:
                    return symbol, None, exc
            if len(todo) > KLINE_PARALLEL_MIN:
                with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as pool:
                    results = list(pool.map(fetch, todo))
            else:
                results = [fetch(s) for s in todo]
            for symbol, k1, exc in results:
                if exc is not None:
                    log.warning("%s 1 saatlik mumlar alınamadı: %s", symbol, exc)
                    continue
                self._h1_cache[symbol] = (hour, k1)
            for symbol, k in klines.items():
                hit = self._h1_cache.get(symbol)
                if hit is not None:
                    k["h1"] = hit[1]

    # ================================================================ değerleme
    def mark_position(self, pos: Position, now: float, price: Optional[float] = None) -> Mark:
        if not pos.futures:
            return super().mark_position(pos, now)
        if price is None:
            price = self.exchange.last_price(pos.symbol)
        gross = _sign(pos) * pos.qty * (price - pos.entry_price)
        exit_fee = pos.qty * price * self.cfg.futures_taker_fee
        pnl = gross - pos.buy_fee_usdt - exit_fee
        net_pct = pnl / pos.cost_usdt * 100 if pos.cost_usdt else 0.0
        roe = gross / pos.margin_usdt * 100 if pos.margin_usdt else 0.0
        mark = FMark(price, pos.cost_usdt + pnl, pnl, net_pct, now, roe)
        self.marks[pos.symbol] = mark
        pos.peak_pnl_pct = max(pos.peak_pnl_pct, net_pct)
        pos.min_pnl_pct = min(pos.min_pnl_pct, net_pct)
        return mark

    def levels(self, pos: Position) -> dict:
        if not pos.futures:
            return super().levels(pos)
        trig = pos.entry_price * (1 + _sign(pos) * self._be_trigger_pct(pos) / 100)
        return {"tp": pos.tp_price, "sl": pos.sl_price, "trail_on": trig, "liq": pos.liq_price}

    def _be_trigger_pct(self, pos: Position) -> float:
        """Pozisyonun başabaş tetiği (fiyat %): Faz 8'de ek pozisyon olarak açıldıysa girişte kaydedilen değer
        (pos.metrics["be_trigger"]), yoksa güncel BREAKEVEN_TRIGGER_PCT."""
        try:
            own = float(pos.metrics.get("be_trigger", 0.0))
        except (TypeError, ValueError):
            own = 0.0
        return own if own > 0 else self.cfg.breakeven_trigger_pct

    # ================================================================ pozisyon yönetimi
    def manage_positions(self, now: float) -> None:
        ex = self.exchange
        for pos in list(self.state.positions.values()):
            if not pos.futures:
                continue
            if not ex.is_paper and ex.position(pos.symbol) is None:
                self._closed_on_exchange(pos)                      # Stop / hedef / likidasyon / elle kapatma
                continue
            price = ex.last_price(pos.symbol)
            if ex.is_paper:
                hit = ex.triggered(pos.symbol, price)
                if hit is not None:
                    self._paper_fill(pos, hit[1], price)
                    continue
            mark = self.mark_position(pos, now, price)
            reason = self._exit_reason(pos, price, now)
            if reason is not None:
                log.info("%s çıkış: %s (fiyat %.8g, ROE %.2f%%)", pos.symbol, reason.value, price, mark.roe)
                self.close_position(pos, reason)
                continue
            self._check_breakeven(pos, price, mark, now)
            if pos.symbol in self.state.positions:
                self._check_trailing(pos, price, mark, now)
        if now - self._last_position_log >= POSITION_LOG_SEC:
            self._last_position_log = now
            for pos in self.state.positions.values():
                m = self.marks.get(pos.symbol)
                if m:
                    log.info("Pozisyon %s %s | fiyat %.8g | ROE %.2f%% | net %s USDT | %s", pos.symbol,
                             futures_tag(pos), m.bid, getattr(m, "roe", 0.0), fmt_signed(m.pnl_usdt, 4),
                             fmt_duration(now - pos.opened_at))
        self._check_kill_switch()

    def _hold_minutes(self, pos: Position) -> int:
        """Pozisyonun zaman stopu (dk): Faz 9'da ek pozisyon / dalgada açılan ilk pozisyon için girişte kaydedilen değer
        (pos.metrics["hold_min"]), yoksa güncel MAX_HOLD_MINUTES."""
        try:
            own = int(float(pos.metrics.get("hold_min", 0)))
        except (TypeError, ValueError):
            own = 0
        return own if own > 0 else int(self.cfg.max_hold_minutes)

    def _exit_reason(self, pos: Position, price: float, now: float) -> Optional[ExitReason]:
        hold = self._hold_minutes(pos)
        if hold > 0 and now - pos.opened_at >= hold * 60:
            return ExitReason.MAX_HOLD                           # Zaman stopu: piyasa emriyle kapat
        stop_reason = _stop_reason(pos)
        if _worse(pos, price, pos.sl_price):
            # Binance'teki stop normalde kendisi çalışır; emir yoksa ya da fiyat stopu belirgin geçtiyse bot kapatır
            beyond = abs(price / pos.sl_price - 1) * 100 if pos.sl_price else 0.0
            if self.exchange.is_paper or not pos.stop_order_id or beyond >= SOFT_STOP_GRACE_PCT:
                return stop_reason
        if not pos.tp_order_id and _reached(pos, price, pos.tp_price):
            return ExitReason.TAKE_PROFIT
        return None

    def _check_breakeven(self, pos: Position, price: float, mark: Mark, now: float) -> None:
        """Erken başabaş kalkanı: fiyat +BREAKEVEN_TRIGGER_PCT'ye (Faz 8: ek pozisyonda FUTURES_EXTRA_BE_TRIGGER_PCT'ye)
        gelince stop girişin +BREAKEVEN_FLOOR_PCT'sine."""
        cfg, ex = self.cfg, self.exchange
        if not cfg.breakeven_enabled or pos.breakeven_active or now < self._be_retry_at.get(pos.symbol, 0.0):
            return
        sign = _sign(pos)
        if not _reached(pos, price, pos.entry_price * (1 + sign * self._be_trigger_pct(pos) / 100)):
            return
        new_stop = ex.price_to_precision(pos.symbol, pos.entry_price * (1 + sign * cfg.breakeven_floor_pct / 100))
        if _worse(pos, price, new_stop):
            self.close_position(pos, ExitReason.BREAKEVEN)
            return
        if not _worse(pos, pos.sl_price, new_stop):      # İzleyen stop zaten daha iyi bir yerde
            pos.breakeven_active = True
            self.store.save(self.state)
            return
        if not self._move_stop(pos, new_stop, now, "başabaş"):
            if pos.symbol in self._stop_passed:
                # Stop konurken fiyat yeni stopun gerisine düşmüş (tetik ile taban arası dar olduğunda, ör. Faz 8'de
                # %0.2): backtestteki gibi beklemeden çıkılır (eski zarar kes geri kondu, kapanış olmazsa o korur)
                self._stop_passed.discard(pos.symbol)
                if pos.symbol in self.state.positions:
                    log.info("%s başabaş: fiyat yeni stopu geçmiş, piyasa emriyle kapatılıyor", pos.symbol)
                    self.close_position(pos, ExitReason.BREAKEVEN)
            return
        pos.breakeven_active = True
        self.store.save(self.state)
        self.notifier.notify(Messages.futures_breakeven(pos, price, new_stop, getattr(mark, "roe", 0.0)), "update")

    def _check_trailing(self, pos: Position, price: float, mark: Mark, now: float) -> None:
        """İzleyen stop: en iyi fiyat girişten +FUTURES_TRAILING_ACT_PCT gidince zarar kes, o tepenin
        FUTURES_TRAILING_GAP_PCT gerisine çekilir ve yalnızca lehte ilerler. Bot her kontrolde tepeyi izler ve
        seviye geçilirse hemen kapatır; Binance'teki stop emri de (en sık TRAIL_MOVE_SEC'de bir) taşınır."""
        cfg, ex = self.cfg, self.exchange
        if pos.peak_price <= 0 or _reached(pos, price, pos.peak_price):
            pos.peak_price = price
        if not cfg.futures_trailing:
            return
        sign = _sign(pos)
        if not _reached(pos, pos.peak_price, pos.entry_price * (1 + sign * cfg.futures_trailing_act_pct / 100)):
            return
        level = ex.price_to_precision(pos.symbol, pos.peak_price * (1 - sign * cfg.futures_trailing_gap_pct / 100))
        if not _worse(pos, pos.sl_price, level) or level == pos.sl_price:
            return                                       # Mevcut stop (başabaş / önceki izleyen) daha iyi
        if _worse(pos, price, level):                    # Tepeden mesafe kadar geri döndü
            log.info("%s izleyen stop: fiyat %.8g, seviye %.8g (tepe %.8g)", pos.symbol, price, level, pos.peak_price)
            self.close_position(pos, ExitReason.TRAILING_STOP)
            return
        first = not pos.trailing_active
        step = abs(level / pos.sl_price - 1) * 100 if pos.sl_price else 100.0
        if not first and (step < TRAIL_MIN_STEP_PCT or now < self._trail_at.get(pos.symbol, 0.0)):
            return
        self._trail_at[pos.symbol] = now + TRAIL_MOVE_SEC
        if not self._move_stop(pos, level, now, "izleyen stop"):
            return
        pos.trailing_active = True
        self.store.save(self.state)
        if first:
            self.notifier.notify(Messages.futures_trailing(pos, price, level, getattr(mark, "roe", 0.0), cfg),
                                 "update")

    def _move_stop(self, pos: Position, new_stop: float, now: float, what: str) -> bool:
        """Binance'teki STOP_MARKET emrini yeni seviyeye taşır (iptal + yeni emir). Olmazsa eski stopu geri koyar ve
        False döner (bir süre sonra tekrar denenir); eski de konamazsa pozisyon güvenlik için kapatılır."""
        ex = self.exchange
        old_id, old_stop = pos.stop_order_id, pos.sl_price
        self._stop_passed.discard(pos.symbol)
        try:
            if old_id:
                ex.cancel(pos.symbol, old_id, conditional=True)
            pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, new_stop)
        except FuturesError as exc:
            self._be_retry_at[pos.symbol] = now + BE_RETRY_SEC
            self._trail_at[pos.symbol] = now + BE_RETRY_SEC
            log.warning("%s %s Binance'e taşınamadı: %s", pos.symbol, what, exc)
            try:                                                    # Eski stopu geri koy: korumasız kalmasın
                pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, old_stop)
            except FuturesError as exc2:
                self.notifier.send(Messages.error(f"{pos.symbol} stop emri yeniden konamadı ({exc2}); pozisyon "
                                                  f"güvenlik için kapatılıyor."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                return False
            self.store.save(self.state)
            if exc.code == STOP_WOULD_TRIGGER:           # Fiyat yeni stopu çoktan geçmiş: çağıran karar verir
                self._stop_passed.add(pos.symbol)
                return False
            self.notifier.send_error(f"stop-{pos.symbol}", Messages.warning(
                f"{pos.symbol} {what} Binance'e taşınamadı ({exc}); eski zarar kes duruyor, tekrar denenecek."),
                cooldown_sec=300)
            return False
        pos.sl_price = new_stop
        return True

    # ================================================================ kapanış
    def close_position(self, pos: Position, reason: ExitReason) -> bool:
        """Piyasa emriyle (reduceOnly) kapatır, kalan stop / hedef emirlerini iptal eder."""
        if not pos.futures:
            return super().close_position(pos, reason)
        ex = self.exchange
        close_side = "sell" if pos.side == "long" else "buy"
        try:
            fill = ex.market_order(pos.symbol, close_side, pos.qty, reduce_only=True)
        except FuturesError as exc:
            if not ex.is_paper:
                try:
                    gone = ex.position(pos.symbol) is None
                except Exception:  # noqa: BLE001
                    gone = False
                if gone:                                        # Bu arada stop / hedef çalışmış
                    self._closed_on_exchange(pos)
                    return True
            self.notifier.send_error(f"close-{pos.symbol}", Messages.error(
                f"{pos.symbol} kapatılamadı: {describe_error(exc)} — bir sonraki kontrolde tekrar denenecek "
                f"(Binance'teki zarar kes emri bekliyor)."), cooldown_sec=120)
            return False
        funding = 0.0
        if not ex.is_paper:
            ex.cancel_all(pos.symbol)
            funding = ex.funding_paid(pos.symbol, int(pos.opened_at * 1000))
        self._finalize_futures(pos, fill.price, fill.fee, reason, funding)
        return True

    def _paper_fill(self, pos: Position, order: dict, price: float) -> None:
        """Test modu: fiyat sanal stop / hedef emrine ulaştı."""
        cfg = self.cfg
        slip = cfg.futures_slippage_pct / 100
        if order["kind"] == "tp":
            limit = order.get("limit", True)
            exit_price = order["price"] if limit else price * (1 - _sign(pos) * slip)
            fee = pos.qty * exit_price * (cfg.futures_maker_fee if limit else cfg.futures_taker_fee)
            reason = ExitReason.TAKE_PROFIT
        else:
            exit_price = price * (1 - _sign(pos) * slip)              # STOP_MARKET: tetiklenince piyasa fiyatından
            fee = pos.qty * exit_price * cfg.futures_taker_fee
            reason = _stop_reason(pos)
        self._finalize_futures(pos, exit_price, fee, reason)

    def _closed_on_exchange(self, pos: Position) -> None:
        """Pozisyon Binance'te kapanmış (stop / hedef / likidasyon / elle): kapanışı bul ve kaydet."""
        ex = self.exchange
        since = int(pos.opened_at * 1000)
        fill = ex.closing_fill(pos.symbol, since, pos.side)
        stop_reason = _stop_reason(pos)
        if pos.tp_order_id and ex.order_filled(pos.symbol, pos.tp_order_id) is not None:
            reason = ExitReason.TAKE_PROFIT
        elif pos.stop_order_id and ex.order_filled(pos.symbol, pos.stop_order_id, conditional=True) is not None:
            reason = stop_reason
        elif fill is not None and pos.liq_price and not _worse(pos, pos.liq_price, fill.price):
            reason = ExitReason.LIQUIDATION                     # Likidasyon fiyatında / ötesinde kapanmış
        elif fill is not None and pos.sl_price and (_worse(pos, fill.price, pos.sl_price)
                                                    or abs(fill.price / pos.sl_price - 1) * 100 <= 0.3):
            reason = stop_reason                                # Stop fiyatında / ötesinde kapanmış
        elif fill is not None and pos.tp_price and _reached(pos, fill.price, pos.tp_price * (1 - _sign(pos) * 0.001)):
            reason = ExitReason.TAKE_PROFIT
        else:
            reason = ExitReason.EXTERNAL
        estimated = fill is None
        if fill is None:
            price = self.marks[pos.symbol].bid if pos.symbol in self.marks else pos.sl_price or pos.entry_price
            fee = pos.qty * price * self.cfg.futures_taker_fee
        else:
            price, fee = fill.price, fill.fee
        ex.cancel_all(pos.symbol)
        funding = ex.funding_paid(pos.symbol, since)
        if reason == ExitReason.LIQUIDATION:
            self.notifier.send(Messages.error(f"{pos.symbol} {futures_tag(pos)} LİKİDE EDİLDİ: yalıtılmış teminat "
                                              f"({fmt_usdt(pos.margin_usdt)} USDT) kaybedildi."), critical=True)
        self._finalize_futures(pos, price, fee, reason, funding, estimated)

    def _finalize_futures(self, pos: Position, exit_price: float, exit_fee: float, reason: ExitReason,
                          funding: float = 0.0, estimated: bool = False) -> None:
        now = self.time_fn()
        gross = _sign(pos) * pos.qty * (exit_price - pos.entry_price)
        pnl = gross - pos.buy_fee_usdt - exit_fee - funding
        if reason == ExitReason.LIQUIDATION:
            pnl = min(pnl, -pos.margin_usdt - pos.buy_fee_usdt)
        proceeds = pos.cost_usdt + pnl
        if self.exchange.is_paper:
            self.exchange.close_paper(pos.symbol, exit_price, pos.qty, exit_fee)
        state = self.state
        state.realized_pnl += pnl
        state.trades += 1
        if pnl >= 0:
            state.wins += 1
        else:
            state.losses += 1
        state.total_fees += exit_fee
        state.positions.pop(pos.symbol, None)
        prev_until = state.cooldowns.get(pos.symbol, 0.0)
        state.cooldowns[pos.symbol] = now + cooldown_minutes(self.cfg, pnl) * 60
        self._mark_reentry(pos.symbol, now, pnl, prev_until)
        self.marks.pop(pos.symbol, None)
        self._be_retry_at.pop(pos.symbol, None)
        self._trail_at.pop(pos.symbol, None)
        self.store.save(state)
        try:
            logged = self.trade_log.append(pos, exit_price, proceeds, pos.buy_fee_usdt + exit_fee, reason.value, now)
        except Exception as exc:  # noqa: BLE001 - kayıt hatası bildirimi / kill-switch'i engellemesin
            log.error("İşlem kaydı yazılamadı: %s", exc)
            logged = False
        if not logged:
            self.notifier.send_error("tradelog-locked", Messages.warning(
                f"İşlem kaydı dosyası ({self.trade_log.path.name}) yazılamadı — Excel'de açık olabilir."),
                cooldown_sec=3600)
        roe = pnl / pos.margin_usdt * 100 if pos.margin_usdt else 0.0
        log.info("Vadeli pozisyon kapandı: %s %s | %s | çıkış %.8g | net %.4f USDT (ROE %.2f%%)", pos.symbol,
                 futures_tag(pos), reason.value, exit_price, pnl, roe)
        self.notifier.notify(Messages.futures_close(
            reason.value, pos, exit_price, exit_fee, funding, pnl, roe, now - pos.opened_at, state.capital,
            state.realized_pnl, state.trades, state.wins, state.losses, self.cfg, estimated),
            "close", critical=reason.value in CRITICAL_EXITS)
        self._after_close(pos.symbol, pnl, reason, now)

    # ================================================================ mutabakat
    def _reconcile_positions_on_startup(self) -> None:
        """Kayıtlı pozisyonları Binance ile karşılaştırır; bot kapalıyken kapanmışsa kaydeder, açıksa korumayı
        (stop + hedef) yeniden kurar. Botun bilmediği vadeli pozisyonlar için uyarır (dokunmaz)."""
        ex = self.exchange
        for pos in list(self.state.positions.values()):
            if not pos.futures:
                self.notifier.send(Messages.warning(f"{pos.symbol} spot pozisyonu vadeli modda yönetilmez."))
                continue
            if ex.is_paper:
                ex.open_paper(pos.symbol, pos.side, pos.qty, pos.entry_price, pos.margin_usdt, pos.leverage)
            elif ex.position(pos.symbol) is None:
                self._closed_on_exchange(pos)
                continue
            else:
                ex.cancel_all(pos.symbol)
            try:
                pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, pos.sl_price)
            except FuturesError as exc:
                self.notifier.send(Messages.error(f"{pos.symbol} zarar kes emri yeniden konamadı ({exc}); pozisyon "
                                                  f"güvenlik için kapatılıyor."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                continue
            try:
                pos.tp_order_id = ex.place_take_profit(pos.symbol, pos.side, pos.qty, pos.tp_price,
                                                       limit=self.cfg.futures_tp_order == "limit")
            except FuturesError as exc:
                pos.tp_order_id = ""
                log.warning("%s kâr al emri yeniden konamadı: %s", pos.symbol, exc)
            self.notifier.send(f"♻️ {esc(pos.symbol)} {futures_tag(pos)} pozisyonu takibe devam ediliyor "
                               f"(giriş {fmt_price(pos.entry_price)}, stop {fmt_price(pos.sl_price)}).")
        if not ex.is_paper:
            try:
                pending = (self.state.pending_entry or {}).get("symbol")
                others = [p for p in ex.all_positions() if p.symbol not in self.state.positions and p.symbol != pending]
            except FuturesError as exc:
                log.warning("Vadeli pozisyonlar okunamadı: %s", exc)
                others = []
            if others:
                self.notifier.send(Messages.warning(
                    "Binance vadeli hesabında botun açmadığı pozisyon(lar) var; bot bunlara dokunmaz ama serbest "
                    "bakiye azalır: " + ", ".join(f"{p.symbol} {p.side.upper()} {p.qty:g}" for p in others)))
        self.store.save(self.state)

    def unreconciled_reason(self) -> Optional[str]:
        return None

    def unallocated_balance(self) -> Optional[float]:
        if self.exchange.is_paper:
            return None
        free = self.exchange.free_balance()
        return free - max(0.0, self.state.capital - self.state.committed)

    # ================================================================ bilgi
    def scan_report(self) -> str:
        r = self.last_scan
        if r is None:
            return "🔍 Henüz vadeli tarama yapılmadı (5 dk mum kapanışlarında, pozisyon yokken taranır)."
        age = self.time_fn() - self.last_scan_at
        lines = [f"🔍 <b>Son Vadeli Tarama</b> ({age:.0f} sn önce)",
                 f"Coin: {r.candidates} | Analiz: {r.analyzed} | Sinyal: {len(r.signals)}"]
        if self.account_block:
            lines.append(f"⛔ {esc(self.account_block[:600])}")
        if futures_scan_shifts(self.cfg):
            lines.append("⏱️ Her dakika tarama açık: 5 dk kapanışlarının yanında aradaki dakikalarda da taranır "
                         "(1 dk mumlarla, aynı kural)")
        elif getattr(self.cfg, "futures_minute_scan", False):
            lines.append("⏱️ Her dakika tarama açık ama bu ayarlarla kullanılamıyor (5 dk'da bir taranıyor)")
        names = [s[:-4] for s in (self._universe_symbols() if self.cfg.futures_universe > 0
                                  else [c + "USDT" for c in futures_coin_list(self.cfg)])]
        lines.append(f"🪙 Evren: {esc(self.universe_label())}: {esc(', '.join(names[:30]))}"
                     + (" …" if len(names) > 30 else ""))
        if self._ranking_error and (not self.universe or self._ranking_source == "24h"):
            lines.append(f"⚠️ En hacimli coin sıralaması alınamadı: {esc(self._ranking_error[:150])}")
        if self.state.positions and self.slots() > 1:
            lines.append(f"📦 {len(self.state.positions)}/{self.slots()} pozisyon açık: ek pozisyon yalnızca son 1 saatte "
                         f"≥{self.cfg.futures_extra_breadth} coin %3+ düşerken açılır.")
        elif self.state.positions:
            lines.append("📦 Açık pozisyon var: yeni tarama pozisyon kapanınca yapılır.")
        if self._cascade_on() and self.last_breadth is not None:
            cfg = self.cfg
            extra = ""
            if cfg.futures_reentry_breadth > 0:
                extra += f", dalgada yeniden giriş ≥{cfg.futures_reentry_breadth}"
            if futures_wave_rules(cfg):
                extra += f", dalga eşikleri ≥{cfg.futures_wave_breadth}"
            if (cfg.futures_max_positions > 1 and cfg.breakeven_enabled
                    and futures_be_trigger(cfg, 1) != cfg.breakeven_trigger_pct):
                extra += f", ek pozisyon başabaşı +%{cfg.futures_extra_be_trigger_pct:g}"
            if futures_hold_minutes(cfg, 1, None) != cfg.max_hold_minutes:
                extra += f", ek pozisyon zaman stopu {cfg.futures_extra_hold_minutes} dk"
            if cfg.futures_wave_hold_minutes > 0:
                extra += (f", ≥{cfg.futures_wave_hold_breadth} coin düşerken ilk pozisyon "
                          f"{cfg.futures_wave_hold_minutes} dk")
            lines.append(f"🌊 Genişlik: {self.last_breadth} coin son 1 saatte %3+ düştü (dalga ≥"
                         f"{cfg.futures_extra_breadth}, yalnız çöküş <{cfg.futures_isolated_breadth}{extra})")
        tm = self.last_timing
        if tm and len(tm.get("ms") or []) >= 3:
            st, kl, sg = (v / 1000 for v in tm["ms"][:3])
            lines.append(f"⏱️ Zamanlama (mum kapanışından sonra): tarama {st:.1f} sn'de başladı, mumlar {kl:.1f} sn'de "
                         f"alındı" + (f" ({tm['kt']} denemede)" if tm.get("kt") else "")
                         + f", sinyaller {sg:.1f} sn'de hazırdı | hızlı giriş {'açık' if self._fast() else 'kapalı'}")
        pe = self.state.pending_entry
        if pe:
            left = max(0.0, float(pe.get("expires_at", 0.0)) - self.time_fn())
            lines.append(f"⏳ Limit giriş emri bekliyor: {esc(str(pe.get('symbol')))} {str(pe.get('side')).upper()} @ "
                         f"{fmt_price(float(pe.get('price', 0.0)))} (en fazla {left:.0f} sn daha)")
        if self.state.paused:
            lines.append("⏸️ Yeni işlemler durdurulmuş (/devam).")
        for s in self.last_signals[:3]:
            lines.append(f"• {esc(s.symbol)} {s.side.upper()}: {esc(s.note())}")
        if r.rejects:
            lines.append("Elenme nedenleri: " + ", ".join(f"{esc(k)}: {v}" for k, v in r.rejects.most_common(5)))
        d = self.day_stats
        if d.scans:
            lines.append(f"\n📅 Bugün: {d.scans} tarama, {d.signals} sinyal, {d.buys} işlem")
        return "\n".join(lines)
