"""
Ana işlem motoru (TradingBot).

Döngü:
  - Boş pozisyon yeri (slot) varsa: her SCAN_INTERVAL_SEC (30 sn) piyasayı tarar, sinyal varsa alır.
  - Açık pozisyon(lar) varsa fiyatları sık sık kontrol eder (anlık fiyat akışı varsa ~1 sn, yoksa 5 sn);
    kâr al / zarar kes / izleyen stop / başabaş / kill-switch koşullarına "anında" tepki verir.
  - Her alımdan sonra Binance'e bir YEDEK zarar-kes emri bırakılır: bilgisayar kapansa bile
    pozisyon borsa tarafında korunur. Bot satış yapmadan önce bu emri iptal eder.
  - Telegram komutlarını işler, günlük/haftalık rapor gönderir, kesintileri bildirir.

Aynı anda kaç coin tutulacağı (MAX_OPEN_POSITIONS) bakiyeye göre otomatik ayarlanır:
her pozisyon, fiyat stop-loss'a düşse bile Binance'in minimum emir tutarının (~5 USDT)
üstünde kalmalıdır. 11 USDT → 1 pozisyon, ~12 USDT → 2, ~17.5 USDT ve üstü → 3.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import requests

from binance_client import BinanceAPIError, RateLimitError
from config import divergence_trading, is_rotation, parse_hhmm
from control_panel import READ_ONLY_CALLBACKS
from entry_filters import live_reject
from exchange import BaseExchange, FeeInfo, Fill, OrderError, combine_fills
from logger import get_logger
from risk import (ExitReason, Position, cooldown_minutes, evaluate_exit, kill_switch_hit, net_exit_value, plan_entry,
                  pnl_pct, position_sl, position_slots, position_tp, price_for_pnl, trailing_floor_pct,
                  update_breakeven, update_trailing)
from state import BotState, EquityLog, StateStore, TradeLog
from strategy import ReboundStrategy, RsiDivergenceStrategy, ScanResult, Signal
from telegram_bot import (CRITICAL_EXITS, Messages, TelegramNotifier, esc, fmt_duration, fmt_qty, fmt_rate,
                          fmt_signed, fmt_usdt)

log = get_logger()

RULES_REFRESH_SEC = 3600
POSITION_LOG_SEC = 60
BACKUP_CHECK_SEC = 60        # Yedek stop emrinin durumu bu aralıkla sorgulanır
PENDING_CHECK_SEC = 15       # Sonucu belirsiz emirler bu aralıkla sorgulanır
PENDING_GIVE_UP_SEC = 180    # Bu süre sonunda Binance'te bulunamayan emir hiç gitmemiş sayılır
ALIVE_SAVE_SEC = 60
DELIST_WARN_SEC = 3600
STREAM_MAX_AGE = 3.0         # Anlık fiyat bu kadar saniyeden eskiyse REST'e dön
WEEKLY_DEFAULT_TIME = "23:55"  # Günlük rapor kapalıyken haftalık raporun saati
KILL_RETRY_SEC = 5           # Kill-switch sonrası satılamayan pozisyonlar için tekrar deneme aralığı
FEE_CHECK_SEC = 3600         # Komisyon / BNB bakiyesi kontrol aralığı
FEE_WARN_COOLDOWN_SEC = 6 * 3600  # Yanlış FEE_RATE uyarısı en fazla bu aralıkla tekrarlanır
BNB_LOW_NOTICE_SEC = 24 * 3600   # "BNB azalıyor" bilgisi en fazla günde bir
UNSELLABLE_CODES = {"MIN_NOTIONAL", "LOT_SIZE", "NO_BALANCE"}
ROTATION_DELAY_SEC = 300     # Rotasyon kararları günlük mum kapandıktan (UTC 00:00) 5 dk sonra verilir
SCAN_TIME_BUDGET_SEC = 45     # Bir tarama en fazla bu kadar sürer (Binance yavaşsa kalan adaylar sonraki taramaya)
DIV_SCAN_DELAY_SEC = 60      # RSI_DIV_TRADE: saatlik mum kapandıktan bu kadar sonra taranır (Binance'te yerleşsin)
DIV_RETRY_SEC = 300          # RSI_DIV_TRADE taraması hata verirse aynı saat içinde bu kadar sonra tekrar denenir
SLOW_TICK_SEC = 20           # Döngü bundan uzun sürerse adım adım süreler loga yazılır
# Tarama sürerken de hemen cevaplanan, yalnızca bilgi gösteren komutlar (diğerleri tarama bitince çalışır)
READ_ONLY_COMMANDS = frozenset({"durum", "pozisyon", "bakiye", "islemler", "csv", "istatistik", "rapor", "tarama",
                                "ayarlar", "ping", "yardim", "help", "start", "menu", "panel", "piyasa", "gecikme",
                                "loglar", "log", "ip", "pano", "toz", "backtest", "bt"})
# Risk sigortaları: zarar serisine sayılan çıkışlar; seriyi ne bozan ne artıran (elle / bot dışı) çıkışlar
STOP_EXITS = frozenset({ExitReason.STOP_LOSS, ExitReason.BACKUP_STOP})
NEUTRAL_EXITS = frozenset({ExitReason.MANUAL, ExitReason.KILL_SWITCH, ExitReason.EXTERNAL})
SLIPPAGE_WINDOW = 10           # Kayma alarmı: son bu kadar alımın ortalaması
SLIPPAGE_MIN_SAMPLES = 5
SLIPPAGE_ALERT_COOLDOWN_SEC = 6 * 3600
ROTATION_SELL_RETRY_SEC = 30  # Satılamayan rotasyon pozisyonu bu aralıkla tekrar denenir  # Kalanın satılamayacak kadar az olduğu hatalar


@dataclass
class Mark:
    """Açık pozisyonun son değerlemesi."""
    bid: float
    net_value: float
    pnl_usdt: float
    pnl_pct: float
    at: float


@dataclass
class DayScanStats:
    day: str = ""
    scans: int = 0
    signals: int = 0
    buys: int = 0
    rejects: Counter = field(default_factory=Counter)


class TradingBot:
    def __init__(self, cfg, exchange: BaseExchange, strategy: ReboundStrategy, notifier: TelegramNotifier,
                 store: StateStore, state: BotState, trade_log: TradeLog, equity_log: Optional[EquityLog] = None,
                 price_stream=None, command_listener=None, time_fn=time.time, settings_path=None, ip_monitor=None):
        self.cfg = cfg
        self.settings_path = settings_path   # Telegram menüsünden değişen ayarların yazıldığı .env (None = yazılmaz)
        self.ip_monitor = ip_monitor
        self.exchange = exchange
        self.strategy = strategy
        self.notifier = notifier
        self.store = store
        self.state = state
        self.trade_log = trade_log
        self.equity_log = equity_log
        self.price_stream = price_stream
        self.command_listener = command_listener
        self.time_fn = time_fn
        notifier.mode = cfg.notify_mode
        from commands import CommandProcessor  # döngüsel içe aktarmayı önlemek için
        from backtest_runner import BacktestRunner
        from market_scan import DivergenceWatcher
        from smart_manager import SmartManager
        self.commands = CommandProcessor(self, time_fn=time_fn)
        self.smart = SmartManager(self)        # Akıllı pozisyon yönetimi / giriş, canlı pano
        self.divergence = DivergenceWatcher(self.commands.panel._client_for_scan, time_fn=time_fn)
        self.backtests = BacktestRunner(self)  # Telegram'dan başlatılan backtest (ayrı süreç)
        self._deferred_commands: list = []   # Tarama sırasında gelen, tarama bitince çalışacak komutlar
        self._scan_deadline = 0.0

        now = time_fn()
        self.running = False
        self.exit_code = 0
        self.next_scan_at = 0.0
        self.next_status_at = (now + cfg.status_report_minutes * 60
                               if cfg.status_report_minutes > 0 else float("inf"))
        self.next_equity_at = now + cfg.equity_log_minutes * 60 if cfg.equity_log_minutes > 0 else float("inf")
        self.marks: Dict[str, Mark] = {}
        self.last_scan: Optional[ScanResult] = None
        self.last_scan_at = 0.0
        self.day_stats = DayScanStats()
        self.last_loop_at = 0.0
        self.net_down_since: Optional[float] = None
        self._last_position_log = 0.0
        self._last_alive_save = 0.0
        self._next_backup_check: Dict[str, float] = {}
        self._next_pending_check = 0.0
        self._next_btc_check = 0.0
        self._next_delist_warn = 0.0
        self._next_fee_check = now + FEE_CHECK_SEC
        self.fee_info: Optional[FeeInfo] = None  # Son okunan komisyon / BNB durumu (/ayarlar bunu gösterir)
        self._fee_noted_rate: Optional[float] = None  # "FEE_RATE temkinli" bilgisi bu oran için verildi
        self._bnb_empty_noted = False     # "BNB yetmiyor" bilgisi bu süreçte verildi
        self._bnb_fees_noted = False      # "BNB_FEES açık ama komisyon BNB'den kesilmiyor" uyarısı verildi
        self._next_bnb_low_notice = 0.0   # "BNB azalıyor" bilgisi en erken bu anda tekrar verilir
        self._announced = False  # Açılış mesajları ağ hatasıyla tekrarlanan denemelerde yinelenmesin
        self._next_kill_retry = 0.0
        self._next_kill_notice = 0.0
        self._error_streak = 0
        self._next_rotation_sell = 0.0
        self._div_hour = -1                        # RSI_DIV_TRADE: en son taranan saatlik mum (UTC saat sayısı)
        self._next_div_try = 0.0
        self.last_div_scan: Optional[ScanResult] = None   # RSI_DIV_TRADE'in son taraması (/tarama)
        self.last_div_scan_at = 0.0
        self.last_rotation: Optional[str] = None   # Son yeniden dengeleme özeti (/tarama)
        self.last_rotation_at = 0.0

    # ================================================================ yaşam döngüsü
    def run(self, do_startup: bool = True) -> int:
        if do_startup:
            self.startup()
        self.running = True
        try:
            while self.running:
                now = self.time_fn()
                self._check_loop_gap(now)
                self.last_loop_at = now
                try:
                    self.tick()
                    self._error_streak = 0
                    self._network_ok()
                except RateLimitError as exc:
                    log.warning("Binance istek limiti: %s sn bekleniyor", exc.retry_after)
                    self.notifier.send_error("ratelimit", Messages.warning(
                        f"Binance istek limiti aşıldı, {exc.retry_after} sn bekleniyor."))
                    self._wait_rate_limit(exc.retry_after)
                    continue
                except (requests.RequestException, BinanceAPIError) as exc:
                    if isinstance(exc, requests.RequestException):
                        self._network_down()
                    self._handle_error(exc, "network")
                    continue
                except Exception as exc:  # noqa: BLE001 - bot beklenmedik hatada çökmesin
                    log.exception("Beklenmedik hata")
                    self._handle_error(exc, f"unexpected-{type(exc).__name__}")
                    continue
                if self.running:
                    self._sleep(self._next_sleep())
        except KeyboardInterrupt:
            log.info("Ctrl+C alındı, bot durduruluyor...")
        finally:
            self.state.last_alive = self.time_fn()
            self.store.save(self.state)
            if self.command_listener is not None:
                self.command_listener.stop()
            if self.ip_monitor is not None:
                self.ip_monitor.stop()
            if self.smart.liq_feed is not None:
                self.smart.liq_feed.stop()
            self.backtests.shutdown()
            if self.price_stream is not None:
                self.price_stream.stop()
            if not self.state.killed or self.state.positions:
                protected = sum(1 for p in self.state.positions.values() if p.backup_order_id is not None)
                self.notifier.send(Messages.stopped(
                    self.state.capital, self.state.realized_pnl, self.cfg, list(self.state.positions), protected))
        return 2 if self.state.killed and not self.state.positions else self.exit_code

    def startup(self) -> None:
        cfg = self.cfg
        if not self.exchange.is_paper:
            offset = self.exchange.client.sync_time()
            log.info("Binance saat farkı: %d ms", offset)
            self.exchange.check_trading_enabled()
        pairs = self.exchange.refresh_rules()
        log.info("%d adet %s paritesi yüklendi", pairs, cfg.quote_asset)

        now = self.time_fn()
        if not self._announced:
            self._announce_startup(pairs, now)
            self._announced = True
        self._reconcile_positions_on_startup()
        self._check_commission()
        self._next_fee_check = now + FEE_CHECK_SEC

        self._init_report_markers(now)
        self.state.last_alive = now
        self.store.save(self.state)

        if self.price_stream is not None:
            self.price_stream.start()
            self._sync_stream_symbols()
        if self.command_listener is not None:
            self.command_listener.start()
        if self.ip_monitor is not None:
            self.ip_monitor.start()
        self._sync_liq_feed()

    def _sync_liq_feed(self) -> None:
        """LIQ_FEED açıksa likidasyon akışını başlatır, kapatıldıysa durdurur."""
        smart = self.smart
        if self.cfg.liq_feed and smart.liq_feed is None:
            from liquidations import LiquidationFeed
            feed = LiquidationFeed()
            if feed.available:
                smart.liq_feed = feed
                feed.start()
            else:
                log.warning("websocket-client kurulu değil; likidasyon akışı başlatılamadı")
        elif not self.cfg.liq_feed and smart.liq_feed is not None:
            smart.liq_feed.stop()
            smart.liq_feed = None

    def _announce_startup(self, pairs: int, now: float) -> None:
        cfg = self.cfg
        self.notifier.send(Messages.started(cfg, self.state.capital, self.state.realized_pnl, pairs, self.target_slots(),
                                            loss_limit=self.loss_limit, loss_pct=self.loss_pct))
        for note in cfg.notes:
            self.notifier.send(Messages.warning(note))
        if self.state.paused:
            self.notifier.send("⏸️ Yeni alımlar daha önce /durdur ile durdurulmuştu. Devam için /devam yazın.")
        if self.state.killed and self.state.positions:
            self.notifier.send(Messages.warning(
                "Kill-switch daha önce tetiklenmişti ama satılamamış pozisyon kaldı. Bot yalnızca bu pozisyonları "
                "satmak için çalışıyor; yeni alım yapılmayacak."), critical=True)

        if self.state.last_alive and now - self.state.last_alive > cfg.outage_alert_sec:
            self.notifier.send(Messages.outage((now - self.state.last_alive) / 60, list(self.state.positions),
                                               startup=True), critical=bool(self.state.positions))

    def stop(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.running = False

    def set_paused(self, paused: bool) -> None:
        self.state.paused = paused
        self.store.save(self.state)

    def apply_config(self, cfg) -> None:
        """Telegram menüsünden değişen ayarları çalışan bota uygular (strateji, filtreler, bildirim modu)."""
        self.cfg = cfg
        for part in (self.strategy, getattr(self.strategy, "coin_filter", None), getattr(self.strategy, "regime", None)):
            if part is not None and hasattr(part, "cfg"):
                part.cfg = cfg
        self.notifier.mode = cfg.notify_mode
        if self.running:
            self._sync_liq_feed()

    # ================================================================ ana adım
    def tick(self) -> None:
        now = self.time_fn()
        steps: Dict[str, float] = {}
        mark = time.monotonic()

        def lap(name: str) -> None:
            nonlocal mark
            t = time.monotonic()
            steps[name] = steps.get(name, 0.0) + t - mark
            mark = t

        try:
            if now - self.exchange.rules_loaded_at > RULES_REFRESH_SEC:
                try:
                    self.exchange.refresh_rules()
                except Exception as exc:  # noqa: BLE001 - eski kurallarla devam et
                    log.warning("Parite kuralları yenilenemedi: %s", exc)
                lap("parite kuralları")

            self.process_commands()
            lap("komutlar")
            if not self.running:
                return
            self._process_pending_orders(now)
            self.smart.process_entries(now)

            if self.state.positions:
                self.manage_positions(now)
            elif self.state.killed or kill_switch_hit(self.state.realized_pnl, 0.0, self.loss_limit):
                self.trigger_kill(self.state.realized_pnl)
            lap("pozisyonlar")
            if not self.running:
                return

            # Uyumsuzluk alımı (saatte bir) taradığı döngüde ana tarama bir sonraki döngüye kalır: ikisi aynı döngüde
            # yapılıp döngüyü uzatmasın
            div_scanned = divergence_trading(self.cfg) and self.divergence_tick(now)
            if div_scanned:
                lap("uyumsuzluk taraması")
            if getattr(self.strategy, "is_rotation", False):
                if not self.state.killed:
                    self.rotation_tick(now)
                lap("rotasyon")
            elif (not div_scanned and not self.state.paused and not self.state.killed and now >= self.next_scan_at
                    and self.free_slots() > 0 and not self.entries_frozen(now)):
                self.next_scan_at = now + self.cfg.scan_interval_sec
                self.scan_and_enter(now)
                lap("tarama")

            self._periodic(now)
            lap("raporlar / kontroller")
        finally:
            total = sum(steps.values())
            if total > SLOW_TICK_SEC:
                log.warning("Döngü %.0f sn sürdü: %s", total, ", ".join(
                    f"{k} {v:.0f} sn" for k, v in sorted(steps.items(), key=lambda kv: -kv[1]) if v >= 1))

    def process_commands(self) -> None:
        if self.command_listener is None:
            return
        pending, self._deferred_commands = self._deferred_commands, []
        for cmd in pending + self.command_listener.get_commands():
            self._run_command(cmd)
            if not self.running:
                return

    def _run_command(self, cmd) -> None:
        shown = "***" if cmd.name == "evet" and cmd.args else " ".join(cmd.args)  # PIN log'a yazılmasın
        log.info("Telegram komutu: /%s %s", cmd.name, shown)
        self.commands.handle(cmd)

    def _during_scan(self) -> bool:
        """Taramada her adaydan önce: bilgi komutlarını hemen cevaplar, işlem yapan komutları (satış, durdurma,
        onay...) tarama bitince çalıştırmak üzere sıraya alır. Süre sınırı dolduysa False (taramayı bitir)."""
        if self.command_listener is not None:
            for cmd in self.command_listener.get_commands():
                if (cmd.name in READ_ONLY_COMMANDS if cmd.kind != "callback"
                        else bool(cmd.args) and cmd.args[0] in READ_ONLY_CALLBACKS):
                    self._run_command(cmd)
                else:
                    self._deferred_commands.append(cmd)
        return time.monotonic() < self._scan_deadline

    # ================================================================ pozisyon sayısı
    def slots(self) -> int:
        """Mevcut sermayeyle güvenle açılabilecek pozisyon sayısı."""
        return position_slots(self.state.capital, self.exchange.typical_min_notional(), self.cfg)

    def free_slots(self) -> int:
        """Boş yer: onayı / dipten dönüşü beklenen girişler de yer tutar."""
        return max(0, self.slots() - len(self.state.positions) - self.smart.pending_count())

    # ================================================================ sermaye / kill-switch limiti
    @property
    def loss_limit(self) -> float:
        """Kill-switch limiti (USDT): botun toplam sermayesinin MAX_TOTAL_LOSS_PCT'si."""
        return self.cfg.loss_limit(self.state.start_capital, self.state.capital_added)

    @property
    def loss_pct(self) -> float:
        """Kill-switch limitinin botun sermayesine oranı (%)."""
        return self.cfg.loss_pct(self.state.start_capital, self.state.capital_added)

    def unallocated_balance(self) -> Optional[float]:
        """Canlı modda Spot'ta olup bota henüz ayrılmamış serbest bakiye (test modunda None).

        Yalnızca `unreconciled_reason()` None iken güvenilirdir: aksi halde botun kendi satış geliri
        (henüz işlenmemiş yedek stop / belirsiz satış) serbest bakiyede görünüp yatırılmış para sanılabilir.
        """
        if self.exchange.is_paper:
            return None
        free = self.exchange.free_balance(self.cfg.quote_asset)
        bot_cash = max(0.0, self.state.capital - self.state.committed)
        return free - bot_cash

    def unreconciled_reason(self) -> Optional[str]:
        """Botun kayıtları cüzdanla henüz eşleşmiyorsa nedeni (canlı mod), eşleşiyorsa None."""
        if self.exchange.is_paper:
            return None
        if self.state.pending_orders:
            return "sonucu henüz belli olmayan bir emir var"
        for pos in self.state.positions.values():
            if pos.pending_sell_client_id:
                return f"{pos.symbol} satışının sonucu henüz belli değil"
            # Yedek stop (tamamen ya da kısmen) çalışıp bot bunu henüz işlemediyse coinler cüzdanda eksiktir
            if self.exchange.total_balance(pos.base_asset) < pos.qty * 0.995:
                return f"{pos.symbol} coinlerinin bir kısmı cüzdanda yok (yedek stop çalışmış olabilir)"
        return None

    def add_capital(self, amount: float) -> None:
        """Hesaba yatırılan parayı botun sermayesine ekler (/sermayeekle).

        Sermaye tabanı büyüdüğü için kill-switch limiti ve pozisyon sayısı da buna göre artar.
        Test modunda sanal bakiyeye de eklenir.
        """
        if amount <= 0:
            raise ValueError("Eklenecek tutar sıfırdan büyük olmalı")
        state = self.state
        state.start_capital = round(state.start_capital + amount, 8)
        state.capital_added = round(state.capital_added + amount, 8)
        if self.exchange.is_paper:
            q = self.cfg.quote_asset
            state.paper_balances[q] = round(state.paper_balances.get(q, 0.0) + amount, 8)
        self.store.save(state)
        log.info("Sermaye eklendi: +%.2f %s → toplam sermaye %.2f, kill-switch limiti %.2f",
                 amount, self.cfg.quote_asset, state.capital, self.loss_limit)

    # ================================================================ fiyat
    def bid_ask(self, symbol: str):
        """Önce anlık fiyat akışı (WebSocket), yoksa/bayatsa REST."""
        if self.price_stream is not None:
            quote = self.price_stream.get_bid_ask(symbol, max_age=STREAM_MAX_AGE)
            if quote:
                return quote
        return self.exchange.get_bid_ask(symbol)

    def _sync_stream_symbols(self) -> None:
        if self.price_stream is None:
            return
        symbols = set(self.state.positions) | {s for s, e in self.smart.entries.items() if e.kind == "trail"}
        if symbols and self.cfg.btc_crash_exit:
            symbols.add("BTCUSDT")
        self.price_stream.set_symbols(symbols)

    # ================================================================ giriş
    def scan_and_enter(self, now: float) -> None:
        state = self.state
        state.cooldowns = {s: t for s, t in state.cooldowns.items() if t > now}
        slots = self.slots()
        free_slots = slots - len(state.positions) - self.smart.pending_count()
        if free_slots <= 0:
            return
        result = self._run_scan(self.strategy, slots)
        self._record_scan(result, now)
        log.info("%s | pozisyon %d/%d", result.summary(), len(state.positions), slots)
        self._enter_signals(result, slots, free_slots)

    def divergence_tick(self, now: float) -> bool:
        """RSI_DIV_TRADE: ana stratejinin YANINDA, her saatlik mum kapandıktan sonra bir kez pozitif RSI uyumsuzluğu +
        dönüş teyidi taraması (STRATEGY=rsi_divergence ile aynı kurallar; geçmiş veriyle: --strateji rsi_divergence).
        Sinyaller ana taramayla aynı giriş adımlarından geçer (spread, canlı filtreler, bakiye, onay, kademeli alım);
        "ask" modunda her alım Telegram'dan sorulur. O saat boş pozisyon yeri yoksa yer açılınca taranır.
        Tarama yapıldıysa True."""
        state, cfg = self.state, self.cfg
        hour = int(now // 3600)
        if hour <= self._div_hour or now - hour * 3600 < DIV_SCAN_DELAY_SEC or now < self._next_div_try:
            return False
        if state.paused or state.killed or self.entries_frozen(now):
            return False
        state.cooldowns = {s: t for s, t in state.cooldowns.items() if t > now}
        slots = self.slots()
        free_slots = slots - len(state.positions) - self.smart.pending_count()
        if free_slots <= 0:
            return False
        self._next_div_try = now + DIV_RETRY_SEC   # Tarama hata verirse (ağ, istek limiti) bu saat yeniden denenir
        # Ana stratejinin puan / hedef alanı eşikleri ve art arda tarama teyidi bu taramaya uymaz (saatte bir tarar)
        div_cfg = replace(cfg, strategy="rsi_divergence", signal_confirm_scans=1, min_signal_score=0.0,
                          min_room_ratio=0.0)
        strategy = RsiDivergenceStrategy(self.exchange.client, div_cfg,
                                         coin_filter=getattr(self.strategy, "coin_filter", None),
                                         regime=getattr(self.strategy, "regime", None))
        result = self._run_scan(strategy, slots)
        self._div_hour = hour
        self.last_div_scan, self.last_div_scan_at = result, now
        log.info("RSI uyumsuzluğu taraması (RSI_DIV_TRADE=%s): %s", cfg.rsi_div_trade, result.summary())
        extra: dict = {}
        if cfg.rsi_div_tp_pct > 0:
            extra["tp_pct"] = cfg.rsi_div_tp_pct
        if cfg.rsi_div_sl_pct > 0:
            extra["sl_pct"] = cfg.rsi_div_sl_pct
        if cfg.rsi_div_trade == "ask":
            extra["approval"] = "RSI uyumsuzluğu alımı (RSI_DIV_TRADE=ask)"
        self._enter_signals(result, slots, free_slots, extra)
        return True

    def _scan_excluded(self) -> Set[str]:
        state = self.state
        excluded = (set(state.cooldowns) | set(state.positions) | {p["symbol"] for p in state.pending_orders.values()}
                    | set(self.smart.entries))
        if self.fee_info is not None and self.fee_info.bnb_enabled:
            # Komisyon BNB'den kesilirken BNB'nin kendisini almak, pozisyon miktarının komisyonlarla
            # erimesine ve komisyon için ayrılan BNB'nin satılmasına yol açar
            excluded.add(f"BNB{self.cfg.quote_asset}")
        return excluded

    def _run_scan(self, strategy, slots: int) -> ScanResult:
        """Taramayı süre sınırıyla yapar; aday aralarında bilgi komutları cevaplanır (bkz. _during_scan)."""
        self._scan_deadline = time.monotonic() + SCAN_TIME_BUDGET_SEC
        strategy.between_candidates = self._during_scan
        try:
            # Sermaye pozisyonlara eşit bölünür (toz kontrolü bu tutara göre)
            return strategy.scan(self.exchange.rules, self._scan_excluded(), self.state.capital / slots)
        finally:
            strategy.between_candidates = None

    def _enter_signals(self, result: ScanResult, slots: int, free_slots: int, extra: Optional[dict] = None) -> None:
        """Tarama sinyallerinden alım. `extra`: girişe eklenecek bilgiler (pozisyona özel Kâr Al / Zarar Kes, onay)."""
        state, cfg = self.state, self.cfg
        if not result.signals:
            return
        if result.regime == "ZAYIF":  # Zayıf piyasada aynı anda en fazla 1 pozisyon
            free_slots = min(free_slots, max(0, 1 - len(state.positions)))
            if free_slots <= 0:
                return

        capital = state.capital
        target = capital / slots  # Sermaye pozisyonlara eşit bölünür
        free = self.exchange.free_balance(cfg.quote_asset)
        opened = 0
        for signal in result.signals[: free_slots + 3]:
            if opened >= free_slots:
                break
            rules = self.exchange.rules.get(signal.symbol)
            if rules is None or signal.symbol in state.positions:
                continue
            bid, ask = self.bid_ask(signal.symbol)
            spread = (ask - bid) / ((ask + bid) / 2) * 100 if ask > 0 and bid > 0 else 100.0
            if cfg.spread_filter and spread > cfg.max_spread_pct:
                log.info("%s atlandı: spread %%%.2f > %%%.2f", signal.symbol, spread, cfg.max_spread_pct)
                self.day_stats.rejects["spread geniş"] += 1
                continue
            why = live_reject(cfg, self.exchange.client, signal.symbol, ask)
            if why:
                self.day_stats.rejects[why] += 1
                continue
            remaining = capital - state.committed
            is_last_slot = len(state.positions) + 1 >= slots
            # Son slot artanı da kullanır; yedek akçe açıksa (CAPITAL_USAGE_PCT < 100) diğer pozisyonların yedeğine
            # dokunulmaz
            budget = remaining if is_last_slot and cfg.capital_usage_pct >= 100 else min(target, remaining)
            plan = plan_entry(min(budget, free), rules, ask, cfg)
            if not plan.ok:
                log.info("%s atlandı: %s", signal.symbol, plan.reason)
                self.day_stats.rejects[plan.reason.split(" (")[0][:40]] += 1
                if "bakiye" in plan.reason.lower():
                    self.notifier.send_error("low-balance", Messages.warning(
                        f"{plan.reason}. Serbest bakiye: {free:.2f} {cfg.quote_asset}"), cooldown_sec=3600)
                    return
                continue
            context = {"spread_pct": spread, "btc_1h_pct": result.btc_1h_pct, "regime": result.regime,
                       "expected_ask": ask, **(extra or {})}
            # Onay (SIGNAL_APPROVAL), dipten dönüş (TRAILING_BUY) ve ATR hedefleri burada devreye girer
            outcome = self.smart.enter(signal, min(budget, free), slots, context, ask)
            if outcome == "opened":
                opened += 1
                free -= state.positions[signal.symbol].cost_usdt
            elif outcome == "pending":
                opened += 1
                free -= plan.quote_amount

    # ================================================================ momentum rotasyonu
    @staticmethod
    def rotation_day(now: float) -> int:
        """Rotasyon günü (UTC): günlük mum kapandıktan ROTATION_DELAY_SEC sonra bir sonraki güne geçer."""
        return int((now - ROTATION_DELAY_SEC) // 86400)

    def rotation_tick(self, now: float) -> None:
        """STRATEGY=rotation: her gün BTC trend kontrolü, ROTATION_DAYS günde bir yeniden dengeleme.

        Backtest'in gün kapanışı kararlarıyla aynı mantık (rotation.plan_rotation). Kararlar günlük mum kapandıktan
        ROTATION_DELAY_SEC sonra verilir. /durdur yeniden dengelemeyi erteler; BTC trend satışları sürer.
        """
        state, cfg = self.state, self.cfg
        self._retry_rotation_sells(now, refill=True)
        if now < self.next_scan_at:
            return
        self.next_scan_at = now + cfg.scan_interval_sec
        if now % 86400 < ROTATION_DELAY_SEC:
            return   # Günlük mum yeni kapandı; Binance verisi oturana kadar (ve gün etiketi kaymasın diye) beklenir
        day = self.rotation_day(now)
        if state.rotation_refill and state.rotation_plan_day != day:
            state.rotation_refill = False   # Boşalan yer yalnızca dengeleme günü doldurulur (backtest gibi)
            self.store.save(state)
        due = state.last_rebalance_day < 0 or day - state.last_rebalance_day >= cfg.rotation_days
        pending_buy = any(p.get("side", "BUY") == "BUY" for p in state.pending_orders.values())
        if due and not state.paused:
            self.rebalance(day, now)
        elif state.rotation_refill and not state.rotation_sells and not pending_buy and not state.paused:
            self.refill(day, now)
        elif cfg.rotation_btc_ma_days > 0 and state.positions and state.last_trend_check_day != day:
            on, note = self.strategy.risk_on()
            state.last_trend_check_day = day
            self.store.save(state)
            if on is False:
                self.notifier.send(Messages.warning(
                    f"BTC düşüş trendine girdi ({note}). Rotasyon pozisyonları satılıp USDT'de bekleniyor; "
                    f"trend düzelince bir sonraki yeniden dengelemede tekrar alım yapılır."), critical=True)
                self._queue_rotation_sells(list(state.positions), ExitReason.TREND_FILTER, now)

    def target_slots(self) -> int:
        """Aynı anda tutulacak coin sayısı (rotasyonda ROTATION_TOP_N ile sınırlı)."""
        slots = self.slots()
        return min(self.cfg.rotation_top_n, slots) if getattr(self.strategy, "is_rotation", False) else slots

    def _rotation_plan(self, slots: int):
        state, cfg = self.state, self.cfg
        excluded = {p["symbol"] for p in state.pending_orders.values()}
        if self.fee_info is not None and self.fee_info.bnb_enabled:
            excluded.add(f"BNB{cfg.quote_asset}")  # Komisyon BNB'den kesilirken BNB alınmaz (bkz. scan_and_enter)
        held = [s for s in state.positions if s not in state.rotation_sells]   # Satışı süren coin elde sayılmaz
        return self.strategy.plan(held, self.exchange.rules, excluded, slots)

    def rebalance(self, day: int, now: float) -> None:
        """Planlı yeniden dengeleme. Dengeleme günü ancak satış ve alımlar bitince kaydedilir: arada ağ hatası olursa
        bir sonraki kontrolde aynı günün kapanışları ve AYNI hedef coin sayısıyla yeniden planlanır (alınanlar elde
        sayılır; gönderilmekte olan alım önceden kaydedildiği için iki kez alınmaz)."""
        state = self.state
        if state.rotation_plan_day != day:
            state.rotation_plan_day, state.rotation_plan_slots = day, self.target_slots()   # Satışlardan ÖNCE
            self.store.save(state)
        slots = state.rotation_plan_slots
        plan, note = self._rotation_plan(slots)
        reason = ExitReason.TREND_FILTER if plan.risk_on is False else ExitReason.ROTATION
        if plan.risk_on is not False:
            # Trend düzeldi ama eski trend satışı hâlâ sürüyor: artık sıralama satışıdır, satılınca yeri bugün dolar
            for symbol, why in list(state.rotation_sells.items()):
                if why == ExitReason.TREND_FILTER.value:
                    state.rotation_sells[symbol] = ExitReason.ROTATION.value
        to_sell = list(plan.sell)
        self._queue_rotation_sells(to_sell, reason, now)
        sold = [s for s in to_sell if s not in state.positions]
        leftover = set(state.rotation_sells) - set(to_sell)   # Önceki günlerden süren satışlar
        bought = self._rotation_buys(plan, slots) if plan.risk_on is not False else []
        # Özet: plan istediği için satışı iptal edilen coin tutulur; süren satışlar "henüz satılamayan" sayılır
        plan.keep += [s for s in plan.candidates if s in leftover and s in state.positions
                      and s not in state.rotation_sells]
        unsold = [s for s in state.rotation_sells if s in state.positions]
        state.last_rebalance_day = state.last_trend_check_day = day
        state.rotation_refill = False
        self.store.save(state)
        self._announce_rotation(plan, note, sold, unsold, bought, now)

    def refill(self, day: int, now: float) -> None:
        """Dengeleme günü boşalan / dolmayan yerleri YALNIZCA alımla doldurur (elde tutulanlar satılmaz, takvim
        değişmez): dengelemede satılamayıp sonra satılan coin, sonucu belirsiz kalıp sonra netleşen alım."""
        state = self.state
        plan, note = self._rotation_plan(state.rotation_plan_slots)
        bought = self._rotation_buys(plan, state.rotation_plan_slots) if plan.risk_on is not False else []
        state.rotation_refill = False
        self.store.save(state)
        if bought:
            self._announce_rotation(plan, note, [], [], bought, now, refill=True)

    def _announce_rotation(self, plan, note: str, sold, unsold, bought, now: float, refill: bool = False) -> None:
        text = Messages.rotation(plan, note, sold, unsold, bought, self.cfg, refill=refill)
        self.last_rotation, self.last_rotation_at = text, now
        log.info("Rotasyon%s: tut %s | sat %s | al %s | %s", " (doldurma)" if refill else "", plan.keep,
                 sold + unsold, bought, note)
        self.notifier.notify(text, "close")

    def _rotation_buys(self, plan, slots: int) -> List[str]:
        state, cfg = self.state, self.cfg
        # Satışlardan SONRAKİ sermaye (satışın kârı / zararı dahil); zarar yer sayısını azaltmışsa ona göre
        slots = min(slots, self.slots())
        if slots <= 0:
            return []
        capital = state.capital
        target = capital / slots   # Eşit ağırlık
        free = self.exchange.free_balance(cfg.quote_asset)
        momentum = dict(plan.ranking)
        bought: List[str] = []
        for symbol in plan.candidates:
            pos = state.positions.get(symbol)
            if symbol in state.rotation_sells and pos is not None and self._still_held(pos):
                # Önceki (trend / sıralama) satışı hâlâ sürüyor ama yeni plan bu coini istiyor: satış iptal, tutulur
                state.rotation_sells.pop(symbol, None)
                self.store.save(state)
            pending_buys = sum(1 for p in state.pending_orders.values() if p.get("side", "BUY") == "BUY")
            if len(state.positions) + pending_buys >= slots:
                break
            rules = self.exchange.rules.get(symbol)
            if rules is None or symbol in state.positions:
                continue
            bid, ask = self.bid_ask(symbol)
            spread = (ask - bid) / ((ask + bid) / 2) * 100 if ask > 0 and bid > 0 else 100.0
            if spread > cfg.max_spread_pct:
                log.info("%s atlandı: spread %%%.2f > %%%.2f", symbol, spread, cfg.max_spread_pct)
                continue
            remaining = capital - state.committed
            is_last_slot = len(state.positions) + 1 >= slots
            budget = remaining if is_last_slot else min(target, remaining)
            entry = plan_entry(min(budget, free), rules, ask, cfg)
            if not entry.ok:
                log.info("%s atlandı: %s", symbol, entry.reason)
                if "bakiye" in entry.reason.lower():
                    self.notifier.send_error("low-balance", Messages.warning(
                        f"{entry.reason}. Serbest bakiye: {free:.2f} {cfg.quote_asset}"), cooldown_sec=3600)
                    break
                continue
            signal = Signal(symbol, ask, momentum[symbol], {
                "kind": "rotation", "momentum_pct": momentum[symbol], "rank": float(plan.rank_of(symbol) or 0),
                "lookback_days": float(cfg.rotation_lookback_days)})
            context = {"spread_pct": spread, "regime": "ROTASYON", "expected_ask": ask}
            # Emir gönderilmeden ÖNCE kaydedilir: bot tam bu sırada kapanırsa açılışta Binance'ten sorgulanır
            cid = f"tbR{int(self.time_fn() * 1000)}{len(bought)}"
            state.pending_orders[cid] = {
                "symbol": symbol, "side": "BUY", "created_at": self.time_fn(), "note": signal.note(),
                "score": signal.score, "metrics": dict(signal.metrics), "slots": slots, "spread_pct": spread,
                "regime": "ROTASYON"}
            self.store.save(state)
            if self.open_position(signal, entry.quote_amount, slots, context, client_order_id=cid):
                bought.append(symbol)
                self.day_stats.buys += 1
                free -= state.positions[symbol].cost_usdt
            elif cid in state.pending_orders:
                break   # Sonucu belirsiz alım: bakiye ve yer hesabı netleşmeden başka alım yapılmaz
        return bought

    def _still_held(self, pos: Position) -> bool:
        """Sonucu belirsiz kalan satış emri varsa Binance'ten sorgular: emir gerçekleşmediyse coin hâlâ eldedir
        (True). Gerçekleştiyse veya sorgulanamadıysa False: satış listesi close_position ile netleştirir."""
        if not pos.pending_sell_client_id:
            return True
        try:
            earlier = self.exchange.recover_order(pos.symbol, pos.pending_sell_client_id, "SELL")
        except (OrderError, requests.RequestException, BinanceAPIError) as exc:
            log.warning("%s önceki satış emri sorgulanamadı: %s", pos.symbol, exc)
            return False
        if earlier is not None:
            return False
        pos.pending_sell_client_id = ""
        self._next_backup_check[pos.symbol] = 0   # Satış denemesinde kaldırılan yedek stop geri konsun
        return True

    def _queue_rotation_sells(self, symbols: List[str], reason: ExitReason, now: float) -> None:
        """Satılacakları kalıcı listeye yazar ve hemen satmayı dener (satılamayanlar sonra tekrar denenir)."""
        if not symbols:
            return
        for symbol in symbols:
            self.state.rotation_sells[symbol] = reason.value
        self.store.save(self.state)
        self._next_rotation_sell = 0.0
        self._retry_rotation_sells(now)

    def _retry_rotation_sells(self, now: float, refill: bool = False) -> None:
        """Satış listesindekileri satmayı dener. refill=True: dengelemeden SONRA satılabilen (sıralamadan düşen)
        coinin boşalttığı yer bir sonraki taramada doldurulur (backtest satış + alımı aynı anda yapar)."""
        state = self.state
        if not state.rotation_sells or now < self._next_rotation_sell:
            return
        failed = False
        for symbol, reason in list(state.rotation_sells.items()):
            pos = state.positions.get(symbol)
            if pos is None:   # Bu arada başka yoldan kapandı (zarar kes, yedek stop, /sat): yer yine boşaldı
                state.rotation_sells.pop(symbol, None)
                if refill and reason == ExitReason.ROTATION.value:
                    state.rotation_refill = True
            elif self.close_position(pos, ExitReason(reason)):
                state.rotation_sells.pop(symbol, None)
                if refill and reason == ExitReason.ROTATION.value:
                    state.rotation_refill = True
            else:
                failed = True
        self._next_rotation_sell = now + ROTATION_SELL_RETRY_SEC if failed else 0.0
        self.store.save(state)

    def open_position(self, signal, quote_amount: float, slots: int = 1, context: Optional[dict] = None,
                      client_order_id: Optional[str] = None) -> bool:
        """client_order_id: emir önceden pending_orders'a yazıldıysa kimliği (başarı / ret sonrası kayıt silinir)."""
        context = context or {}
        try:
            fill = (self.exchange.market_buy(signal.symbol, quote_amount, client_order_id=client_order_id)
                    if client_order_id else self.exchange.market_buy(signal.symbol, quote_amount))
        except OrderError as exc:
            if client_order_id and not (exc.code == "UNKNOWN" and exc.client_order_id):
                self.state.pending_orders.pop(client_order_id, None)   # Emir reddedildi: gerçekleşmedi
                self.store.save(self.state)
            if exc.code == "UNKNOWN" and exc.client_order_id:
                # İnternet koptu, emrin gerçekleşip gerçekleşmediği bilinmiyor: takibe al
                self.state.pending_orders[exc.client_order_id] = {
                    "symbol": signal.symbol, "side": "BUY", "created_at": self.time_fn(),
                    "note": signal.note(), "score": signal.score, "metrics": dict(signal.metrics),
                    "slots": slots, **{k: v for k, v in context.items() if k != "expected_ask"},
                }
                self.store.save(self.state)
                self.notifier.send(Messages.warning(
                    f"{signal.symbol} alım emrinin sonucu bağlantı hatası yüzünden öğrenilemedi. "
                    f"Bot Binance'ten kontrol edip gerçekleştiyse pozisyonu takibe alacak."), critical=True)
            else:
                self.notifier.send_error(f"buy-{signal.symbol}", Messages.error(
                    f"{signal.symbol} alımı başarısız: {exc}"))
            return False

        expected = context.get("expected_ask")
        if expected:
            slip = (fill.avg_price - expected) / expected * 100
            if self.cfg.slippage_warn_pct > 0 and slip > self.cfg.slippage_warn_pct:
                self.notifier.send(Messages.warning(
                    f"{signal.symbol} alış fiyatı beklenenden %{slip:.2f} yüksek gerçekleşti (kayma)."))
            self._track_slippage(slip)
        self._register_position(signal.symbol, fill, signal.note(), signal.score, dict(signal.metrics),
                                 context, slots, client_order_id)
        return True

    def _register_position(self, symbol: str, fill: Fill, note: str, score: float, metrics: dict,
                           context: dict, slots: int, client_order_id: Optional[str] = None) -> Position:
        """client_order_id: önceden kaydedilmiş alım kaydı; pozisyon state'e girdiği anda silinir (arada bot
        durdurulursa kayıt kalır ve açılışta Binance'ten sorgulanır — coin takipsiz kalıp tekrar alınmaz)."""
        rules = self.exchange.rules_for(symbol)
        pos = Position(symbol=symbol, base_asset=rules.base_asset, qty=fill.qty,
                       entry_price=fill.avg_price, cost_usdt=fill.usdt, opened_at=self.time_fn(),
                       buy_fee_usdt=fill.fee_usdt, signal_note=note, score=score, metrics=metrics,
                       spread_pct=float(context.get("spread_pct") or 0.0), btc_1h_pct=context.get("btc_1h_pct"),
                       regime=str(context.get("regime") or ""), tp_pct=context.get("tp_pct"),
                       sl_pct=context.get("sl_pct"), reserve_usdt=float(context.get("reserve_usdt") or 0.0))
        if not self.exchange.is_paper:
            try:  # Cüzdandaki gerçek miktar daha azsa onu esas al
                held = self.exchange.free_balance(rules.base_asset)
                if 0 < held < pos.qty:
                    pos.qty = held
            except Exception as exc:  # noqa: BLE001
                log.warning("Alış sonrası bakiye doğrulanamadı: %s", exc)

        self.state.positions[pos.symbol] = pos
        if client_order_id:
            self.state.pending_orders.pop(client_order_id, None)
        self.state.total_fees += fill.fee_usdt
        backup_line = self._place_backup(pos)
        self.store.save(self.state)
        self._sync_stream_symbols()
        self.notifier.notify(Messages.buy(pos, self.levels(pos), note, self.cfg, len(self.state.positions), slots,
                                          backup_line), "open")
        return pos

    def levels(self, pos: Position) -> dict:
        """Net K/Z hedeflerine karşılık gelen fiyat seviyeleri."""
        cfg = self.cfg
        qty = self.exchange.sellable_qty(pos.symbol, pos.qty)
        cost = pos.cost_usdt
        tp, sl = position_tp(pos, cfg), position_sl(pos, cfg)   # ATR_TARGETS ile pozisyona özel olabilir
        return {
            "tp_pct": tp, "sl_pct": sl,
            "tp": price_for_pnl(tp, cost, qty, cfg.fee_rate),
            "tp_value": cost * (1 + tp / 100),
            "sl": price_for_pnl(-sl, cost, qty, cfg.fee_rate),
            "sl_value": cost * (1 - sl / 100),
            "trail_on": price_for_pnl(cfg.trailing_activation_pct, cost, qty, cfg.fee_rate),
            "trail_floor": price_for_pnl(cfg.trailing_floor_pct, cost, qty, cfg.fee_rate),
            "trail_value": cost * (1 + cfg.trailing_floor_pct / 100),
        }

    # ================================================================ yedek stop emri
    def _place_backup(self, pos: Position) -> str:
        """Binance'e yedek zarar-kes emri bırakır. Telegram için açıklama satırı döner."""
        cfg = self.cfg
        if not cfg.backup_stop_enabled:
            return ""
        sellable = self.exchange.sellable_qty(pos.symbol, pos.qty)
        stop_price = price_for_pnl(-cfg.backup_stop_pct, pos.cost_usdt, sellable, cfg.fee_rate)
        try:
            backup = self.exchange.place_backup_stop(pos.symbol, pos.qty, stop_price, cfg.backup_stop_limit_gap_pct)
        except OrderError as exc:
            log.warning("%s yedek stop emri konulamadı: %s", pos.symbol, exc)
            return f"⚠️ Binance yedek stop konulamadı: {esc(str(exc))}"
        except (requests.RequestException, BinanceAPIError) as exc:
            log.warning("%s yedek stop emri konulamadı: %s", pos.symbol, exc)
            return "⚠️ Binance yedek stop şu an konulamadı, tekrar denenecek"
        pos.backup_order_id = backup.order_id
        pos.backup_stop_price = backup.stop_price
        pos.backup_qty = backup.qty
        self._next_backup_check[pos.symbol] = self.time_fn() + BACKUP_CHECK_SEC
        kind = "piyasa" if backup.kind == "STOP_LOSS" else "limitli"
        return (f"🧷 Binance yedek stop (-%{cfg.backup_stop_pct:g}, {kind}): {backup.stop_price:.8g} "
                f"— bilgisayar kapalıyken de korur")

    def _check_backup(self, pos: Position, bid: float, now: float) -> bool:
        """Yedek stop emri borsada çalıştıysa pozisyonu kapatır. Kapandıysa True."""
        if pos.backup_order_id is None:
            if self.cfg.backup_stop_enabled and now >= self._next_backup_check.get(pos.symbol, 0):
                self._next_backup_check[pos.symbol] = now + BACKUP_CHECK_SEC * 5
                if self._place_backup(pos):  # Daha önce konulamadıysa tekrar dene
                    self.store.save(self.state)
            return False
        near = pos.backup_stop_price > 0 and bid <= pos.backup_stop_price * 1.005
        if not near and now < self._next_backup_check.get(pos.symbol, 0):
            return False
        self._next_backup_check[pos.symbol] = now + BACKUP_CHECK_SEC
        st = self.exchange.backup_stop_status(pos.symbol, pos.backup_order_id, pos.backup_qty, pos.backup_stop_price)
        if st.status == "FILLED" and st.fill:
            self._close_with_fills(pos, [st.fill], ExitReason.BACKUP_STOP)
            return True
        if st.status == "GONE":
            if st.fill:  # Kısmen dolup iptal olmuş: dolan kısmın sonucunu hesaba yaz
                self._realize_partial(pos, st.fill)
            log.warning("%s yedek stop emri artık Binance'te yok (%s), yeniden konulacak", pos.symbol, st.detail)
            pos.backup_order_id = None
            self._sync_with_wallet(pos)
            self._next_backup_check[pos.symbol] = 0
            self.store.save(self.state)
        return False

    def _realize_partial(self, pos: Position, fill: Fill) -> None:
        """Pozisyonun bir kısmı satıldıysa (kısmi dolum) o kısmın K/Z'sini gerçekleşmiş say."""
        if fill.qty <= 0 or pos.qty <= 0:
            return
        share = min(1.0, fill.qty / pos.qty)
        cost_part = pos.cost_usdt * share
        self.state.realized_pnl += fill.usdt - cost_part
        self.state.total_fees += fill.fee_usdt
        pos.cost_usdt -= cost_part
        pos.buy_fee_usdt *= (1 - share)
        pos.qty = max(0.0, pos.qty - fill.qty)
        log.warning("%s pozisyonunun %.8g kadarı yedek stop ile satıldı (kısmi), kalan %.8g",
                    pos.symbol, fill.qty, pos.qty)

    # ================================================================ pozisyon takibi
    def mark_position(self, pos: Position, now: float) -> Mark:
        cfg = self.cfg
        bid, _ = self.bid_ask(pos.symbol)
        sellable = self.exchange.sellable_qty(pos.symbol, pos.qty)
        value = net_exit_value(sellable, bid, cfg.fee_rate)
        mark = Mark(bid, value, value - pos.cost_usdt, pnl_pct(value, pos.cost_usdt), now)
        self.marks[pos.symbol] = mark

        old_peak = pos.peak_pnl_pct
        changed = False
        # Bu kontrolde zaten Kâr Al ile satılacaksa "koruma aktif" mesajları gereksiz ve kafa karıştırıcı
        announce = mark.pnl_pct < cfg.take_profit_pct
        if update_trailing(pos, mark.pnl_pct, cfg):
            floor = trailing_floor_pct(pos, cfg)
            if announce:
                self.notifier.notify(Messages.trailing_activated(
                    pos, mark.pnl_pct, price_for_pnl(floor, pos.cost_usdt, sellable, cfg.fee_rate), floor, cfg),
                    "update")
            changed = True
        if update_breakeven(pos, cfg):
            if announce:
                self.notifier.notify(Messages.breakeven_activated(
                    pos, mark.pnl_pct, price_for_pnl(cfg.breakeven_floor_pct, pos.cost_usdt, sellable, cfg.fee_rate),
                    cfg), "update")
            changed = True
        if changed or pos.peak_pnl_pct > old_peak + 0.25:
            self.store.save(self.state)
        return mark

    def unrealized(self) -> float:
        return sum(m.pnl_usdt for s, m in self.marks.items() if s in self.state.positions)

    def manage_positions(self, now: float) -> None:
        cfg = self.cfg
        for pos in list(self.state.positions.values()):
            mark = self.mark_position(pos, now)
            if self._check_backup(pos, mark.bid, now):
                continue

        if now - self._last_position_log >= POSITION_LOG_SEC:
            self._last_position_log = now
            for pos in self.state.positions.values():
                m = self.marks.get(pos.symbol)
                if m:
                    log.info("Pozisyon %s | fiyat %.8g | net değer %.4f | K/Z %s USDT (%s%%) | zirve %.2f%% | %s",
                             pos.symbol, m.bid, m.net_value, fmt_signed(m.pnl_usdt, 4), fmt_signed(m.pnl_pct),
                             pos.peak_pnl_pct, fmt_duration(now - pos.opened_at))

        # Kill-switch: gerçekleşen + tüm açık pozisyonların zararı toplamı
        if self._check_kill_switch():
            return

        if cfg.btc_crash_exit and self.state.positions and self._btc_crashing(now):
            self.notifier.send(Messages.warning(
                f"BTC son {cfg.btc_crash_window_min} dk içinde %{cfg.btc_crash_drop_pct:g}'den fazla düştü. "
                f"Açık pozisyonlar erken satılıyor."), critical=True)
            for pos in list(self.state.positions.values()):
                self.close_position(pos, ExitReason.BTC_CRASH)
        else:
            for pos in list(self.state.positions.values()):
                mark = self.marks.get(pos.symbol)
                if mark is None:
                    continue
                # Akıllı yönetim: hedef uzatma, süre / kaçış soruları, DCA / piramit (pozisyonu değiştirebilir)
                reason = self.smart.check_position(pos, mark, now)
                if pos.symbol not in self.state.positions:
                    continue   # Yedek stop ekleme sırasında çalıştı: pozisyon kapandı
                if reason is None:
                    mark = self.marks.get(pos.symbol) or mark   # Ekleme yapıldıysa yeni maliyete göre değerleme
                    reason = evaluate_exit(pos, mark.pnl_pct, now, cfg)
                if reason:
                    log.info("%s çıkış koşulu: %s (net K/Z %%%.2f)", pos.symbol, reason.value, mark.pnl_pct)
                    self.close_position(pos, reason)

        # Bir pozisyon zararla kapandıysa, kalanlarla birlikte limit aşılmış olabilir
        self._check_kill_switch()

    def _btc_crashing(self, now: float) -> bool:
        """BTC son BTC_CRASH_WINDOW_MIN dakikadaki zirvesinden BTC_CRASH_DROP_PCT'den fazla düştü mü?"""
        if now < self._next_btc_check:
            return False
        self._next_btc_check = now + 30
        window = max(2, self.cfg.btc_crash_window_min)
        rows = self.exchange.client.klines("BTCUSDT", "1m", window + 1)
        if not rows:
            return False
        peak = max(float(r[2]) for r in rows)
        last = float(rows[-1][4])
        if self.price_stream is not None:
            live = self.price_stream.get_bid_ask("BTCUSDT", max_age=STREAM_MAX_AGE)
            if live:
                last = live[0]
        drop = (last - peak) / peak * 100 if peak > 0 else 0.0
        return drop <= -self.cfg.btc_crash_drop_pct

    def _check_kill_switch(self) -> bool:
        """Kill-switch iki aşamalıdır:
        1) Toplam zarar limite ulaşınca durum hemen 'killed' olarak kaydedilir (yeni alım ASLA yapılmaz,
           bot yeniden başlatılsa bile).
        2) Açık pozisyonlar satılana kadar satış tekrar denenir; satılamayanların yedek stop emri yeniden
           konur. Hepsi satılınca bot kapanır ve kilitli kalır.
        """
        if self.state.killed:
            self._liquidate()
            return True
        realized, unrealized = self.state.realized_pnl, self.unrealized()
        if not kill_switch_hit(realized, unrealized, self.loss_limit):
            return False
        total = realized + unrealized
        log.error("KILL-SWITCH: toplam K/Z %.4f USDT", total)
        self._mark_killed(total)
        self._liquidate(first=True)
        return True

    def _mark_killed(self, total_pnl: float) -> None:
        self.state.killed = True
        self.state.kill_reason = (f"Toplam K/Z {total_pnl:.4f} USDT (limit -%{self.loss_pct:.4g} = "
                                  f"-{self.loss_limit:.2f} USDT)")
        self.state.killed_at = self.time_fn()
        self.store.save(self.state)

    def _liquidate(self, first: bool = False) -> None:
        """Kill-switch sonrası tüm pozisyonları satmayı dener (başarısızlar ~5 sn sonra tekrar)."""
        now = self.time_fn()
        if not first and now < self._next_kill_retry:
            return
        self._next_kill_retry = now + KILL_RETRY_SEC
        left = [pos.symbol for pos in list(self.state.positions.values())
                if not self.close_position(pos, ExitReason.KILL_SWITCH)]
        total = self.state.realized_pnl + self.unrealized()
        if not left:
            self.trigger_kill(total)
        elif first or now >= self._next_kill_notice:
            self._next_kill_notice = now + 600
            self.notifier.send(Messages.kill_switch(total, self.state.start_capital + total, self.cfg,
                                                    self.loss_limit, self.loss_pct, left,
                                                    retrying=True), critical=True)

    # ================================================================ çıkış
    def close_position(self, pos: Position, reason: ExitReason) -> bool:
        """Önce Binance'teki yedek stop emrini iptal eder, sonra piyasa fiyatından satar.

        Satış yapılamazsa pozisyon açık kalır (dolan kısımlar muhasebeye yazılır), yedek stop yeniden
        konur ve False döner; bir sonraki kontrolde tekrar denenir.
        """
        fills: List[Fill] = []
        if pos.backup_order_id is not None:
            st = self.exchange.cancel_backup_stop(pos.symbol, pos.backup_order_id)
            if st.status == "ERROR":
                self.notifier.send_error(f"cancel-{pos.symbol}", Messages.error(
                    f"{pos.symbol} yedek stop emri iptal edilemedi ({st.detail}); satış bir sonraki "
                    f"kontrolde tekrar denenecek."), cooldown_sec=300)
                return False
            if st.fill:
                fills.append(st.fill)
            if st.status == "FILLED":
                self._close_with_fills(pos, fills, ExitReason.BACKUP_STOP)
                return True
            pos.backup_order_id = None
            self.store.save(self.state)

        if pos.pending_sell_client_id:
            # Önceki satış emrinin sonucu belirsizdi: tekrar satmadan önce gerçekleşmiş mi bak
            try:
                earlier = self.exchange.recover_order(pos.symbol, pos.pending_sell_client_id, "SELL")
            except (OrderError, requests.RequestException, BinanceAPIError) as exc:
                log.warning("%s önceki satış emri sorgulanamadı: %s", pos.symbol, exc)
                return self._sell_failed(pos, reason, fills, None)
            pos.pending_sell_client_id = ""
            if earlier is not None:
                fills.append(earlier)

        remaining = pos.qty - sum(f.qty for f in fills)
        if fills and self.exchange.sellable_qty(pos.symbol, remaining) <= 0:
            self._close_with_fills(pos, fills, reason)  # Kalan yalnızca satılamaz toz
            return True
        try:
            fills.append(self.exchange.market_sell(pos.symbol, remaining))
        except OrderError as exc:
            if exc.code == "NO_BALANCE" and not self.exchange.is_paper:
                return self._handle_missing_balance(pos, reason, fills)
            if fills and exc.code in UNSELLABLE_CODES:
                self._close_with_fills(pos, fills, reason)  # Kalan minimum emir tutarının altında
                return True
            return self._sell_failed(pos, reason, fills, exc)
        self._close_with_fills(pos, fills, reason)
        return True

    def _sell_failed(self, pos: Position, reason: ExitReason, fills: List[Fill], exc: Optional[OrderError]) -> bool:
        """Satış yapılamadı: satılan kısmı hesaba yaz, pozisyonu koru, yedek stopu geri koy."""
        part = combine_fills(fills)
        if part is not None:
            self._realize_partial(pos, part)
        if exc is not None and exc.code == "UNKNOWN" and exc.client_order_id:
            pos.pending_sell_client_id = exc.client_order_id
        self._next_backup_check[pos.symbol] = 0  # Pozisyon korunmasız kalmasın
        self.store.save(self.state)
        if exc is not None:
            self.notifier.send_error(f"sell-{pos.symbol}-{exc.code}", Messages.error(
                f"{pos.symbol} satılamadı ({reason.value}): {exc}. Tekrar denenecek."), cooldown_sec=300)
        return False

    def _handle_missing_balance(self, pos: Position, reason: ExitReason, fills: List[Fill]) -> bool:
        """Canlı modda coin serbest bakiyede yok: kilitli bir bot emri mi, yoksa bot dışında mı satıldı?"""
        if self.exchange.cancel_bot_stop_orders(pos.symbol) > 0:
            # Bilinmeyen (yetim) bot stop emri coinleri kilitliyordu; artık serbestler
            try:
                remaining = pos.qty - sum(f.qty for f in fills)
                fills.append(self.exchange.market_sell(pos.symbol, remaining))
            except OrderError as exc:
                return self._sell_failed(pos, reason, fills, exc)
            self._close_with_fills(pos, fills, reason)
            return True
        held = self.exchange.total_balance(pos.base_asset)
        if self.exchange.sellable_qty(pos.symbol, held) > 0:
            # Coinler cüzdanda ama başka (kullanıcının) bir emirde kilitli: kapatılmış SAYMA
            self.notifier.send_error(f"locked-{pos.symbol}", Messages.warning(
                f"{pos.symbol} coinleri Binance'te sizin açtığınız bir emirde kilitli görünüyor; bot satamıyor. "
                f"Emri iptal ederseniz bot satışı tekrar dener."), cooldown_sec=1800)
            return self._sell_failed(pos, reason, fills, None)
        real = None
        try:
            real = self.exchange.find_sell_fills(pos.symbol, pos.opened_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s satış geçmişi okunamadı: %s", pos.symbol, exc)
        if real is not None:
            fills.append(_scaled_fill(real, pos.qty - sum(f.qty for f in fills)))
            self.notifier.send(Messages.warning(
                f"{pos.symbol} bot dışında satılmış; Binance işlem geçmişindeki gerçek satış fiyatı kullanıldı."),
                critical=True)
            self._close_with_fills(pos, fills, ExitReason.EXTERNAL)
            return True
        mark = self.marks.get(pos.symbol)
        self.notifier.send(Messages.warning(
            f"{pos.symbol} cüzdanda bulunamadı (elle satılmış olabilir). Güncel fiyattan kapatılmış sayılıyor."),
            critical=True)
        part = combine_fills(fills)
        proceeds = (part.usdt if part else 0.0) + (mark.net_value if mark else
                                                   net_exit_value(pos.qty, pos.entry_price, self.cfg.fee_rate))
        self._finalize(pos, mark.bid if mark else pos.entry_price, proceeds, 0.0, ExitReason.EXTERNAL, estimated=True)
        return True

    def _sync_with_wallet(self, pos: Position) -> None:
        """Canlı modda cüzdandaki miktar pozisyondan belirgin azsa (elle kısmi satış), farkı
        Binance işlem geçmişindeki gerçek fiyatla (bulunamazsa güncel fiyatla) gerçekleşmiş say."""
        if self.exchange.is_paper:
            return
        rules = self.exchange.rules.get(pos.symbol)
        step = float(rules.market_step_size) if rules else 0.0
        held = self.exchange.total_balance(pos.base_asset)
        missing = pos.qty - held
        if missing <= max(step, pos.qty * 0.001):
            return
        real = None
        try:
            real = self.exchange.find_sell_fills(pos.symbol, pos.opened_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s satış geçmişi okunamadı: %s", pos.symbol, exc)
        if real is not None and real.qty > 0:
            part = _scaled_fill(real, missing)
        else:
            mark = self.marks.get(pos.symbol)
            price = mark.bid if mark else pos.entry_price
            part = Fill(pos.symbol, "SELL", missing, price, net_exit_value(missing, price, self.cfg.fee_rate),
                        missing * price * self.cfg.fee_rate, "EXTERNAL")
        self._realize_partial(pos, part)
        self.notifier.send(Messages.warning(
            f"{pos.symbol}: cüzdanda {missing:.8g} adet eksik (bot dışında kısmen satılmış). Bu kısım "
            f"hesaba yazıldı, kalan {pos.qty:.8g} adet takip edilmeye devam ediyor."), critical=True)
        self.store.save(self.state)

    def _close_with_fills(self, pos: Position, fills: List[Fill], reason: ExitReason) -> None:
        fill = combine_fills(fills)
        if fill is None:
            mark = self.marks.get(pos.symbol)
            self._finalize(pos, mark.bid if mark else pos.entry_price, mark.net_value if mark else 0.0,
                           0.0, reason, estimated=True)
            return
        self._finalize(pos, fill.avg_price, fill.usdt, fill.fee_usdt, reason)

    def _finalize(self, pos: Position, exit_price: float, proceeds: float, sell_fee: float,
                  reason: ExitReason, estimated: bool = False) -> None:
        now = self.time_fn()
        pnl = proceeds - pos.cost_usdt
        pct = pnl_pct(proceeds, pos.cost_usdt)
        state = self.state
        state.realized_pnl += pnl
        state.trades += 1
        if pnl >= 0:
            state.wins += 1
        else:
            state.losses += 1
        state.total_fees += sell_fee
        state.positions.pop(pos.symbol, None)
        state.cooldowns[pos.symbol] = now + cooldown_minutes(self.cfg, pnl) * 60
        self.marks.pop(pos.symbol, None)
        self._next_backup_check.pop(pos.symbol, None)
        self.store.save(state)
        try:
            logged = self.trade_log.append(pos, exit_price, proceeds, pos.buy_fee_usdt + sell_fee, reason.value, now)
        except Exception as exc:  # noqa: BLE001 - kayıt hatası satış bildirimini / kill-switch'i engellemesin
            log.error("İşlem kaydı yazılamadı: %s", exc)
            logged = False
        if not logged:
            self.notifier.send_error("tradelog-locked", Messages.warning(
                f"İşlem kaydı dosyası ({self.trade_log.path.name}) yazılamadı — Excel'de açık olabilir. "
                f"Kapattığınızda bekleyen kayıtlar otomatik yazılacak."), cooldown_sec=3600)
        self._sync_stream_symbols()

        self.notifier.notify(Messages.sell(
            reason.value, pos, exit_price, proceeds, sell_fee, pnl, pct, now - pos.opened_at,
            state.capital, state.realized_pnl, state.trades, state.wins, state.losses, self.cfg, estimated,
            open_positions=len(state.positions)), "close", critical=reason.value in CRITICAL_EXITS)
        self._after_close(pos.symbol, pnl, reason, now)

    # ================================================================ kill-switch
    def trigger_kill(self, total_pnl: float) -> None:
        """Kill-switch'in son adımı: tüm pozisyonlar satıldı, bot kilitlenip kapanır."""
        if not self.state.killed:
            self._mark_killed(total_pnl)
        balance = self.state.start_capital + total_pnl
        self.notifier.send(Messages.kill_switch(total_pnl, balance, self.cfg, self.loss_limit, self.loss_pct, []),
                           critical=True)
        self.running = False

    # ================================================================ mutabakat
    def _reconcile_positions_on_startup(self) -> None:
        """Bot kapalıyken olanları Binance ile karşılaştırır (yedek stop çalıştı mı, elle satıldı mı?)."""
        since = self.state.last_alive or 0.0
        for pos in list(self.state.positions.values()):
            if self.exchange.is_paper:
                if pos.backup_order_id is not None and since:
                    st = self.exchange.check_backup_offline(pos.symbol, pos.backup_qty, pos.backup_stop_price,
                                                            max(since, pos.opened_at))
                    if st.status == "FILLED" and st.fill:
                        self._close_with_fills(pos, [st.fill], ExitReason.BACKUP_STOP)
                        continue
                self.notifier.send(Messages.position_resumed(pos))
                continue

            if pos.backup_order_id is not None:
                st = self.exchange.backup_stop_status(pos.symbol, pos.backup_order_id)
                if st.status == "FILLED" and st.fill:
                    self._close_with_fills(pos, [st.fill], ExitReason.BACKUP_STOP)
                    continue
                if st.status == "GONE":
                    if st.fill:
                        self._realize_partial(pos, st.fill)
                    pos.backup_order_id = None
            total = self.exchange.total_balance(pos.base_asset)
            if self.exchange.sellable_qty(pos.symbol, min(pos.qty, total)) <= 0:
                self._handle_missing_balance(pos, ExitReason.EXTERNAL, [])
                continue
            if pos.backup_order_id is None:
                self._sync_with_wallet(pos)
                self._place_backup(pos)
            self.notifier.send(Messages.position_resumed(pos))
        self.store.save(self.state)

    def _process_pending_orders(self, now: float) -> None:
        """İnternet kesintisi yüzünden sonucu bilinmeyen alım emirlerini Binance'ten öğrenir."""
        if not self.state.pending_orders or now < self._next_pending_check:
            return
        self._next_pending_check = now + PENDING_CHECK_SEC
        for client_id, info in list(self.state.pending_orders.items()):
            symbol = info["symbol"]
            try:
                fill = self.exchange.recover_order(symbol, client_id, info.get("side", "BUY"))
            except (OrderError, requests.RequestException, BinanceAPIError) as exc:
                log.warning("Belirsiz emir (%s) sorgulanamadı: %s", symbol, exc)
                continue
            if fill is not None:
                self._rotation_pending_resolved(info, now)   # Bayrak pozisyonla aynı kayıtta diske yazılsın
                if symbol not in self.state.positions:
                    self.notifier.send(f"♻️ <b>Belirsiz kalan {esc(symbol)} alımı gerçekleşmiş</b> — "
                                       f"pozisyon takibe alındı.", critical=True)
                    # Kayıt pozisyon state'e girerken silinir (arada durdurulursa sorgu tekrarlanır)
                    self._register_position(symbol, fill, info.get("note", ""), float(info.get("score") or 0),
                                            info.get("metrics") or {}, info, int(info.get("slots") or 1),
                                            client_id)
                self.state.pending_orders.pop(client_id, None)
            elif now - float(info.get("created_at", now)) > PENDING_GIVE_UP_SEC:
                log.info("Belirsiz %s emri Binance'te bulunamadı; hiç gerçekleşmemiş sayılıyor.", symbol)
                del self.state.pending_orders[client_id]
                self._rotation_pending_resolved(info, now)
            self.store.save(self.state)

    def _rotation_pending_resolved(self, info: dict, now: float) -> None:
        """Rotasyonda sonucu belirsiz alım netleşti: dengeleme günündeysek kalan boş yerler doldurulsun."""
        if (getattr(self.strategy, "is_rotation", False) and info.get("side", "BUY") == "BUY"
                and self.rotation_day(now) == self.state.rotation_plan_day):
            self.state.rotation_refill = True

    # ================================================================ komisyon / BNB indirimi
    def _check_commission(self) -> None:
        """Gerçek komisyonu (BNB indirimi dahil) FEE_RATE ile karşılaştırır ve BNB bakiyesini izler.

        Sadece OKUR ve uyarır: BNB satın almaz, Binance'teki "BNB ile komisyon öde" ayarını değiştirmez.
        Açılışta ve FEE_CHECK_SEC'te bir çalışır. FEE_RATE karşılaştırması (ve BNBUSDT'nin işlem dışı
        bırakılması) her zaman yapılır; BNB_FEE_CHECK=false yalnızca BNB bakiyesi bildirimlerini kapatır.
        """
        if self.exchange.is_paper:
            return
        try:
            info = self.refresh_fee_info()
            if info is not None:
                self._fee_rate_notice(info)
                self._bnb_fees_notice(info)
                if self.cfg.bnb_fee_check:
                    self._bnb_balance_notice(info)
        except Exception as exc:  # noqa: BLE001 - izleme hatası işlemleri etkilemesin
            log.debug("Komisyon/BNB kontrolü yapılamadı: %s", exc)

    def refresh_fee_info(self) -> Optional[FeeInfo]:
        """Komisyon / BNB durumunu Binance'ten okuyup saklar (hata olursa None, eski bilgi korunur)."""
        try:
            info = self.exchange.fee_info(f"BTC{self.cfg.quote_asset}")
        except Exception as exc:  # noqa: BLE001
            log.debug("Komisyon oranı okunamadı: %s", exc)
            return None
        if info is not None:
            self.fee_info = info
        return info

    def position_size(self) -> float:
        """Bir pozisyonun yaklaşık büyüklüğü (BNB'nin kaç işleme yeteceğini tahmin etmek için)."""
        return self.state.capital / max(1, self.slots())

    def _fee_rate_notice(self, info: FeeInfo) -> None:
        size = self.position_size()
        cfg_rate, real = self.cfg.fee_rate, info.rate_for(size)
        status = info.status_for(cfg_rate, size)
        if status != "conservative":
            self._fee_noted_rate = None  # Durum sonra tekrar değişirse bilgi notu yeniden verilsin
        if status == "conservative":
            # Zararsız (bot temkinli hesaplıyor): her oran için yalnızca bir kez, sessiz bildirim
            if self._fee_noted_rate is not None and abs(self._fee_noted_rate - real) <= 1e-9:
                return
            self._fee_noted_rate = real
            why = (f"BNB ile komisyon ödeme açık, %{info.discount * 100:g} indirim" if info.covers(size)
                   else "hesabınızın oranı daha düşük")
            self.notifier.send(Messages.info(
                f"Gerçek işlem komisyonunuz {fmt_rate(real)} ({why}); bot ise FEE_RATE {fmt_rate(cfg_rate)} ile "
                f"hesaplıyor. Bu temkinli bir hesap: gerçek kâr/zarar biraz daha iyi çıkar. İsterseniz .env "
                f"dosyasında FEE_RATE={real:g} yapabilirsiniz."), silent=True)
        elif status == "optimistic":
            if info.covers(size):
                why, advice = "hesabınızın oranı ayardan yüksek", f".env dosyasında FEE_RATE={real:g} yapın."
            elif info.bnb_enabled:
                why = "BNB bakiyeniz bitmiş ya da bir işlemin komisyonuna yetmiyor, indirim uygulanmıyor"
                advice = f"Binance'te biraz BNB alın ya da .env dosyasında FEE_RATE={info.taker:g} yapın."
            else:
                why = "Binance'te BNB ile komisyon ödeme kapalı"
                advice = (f".env dosyasında FEE_RATE={info.taker:g} yapın ya da Binance'te \"BNB ile komisyon "
                          f"öde\" seçeneğini açıp biraz BNB alın.")
            # Hesaplar gerçekten yanlış olduğu için önemli uyarı (sessiz saatlerde de gelir, 6 saatte bir)
            self.notifier.send_error("fee-optimistic", Messages.warning(
                f"Gerçek işlem komisyonu {fmt_rate(real)}, ayardaki FEE_RATE ise {fmt_rate(cfg_rate)} ({why}). "
                f"Net kâr al / zarar kes hesapları fazla iyimser: satışlar hedeflenenden biraz düşük kârla "
                f"veya biraz büyük zararla gerçekleşir. {advice}"), cooldown_sec=FEE_WARN_COOLDOWN_SEC)

    def _bnb_fees_notice(self, info: FeeInfo) -> None:
        """BNB_FEES=true (toz sınırı uygulanmıyor) ama komisyon BNB'den kesilmiyorsa bir kez uyarır: komisyon alınan
        coinden kesilir ve BTC gibi LOT adımı büyük coinlerde satılamayan küçük bir kalıntı (toz) oluşur."""
        if not self.cfg.bnb_fees or self._bnb_fees_noted:
            return
        if info.bnb_enabled and info.covers(self.position_size()):
            return
        self._bnb_fees_noted = True
        why = ("Binance'te \"BNB ile komisyon öde\" kapalı" if not info.bnb_enabled
               else "Spot'taki BNB bir işlemin komisyonuna yetmiyor")
        self.notifier.send(Messages.warning(
            f"BNB_FEES=true ama {why}: komisyon alınan coinden kesilir, BTC gibi pahalı coinlerde satılamayan küçük "
            "bir kalıntı (toz) kalabilir. Binance'te biraz BNB alın ya da .env dosyasında BNB_FEES=false yapın."))

    def _bnb_balance_notice(self, info: FeeInfo) -> None:
        """BNB indirimi açıkken BNB azaldıysa/bittiyse SESSİZ bilgi verir. Bot otomatik BNB ALMAZ.

        Hesaplar doğru olduğu sürece (FEE_RATE gerçek oranla uyumlu) bu yalnızca bir fırsat bilgisidir:
        BNB bitti bildirimi süreç başına bir kez, "azalıyor" bildirimi en fazla günde bir kez gönderilir.
        """
        if not info.bnb_enabled or info.discount <= 0 or self.cfg.bnb_min_trades <= 0:
            return
        size = self.position_size()
        left = info.bnb_trades(size)
        if left is None or left >= self.cfg.bnb_min_trades:
            self._bnb_empty_noted = False
            return
        hint = (" Bot otomatik BNB almaz (canlı modda BNB_TOPUP_USDT > 0 ise düğmeyle sorar). (Bu bildirimi "
                "kapatmak için .env: BNB_MIN_TRADES=0)")
        if not info.covers(size):
            if info.status_for(self.cfg.fee_rate, size) == "optimistic":
                return  # FEE_RATE uyarısı BNB'nin bittiğini zaten söyledi
            if self._bnb_empty_noted:
                return
            self._bnb_empty_noted = True
            what = "Spot'ta BNB yok" if info.bnb_free <= 0 else "Spot'taki BNB bir işlemin komisyonuna yetmiyor"
            self.smart.notice_with_bnb_offer(Messages.info(
                f"Binance'te BNB ile komisyon ödeme açık ama {what}: "
                f"komisyon şu an {fmt_rate(info.taker)} olarak kesiliyor (bot da böyle hesaplıyor, sorun yok). "
                f"%25 indirim isterseniz Binance'te biraz BNB alın." + hint))
            return
        self._bnb_empty_noted = False
        now = self.time_fn()
        if now < self._next_bnb_low_notice:
            return
        self._next_bnb_low_notice = now + BNB_LOW_NOTICE_SEC
        enough = f"yaklaşık {int(left)} işlemlik (alış + satış) kaldı" if left >= 1 else "bir al-sata yetmiyor"
        after = (f" O zaman .env'de FEE_RATE={info.taker:g} yapın." if self.cfg.fee_rate < info.taker - 1e-9 else "")
        self.smart.notice_with_bnb_offer(Messages.warning(
            f"BNB bakiyeniz ({fmt_qty(info.bnb_free)} BNB ≈ {fmt_usdt(info.bnb_value)} {self.cfg.quote_asset}) "
            f"{enough}; bitince komisyon {fmt_rate(info.taker)}'a döner.{after} Devam etmesini isterseniz "
            f"Binance'te biraz BNB alın." + hint))

    def fee_status_line(self) -> str:
        """/ayarlar için BNB indirimi durumu (son kontrolden; yoksa bir kez okunur)."""
        fee_rate = f"FEE_RATE {fmt_rate(self.cfg.fee_rate)}"
        if self.exchange.is_paper:
            return f"🪙 BNB indirimi: test modunda kullanılmaz | {fee_rate}"
        info = self.fee_info or self.refresh_fee_info()
        if info is None:
            return f"🪙 BNB indirimi: bilinmiyor (Binance'ten okunamadı) | {fee_rate}"
        size = self.position_size()
        status = Messages.bnb_status(info, size, self.cfg.quote_asset)
        taker = "" if info.bnb_enabled else f" (komisyon {fmt_rate(info.taker)})"
        off = "" if self.cfg.bnb_fee_check else " | BNB bildirimleri kapalı"
        return f"🪙 {status}{taker} | {fee_rate}{off}"

    # ================================================================ kesinti / internet
    def _check_loop_gap(self, now: float) -> None:
        """Döngü uzun süre durduysa (bilgisayar uykuya geçtiyse) haber ver."""
        if self.last_loop_at and now - self.last_loop_at > self.cfg.outage_alert_sec:
            minutes = (now - self.last_loop_at) / 60
            log.warning("Döngü %.1f dk durmuştu (uyku/askıya alma?)", minutes)
            self.notifier.send(Messages.outage(minutes, list(self.state.positions)),
                               critical=bool(self.state.positions))
            self.next_scan_at = 0.0

    def _network_down(self) -> None:
        if self.net_down_since is None:
            self.net_down_since = self.time_fn()
            log.warning("İnternet / Binance bağlantısı kesildi")

    def _network_ok(self) -> None:
        if self.net_down_since is None:
            return
        minutes = (self.time_fn() - self.net_down_since) / 60
        self.net_down_since = None
        if minutes >= 1:
            lines = []
            for pos in self.state.positions.values():
                m = self.marks.get(pos.symbol)
                if m:
                    lines.append(f"📦 {esc(pos.symbol)}: {fmt_signed(m.pnl_pct)}% ({fmt_signed(m.pnl_usdt)} "
                                 f"{self.cfg.quote_asset})")
            self.notifier.send(Messages.network_restored(minutes, lines), critical=bool(self.state.positions))

    # ================================================================ raporlar
    def _record_scan(self, result: ScanResult, now: float) -> None:
        self.last_scan, self.last_scan_at = result, now
        today = date.fromtimestamp(now).isoformat()
        if self.day_stats.day != today:
            self.day_stats = DayScanStats(day=today)
        self.day_stats.scans += 1
        self.day_stats.signals += len(result.signals)
        self.day_stats.rejects.update(result.rejects)
        if not result.market_ok:
            key = "BTC filtresi" if result.market_note.startswith("BTC") else result.market_note.split(":")[0]
            self.day_stats.rejects[key] += 1

    def scan_report(self) -> str:
        if getattr(self.strategy, "is_rotation", False):
            if self.last_rotation is None:
                day = self.state.last_rebalance_day
                when = ("henüz yapılmadı" if day < 0 else
                        f"son dengeleme {(date(1970, 1, 1) + timedelta(days=day)).strftime('%d.%m.%Y')} (UTC)")
                return f"🔄 Rotasyon: bu oturumda yeniden dengeleme yapılmadı ({when})."
            age = (self.time_fn() - self.last_rotation_at) / 3600
            return f"{self.last_rotation}\n<i>({age:.0f} saat önce)</i>"
        r = self.last_scan
        if r is None:
            return "🔍 Henüz tarama yapılmadı." + self._div_scan_line()
        age = self.time_fn() - self.last_scan_at
        lines = [f"🔍 <b>Son Tarama</b> ({age:.0f} sn önce)"]
        if self.state.paused:
            lines.append("⏸️ Yeni alımlar durdurulmuş (/devam).")
        if self.free_slots() == 0:
            lines.append(f"📦 Pozisyon yerleri dolu ({len(self.state.positions)}/{self.slots()}); "
                         f"yer açılınca tarama yapılır.")
        if not r.market_ok:
            lines.append(f"⛔ {esc(r.market_note)}")
        else:
            lines.append(f"🌡️ Piyasa: {r.regime}" + (f" — {esc(r.regime_note)}" if r.regime_note else ""))
            lines.append(f"Aday: {r.candidates} | Analiz: {r.analyzed} | Sinyal: {len(r.signals)}")
            if r.signals:
                lines.append("En iyi sinyaller: " + ", ".join(f"{esc(s.symbol)} ({s.score:.1f})" for s in r.signals[:3]))
            if r.rejects:
                lines.append("En sık eleme nedenleri: " + ", ".join(
                    f"{esc(k)}: {v}" for k, v in r.rejects.most_common(5)))
            if r.near_misses:
                lines.append("Örnekler: " + ", ".join(f"{esc(s)} ({esc(w)})" for s, w in r.near_misses[:5]))
        d = self.day_stats
        if d.scans:
            lines.append(f"\n📅 Bugün: {d.scans} tarama, {d.signals} sinyal, {d.buys} alım")
            if d.rejects:
                lines.append("Bugün en sık: " + ", ".join(f"{esc(k)}: {v}" for k, v in d.rejects.most_common(3)))
        return "\n".join(lines) + self._div_scan_line()

    def _div_scan_line(self) -> str:
        """RSI_DIV_TRADE açıksa son uyumsuzluk taramasının özeti (ana stratejinin yanında, saatte bir)."""
        if not divergence_trading(self.cfg):
            return ""
        mode = "sorarak" if self.cfg.rsi_div_trade == "ask" else "otomatik"
        r = self.last_div_scan
        if r is None:
            return f"\n\n📐 Uyumsuzluk alımı ({mode}): bu oturumda henüz taranmadı (saatlik mum kapanışından sonra)."
        age = (self.time_fn() - self.last_div_scan_at) / 60
        text = (f"\n\n📐 <b>Uyumsuzluk alımı</b> ({mode}, {age:.0f} dk önce): aday {r.candidates}, analiz "
                f"{r.analyzed}, sinyal {len(r.signals)}")
        if not r.market_ok:
            text += f" — ⛔ {esc(r.market_note)}"
        elif r.signals:
            text += " (" + ", ".join(esc(sig.symbol) for sig in r.signals[:3]) + ")"
        elif r.rejects:
            text += " | eleme: " + ", ".join(f"{esc(k)}: {v}" for k, v in r.rejects.most_common(3))
        return text

    def stats_text(self) -> str:
        import stats
        trades = stats.load_trades(self.trade_log.path)
        if not trades:
            return "📭 Henüz kapanmış işlem yok; istatistikler ilk işlemlerden sonra oluşur."
        st = stats.compute_stats(trades)
        prob = stats.kill_switch_probability([t["kar_zarar_usdt"] for t in trades], self.state.realized_pnl,
                                             self.loss_limit)
        return stats.format_stats(st, self.cfg.quote_asset, self.state.realized_pnl, self.loss_limit,
                                  0.0 if is_rotation(self.cfg.strategy) else self.cfg.take_profit_pct,
                                  self.cfg.stop_loss_pct, prob)

    def period_report(self, start: datetime, end: datetime, title: str) -> str:
        import stats
        trades = stats.load_trades(self.trade_log.path)
        unrealized = self.unrealized()
        extra = [f"💰 Bakiye: {fmt_usdt(self.state.capital + unrealized)} {self.cfg.quote_asset} | "
                 f"Toplam K/Z: {fmt_signed(self.state.realized_pnl + unrealized)}",
                 f"☠️ Kill-switch'e kalan: {fmt_usdt(self.loss_limit + self.state.realized_pnl + unrealized)} "
                 f"{self.cfg.quote_asset}"]
        d = self.day_stats
        report_day = (end - timedelta(seconds=1)).date().isoformat()
        if d.scans and d.day == report_day:
            reasons = ", ".join(f"{k}: {v}" for k, v in d.rejects.most_common(3))
            extra.append(f"🔍 {d.scans} tarama, {d.signals} sinyal, {d.buys} alım"
                         + (f" | en sık eleme: {esc(reasons)}" if reasons else ""))
        return stats.format_period_report(trades, start, end, title, self.cfg.quote_asset, extra)

    def _report_minute(self, weekly: bool = False) -> Optional[int]:
        """Rapor saati (gece yarısından dakika). Günlük rapor kapalıysa haftalık rapor 23:55'te gider."""
        text = self.cfg.daily_report_time or (WEEKLY_DEFAULT_TIME if weekly else "")
        return parse_hhmm(text) if text else None

    @staticmethod
    def _last_report_day(now: float, minute: int) -> date:
        """Rapor saati geçmişse bugün, geçmemişse dün."""
        dt = datetime.fromtimestamp(now)
        return dt.date() if dt.hour * 60 + dt.minute >= minute else dt.date() - timedelta(days=1)

    @staticmethod
    def _report_window(day: date, minute: int, days: int):
        """Rapor penceresi: bir önceki rapor anından bu rapor anına kadar (hiçbir işlem arada kalmaz)."""
        end = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
        return end - timedelta(days=days), end

    def _init_report_markers(self, now: float) -> None:
        """İlk çalıştırmada geçmiş günler için rapor gönderilmesin."""
        target = self._daily_target(now)
        if target and not self.state.last_daily_report:
            self.state.last_daily_report = target.isoformat()
        week = self._weekly_target(now)
        if week and not self.state.last_weekly_report:
            self.state.last_weekly_report = _week_key(week)

    def _daily_target(self, now: float) -> Optional[date]:
        """Raporu gönderilmesi gereken en son gün."""
        minute = self._report_minute()
        return None if minute is None else self._last_report_day(now, minute)

    def _weekly_target(self, now: float) -> Optional[date]:
        """Haftalık raporu gönderilmesi gereken en son pazar günü."""
        if not self.cfg.weekly_report:
            return None
        last_day = self._last_report_day(now, self._report_minute(weekly=True))
        return last_day - timedelta(days=(last_day.weekday() - 6) % 7)

    def _maybe_reports(self, now: float) -> None:
        target = self._daily_target(now)
        if target and self.state.last_daily_report < target.isoformat():
            start, end = self._report_window(target, self._report_minute(), 1)
            label = (end - timedelta(hours=12)).date()  # Pencerenin büyük kısmının düştüğü gün
            self.notifier.notify(self.period_report(start, end, f"📅 Günlük Özet ({label:%d.%m.%Y})"), "routine",
                                 silent=True)
            self.state.last_daily_report = target.isoformat()
            self.store.save(self.state)
        sunday = self._weekly_target(now)
        if sunday and self.state.last_weekly_report < _week_key(sunday):
            start, end = self._report_window(sunday, self._report_minute(weekly=True), 7)
            first_day = (end - timedelta(days=6, hours=12)).date()
            self.notifier.notify(self.period_report(start, end,
                                                    f"🗓️ Haftalık Özet ({first_day:%d.%m} – {sunday:%d.%m.%Y})"),
                                 "routine", silent=True)
            self.state.last_weekly_report = _week_key(sunday)
            self.store.save(self.state)

    def _periodic(self, now: float) -> None:
        if now >= self.next_status_at:
            self.next_status_at = now + self.cfg.status_report_minutes * 60
            self.send_status(routine=True)
        self._maybe_reports(now)
        self._check_freeze_end(now)
        self._check_ip()
        self.smart.update_board(now)
        self.smart.check_liquidations(now)
        self.divergence.maybe_start(self.cfg, self.exchange.rules, self.notifier, now)
        self.backtests.poll(now)
        if self.equity_log is not None and now >= self.next_equity_at:
            self.next_equity_at = now + self.cfg.equity_log_minutes * 60
            self.equity_log.append(now, self.state.capital, self.unrealized(), len(self.state.positions))
        coin_filter = getattr(self.strategy, "coin_filter", None)
        if coin_filter is not None and self.state.positions and now >= self._next_delist_warn:
            self._next_delist_warn = now + DELIST_WARN_SEC
            for text in coin_filter.open_position_warnings(list(self.state.positions)):
                self.notifier.send_error(f"delist-{text[:20]}", Messages.warning(text), cooldown_sec=6 * 3600)
        if now >= self._next_fee_check:
            self._next_fee_check = now + FEE_CHECK_SEC
            self._check_commission()
        if now - self._last_alive_save >= ALIVE_SAVE_SEC:
            self._last_alive_save = now
            self.trade_log.flush_pending()
            self.state.last_alive = now
            self.store.save(self.state)

    def send_status(self, silent: bool = True, routine: bool = False) -> None:
        state = self.state
        lines = []
        for pos in state.positions.values():
            m = self.marks.get(pos.symbol)
            if m:
                lines.append(f"📦 <b>{pos.symbol}</b> {fmt_signed(m.pnl_pct)}% "
                             f"({fmt_signed(m.pnl_usdt)} {self.cfg.quote_asset}) | "
                             f"{fmt_duration(self.time_fn() - pos.opened_at)}")
        unrealized = self.unrealized()
        total = state.realized_pnl + unrealized
        text = Messages.status(
            state.capital + unrealized, total, state.trades, state.wins, state.losses, self.cfg,
            "\n".join(lines), self.loss_limit + total, len(state.positions), self.target_slots())
        if routine:   # Periyodik rapor bildirim moduna tabidir; /durum cevabı her zaman gider
            self.notifier.notify(text, "routine", silent=silent)
        else:
            self.notifier.send(text, silent=silent)

    # ================================================================ risk sigortaları
    def entries_frozen(self, now: float) -> bool:
        return self.state.freeze_until > now

    def freeze_note(self) -> str:
        """Alımlar sigorta nedeniyle dondurulmuşsa açıklaması, değilse ''."""
        if not self.entries_frozen(self.time_fn()):
            return ""
        until = datetime.fromtimestamp(self.state.freeze_until)
        return f"🧊 Yeni alımlar donduruldu (bitiş {until:%d.%m %H:%M}): {self.state.freeze_reason}"

    def unfreeze(self) -> None:
        self.state.freeze_until = 0.0
        self.state.freeze_reason = ""
        self.state.sl_streak = 0
        self.store.save(self.state)

    def _freeze(self, until: float, reason: str) -> None:
        state = self.state
        if until <= state.freeze_until:
            return
        state.freeze_until, state.freeze_reason = until, reason
        self.store.save(state)
        log.warning("Yeni alımlar donduruldu (bitiş %s): %s", datetime.fromtimestamp(until), reason)
        self.notifier.send(
            f"🧊 <b>Yeni alımlar donduruldu</b> (bitiş {datetime.fromtimestamp(until):%d.%m %H:%M}): {esc(reason)}.\n"
            f"Açık pozisyonların takibi sürüyor. Erken açmak için /menu → Risk → Dondurmayı kaldır.")

    def _check_freeze_end(self, now: float) -> None:
        state = self.state
        if state.freeze_until and now >= state.freeze_until:
            state.freeze_until, state.freeze_reason = 0.0, ""
            self.store.save(state)
            self.notifier.send("▶️ Alım dondurması sona erdi; bot yeniden fırsat arıyor.")

    def _day_key(self, now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def today_pnl(self) -> float:
        """Bugünkü (yerel saat) gerçekleşen net K/Z."""
        return self.state.day_pnl if self.state.day_key == self._day_key(self.time_fn()) else 0.0

    def daily_loss_usdt(self) -> float:
        return self.state.start_capital * self.cfg.daily_loss_limit_pct / 100

    def _after_close(self, symbol: str, pnl: float, reason: ExitReason, now: float) -> None:
        """Kapanan işlemden sonra: günlük limit, zarar serisi sigortası ve coin karnesi."""
        state, cfg = self.state, self.cfg
        if is_rotation(cfg.strategy):   # Rotasyon kendi takvimiyle alır; sigortalar 5 dk / swing taraması içindir
            return
        day = self._day_key(now)
        if state.day_key != day:
            state.day_key, state.day_pnl = day, 0.0
        state.day_pnl += pnl
        midnight = (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)).timestamp()
        q = cfg.quote_asset
        if cfg.daily_loss_limit_pct > 0 and state.day_pnl <= -self.daily_loss_usdt() + 1e-9:
            self._freeze(midnight, f"günlük zarar limiti (bugün {state.day_pnl:+.2f} {q}, sınır "
                                   f"-%{cfg.daily_loss_limit_pct:g})")
        if (cfg.daily_profit_target_pct > 0
                and state.day_pnl >= state.start_capital * cfg.daily_profit_target_pct / 100):
            self._freeze(midnight, f"günlük kâr hedefine ulaşıldı (bugün {state.day_pnl:+.2f} {q}); kâr korunuyor")
        if reason in STOP_EXITS:
            state.sl_streak += 1
            state.coin_sl_streak[symbol] = state.coin_sl_streak.get(symbol, 0) + 1
        elif reason not in NEUTRAL_EXITS:
            state.sl_streak = 0
            state.coin_sl_streak.pop(symbol, None)
        if cfg.loss_streak_limit > 0 and state.sl_streak >= cfg.loss_streak_limit:
            streak, state.sl_streak = state.sl_streak, 0
            self._freeze(now + cfg.loss_streak_pause_hours * 3600,
                         f"üst üste {streak} zarar kes (piyasa stratejiye uygun değil olabilir)")
        if cfg.coin_sl_limit > 0 and state.coin_sl_streak.get(symbol, 0) >= cfg.coin_sl_limit:
            count = state.coin_sl_streak.pop(symbol)
            self.notifier.send_menu(
                f"📒 <b>Coin karnesi:</b> {esc(symbol)} son {count} işlemde üst üste zarar kesle kapandı.\n"
                f"24 saat kara listeye alınsın mı? (Bu sürede {esc(symbol)} alınmaz.)",
                [[("⛔ Evet, 24 saat", f"bl:{symbol}"), ("Hayır", f"nobl:{symbol}")]])
        self.store.save(state)

    def _track_slippage(self, slip_pct: float) -> None:
        """Kayma alarmı: son SLIPPAGE_WINDOW alımın ortalama kayması SLIPPAGE_ALERT_PCT'yi aşarsa uyarır."""
        hist = self.state.slippage_hist
        hist.append(round(slip_pct, 4))
        del hist[:-SLIPPAGE_WINDOW]
        limit = self.cfg.slippage_alert_pct
        if limit <= 0 or len(hist) < SLIPPAGE_MIN_SAMPLES:
            return
        avg = sum(hist) / len(hist)
        if avg > limit:
            self.notifier.send_error("slippage-avg", Messages.warning(
                f"Son {len(hist)} alımda ortalama kayma %{avg:.2f} (alışlar beklenen fiyattan pahalı gerçekleşiyor, "
                f"sınır %{limit:g}). Sığ emir defterli coinlerde olur: MAX_SPREAD_PCT'yi düşürmeyi, "
                f"MIN_QUOTE_VOLUME_24H'yi artırmayı veya /menu → Filtreler'den defter filtrelerini açmayı düşünün."),
                cooldown_sec=SLIPPAGE_ALERT_COOLDOWN_SEC)

    def _check_ip(self) -> None:
        """IP izleyicisinin bulduğu yeni dış IP'yi kayıtlı olanla karşılaştırır; değiştiyse alarm."""
        if self.ip_monitor is None:
            return
        for family, ip in self.ip_monitor.poll():
            old = self.state.public_ips.get(family)
            self.state.public_ips[family] = ip
            self.store.save(self.state)
            if old is None:
                log.info("Sunucunun dış %s adresi: %s", family, ip)
            elif old != ip:
                log.warning("Sunucunun dış %s adresi değişti: %s → %s", family, old, ip)
                self.notifier.send(
                    f"🚨 <b>Sunucunun dış IP adresi değişti</b> ({family}): <code>{esc(old)}</code> → "
                    f"<code>{esc(ip)}</code>\nBinance API anahtarında IP kısıtlaması varsa yeni adresi Binance → "
                    f"API Yönetimi'nde ekleyin; yoksa emirler -2015 hatasıyla reddedilir.", critical=True)

    # ================================================================ yardımcılar
    def _next_sleep(self) -> float:
        if self.state.positions or self.smart.entries:
            if self.price_stream is not None and self.price_stream.connected:
                return self.cfg.ws_position_check_sec
            return self.cfg.position_check_sec
        return max(1.0, min(self.next_scan_at - self.time_fn(), self.cfg.scan_interval_sec))

    def _handle_error(self, exc: Exception, key: str) -> None:
        self._error_streak += 1
        log.error("Hata (%d. kez üst üste): %s", self._error_streak, exc)
        self.notifier.send_error(key, Messages.error(f"{type(exc).__name__}: {exc}"))
        # Açık pozisyon varken bağlantıyı sık dene ki internet gelir gelmez kontrol yapılsın
        cap = 10 if self.state.positions else 60
        self._sleep(min(cap, 5 * self._error_streak))

    def _wait_rate_limit(self, seconds: float) -> None:
        """Binance istek yasağı: süre dolana kadar Binance'e HİÇ istek atılmaz (yasak uzamasın).
        Gelen Telegram komutları kuyrukta bekler ve yasak bitince işlenir."""
        until = time.monotonic() + max(0.0, seconds)
        while self.running:
            left = until - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 1.0))
        self.last_loop_at = self.time_fn()  # Bilinçli bekleme "bot çalışmadı" uyarısı üretmesin

    def _sleep(self, seconds: float) -> None:
        """Bekler; Telegram komutu gelirse beklemeyi erken bitirir."""
        seconds = max(0.0, seconds)
        if self.command_listener is not None:
            self.command_listener.wakeup.wait(seconds)
        else:
            time.sleep(seconds)


def _scaled_fill(fill: Fill, qty: float) -> Fill:
    """Bot dışı satışta kullanıcı başka coin de sattıysa, sadece pozisyon miktarı kadarını al."""
    if fill.qty <= 0 or qty <= 0 or qty >= fill.qty:
        return fill
    ratio = qty / fill.qty
    return Fill(fill.symbol, fill.side, qty, fill.avg_price, fill.usdt * ratio, fill.fee_usdt * ratio, fill.order_id)


def _week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"
