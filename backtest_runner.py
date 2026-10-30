"""
Telegram'dan backtest: /backtest veya /menu → 🧪 Backtest (tamamen düğmelerle).

Menüde tür (normal, çıkış / giriş / filtre taraması, A/B testi, strateji karşılaştırma, turnuva, hepsi), strateji,
dönem, bitiş tarihi, sermaye, coin sayısı, tarama değerleri ve YALNIZCA o teste özel ayar değişiklikleri (Kâr Al,
Zarar Kes, filtreler, akıllı yönetim...) seçilir. Botun kendi ayarları değişmez.

Backtest botun içinde DEĞİL, ayrı bir süreçte (python main.py --backtest ...) düşük öncelikle çalışır:
  - Bot pozisyonları izlemeye devam eder. Bellek yetmezse işletim sistemi önce backtest sürecini kapatır.
  - Aynı anda yalnızca bir backtest çalışır. Süre sınırı yoktur: saatler sürse de bitene kadar çalışır (menüdeki
    "Durdur" düğmesiyle elle durdurulabilir). Bot yeniden başlarsa çalışan backtest de durur.
Sonuçlar: rapor Telegram mesajı olarak (tablolar düzgün görünsün diye eşit aralıklı yazıyla) ve .txt dosyası olarak,
grafik resim, tablolar Excel ile açılabilen CSV dosyası olarak gelir. Son birkaç sonucun dosyaları
data/backtest_runs/ klasöründe saklanır.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from config import (BASE_DIR, DATA_DIR, SETTINGS_FILE, STRATEGY_NAMES, Config, ConfigError, is_long_horizon,
                    is_mean_reversion, is_rotation, with_strategy)
from logger import get_logger
from runtime_settings import apply_overrides, format_value
from telegram_bot import LINE, esc, fmt_duration

if TYPE_CHECKING:  # pragma: no cover
    from trader import TradingBot

log = get_logger()

RUNS_DIR = DATA_DIR / "backtest_runs"
RUNS_KEPT = 5                      # En son kaç backtest'in dosyaları saklanır
PROGRESS_EDIT_SEC = 20             # Çalışırken durum mesajı en fazla bu sıklıkla güncellenir
CHUNK_CHARS = 3500                 # Telegram mesajı başına rapor uzunluğu (sınır ~4000)
MAX_CHUNKS = 6                     # Daha uzun rapor yalnızca dosya olarak gelir
NICE_LEVEL = 10

# Tür → (ad, komut satırı bayrakları, açıklama)
KINDS: Dict[str, Tuple[str, Tuple[str, ...], str]] = {
    "normal": ("Normal backtest", (), "tek ayar seti: işlemler, teşhis, K/Z grafiği"),
    "tara": ("Çıkış taraması", ("--tara",), "Kâr Al / Zarar Kes / izleyen stop / süre birleşimleri"),
    "giris": ("Giriş taraması", ("--tara-giris",), "piyasa rejimi, 24s aralık, sinyal puanı, hedef alanı"),
    "filtre": ("Filtre gevşetme", ("--tara-filtre",), "rebound filtrelerini tek tek gevşetir (işlem sayısı)"),
    "ab": ("A/B filtre testi", ("--tara-ab",), "her filtre / sigorta / akıllı yönetim tek tek açılıp kapanır"),
    "karsilastir": ("Strateji karşılaştırma", ("--strateji-karsilastir",),
                    "aynı mum aralığındaki stratejiler aynı veriyle"),
    "turnuva": ("Strateji turnuvası", ("--turnuva",), "tüm strateji türleri × çıkış kuralları (varsayılan 1 yıl)"),
    "deney": ("Zarar azaltma deneyi", ("--deney",),
              "önerilen ayarlar + takılan işlem / zarar sonrası bekleme / önceki trend, kill-switch açık"),
    "hepsi": ("Tüm taramalar", ("--tara", "--tara-giris", "--tara-filtre", "--tara-ab"),
              "çıkış + giriş + filtre + A/B; veri bir kez indirilir"),
}
DAY_CHOICES = (7, 14, 30, 45, 60, 90, 120, 180, 270, 365, 540, 730, 1095)
END_CHOICES = ("today", "prev", "30", "60", "90", "180", "365")
CAPITAL_CHOICES = ("cfg", "bot", "11", "20", "32", "50", "100", "250", "500", "1000")
COIN_CHOICES = (10, 20, 30, 40, 60, 80, 100)
# Çıkış taraması (--tara) değerleri; "" = botun kendi tarama ızgarası
GRIDS: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "tp": ("🎯 Kâr Al %", "--tp", ("", "1,1.5,2,3", "2,3,5", "3,5,8", "5,10,15", "10,25,50")),
    "sl": ("🛑 Zarar Kes %", "--sl", ("", "1,2,3", "2,3,5", "3,5,8", "5,8,10", "10,15,20")),
    "iz": ("📈 İzleyen stop başlangıcı %", "--iz", ("", "0.5,1", "1,2", "2,4", "4,8")),
    "sure": ("⏱️ Maks. süre (dk, 0 = sınırsız)", "--sure",
             ("", "0,240", "120,240,480", "250,500,1000", "720,1440,2880", "0,1440,4320,7200")),
    "alan": ("📏 Hedef alanı oranı", "--alan", ("", "0,0.5,1", "0,1,2")),
}
# Yalnızca bu test için ayar değişiklikleri: grup → (ad, seçenekler: her biri {ANAHTAR: değer})
PRESETS: Dict[str, Tuple[str, Tuple[Dict[str, str], ...]]] = {
    "tp": ("🎯 Kâr Al %", tuple({"TAKE_PROFIT_PCT": v} for v in
                               ("1", "1.5", "2", "2.5", "3", "4", "5", "8", "10", "15", "25", "50"))),
    "sl": ("🛑 Zarar Kes %", tuple({"STOP_LOSS_PCT": v} for v in
                                  ("1", "1.5", "2", "3", "4", "5", "6", "8", "10", "15", "20"))),
    "tr": ("📈 İzleyen stop (başlar → satar)", tuple(
        {"TRAILING_ACTIVATION_PCT": a, "TRAILING_FLOOR_PCT": f} for a, f in
        (("1", "0.5"), ("1.5", "0.8"), ("2", "1"), ("3", "1.5"), ("5", "2.5"), ("8", "4"), ("10", "5"))) + (
        {"TRAILING_ACTIVATION_PCT": "20", "TRAILING_FLOOR_PCT": "10"},)),
    "be": ("🔒 Başabaş (tetik → taban)", ({"BREAKEVEN_ENABLED": "false"},) + tuple(
        {"BREAKEVEN_ENABLED": "true", "BREAKEVEN_TRIGGER_PCT": t, "BREAKEVEN_FLOOR_PCT": f} for t, f in
        (("0.7", "0.2"), ("1", "0.5"), ("2", "0.5"), ("3", "1"), ("5", "1")))),
    "hold": ("⏱️ Maks. tutma (dk)", tuple({"MAX_HOLD_MINUTES": v} for v in
                                         ("0", "60", "120", "240", "500", "720", "1440", "2880", "4320", "7200"))),
    "pos": ("📦 En fazla pozisyon", tuple({"MAX_OPEN_POSITIONS": v} for v in ("1", "2", "3", "4", "5"))),
    "use": ("💰 İlk giriş % (yedek akçe)", tuple({"CAPITAL_USAGE_PCT": v} for v in ("100", "75", "50", "33"))),
    "cool": ("⏳ Aynı coine tekrar giriş (dk)", tuple({"SYMBOL_COOLDOWN_MINUTES": v} for v in
                                                     ("0", "15", "30", "60", "120", "240"))),
    "streak": ("🔁 Zarar serisi sigortası (SL)", tuple({"LOSS_STREAK_LIMIT": v} for v in ("0", "2", "3", "4", "5"))),
    "daily": ("📉 Günlük zarar limiti %", tuple({"DAILY_LOSS_LIMIT_PCT": v} for v in ("0", "2", "3", "5"))),
    "div": ("📐 Uyumsuzluk RSI eşiği (ilk dipte RSI en fazla)", tuple({"RSI_DIV_OVERSOLD": v} for v in
                                                                  ("25", "30", "35", "40", "45"))),
    "ret": ("↩️ Rebound: en fazla geri alınan (MAX_RETRACE)", tuple({"MAX_RETRACE": v} for v in
                                                                   ("0.5", "0.6", "0.75", "0.9"))),
    # Giriş taramasının (--tara-giris) satırları: bulduğunuz birleşimi kill-switch açık normal backtest'le doğrulamak
    # için. Puan ölçeği stratejiye özeldir (giriş taraması verideki ortanca / üst çeyrek puanı önerir)
    "room": ("📏 Hedef alanı oranı (MIN_ROOM_RATIO)", tuple({"MIN_ROOM_RATIO": v} for v in
                                                          ("0", "0.25", "0.5", "0.75", "1", "1.5"))),
    "drop": ("📉 24s en fazla düşüş % (MAX_24H_DROP_PCT)", tuple({"MAX_24H_DROP_PCT": v} for v in
                                                                ("8", "10", "15", "20", "30"))),
    "score": ("🏅 En düşük sinyal puanı (MIN_SIGNAL_SCORE)", tuple(
        {"MIN_SIGNAL_SCORE": v} for v in ("0", "10", "12", "14", "15", "16", "17", "18", "20"))),
    "stale": ("🧊 Takılan işlem çıkışı (dk)", tuple({"STALE_EXIT_MINUTES": v} for v in ("0", "60", "120", "240"))),
    "lcool": ("⏳ Zarardan sonra coine bekleme (dk)", tuple({"LOSS_COOLDOWN_MINUTES": v} for v in
                                                            ("0", "240", "720", "1440"))),
}
TOGGLES: Tuple[Tuple[str, str], ...] = (
    ("VWAP_FILTER", "VWAP"), ("FAKEOUT_FILTER", "Fitil"), ("OVERHEAT_FILTER", "Isınma"), ("BTC_FILTER", "BTC sig."),
    ("REGIME_FILTER", "Rejim"), ("REGIME_SKIP_WEAK", "Zayıfta alma"), ("TRAILING_DYNAMIC", "Dinamik iz."),
    ("ATR_TARGETS", "ATR hedef"), ("TP_EXTEND", "Hedef uzatma"), ("TRAILING_BUY", "Kademeli alım"),
    ("RSI_DIV_FILTER", "Uyumsuzluk teyidi"), ("RSI_DIV_TREND_FILTER", "Uyumsuzlukta trend"),
    ("PRIOR_TREND_FILTER", "Önceki trend"),
)
MODES: Tuple[Tuple[str, str], ...] = (("DCA_MODE", "DCA"), ("PYRAMID_MODE", "Piramit"),
                                      ("TIME_EXIT_MODE", "Süre kuralı"), ("ESCAPE_MODE", "Erken kaçış"))
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| INFO")


# ================================================================ taslak (menüde seçilen ayarlar)
@dataclass
class BacktestDraft:
    kind: str = "normal"
    strategy: str = ""            # "" = botun stratejisi
    days: int = 0                 # 0 = otomatik (stratejiye göre 30 / 365 / 1095)
    end: str = "today"            # today | prev (önceki dönem: doğrulama) | N (N gün önce biter)
    capital: str = "cfg"          # cfg = START_CAPITAL_USDT | bot = botun güncel sermayesi | sayı
    coins: int = 40
    chart: bool = True
    grids: Dict[str, str] = field(default_factory=dict)
    overrides: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "BacktestDraft":
        draft = cls()
        if not isinstance(data, dict):
            return draft
        known = {f.name: f for f in fields(cls)}
        for key, value in data.items():
            if key in known and isinstance(value, type(getattr(draft, key))):
                setattr(draft, key, value)
        if draft.kind not in KINDS:
            draft.kind = "normal"
        if draft.strategy not in STRATEGY_NAMES:
            draft.strategy = ""
        return draft


def strategy_of(draft: BacktestDraft, cfg) -> str:
    return draft.strategy or cfg.strategy


def default_days(kind: str, strategy: str) -> int:
    """Komut satırındaki varsayılanla aynı (main.run_backtest_cli)."""
    if is_rotation(strategy) and kind != "turnuva":
        return 1095
    if kind in ("turnuva", "deney") or is_long_horizon(strategy) or is_mean_reversion(strategy):
        return 365
    return 30


def effective_days(draft: BacktestDraft, cfg) -> int:
    return draft.days or default_days(draft.kind, strategy_of(draft, cfg))


def end_date(draft: BacktestDraft, cfg, today: date) -> Optional[date]:
    """Dönemin bitiş günü; None = bugün (şimdiye kadar)."""
    if draft.end == "prev":
        return today - timedelta(days=effective_days(draft, cfg))
    if draft.end.isdigit():
        return today - timedelta(days=int(draft.end))
    return None


def capital_of(draft: BacktestDraft, bot) -> Optional[float]:
    """--sermaye değeri; None = ayardaki START_CAPITAL_USDT."""
    if draft.capital == "bot":
        return round(bot.state.capital, 2)
    try:
        value = float(draft.capital)
    except ValueError:
        return None
    return value if value > 0 else None


def base_config(draft: BacktestDraft, cfg) -> Config:
    """Değişiklikler uygulanmadan önceki ayarlar: seçilen stratejiye geçilmiş (komut satırındaki gibi)."""
    return with_strategy(cfg, draft.strategy)[0] if draft.strategy else cfg


def effective_config(draft: BacktestDraft, cfg) -> Config:
    """Testin kullanacağı ayarlar (geçersizse ConfigError)."""
    return apply_overrides(base_config(draft, cfg), draft.overrides)


def build_args(draft: BacktestDraft, bot, out_dir: Path, today: date) -> List[str]:
    """main.py'ye verilecek argümanlar."""
    cfg = bot.cfg
    args = ["--backtest", *KINDS[draft.kind][1], "--gun", str(effective_days(draft, cfg)),
            "--coin-sayisi", str(draft.coins), "--cikti-klasoru", str(out_dir)]
    if draft.strategy and draft.kind != "turnuva":
        args += ["--strateji", draft.strategy]
    end = end_date(draft, cfg, today)
    if end is not None:
        args += ["--bitis", end.isoformat()]
    capital = capital_of(draft, bot)
    if capital is not None:
        args += ["--sermaye", f"{capital:g}"]
    if not draft.chart:
        args.append("--grafiksiz")
    if draft.kind in ("tara", "hepsi"):
        for key, (_label, flag, _choices) in GRIDS.items():
            if draft.grids.get(key):
                args += [flag, draft.grids[key]]
    for key, value in sorted(draft.overrides.items()):
        args += ["--ayar", f"{key}={value}"]
    if bot.settings_path is not None:
        args += ["--env", str(bot.settings_path)]
    return args


def describe(draft: BacktestDraft, bot, today: date) -> str:
    """Tek satır özet: 'A/B filtre testi | rebound | 90 gün → 27.10 | 32 USDT | 40 coin'."""
    cfg = bot.cfg
    days = effective_days(draft, cfg)
    end = end_date(draft, cfg, today)
    capital = capital_of(draft, bot)
    parts = [KINDS[draft.kind][0]]
    if draft.kind != "turnuva":
        parts.append(strategy_of(draft, cfg))
    parts.append(f"{days} gün" + (f", bitiş {end:%d.%m.%Y}" if end else ""))
    parts.append(f"{capital if capital is not None else cfg.start_capital:g} {cfg.quote_asset}")
    parts.append(f"{draft.coins} coin")
    if draft.overrides:
        parts.append("ayar: " + ", ".join(f"{k}={v}" for k, v in sorted(draft.overrides.items())))
    return " | ".join(parts)


# ================================================================ alt süreç
@dataclass
class _Job:
    proc: object
    out_file: object
    out_path: Path
    run_dir: Path
    started: float
    summary: str
    message_id: Optional[int] = None
    next_progress: float = 0.0
    last_progress: str = ""
    stopped: str = ""            # Kullanıcı durdurdu / süre doldu / bot kapandı


def child_env(env_path: Optional[Path]) -> Dict[str, str]:
    """Backtest sürecinin ortam değişkenleri.

    Bot açılırken .env ve ayarlar.env'deki değerler ortam değişkenlerine yüklendi; menüden sonradan değiştirilen ayarlar
    ise yalnızca dosyalara yazıldı. Bu anahtarlar çıkarılır ki backtest dosyaları BAŞTAN (güncel haliyle) okusun.
    """
    from dotenv import dotenv_values

    path = Path(env_path) if env_path else BASE_DIR / ".env"
    keys = set()
    for candidate in (path, path.parent / SETTINGS_FILE):
        if candidate.exists():
            keys |= set(dotenv_values(candidate))
    env = {k: v for k, v in os.environ.items() if k not in keys}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def command_for(args: Sequence[str]) -> List[str]:
    cmd = [sys.executable, str(BASE_DIR / "main.py"), *args]
    nice = shutil.which("nice") if os.name == "posix" else None
    return ([nice, "-n", str(NICE_LEVEL)] + cmd) if nice else cmd


def _deprioritize(pid: int) -> None:
    """Bellek yetmezse işletim sistemi (OOM) botu değil backtest'i kapatsın (Linux; hata olursa geçilir)."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> List[str]:
    """Metni satır sınırlarından `limit` karakteri aşmayan parçalara böler (çok uzun satır kırpılır)."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.splitlines():
        line = line[:limit]
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def clean_output(text: str) -> str:
    """Raporda gösterilmeyecek satırlar: ilerleme (⏳) ve INFO log satırları."""
    keep = [line.rstrip() for line in text.splitlines()
            if not line.lstrip().startswith("⏳") and not _LOG_LINE.match(line)]
    return "\n".join(keep).strip()


class BacktestRunner:
    def __init__(self, bot: "TradingBot"):
        self.bot = bot
        self.job: Optional[_Job] = None
        self.last_dir: Optional[Path] = None
        self.popen = subprocess.Popen       # Testlerde değiştirilebilir
        self.runs_dir = RUNS_DIR

    @property
    def running(self) -> bool:
        return self.job is not None

    def elapsed(self) -> float:
        return self.bot.time_fn() - self.job.started if self.job else 0.0

    # ------------------------------------------------------------ başlat / durdur
    def start(self, draft: BacktestDraft) -> str:
        """Backtest'i başlatır; sonucu bildiren mesaj (hata varsa nedeni) döner."""
        bot = self.bot
        if self.job is not None:
            return "⏳ Zaten çalışan bir backtest var. Bitmesini bekleyin veya ⏹ Durdur'a basın."
        try:
            effective_config(draft, bot.cfg)
        except ConfigError as exc:
            return f"⚠️ Backtest ayarları geçersiz: {esc(str(exc).replace('Ayar hataları:', '').strip())}"
        now = bot.time_fn()
        today = datetime.fromtimestamp(now).date()
        run_dir = self.runs_dir / datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        try:
            self._cleanup(keep=RUNS_KEPT - 1)       # Yenisiyle birlikte en fazla RUNS_KEPT klasör
            run_dir.mkdir(parents=True, exist_ok=True)
            out_path = run_dir / "cikti.txt"
            out_file = open(out_path, "w", encoding="utf-8")
        except OSError as exc:
            return f"⚠️ Backtest klasörü oluşturulamadı: {esc(exc)}"
        args = build_args(draft, bot, run_dir, today)
        kwargs = {"cwd": str(BASE_DIR), "stdout": out_file, "stderr": subprocess.STDOUT,
                  "env": child_env(bot.settings_path), "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            proc = self.popen(command_for(args), **kwargs)
        except OSError as exc:
            out_file.close()
            return f"⚠️ Backtest başlatılamadı: {esc(exc)}"
        _deprioritize(getattr(proc, "pid", 0))
        summary = describe(draft, bot, today)
        self.job = _Job(proc, out_file, out_path, run_dir, now, summary, next_progress=now + PROGRESS_EDIT_SEC)
        log.info("Telegram'dan backtest başlatıldı: %s", " ".join(args))
        job = self.job

        def remember(mid: int) -> None:
            job.message_id = mid

        bot.notifier.send_menu(self._status_text(job, "başladı"), [[("⏹ Durdur", "bt:stop")]], on_sent=remember)
        return ""

    def cancel(self, why: str = "durduruldu") -> str:
        job = self.job
        if job is None:
            return "ℹ️ Çalışan bir backtest yok."
        job.stopped = why
        self._kill(job)
        self._finish(job, None)
        return ""

    def shutdown(self) -> None:
        """Bot kapanırken: çalışan backtest de durur (bildirim gönderilmez)."""
        job, self.job = self.job, None
        if job is not None:
            self._kill(job)
            job.out_file.close()

    @staticmethod
    def _kill(job: _Job) -> None:
        try:
            job.proc.terminate()
            job.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - kapanmazsa zorla
            try:
                job.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ takip (ana döngüden)
    def poll(self, now: float) -> None:
        job = self.job
        if job is None:
            return
        code = job.proc.poll()
        if code is None:   # Süre sınırı yok: bitene (ya da elle durdurulana) kadar beklenir
            if now >= job.next_progress:
                job.next_progress = now + PROGRESS_EDIT_SEC
                self._update_progress(job)
            return
        self._finish(job, code)

    def _status_text(self, job: _Job, state: str) -> str:
        line = f"\n<code>{esc(job.last_progress)}</code>" if job.last_progress else ""
        return (f"🧪 <b>Backtest {state}</b> ({fmt_duration(self.bot.time_fn() - job.started)})\n{esc(job.summary)}"
                f"{line}\n<i>Bot bu sırada normal çalışmaya devam ediyor. Bitince sonuçlar buraya gelecek.</i>")

    def _update_progress(self, job: _Job) -> None:
        last = ""
        for line in _tail(job.out_path).splitlines():
            if "⏳" in line or line.startswith("🧪") or line.startswith("🏆"):
                last = line.strip()
        job.last_progress = last
        if job.message_id:   # Geçen süre de güncellensin diye her seferinde düzenlenir
            self.bot.notifier.send_menu(self._status_text(job, "çalışıyor"), [[("⏹ Durdur", "bt:stop")]],
                                        job.message_id, quiet_log=True)

    def _finish(self, job: _Job, code: Optional[int]) -> None:
        self.job = None
        try:
            job.out_file.close()
        except Exception:  # noqa: BLE001
            pass
        self.last_dir = job.run_dir
        notifier = self.bot.notifier
        took = fmt_duration(self.bot.time_fn() - job.started)
        raw = _read(job.out_path)
        report = clean_output(raw)
        try:
            (job.run_dir / "rapor.txt").write_text(report + "\n", encoding="utf-8")
        except OSError:
            pass
        if job.stopped:
            state = f"⏹ <b>Backtest {esc(job.stopped)}</b> ({took})"
        elif code == 0:
            state = f"✅ <b>Backtest bitti</b> ({took})"
        else:
            state = f"❌ <b>Backtest başarısız</b> (çıkış kodu {code}, {took})"
        if job.message_id:   # Durum mesajı son haline gelir (Durdur düğmesi kalkar)
            notifier.send_menu(f"{state}\n{esc(job.summary)}", [], job.message_id)
        log.info("Telegram backtest'i bitti (%s): %s", code, job.summary)
        if job.stopped:
            if not job.message_id:
                notifier.send(f"{state}\n{esc(job.summary)}")
            return
        if code != 0:
            tail = "\n".join(report.splitlines()[-25:]) or "(çıktı yok)"
            notifier.send(f"{state}\n{esc(job.summary)}\n<pre>{esc(tail)}</pre>")
            return
        self.send_results(job.run_dir, state, job.summary)

    def send_results(self, run_dir: Path, state: str, summary: str) -> None:
        """Rapor mesajları (eşit aralıklı yazı), grafik ve dosyalar."""
        notifier = self.bot.notifier
        report = _read(run_dir / "rapor.txt").strip() or "(rapor boş)"
        chunks = split_chunks(esc(report))   # Önce kaçış: Telegram sınırı kaçışlı uzunluğa göre
        notifier.send(f"{state}\n{esc(summary)}")
        for chunk in chunks[:MAX_CHUNKS]:
            notifier.send(f"<pre>{chunk}</pre>")
        if len(chunks) > MAX_CHUNKS:
            notifier.send(f"…rapor çok uzun ({len(chunks)} parça): tamamı aşağıdaki rapor.txt dosyasında.")
        for path in sorted(run_dir.glob("*.png")):
            notifier.send_photo(path, "📊 K/Z dağılımı")
        for path in sorted(run_dir.glob("*.csv")):
            notifier.send_document(path, "📄 Tablonun tamamı (Excel ile açılır)")
        if (run_dir / "rapor.txt").exists():
            notifier.send_document(run_dir / "rapor.txt", "📄 Raporun tamamı")

    def resend_last(self) -> str:
        if self.last_dir is None:
            dirs = self._run_dirs()
            self.last_dir = dirs[-1] if dirs else None
        if self.last_dir is None or not (self.last_dir / "rapor.txt").exists():
            return "ℹ️ Henüz tamamlanmış bir backtest sonucu yok."
        self.send_results(self.last_dir, "📄 <b>Son backtest sonucu</b>", self.last_dir.name)
        return ""

    def _run_dirs(self) -> List[Path]:
        if not self.runs_dir.exists():
            return []
        return sorted(p for p in self.runs_dir.iterdir() if p.is_dir())

    def _cleanup(self, keep: int) -> None:
        """Eski backtest klasörlerini siler (en yeni `keep` tanesi ve son sonuç kalır)."""
        dirs = [d for d in self._run_dirs() if self.job is None or d != self.job.run_dir]
        for old in dirs[:-keep] if keep > 0 else dirs:
            if old == self.last_dir:
                continue
            shutil.rmtree(old, ignore_errors=True)


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(path: Path, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# ================================================================ Telegram menüsü
class BacktestMenu:
    """/backtest düğmeleri. Seçimler bot durumunda saklanır (yeniden başlatınca kaybolmaz)."""

    def __init__(self, proc):
        self.proc = proc

    @property
    def bot(self):
        return self.proc.bot

    @property
    def runner(self) -> BacktestRunner:
        return self.bot.backtests

    def draft(self) -> BacktestDraft:
        return BacktestDraft.from_dict(self.bot.state.backtest_draft)

    def save(self, draft: BacktestDraft) -> None:
        self.bot.state.backtest_draft = draft.to_dict()
        self.bot.store.save(self.bot.state)

    def today(self) -> date:
        return datetime.fromtimestamp(self.bot.time_fn()).date()

    # ------------------------------------------------------------ yönlendirme
    def handle(self, args: List[str], mid: Optional[int]) -> None:
        sub = args[0] if args else ""
        arg = args[1] if len(args) > 1 else None
        draft = self.draft()
        note = ""
        if sub == "" or sub == "main":
            pass
        elif sub == "go":
            note = self.runner.start(draft) or "▶️ Backtest başladı; ilerleme ayrı mesajda."
        elif sub == "stop":
            note = self.runner.cancel() or "⏹ Backtest durduruldu."
        elif sub == "last":
            note = self.runner.resend_last() or "📄 Son sonuç gönderildi."
        elif sub == "kind":
            if arg in KINDS:
                draft.kind = arg
                self.save(draft)
            else:
                return self._pick(mid, "📋 Backtest türü", [
                    (("✅ " if k == draft.kind else "") + name, f"bt:kind:{k}") for k, (name, _f, _h) in KINDS.items()],
                    "\n".join(f"• <b>{name}</b>: {esc(help_)}" for name, _f, help_ in KINDS.values()), per_row=2)
        elif sub == "strat":
            if arg == "cur" or arg in STRATEGY_NAMES:
                draft.strategy = "" if arg == "cur" else arg
                problem = self._validate(draft)
                if problem:
                    note = problem
                else:
                    self.save(draft)
            else:
                from control_panel import STRATEGY_SHORT
                current = self.bot.cfg.strategy
                options = [(("✅ " if not draft.strategy else "") + f"Botun stratejisi ({current})", "bt:strat:cur")]
                options += [(("✅ " if draft.strategy == n else "") + STRATEGY_SHORT.get(n, n), f"bt:strat:{n}")
                            for n in STRATEGY_NAMES]
                return self._pick(mid, "🧭 Backtest stratejisi", options,
                                  "<i>Başka gruba (5 dk / 1 saat / rotasyon) geçilirse çıkışlar o grubun önerilen "
                                  "değerleriyle denenir; Test ayarlarındaki değişiklikler yine de geçerlidir.</i>")
        elif sub == "days":
            if arg is not None and arg.isdigit():
                draft.days = int(arg)
                self.save(draft)
            else:
                auto = default_days(draft.kind, strategy_of(draft, self.bot.cfg))
                options = [(("✅ " if not draft.days else "") + f"Otomatik ({auto})", "bt:days:0")]
                options += [(("✅ " if draft.days == d else "") + f"{d} gün", f"bt:days:{d}") for d in DAY_CHOICES]
                return self._pick(mid, "🗓️ Backtest süresi", options,
                                  "<i>Uzun dönem daha güvenilir sonuç verir ama ilk seferde veri indirmesi uzun sürer "
                                  "(5 dk mumlarla 1 yıl ≈ 10-20 dk).</i>", per_row=3)
        elif sub == "end":
            if arg in END_CHOICES:
                draft.end = arg
                self.save(draft)
            else:
                labels = {"today": "Bugün", "prev": "⏮️ Önceki dönem (doğrulama)"}
                options = [(("✅ " if draft.end == e else "") + labels.get(e, f"{e} gün önce"), f"bt:end:{e}")
                           for e in END_CHOICES]
                return self._pick(mid, "📅 Dönem bitişi", options,
                                  "<i>Önceki dönem: seçtiğiniz süre kadar daha eski bir dönem. Bulduğunuz ayarları, "
                                  "seçerken kullanmadığınız bir dönemde doğrulamak için.</i>")
        elif sub == "cap":
            if arg in CAPITAL_CHOICES:
                draft.capital = arg
                self.save(draft)
            else:
                cfg, state = self.bot.cfg, self.bot.state
                labels = {"cfg": f"Ayardaki ({cfg.start_capital:g})", "bot": f"Botun güncel ({state.capital:.2f})"}
                options = [(("✅ " if draft.capital == c else "") + labels.get(c, f"{c} {cfg.quote_asset}"),
                            f"bt:cap:{c}") for c in CAPITAL_CHOICES]
                return self._pick(mid, "💼 Başlangıç sermayesi", options, per_row=2)
        elif sub == "coin":
            if arg is not None and arg.isdigit() and int(arg) in COIN_CHOICES:
                draft.coins = int(arg)
                self.save(draft)
            else:
                options = [(("✅ " if draft.coins == c else "") + f"{c} coin", f"bt:coin:{c}") for c in COIN_CHOICES]
                return self._pick(mid, "🪙 Coin sayısı (en hacimliler)", options,
                                  "<i>Çok coin daha fazla sinyal ama daha uzun indirme demektir.</i>", per_row=3)
        elif sub == "chart":
            draft.chart = not draft.chart
            self.save(draft)
        elif sub == "grid":
            return self.show_grids(mid)
        elif sub == "gridp" and arg in GRIDS:
            label, _flag, choices = GRIDS[arg]
            current = draft.grids.get(arg, "")
            options = [(("✅ " if c == current else "") + (c or "Otomatik"), f"bt:gridv:{arg}:{i}")
                       for i, c in enumerate(choices)]
            return self._pick(mid, f"🎚️ {label}", options, back="bt:grid", per_row=2)
        elif sub == "gridv" and arg in GRIDS and len(args) > 2 and args[2].isdigit():
            choices = GRIDS[arg][2]
            idx = int(args[2])
            if idx < len(choices):
                if choices[idx]:
                    draft.grids[arg] = choices[idx]
                else:
                    draft.grids.pop(arg, None)
                self.save(draft)
            return self.show_grids(mid)
        elif sub.startswith("ov"):
            return self.handle_overrides(sub, args[1:], mid, draft)
        self.show(mid, note)

    def _validate(self, draft: BacktestDraft) -> str:
        try:
            effective_config(draft, self.bot.cfg)
        except ConfigError as exc:
            return f"⚠️ {esc(str(exc).replace('Ayar hataları:', '').strip())}"
        return ""

    def _pick(self, mid, title: str, options: List[Tuple[str, str]], text: str = "", back: str = "bt",
              per_row: int = 1) -> None:
        rows = [options[i:i + per_row] for i in range(0, len(options), per_row)]
        self.proc.reply_menu(f"<b>{title}</b>" + (f"\n{text}" if text else ""), rows + [[("⬅️ Geri", back)]], mid)

    # ------------------------------------------------------------ ana ekran
    def show(self, mid: Optional[int] = None, note: str = "") -> None:
        bot, cfg = self.bot, self.bot.cfg
        draft = self.draft()
        today = self.today()
        days = effective_days(draft, cfg)
        end = end_date(draft, cfg, today)
        start = (end or today) - timedelta(days=days)
        capital = capital_of(draft, bot)
        strategy = strategy_of(draft, cfg)
        name, _flags, help_ = KINDS[draft.kind]
        warnings = []
        if draft.kind == "filtre" and strategy != "rebound":
            warnings.append("Filtre gevşetme yalnızca rebound için çalışır.")
        if draft.kind in ("ab", "filtre", "karsilastir", "deney") and is_rotation(strategy):
            warnings.append("Rotasyonda bu tür yerine Giriş taraması kullanın.")
        if draft.kind == "turnuva":
            warnings.append("Turnuva tüm stratejileri kendi çıkış kurallarıyla dener; strateji ve çıkış ayarı "
                            "seçimi kullanılmaz (filtre değişiklikleri geçerli).")
        problem = self._validate(draft)
        if problem:
            warnings.append(problem.replace("⚠️ ", "Ayar hatası: "))
        lines = ["🧪 <b>Backtest</b>", LINE,
                 f"📋 Tür: <b>{esc(name)}</b> — {esc(help_)}",
                 f"🧭 Strateji: {esc(strategy) if draft.kind != 'turnuva' else 'hepsi (turnuva)'}"
                 + ("" if draft.strategy else " (botun stratejisi)"),
                 f"🗓️ Dönem: {days} gün{'' if draft.days else ' (otomatik)'} — {start:%d.%m.%Y} → "
                 f"{(end or today):%d.%m.%Y}" + (" (önceki dönem)" if draft.end == "prev" else ""),
                 f"💼 Sermaye: {capital if capital is not None else cfg.start_capital:g} {cfg.quote_asset} | "
                 f"🪙 {draft.coins} coin | 🖼️ grafik {'açık' if draft.chart else 'kapalı'}"]
        if draft.kind in ("tara", "hepsi"):
            lines.append("🎚️ Tarama değerleri: " + ", ".join(
                f"{GRIDS[k][0].split(' ', 1)[1]} {draft.grids.get(k) or 'otomatik'}" for k in GRIDS))
        lines.append("⚙️ Bu teste özel ayarlar: " + (esc(", ".join(f"{k}={v}" for k, v in
                                                                   sorted(draft.overrides.items())))
                                                     if draft.overrides else "yok (botun ayarları)"))
        lines += [f"⚠️ {esc(w)}" for w in warnings]
        if self.runner.running:
            lines.append(f"⏳ <b>Çalışıyor</b> ({fmt_duration(self.runner.elapsed())}): "
                         f"{esc(self.runner.job.summary)}")
        lines += [LINE, "<i>Backtest ayrı bir süreçte çalışır; bot işlem yapmaya devam eder. İlk çalıştırmada veri "
                        "indirilir (birkaç dakika), sonraki çalıştırmalar önbellekten hızlıdır. Botun ayarları "
                        "değişmez.</i>"]
        if note:
            lines += ["", note]
        keyboard = [
            [(f"📋 Tür: {name}", "bt:kind"),
             (f"🧭 {strategy if draft.kind != 'turnuva' else 'hepsi'}", "bt:strat")],
            [(f"🗓️ {days} gün", "bt:days"), ("📅 Bitiş: " + ("önceki dönem" if draft.end == "prev" else
                                                            "bugün" if draft.end == "today" else f"-{draft.end} g"),
                                            "bt:end")],
            [(f"💼 {capital if capital is not None else cfg.start_capital:g} {cfg.quote_asset}", "bt:cap"),
             (f"🪙 {draft.coins} coin", "bt:coin")],
            [("⚙️ Test ayarları" + (f" ({len(draft.overrides)})" if draft.overrides else ""), "bt:ov")]
            + ([("🎚️ Tarama değerleri", "bt:grid")] if draft.kind in ("tara", "hepsi") else []),
            [(("✅" if draft.chart else "⬜") + " Grafik", "bt:chart"), ("📄 Son sonuç", "bt:last")],
            [("⏹ Durdur", "bt:stop")] if self.runner.running else [("▶️ Başlat", "bt:go")],
            [("⬅️ Menü", "menu")],
        ]
        self.proc.reply_menu("\n".join(lines), keyboard, mid)

    def show_grids(self, mid: Optional[int]) -> None:
        draft = self.draft()
        lines = ["🎚️ <b>Çıkış taraması değerleri</b>", "Otomatik = botun stratejiye göre seçtiği değerler.",
                 "<i>Her birleşim aynı veriyle denenir; çok değer seçmek süreyi uzatır.</i>"]
        options = [(f"{label}: {draft.grids.get(k) or 'otomatik'}", f"bt:gridp:{k}") for k, (label, _f, _c) in
                   GRIDS.items()]
        self.proc.reply_menu("\n".join(lines), [[o] for o in options] + [[("⬅️ Backtest", "bt")]], mid)

    # ------------------------------------------------------------ teste özel ayarlar
    def handle_overrides(self, sub: str, args: List[str], mid, draft: BacktestDraft) -> None:
        base = base_config(draft, self.bot.cfg)
        note = ""
        changes: Optional[Dict[str, Optional[str]]] = None
        if sub == "ovp" and args and args[0] in PRESETS:
            return self._show_presets(args[0], draft, mid)
        if sub == "ovv" and len(args) > 1 and args[0] in PRESETS and args[1].isdigit():
            options = PRESETS[args[0]][1]
            if int(args[1]) < len(options):
                changes = dict(options[int(args[1])])
                if changes.get("BREAKEVEN_ENABLED") == "false":   # Kapalıyken tetik / taban değişikliği anlamsız
                    changes.update({"BREAKEVEN_TRIGGER_PCT": None, "BREAKEVEN_FLOOR_PCT": None})
        elif sub == "ovt" and args:
            key = args[0]
            if key in dict(TOGGLES):
                current = getattr(self._effective_or(base, draft), key.lower())
                changes = {key: format_value(not current)}
        elif sub == "ovm" and args:
            key = args[0]
            if key in dict(MODES):
                current = getattr(self._effective_or(base, draft), key.lower())
                changes = {key: "off" if current == "auto" else "auto"}
        elif sub == "ovx":
            draft.overrides = {}
            self.save(draft)
            note = "♻️ Teste özel ayarlar temizlendi: botun ayarlarıyla denenecek."
        if changes is not None:
            candidate = dict(draft.overrides)
            for key, value in changes.items():
                if value is None or format_value(getattr(base, key.lower())) == value:
                    candidate.pop(key, None)          # Botun değeriyle aynı: değişiklik sayılmaz
                else:
                    candidate[key] = value
            trial = BacktestDraft.from_dict({**draft.to_dict(), "overrides": candidate})
            problem = self._validate(trial)
            if problem:
                note = problem
            else:
                draft.overrides = candidate
                self.save(draft)
        self.show_overrides(mid, note)

    @staticmethod
    def _effective_or(base: Config, draft: BacktestDraft) -> Config:
        try:
            return apply_overrides(base, draft.overrides)
        except ConfigError:
            return base

    def show_overrides(self, mid: Optional[int], note: str = "") -> None:
        draft = self.draft()
        base = base_config(draft, self.bot.cfg)
        eff = self._effective_or(base, draft)
        mark = lambda keys: "✏️" if any(k in draft.overrides for k in keys) else ""  # noqa: E731
        hold = eff.max_hold_minutes
        lines = ["⚙️ <b>Bu teste özel ayarlar</b> (botun ayarları değişmez)", LINE,
                 f"🎯 Kâr Al +%{eff.take_profit_pct:g} | 🛑 Zarar Kes -%{eff.stop_loss_pct:g}",
                 f"📈 İzleyen stop +%{eff.trailing_activation_pct:g} → +%{eff.trailing_floor_pct:g} | 🔒 Başabaş "
                 + (f"+%{eff.breakeven_trigger_pct:g} → +%{eff.breakeven_floor_pct:g}" if eff.breakeven_enabled
                    else "kapalı"),
                 f"⏱️ Maks. tutma: {f'{hold} dk' if hold else 'sınırsız'} | 📦 En fazla {eff.max_open_positions} "
                 f"pozisyon | 💰 İlk giriş %{eff.capital_usage_pct:g}",
                 f"🧊 Takılan işlem çıkışı {f'{eff.stale_exit_minutes} dk' if eff.stale_exit_minutes else 'kapalı'} | "
                 f"⏳ Zarardan sonra bekleme {max(eff.loss_cooldown_minutes, eff.symbol_cooldown_minutes)} dk",
                 f"⏳ Tekrar giriş bekleme {eff.symbol_cooldown_minutes} dk | 🔁 Zarar serisi "
                 f"{eff.loss_streak_limit or 'kapalı'} | 📉 Günlük limit "
                 + (f"-%{eff.daily_loss_limit_pct:g}" if eff.daily_loss_limit_pct else "kapalı"),
                 f"📏 Hedef alanı {f'{eff.min_room_ratio:g}' if eff.min_room_ratio else 'kapalı'} | 📉 24s düşüş en "
                 f"fazla %{eff.max_24h_drop_pct:g} | 🏅 Puan en az "
                 f"{f'{eff.min_signal_score:g}' if eff.min_signal_score else 'kapalı'}",
                 "<i>✏️ = bu test için değiştirildi. Akıllı yönetim modları backtest'te yalnızca kapalı / otomatik "
                 "olabilir.</i>"]
        if eff.strategy == "rsi_divergence" or eff.rsi_div_filter:
            lines.insert(-1, f"📐 Uyumsuzluk: ilk dipte RSI ≤ {eff.rsi_div_oversold:g}, ikinci dipte en az "
                             f"+{eff.rsi_div_rise:g} puan | trend filtresi "
                             f"{'açık' if eff.rsi_div_trend_filter else 'kapalı'}"
                             + (" | teyit filtresi açık" if eff.rsi_div_filter else ""))
        if note:
            lines += ["", note]
        preset_buttons = [(f"{mark(PRESETS[g][1][0].keys())}{PRESETS[g][0].split(' (')[0]}", f"bt:ovp:{g}")
                          for g in PRESETS]
        toggle_buttons = [(f"{'✅' if getattr(eff, k.lower()) else '⬜'}{'✏️' if k in draft.overrides else ''} {label}",
                           f"bt:ovt:{k}") for k, label in TOGGLES]
        mode_buttons = [(f"{'✏️' if k in draft.overrides else ''}{label}: "
                         f"{'otomatik' if getattr(eff, k.lower()) == 'auto' else 'kapalı'}", f"bt:ovm:{k}")
                        for k, label in MODES]
        buttons = preset_buttons + toggle_buttons + mode_buttons
        keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        keyboard += [[("♻️ Hepsini sıfırla", "bt:ovx"), ("⬅️ Backtest", "bt")]]
        self.proc.reply_menu("\n".join(lines), keyboard, mid)

    def _show_presets(self, group: str, draft: BacktestDraft, mid) -> None:
        title, options = PRESETS[group]
        eff = self._effective_or(base_config(draft, self.bot.cfg), draft)

        def label(option: Dict[str, str]) -> str:
            current = all(format_value(getattr(eff, k.lower())) == v for k, v in option.items())
            values = [v for k, v in option.items() if k != "BREAKEVEN_ENABLED"]
            if option.get("BREAKEVEN_ENABLED") == "false":
                text = "kapalı"
            elif option.get("MAX_HOLD_MINUTES") == "0":
                text = "sınırsız"
            elif set(option) <= {"LOSS_STREAK_LIMIT", "DAILY_LOSS_LIMIT_PCT", "STALE_EXIT_MINUTES", "MIN_ROOM_RATIO",
                                 "MIN_SIGNAL_SCORE"} and values == ["0"]:
                text = "kapalı"
            else:
                text = " → ".join(values)
            return ("✅ " if current else "") + text

        buttons = [(label(o), f"bt:ovv:{group}:{i}") for i, o in enumerate(options)]
        self._pick(mid, title, buttons, "<i>Yalnızca bu backtest için.</i>", back="bt:ov", per_row=3)
