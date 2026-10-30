"""
Akıllı pozisyon yönetimi ve akıllı giriş (canlı bot). Karar kuralları position_rules.py'dedir (backtest de onları
kullanır); burası Binance'ten veri çeker, Telegram'dan sorar ve emirleri verir.

Açık pozisyonlar (her kontrolde, rotasyon hariç):
  - TP_EXTEND: Kâr Al'a gelindiğinde momentum güçlüyse satmaz; zirveden TP_EXTEND_GAP_PCT geri dönünce satar.
  - TIME_EXIT_MODE: TIME_EXIT_MINUTES dolunca pozisyon yataysa "başabaşta satılsın mı?" (ask) / başabaşta sat (auto).
  - ESCAPE_MODE: net zarar ESCAPE_TRIGGER_PCT'ye inip hacimli satış baskısı görülürse "çıkılsın mı?" / çık.
  - DCA_MODE: net zarar DCA_TRIGGER_PCT'ye inince pozisyonun yedek akçesiyle ekleme (ortalama düşürme).
  - PYRAMID_MODE: net kâr PYRAMID_TRIGGER_PCT'ye çıkıp momentum güçlüyse yedek akçeyle ekleme; ekledikten sonra
    birleşik pozisyon zarara dönmeden satılır (kilit).
Her soru pozisyon başına bir kez sorulur ve QUESTION_TTL_SEC sonra geçersiz olur. COMMAND_PIN ayarlıysa düğmeye
basmak yetmez, "/evet PIN" istenir.

Yeni girişler: SIGNAL_APPROVAL (Telegram onayı), TRAILING_BUY (dipten dönüşte alım). Ayrıca canlı K/Z panosu,
likidasyon bildirimi, pozitif RSI uyumsuzluğu bildirimi, BNB takviyesi ve tozları BNB'ye çevirme.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

import requests

from binance_client import BinanceAPIError, RateLimitError
from exchange import OrderError
from logger import get_logger
from position_rules import (REVERSAL_ROWS, atr_pct, atr_targets, is_flat, momentum_strong, reversal_signal,
                            trailing_buy_trigger, volume_breakdown)
from risk import ExitReason, net_exit_value, plan_entry, pnl_pct, position_sl, position_tp, target_exits
from telegram_bot import LINE, Messages, esc, fmt_duration, fmt_price, fmt_qty, fmt_signed, fmt_usdt

if TYPE_CHECKING:  # pragma: no cover
    from trader import TradingBot

log = get_logger()

QUESTION_TTL_SEC = 900            # Telegram soruları bu süre sonra geçersiz
MOMENTUM_RECHECK_SEC = 300        # Piramit için momentum en fazla bu sıklıkla kontrol edilir
ESCAPE_RECHECK_SEC = 60           # Kaçış için hacim en fazla bu sıklıkla kontrol edilir
APPROVAL_MAX_DRIFT_PCT = 1.0      # Onay gelene kadar fiyat bundan fazla yükseldiyse alınmaz
FORCED_APPROVAL_MIN_SEC = 900     # Saatlik sinyalin onayı (RSI_DIV_TRADE=ask) en az bu kadar beklenir
LIQ_WINDOW_SEC = 300
LIQ_CHECK_SEC = 60
LIQ_ALERT_COOLDOWN_SEC = 3600
BNB_SYMBOL_SUFFIX = "BNB"


@dataclass
class Question:
    kind: str             # time | escape | dca | pyramid | buy | bnb | dust
    symbol: str = ""
    opened_at: int = 0    # Pozisyon soruları: aynı paritede sonradan açılan başka pozisyona uygulanmasın
    expires: float = 0.0
    data: dict = field(default_factory=dict)
    message_id: Optional[int] = None


@dataclass
class PendingEntry:
    kind: str             # trail (dipten dönüş bekleniyor) | approval (Telegram onayı bekleniyor)
    signal: object
    available: float      # Alım için ayrılan tutar (plan_entry'ye verilir; fiyat değişince yeniden planlanır)
    slots: int
    context: dict
    started: float
    expires: float
    low: float = 0.0      # trail: sinyalden beri görülen en düşük alış fiyatı
    token: str = ""       # approval: soru kimliği


class SmartManager:
    def __init__(self, bot: "TradingBot"):
        self.bot = bot
        self.questions: Dict[str, Question] = {}
        self.entries: Dict[str, PendingEntry] = {}
        self._next_check: Dict[str, float] = {}
        self.board: Optional[dict] = None
        self.liq_feed = None
        self._next_liq_check = 0.0
        self._liq_alerted: Dict[str, float] = {}

    @property
    def cfg(self):
        return self.bot.cfg

    @property
    def notifier(self):
        return self.bot.notifier

    def now(self) -> float:
        return self.bot.time_fn()

    # ================================================================ veri
    def _closed(self, symbol: str, interval: str, limit: int) -> List[list]:
        rows = self.bot.exchange.client.klines(symbol, interval, limit) or []
        return rows[:-1]

    def interval(self) -> str:
        """Stratejinin mum aralığı (5 dk stratejileri 5m, swing / ortalamaya dönüş 1h)."""
        return getattr(self.bot.strategy, "bar_interval", "5m")

    def momentum(self, symbol: str) -> bool:
        return momentum_strong(self._closed(symbol, self.interval(), 40))

    def reversal(self, symbol: str) -> Optional[str]:
        """Zarar kes uzatma için dönüş işareti (son REVERSAL_ROWS kapanmış mum; backtest de aynı sayıyla bakar)."""
        return reversal_signal(self._closed(symbol, self.interval(), REVERSAL_ROWS + 1), self.cfg.escape_volume_ratio)

    def hourly_atr(self, symbol: str, price: float) -> Optional[float]:
        return atr_pct(self._closed(symbol, "1h", 30), price)

    def _safe(self, fn, *args, default=None):
        """Veri hatası yönetimi durdurmasın (istek limiti yukarı iletilir)."""
        try:
            return fn(*args)
        except RateLimitError:
            raise
        except (requests.RequestException, BinanceAPIError, ValueError, IndexError, TypeError) as exc:
            log.debug("Akıllı yönetim verisi alınamadı (%s): %s", getattr(fn, "__name__", fn), exc)
            return default

    def _due(self, key: str, every: float) -> bool:
        now = self.now()
        if now < self._next_check.get(key, 0.0):
            return False
        self._next_check[key] = now + every
        return True

    # ================================================================ Telegram soruları
    def ask(self, question: Question, text: str, choices: List[tuple], ttl: int = QUESTION_TTL_SEC,
            silent: bool = False) -> str:
        """Düğmeli soru gönderir. choices: [(düğme yazısı, seçim)] → callback "q:<kimlik>:<seçim>"."""
        token = secrets.token_hex(4)
        question.expires = self.now() + ttl
        self.questions[token] = question
        for old, q in list(self.questions.items()):   # Süresi geçmiş soruları temizle
            if q.expires < self.now():
                self.questions.pop(old, None)

        def remember(mid: int) -> None:
            question.message_id = mid

        keyboard = [[(label, f"q:{token}:{choice}") for label, choice in choices]]
        limit = f"{ttl} sn" if ttl < 120 else f"{ttl // 60} dk"
        self.notifier.send_menu(text + f"\n<i>⏳ {limit} içinde cevaplanmazsa geçersiz olur.</i>", keyboard,
                                on_sent=remember, silent=silent)
        return token

    def answer(self, token: str, choice: str, message_id: Optional[int] = None, confirmed: bool = False) -> None:
        """Soru düğmesine basıldı (veya PIN'li onay geldi)."""
        q = self.questions.get(token)
        reply = self.bot.commands.reply
        if q is None or q.expires < self.now():
            self.questions.pop(token, None)
            reply("ℹ️ Bu sorunun süresi doldu veya zaten cevaplandı.")
            return
        acting = choice not in ("no", "wait")
        if acting and self.cfg.command_pin and not confirmed:
            from commands import PendingConfirm
            self.bot.commands._ask(PendingConfirm("question", f"{token}:{choice}"), "",
                                   "Bu işlem onaylansın mı? (COMMAND_PIN ayarlı)")
            return
        self.questions.pop(token, None)
        handler = getattr(self, f"_answer_{q.kind}")
        result = handler(q, choice)
        if result:
            self.bot.commands.reply_menu(result, [], message_id or q.message_id)

    def _live_position(self, q: Question):
        pos = self.bot.state.positions.get(q.symbol)
        if pos is None or int(pos.opened_at) != q.opened_at:
            return None
        return pos

    def _answer_time(self, q: Question, choice: str) -> str:
        pos = self._live_position(q)
        if pos is None:
            return f"ℹ️ {esc(q.symbol)} pozisyonu artık açık değil."
        if choice == "now":
            self.bot.close_position(pos, ExitReason.TIME_EXIT)
            return f"⚡ {esc(q.symbol)} satılıyor (süre sorusu)."
        if choice == "be":
            pos.exit_at_breakeven = True
            self.bot.store.save(self.bot.state)
            return f"⚖️ {esc(q.symbol)} net K/Z sıfıra (başabaşa) gelince satılacak."
        return f"⏳ {esc(q.symbol)} için normal kurallarla beklemeye devam."

    def _answer_escape(self, q: Question, choice: str) -> str:
        pos = self._live_position(q)
        if pos is None:
            return f"ℹ️ {esc(q.symbol)} pozisyonu artık açık değil."
        if choice == "sell":
            self.bot.close_position(pos, ExitReason.ESCAPE)
            return f"🏃 {esc(q.symbol)} satılıyor (erken kaçış)."
        return f"⏳ {esc(q.symbol)}: zarar kese kadar bekleniyor."

    def _answer_dca(self, q: Question, choice: str) -> str:
        pos = self._live_position(q)
        if pos is None:
            return f"ℹ️ {esc(q.symbol)} pozisyonu artık açık değil."
        if choice != "add":
            return f"👍 {esc(q.symbol)} için ekleme yapılmadı."
        return "" if self.add_to_position(pos, "dca") else f"⚠️ {esc(q.symbol)} için ekleme yapılamadı."

    _answer_pyramid = _answer_dca

    def _answer_buy(self, q: Question, choice: str) -> str:
        entry = self.entries.get(q.symbol)
        if entry is None or entry.kind != "approval" or entry.token != q.data.get("token"):
            return f"ℹ️ {esc(q.symbol)} sinyali artık geçerli değil."
        self.entries.pop(q.symbol, None)
        if choice != "yes":
            self.bot.day_stats.rejects["onay verilmedi"] += 1
            self._snooze(q.symbol)
            return f"❌ {esc(q.symbol)} alınmadı (bir süre tekrar sorulmayacak)."
        why = self._entry_blocked(q.symbol)
        if why:
            return f"⚠️ {esc(q.symbol)} alınmadı: {why}."
        _bid, ask = self.bot.bid_ask(q.symbol)
        start = entry.context.get("expected_ask") or ask
        if ask > start * (1 + APPROVAL_MAX_DRIFT_PCT / 100):
            return (f"⚠️ {esc(q.symbol)} alınmadı: fiyat onay beklerken %{(ask / start - 1) * 100:.1f} yükseldi "
                    f"(sınır %{APPROVAL_MAX_DRIFT_PCT:g}).")
        result = self._start_or_buy(entry.signal, entry.available, entry.slots, entry.context, ask)
        if result == "pending":
            return f"✅ Onaylandı: {esc(q.symbol)} için dipten dönüş bekleniyor (kademeli alım)."
        return "" if result == "opened" else f"⚠️ {esc(q.symbol)} alınamadı (ayrıntı yukarıda)."

    # ================================================================ yeni giriş
    def pending_count(self) -> int:
        return len(self.entries)

    def _entry_blocked(self, symbol: str) -> str:
        bot = self.bot
        if bot.state.killed:
            return "kill-switch tetiklenmiş"
        if bot.state.paused:
            return "alımlar durdurulmuş"
        if bot.entries_frozen(self.now()):
            return "alımlar risk sigortasıyla dondurulmuş"
        if symbol in bot.state.positions:
            return "bu coinde zaten pozisyon var"
        if len(bot.state.positions) >= bot.slots():
            return "boş pozisyon yeri yok"
        return ""

    def _snooze(self, symbol: str) -> None:
        """Onay verilmeyen sinyal sonraki taramada yeniden sorulmasın (SYMBOL_COOLDOWN_MINUTES, en az 15 dk)."""
        until = self.now() + max(15, self.cfg.symbol_cooldown_minutes) * 60
        self.bot.state.cooldowns[symbol] = max(self.bot.state.cooldowns.get(symbol, 0.0), until)

    def enter(self, signal, available: float, slots: int, context: dict, ask: float) -> str:
        """Tarama sinyalinden sonraki giriş adımı: 'opened' | 'pending' (onay / dönüş bekleniyor) | 'failed'.

        context["approval"] doluysa (örn. RSI_DIV_TRADE=ask) sinyal her durumda Telegram'dan sorulur; context'te
        pozisyona özel tp_pct / sl_pct varsa ATR hedefleri yalnızca eksik olanı tamamlar.
        """
        cfg = self.cfg
        forced = context.pop("approval", "")
        atr_value = None
        if cfg.atr_targets or cfg.signal_approval == "volatile":
            atr_value = self._safe(self.hourly_atr, signal.symbol, ask)
        tp, sl = atr_targets(cfg, atr_value)
        if tp is not None:
            context.setdefault("tp_pct", tp)
            context.setdefault("sl_pct", sl)
        why = forced or self._approval_reason(signal.symbol, atr_value)
        if why:
            ttl = max(cfg.approval_timeout_sec, FORCED_APPROVAL_MIN_SEC) if forced else cfg.approval_timeout_sec
            self._ask_approval(signal, available, slots, context, why, ask, atr_value, ttl)
            return "pending"
        return self._start_or_buy(signal, available, slots, context, ask)

    def _approval_reason(self, symbol: str, atr_value: Optional[float]) -> str:
        cfg = self.cfg
        if cfg.signal_approval == "off":
            return ""
        if cfg.signal_approval == "all":
            return "her sinyal onay istiyor (SIGNAL_APPROVAL=all)"
        reasons = []
        if atr_value is not None and atr_value >= cfg.approval_atr_pct:
            reasons.append(f"1 saatlik oynaklık %{atr_value:.1f} (sınır %{cfg.approval_atr_pct:g})")
        if self.liq_feed is not None and cfg.approval_liq_usdt > 0:
            long_usdt, _ = self.liq_feed.recent(symbol, LIQ_WINDOW_SEC)
            if long_usdt >= cfg.approval_liq_usdt:
                reasons.append(f"son 5 dk'da {long_usdt:,.0f} USDT long likidasyonu".replace(",", "."))
        return ", ".join(reasons)

    def _ask_approval(self, signal, available, slots, context, why, ask, atr_value, ttl: int) -> None:
        cfg, symbol = self.cfg, signal.symbol
        context["expected_ask"] = ask
        tp = context.get("tp_pct", cfg.take_profit_pct)
        sl = context.get("sl_pct", cfg.stop_loss_pct)
        liq = ""
        if self.liq_feed is not None:
            long_usdt, short_usdt = self.liq_feed.recent(symbol, LIQ_WINDOW_SEC)
            liq = (f"\n💥 Likidasyon (5 dk): long {long_usdt:,.0f} / short {short_usdt:,.0f} USDT"
                   .replace(",", "."))
        text = (f"🔔 <b>SİNYAL ONAYI</b> — <b>{esc(symbol)}</b>\n{LINE}\n"
                f"❓ Neden soruluyor: {esc(why)}\n"
                f"🔍 {esc(signal.note())}\n"
                f"🏷️ Fiyat: {fmt_price(ask)} | 💵 ~{fmt_usdt(available * cfg.capital_usage_pct / 100)} "
                f"{cfg.quote_asset}\n🎯 Kâr Al +%{tp:g} | 🛑 Zarar Kes -%{sl:g}"
                + (f" | 📏 1s ATR %{atr_value:.1f}" if atr_value else "") + liq)
        question = Question("buy", symbol, data={})
        token = self.ask(question, text, [("✅ Al", "yes"), ("❌ Geç", "no")], ttl=ttl)
        question.data["token"] = token
        now = self.now()
        self.entries[symbol] = PendingEntry("approval", signal, available, slots, context, now, now + ttl,
                                            token=token)

    def _start_or_buy(self, signal, available: float, slots: int, context: dict, ask: float) -> str:
        if self.cfg.trailing_buy:
            now = self.now()
            self.entries[signal.symbol] = PendingEntry("trail", signal, available, slots, context, now,
                                                       now + self.cfg.trailing_buy_minutes * 60, low=ask)
            log.info("%s kademeli alım başladı: fiyat %.8g, dipten %%%.2f dönünce alınacak (en fazla %d dk)",
                     signal.symbol, ask, self.cfg.trailing_buy_pct, self.cfg.trailing_buy_minutes)
            self.bot._sync_stream_symbols()
            return "pending"
        return "opened" if self._buy(signal, available, slots, context, ask) else "failed"

    def _buy(self, signal, available: float, slots: int, context: dict, ask: float) -> bool:
        bot, cfg = self.bot, self.cfg
        free = bot.exchange.free_balance(cfg.quote_asset)
        rules = bot.exchange.rules.get(signal.symbol)
        if rules is None:
            return False
        plan = plan_entry(min(available, free), rules, ask, cfg)
        if not plan.ok:
            log.info("%s alınmadı: %s", signal.symbol, plan.reason)
            bot.day_stats.rejects[plan.reason.split(" (")[0][:40]] += 1
            return False
        context = dict(context, expected_ask=ask)
        context["reserve_usdt"] = round(max(0.0, available - plan.quote_amount), 2) if cfg.capital_usage_pct < 100 else 0.0
        if bot.open_position(signal, plan.quote_amount, slots, context):
            bot.day_stats.buys += 1
            return True
        return False

    def process_entries(self, now: float) -> None:
        """Bekleyen girişler: onayı / dönüşü beklenenler, zaman aşımı ve iptaller (her döngüde)."""
        if not self.entries:
            return
        bot = self.bot
        stop_all = bot.state.killed or bot.state.paused or bot.entries_frozen(now)
        for symbol, e in list(self.entries.items()):
            if stop_all or now > e.expires:
                self.entries.pop(symbol, None)
                why = "alımlar durduruldu" if stop_all else "süre doldu"
                log.info("%s bekleyen giriş iptal (%s, %s)", symbol, e.kind, why)
                bot.day_stats.rejects["onay gelmedi" if e.kind == "approval" else "kademeli alım dönmedi"] += 1
                if e.kind == "approval":
                    self._snooze(symbol)
                    q = self.questions.pop(e.token, None)
                    if q is not None and q.message_id:
                        bot.commands.reply_menu(f"⌛ {esc(symbol)} sinyal onayının {why}; alınmadı.", [], q.message_id)
                continue
            if e.kind != "trail":
                continue
            _bid, ask = bot.bid_ask(symbol)
            if ask <= 0:
                continue
            if ask < e.low:
                e.low = ask
            elif ask >= trailing_buy_trigger(e.low, self.cfg.trailing_buy_pct):
                self.entries.pop(symbol, None)
                why = self._entry_blocked(symbol)
                if why:
                    log.info("%s kademeli alım yapılmadı: %s", symbol, why)
                    continue
                log.info("%s kademeli alım: dip %.8g → %.8g (+%%%.2f)", symbol, e.low, ask, (ask / e.low - 1) * 100)
                self._buy(e.signal, e.available, e.slots, e.context, ask)
        bot._sync_stream_symbols()

    # ================================================================ açık pozisyonlar
    def check_position(self, pos, mark, now: float) -> Optional[ExitReason]:
        """Akıllı yönetim kuralları. Hemen çıkılması gereken bir durum varsa (otomatik kaçış) nedeni döner."""
        cfg = self.cfg
        if not target_exits(cfg):   # Rotasyonda kâr al / süre / ekleme kuralları yoktur
            return None
        pnl = mark.pnl_pct
        if (cfg.tp_extend and not pos.tp_extended and "tp_ext" not in pos.asked
                and pnl >= position_tp(pos, cfg)):
            pos.asked.append("tp_ext")
            if self._safe(self.momentum, pos.symbol, default=False):
                pos.tp_extended = True
                self.bot.store.save(self.bot.state)
                self.notifier.notify(
                    f"🚀 <b>HEDEF UZATILDI</b> — {esc(pos.symbol)}\nNet kâr %{pnl:.2f} (Kâr Al +%{position_tp(pos, cfg):g}) "
                    f"ama momentum güçlü: satmak yerine zirveden %{cfg.tp_extend_gap_pct:g} geri dönünce satılacak.",
                    "update")
        sl = position_sl(pos, cfg)
        if (cfg.sl_extend and not pos.sl_extra_pct and "sl_ext" not in pos.asked
                and -(sl + cfg.sl_extend_pct) < pnl <= -sl):
            pos.asked.append("sl_ext")   # Pozisyon başına bir kez: uzatılmış zarar kese gelinirse satılır
            why = self._safe(self.reversal, pos.symbol, default=None)
            if why:
                pos.sl_extra_pct = cfg.sl_extend_pct
                self.bot.store.save(self.bot.state)
                self.notifier.notify(
                    f"🛟 <b>ZARAR KES UZATILDI</b> — {esc(pos.symbol)}\nNet zarar %{pnl:.2f} (Zarar Kes -%{sl:g}) ama "
                    f"{esc(why)}: zarar kes bir kez -%{position_sl(pos, cfg):g}'e genişletildi, dönüş bekleniyor.",
                    "update")
        if (cfg.time_exit_mode != "off" and "time" not in pos.asked
                and now - pos.opened_at >= cfg.time_exit_minutes * 60 and is_flat(pnl, cfg.time_exit_band_pct)):
            pos.asked.append("time")
            self.bot.store.save(self.bot.state)
            age = fmt_duration(now - pos.opened_at)
            if cfg.time_exit_mode == "auto":
                pos.exit_at_breakeven = True
                self.notifier.notify(f"⏳ {esc(pos.symbol)} {age}dir yatay (net %{pnl:+.2f}): başabaşa gelince "
                                     f"satılacak (TIME_EXIT_MODE=auto).", "update")
            else:
                self.ask(Question("time", pos.symbol, int(pos.opened_at)),
                         f"⏳ <b>SÜRE SORUSU</b> — {esc(pos.symbol)}\n{age}dir yatay seyrediyor (net K/Z "
                         f"%{pnl:+.2f}). Başabaşta satılsın mı?\n<i>Maks. süre kuralı (MAX_HOLD) ayrıca geçerli.</i>",
                         [("⚖️ Başabaşta sat", "be"), ("⚡ Hemen sat", "now"), ("⏳ Bekle", "wait")])
        if (cfg.escape_mode != "off" and "escape" not in pos.asked and pnl <= -cfg.escape_trigger_pct
                and self._due(f"esc:{pos.symbol}", ESCAPE_RECHECK_SEC)):
            rows = self._safe(self._closed, pos.symbol, self.interval(), 30, default=[])
            if rows and volume_breakdown(rows, cfg.escape_volume_ratio):
                pos.asked.append("escape")
                self.bot.store.save(self.bot.state)
                if cfg.escape_mode == "auto":
                    return ExitReason.ESCAPE
                self.ask(Question("escape", pos.symbol, int(pos.opened_at)),
                         f"🏃 <b>HACİMLİ SATIŞ BASKISI</b> — {esc(pos.symbol)}\nNet zarar %{pnl:.2f}; son mum "
                         f"ortalamanın {cfg.escape_volume_ratio:g} katından fazla hacimle ve satıcı ağırlıklı kapandı.\n"
                         f"Zarar kesi (-%{position_sl(pos, cfg):g}) beklemeden çıkılsın mı?",
                         [("⚡ Hemen sat", "sell"), ("⏳ Zarar kese kadar bekle", "wait")])
        add_ok = pos.adds == 0 and pos.reserve_usdt >= self._min_add(pos.symbol)
        if cfg.dca_mode != "off" and add_ok and "dca" not in pos.asked and pnl <= -cfg.dca_trigger_pct:
            pos.asked.append("dca")
            self.bot.store.save(self.bot.state)
            if cfg.dca_mode == "auto":
                self.add_to_position(pos, "dca")
            else:
                self.ask(Question("dca", pos.symbol, int(pos.opened_at)),
                         f"➕ <b>ORTALAMA DÜŞÜRME (DCA)</b> — {esc(pos.symbol)}\nNet zarar %{pnl:.2f}. Yedek akçeden "
                         f"~{fmt_usdt(pos.reserve_usdt)} {cfg.quote_asset} eklensin mi?\n<i>Ekleme sonrası zarar kes "
                         f"yeni ortalamaya göre hesaplanır: pozisyon büyüdüğü için olası zarar da büyür.</i>",
                         [(f"➕ {fmt_usdt(pos.reserve_usdt)} ekle", "add"), ("❌ Hayır", "no")])
        if (cfg.pyramid_mode != "off" and add_ok and "pyramid" not in pos.asked and not pos.tp_extended
                and pnl >= cfg.pyramid_trigger_pct and self._due(f"pyr:{pos.symbol}", MOMENTUM_RECHECK_SEC)
                and self._safe(self.momentum, pos.symbol, default=False)):
            pos.asked.append("pyramid")
            self.bot.store.save(self.bot.state)
            if cfg.pyramid_mode == "auto":
                self.add_to_position(pos, "pyramid")
            else:
                self.ask(Question("pyramid", pos.symbol, int(pos.opened_at)),
                         f"🔺 <b>PİRAMİT</b> — {esc(pos.symbol)}\nNet kâr %{pnl:.2f} ve momentum güçlü. Yedek akçeden "
                         f"~{fmt_usdt(pos.reserve_usdt)} {cfg.quote_asset} eklensin mi?\n<i>Ekleme sonrası birleşik "
                         f"pozisyon zarara dönmeden (net +%{self._lock_pct():g}) satılır.</i>",
                         [(f"🔺 {fmt_usdt(pos.reserve_usdt)} ekle", "add"), ("❌ Hayır", "no")])
        return None

    def _lock_pct(self) -> float:
        return max(0.1, self.cfg.breakeven_floor_pct)

    def _min_add(self, symbol: str) -> float:
        rules = self.bot.exchange.rules.get(symbol)
        min_notional = float(rules.min_notional) if rules is not None else 5.0
        return min_notional * self.cfg.min_notional_safety

    def add_to_position(self, pos, kind: str) -> bool:
        """Yedek akçeyle pozisyona ekler (DCA / piramit): yedek stop iptal → alış → birleştirme → yeni yedek stop."""
        bot, cfg, state = self.bot, self.cfg, self.bot.state
        q = cfg.quote_asset
        if state.killed:
            return False
        cash = state.capital - state.committed
        amount = min(pos.reserve_usdt, cash)
        try:
            amount = min(amount, bot.exchange.free_balance(q))
        except (requests.RequestException, BinanceAPIError, OrderError) as exc:
            log.warning("%s ekleme: bakiye okunamadı: %s", pos.symbol, exc)
            return False
        if amount < self._min_add(pos.symbol):
            bot.commands.reply(f"⚠️ {esc(pos.symbol)} için ekleme yapılamadı: kullanılabilir yedek "
                               f"{fmt_usdt(max(0.0, amount))} {q}, en az {fmt_usdt(self._min_add(pos.symbol))} {q} gerekli.")
            return False
        if pos.backup_order_id is not None:
            st = bot.exchange.cancel_backup_stop(pos.symbol, pos.backup_order_id)
            if st.status == "ERROR":
                bot.commands.reply(Messages.error(f"{pos.symbol} yedek stop iptal edilemedi, ekleme yapılmadı: "
                                                  f"{st.detail}"))
                return False
            if st.status == "FILLED":
                bot._close_with_fills(pos, [st.fill] if st.fill else [], ExitReason.BACKUP_STOP)
                return False
            if st.fill:   # İptalden önce kısmen dolmuş: o kısmın sonucunu hesaba yaz
                bot._realize_partial(pos, st.fill)
            pos.backup_order_id = None
        try:
            fill = bot.exchange.market_buy(pos.symbol, amount)
        except OrderError as exc:
            if exc.code == "UNKNOWN":   # Binance'e ulaşıp ulaşmadığı sorgulanamadı
                self.notifier.send(Messages.warning(
                    f"{pos.symbol} ekleme emrinin sonucu öğrenilemedi. Binance'te kontrol edin: gerçekleştiyse eklenen "
                    f"coinler bot tarafından takip edilmez (elle satabilirsiniz)."), critical=True)
            else:
                bot.commands.reply(Messages.error(f"{pos.symbol} ekleme alımı başarısız: {exc}"))
            bot._place_backup(pos)
            bot.store.save(state)
            return False
        old_qty = pos.qty
        pos.qty = old_qty + fill.qty
        pos.entry_price = ((pos.entry_price * old_qty + fill.avg_price * fill.qty) / pos.qty
                           if pos.qty > 0 else fill.avg_price)
        pos.cost_usdt += fill.usdt
        pos.buy_fee_usdt += fill.fee_usdt
        pos.reserve_usdt = round(max(0.0, pos.reserve_usdt - fill.usdt), 2)
        pos.adds += 1
        state.total_fees += fill.fee_usdt
        if not bot.exchange.is_paper:
            try:   # Cüzdandaki gerçek miktar daha azsa onu esas al
                held = bot.exchange.free_balance(pos.base_asset)
                if 0 < held < pos.qty:
                    pos.qty = held
            except Exception as exc:  # noqa: BLE001
                log.warning("Ekleme sonrası bakiye doğrulanamadı: %s", exc)
        bid, _ = bot.bid_ask(pos.symbol)
        sellable = bot.exchange.sellable_qty(pos.symbol, pos.qty)
        current = pnl_pct(net_exit_value(sellable, bid, cfg.fee_rate), pos.cost_usdt)
        pos.peak_pnl_pct = current        # Koruma seviyeleri yeni maliyete göre baştan
        pos.trailing_active = False
        pos.breakeven_active = False
        bot.mark_position(pos, self.now())   # Değerleme (pano, kill-switch, çıkış kontrolü) yeni maliyete göre
        if kind == "pyramid":
            pos.lock_pct = self._lock_pct()
        backup_line = bot._place_backup(pos)
        bot.store.save(state)
        bot._sync_stream_symbols()
        lv = bot.levels(pos)
        title = "➕ <b>DCA: ORTALAMA DÜŞÜRÜLDÜ</b>" if kind == "dca" else "🔺 <b>PİRAMİT: KÂRDAKİ POZİSYONA EKLENDİ</b>"
        lock = (f"\n🔐 Birleşik pozisyon net +%{pos.lock_pct:g}'nin altına inerse satılır (zarara dönmez)"
                if pos.lock_pct is not None else "")
        log.info("%s ekleme (%s): +%.2f %s @ %.8g → maliyet %.2f, ort. %.8g", pos.symbol, kind, fill.usdt, q,
                 fill.avg_price, pos.cost_usdt, pos.entry_price)
        self.notifier.send(f"{title} — <b>{esc(pos.symbol)}</b>\n{LINE}\n"
                           f"💵 Eklenen: {fmt_usdt(fill.usdt)} {q} @ {fmt_price(fill.avg_price)}\n"
                           f"🪙 Toplam: {fmt_qty(pos.qty)} | Maliyet: {fmt_usdt(pos.cost_usdt)} {q}\n"
                           f"🏷️ Yeni ort. fiyat: {fmt_price(pos.entry_price)} | Net K/Z şu an %{current:+.2f}\n"
                           f"🎯 Kâr Al: {fmt_price(lv['tp'])} | 🛑 Zarar Kes: {fmt_price(lv['sl'])} (yeni maliyete göre)"
                           f"{lock}" + (f"\n{backup_line}" if backup_line else ""))
        return True

    # ================================================================ canlı K/Z panosu
    def board_on(self) -> None:
        now = self.now()
        self.board = {"mid": None, "until": now + self.cfg.pnl_board_minutes * 60, "next": now, "text": ""}
        self.update_board(now, force=True)

    def board_off(self, message_id: Optional[int] = None) -> None:
        board, self.board = self.board, None
        mid = message_id or (board or {}).get("mid")
        text = "⏹ Canlı pano durduruldu. Tekrar açmak için /pano"
        if mid:
            self.notifier.send_menu(text, [], mid)
        else:
            self.bot.commands.reply(text)

    def board_text(self, now: float) -> str:
        bot, cfg = self.bot, self.cfg
        q = cfg.quote_asset
        lines = [f"📈 <b>Canlı K/Z Panosu</b> — {datetime.fromtimestamp(now):%H:%M:%S}", LINE]
        for pos in bot.state.positions.values():
            m = bot.marks.get(pos.symbol)
            if m is None:
                lines.append(f"<b>{esc(pos.symbol)}</b> — fiyat bekleniyor")
                continue
            tp_left = position_tp(pos, cfg) - m.pnl_pct
            sl_left = m.pnl_pct + position_sl(pos, cfg)
            flags = []
            if pos.tp_extended:
                flags.append("🚀 hedef uzatıldı")
            elif pos.trailing_active:
                flags.append("📈 iz. stop")
            if pos.breakeven_active:
                flags.append("🔒 başabaş")
            if pos.adds:
                flags.append(f"➕{pos.adds}")
            lines.append(f"<b>{esc(pos.symbol)}</b> {fmt_signed(m.pnl_pct)}% ({fmt_signed(m.pnl_usdt)} {q}) | "
                         f"{fmt_duration(now - pos.opened_at)}")
            lines.append(f"   🏷️ {fmt_price(m.bid)} | 🎯 hedefe %{max(0.0, tp_left):.2f} | 🛑 stopa %{max(0.0, sl_left):.2f}"
                         + (" | " + " ".join(flags) if flags else ""))
        if not bot.state.positions:
            lines.append("📭 Açık pozisyon yok." + (f" Bekleyen giriş: {', '.join(self.entries)}" if self.entries else ""))
        lines += [LINE, f"Açık K/Z: {fmt_signed(bot.unrealized())} {q} | Gerçekleşen: "
                        f"{fmt_signed(bot.state.realized_pnl)} {q}",
                  f"<i>{self.cfg.pnl_board_sec} sn'de bir güncellenir, "
                  f"{datetime.fromtimestamp(self.board['until']):%H:%M}'de durur.</i>" if self.board else ""]
        return "\n".join(line for line in lines if line)

    def update_board(self, now: float, force: bool = False) -> None:
        board = self.board
        if board is None or (not force and now < board["next"]):
            return
        board["next"] = now + self.cfg.pnl_board_sec
        if now >= board["until"]:
            self.board_off()
            return
        if not force and board["mid"] is None:
            return   # İlk mesajın kimliği henüz gelmedi: ikinci bir pano mesajı açılmasın
        if self.notifier.pending > 3:
            return   # Telegram yavaş: güncellemeleri biriktirme
        text = self.board_text(now)
        if text == board["text"]:
            return
        board["text"] = text

        def remember(mid: int) -> None:
            if self.board is board:
                board["mid"] = mid

        self.notifier.send_menu(text, [[("⏹ Panoyu kapat", "board:off")]], board["mid"], on_sent=remember,
                                quiet_log=not force)

    # ================================================================ likidasyon bildirimi
    def check_liquidations(self, now: float) -> None:
        cfg, feed = self.cfg, self.liq_feed
        if feed is None or cfg.liq_alert_usdt <= 0 or now < self._next_liq_check:
            return
        self._next_liq_check = now + LIQ_CHECK_SEC
        fresh = [(s, lo, sh) for s, lo, sh in feed.spikes(cfg.liq_alert_usdt, LIQ_WINDOW_SEC)
                 if now - self._liq_alerted.get(s, 0.0) >= LIQ_ALERT_COOLDOWN_SEC]
        if not fresh:
            return
        for symbol, _lo, _sh in fresh:
            self._liq_alerted[symbol] = now
        lines = ["💥 <b>Likidasyon dalgası</b> (son 5 dk, vadeli işlemler)"]
        for symbol, long_usdt, short_usdt in fresh[:8]:
            lines.append(f"• <b>{esc(symbol)}</b>: long {long_usdt:,.0f} USDT / short {short_usdt:,.0f} USDT"
                         .replace(",", "."))
        lines.append("<i>Zorla satışlar fiyatı aşağı itip oynaklığı artırır: dipten dönüş fırsatı da olabilir, "
                     "düşüşün devamı da. Bilgi amaçlıdır.</i>")
        self.notifier.notify("\n".join(lines), "info")

    # ================================================================ BNB takviyesi / toz
    def notice_with_bnb_offer(self, text: str) -> None:
        """BNB azaldı / bitti bildirimi. BNB_TOPUP_USDT > 0 ise (canlı) aynı mesaja "BNB al" düğmesi eklenir."""
        cfg, bot = self.cfg, self.bot
        if (cfg.bnb_topup_usdt <= 0 or bot.exchange.is_paper
                or any(q.kind == "bnb" for q in self.questions.values())):
            self.notifier.send(text, silent=True)
            return
        amount = max(cfg.bnb_topup_usdt, self._bnb_min())
        self.ask(Question("bnb", f"{BNB_SYMBOL_SUFFIX}{cfg.quote_asset}", data={"amount": amount}),
                 text + f"\n\n🪙 {fmt_usdt(amount)} {cfg.quote_asset}'lik BNB alınsın mı? <i>(Binance'in en küçük emri "
                 f"~5 USDT olduğu için daha küçük alım yapılamaz. Tutar botun sermayesinden değil, Spot'ta bota "
                 f"ayrılmamış serbest bakiyeden ödenir.)</i>",
                 [(f"🪙 {fmt_usdt(amount)} {cfg.quote_asset} BNB al", "buy"), ("❌ Hayır", "no")], silent=True)

    def _bnb_min(self) -> float:
        rules = self.bot.exchange.rules.get(f"{BNB_SYMBOL_SUFFIX}{self.cfg.quote_asset}")
        return float(rules.min_notional) * 1.1 if rules is not None else 5.5

    def _answer_bnb(self, q: Question, choice: str) -> str:
        if choice != "buy":
            return "👍 BNB alınmadı."
        bot, cfg = self.bot, self.cfg
        amount = float(q.data.get("amount") or 0)
        pending = bot.unreconciled_reason()
        if pending:
            return f"⏳ Şu an BNB alınamıyor: {esc(pending)}. Biraz sonra tekrar deneyin."
        spare = bot.unallocated_balance() or 0.0
        if spare < amount:
            return (f"⚠️ Spot'ta bota ayrılmamış serbest bakiye {fmt_usdt(max(0.0, spare))} {cfg.quote_asset}; "
                    f"{fmt_usdt(amount)} gerekli. BNB'yi Binance'ten elle alabilir veya Spot'a para ekleyebilirsiniz.")
        try:
            fill = bot.exchange.market_buy(q.symbol, amount)
        except OrderError as exc:
            return Messages.error(f"BNB alınamadı: {exc}")
        log.info("BNB takviyesi: %.8g BNB (%.2f %s)", fill.qty, fill.usdt, cfg.quote_asset)
        return f"✅ {fmt_qty(fill.qty)} BNB alındı ({fmt_usdt(fill.usdt)} {cfg.quote_asset})."

    def show_dust(self) -> None:
        bot, cfg = self.bot, self.cfg
        reply = bot.commands.reply
        try:
            details = bot.exchange.dust_assets()
        except OrderError as exc:
            reply(f"ℹ️ {esc(exc)}")
            return
        except (requests.RequestException, BinanceAPIError) as exc:
            reply(Messages.error(f"Toz listesi alınamadı (API anahtarının izni gerekebilir): {exc}"))
            return
        held = {p.base_asset for p in bot.state.positions.values()} | {cfg.quote_asset, BNB_SYMBOL_SUFFIX}
        dust = [(a, amt, bnb) for a, amt, bnb in details if a not in held and bnb > 0]
        if not dust:
            reply("🧹 BNB'ye çevrilecek toz yok (açık pozisyonlardaki coinler hariç tutulur).")
            return
        total = sum(b for _, _, b in dust)
        lines = [f"• {esc(a)}: {fmt_qty(amt)} → ~{b:.6f} BNB" for a, amt, b in dust[:20]]
        self.ask(Question("dust", data={"assets": [a for a, _, _ in dust]}),
                 "🧹 <b>Toz → BNB</b>\n" + "\n".join(lines) +
                 f"\nToplam ~{total:.6f} BNB (Binance küçük bir ücret keser). Çevrilsin mi?\n"
                 "<i>Açık pozisyonlardaki coinlere dokunulmaz.</i>",
                 [("🧹 BNB'ye çevir", "go"), ("❌ Vazgeç", "no")])

    def _answer_dust(self, q: Question, choice: str) -> str:
        if choice != "go":
            return "👍 Tozlar çevrilmedi."
        bot = self.bot
        held = {p.base_asset for p in bot.state.positions.values()}
        assets = [a for a in q.data.get("assets", []) if a not in held]   # Bu arada pozisyon açıldıysa dokunma
        if not assets:
            return "ℹ️ Çevrilecek toz kalmadı."
        try:
            got, fee = bot.exchange.convert_dust(assets)
        except (OrderError, requests.RequestException, BinanceAPIError) as exc:
            return Messages.error(f"Tozlar çevrilemedi: {exc}")
        log.info("Toz → BNB: %s → %.8f BNB (ücret %.8f)", ",".join(assets), got, fee)
        return f"✅ {len(assets)} coinin tozu BNB'ye çevrildi: +{got:.6f} BNB (ücret {fee:.6f} BNB)."
