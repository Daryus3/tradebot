"""
Kripto Al-Sat Botu — giriş noktası.

Kullanım:
    python main.py                     # ayarlar.env / .env'deki TRADING_MODE ile çalışır (varsayılan: paper = test)
    python main.py --mode paper        # 11 USDT sanal bakiye, canlı verilerle simülasyon
    python main.py --mode live         # GERÇEK para ile işlem (onay ister)
    python main.py --selftest          # Bağlantı ve risk kurallarını test eder
    python main.py --istatistik        # İşlem geçmişinden performans istatistikleri
    python main.py --backtest --gun 30 # Stratejiyi geçmiş verilerle dener
    python main.py --backtest --gun 90 --tara       # Aynı veriyle farklı TP/SL/süre ayarlarını karşılaştırır
    python main.py --backtest --gun 90 --tara-giris # Aynı veriyle farklı giriş filtrelerini karşılaştırır
    python main.py --backtest --gun 90 --tara-filtre # rebound filtrelerini tek tek gevşetip işlem sayısına etkisini gösterir
    python main.py --backtest --gun 90 --strateji-karsilastir  # rebound / trend / breakout stratejilerini karşılaştırır
    python main.py --backtest --gun 90 --strateji trend --tara # .env'yi değiştirmeden başka stratejiyle dener
    python main.py --backtest --gun 90 --sermaye 25 # Farklı bir başlangıç sermayesiyle dener
    python main.py --backtest --gun 90 --bitis 2026-07-01  # Bu tarihte biten (daha eski) bir dönemi dener
    python main.py --backtest --gun 365 --strateji swing   # Uzun vadeli strateji: 1 saatlik mumlar, 1 yıllık veri
    python main.py --backtest --strateji swing --strateji-karsilastir  # swing / swing_pullback karşılaştırması
    python main.py --backtest --strateji rotation  # Haftalık momentum rotasyonu: günlük mumlar, 3 yıllık veri
    python main.py --backtest --strateji mean_revert   # Büyük coinlerde trend içi aşırı satım alımı (küçük hedef)
    python main.py --backtest --strateji rsi_divergence   # Pozitif RSI uyumsuzluğu + dönüş teyidiyle alım (1 saat)
    python main.py --backtest --turnuva             # Tüm strateji türleri × çıkış profilleri aynı yılda, sıralı tablo
    python main.py --backtest --gun 90 --tara-ab    # Her giriş filtresini açıp / kapatıp kâra etkisini karşılaştırır
    python main.py --backtest --ayar TAKE_PROFIT_PCT=3 --ayar VWAP_FILTER=true  # Ayarı yalnızca bu test için değiştirir
Telegram'dan da çalıştırılabilir: /backtest (düğmeli menü; ayrı süreçte çalışır, sonuçlar Telegram'a gelir).
    python main.py --telegram-chat-id  # Telegram chat ID'nizi bulur
    python main.py --reset-state       # Kayıtlı durumu (K/Z, kill-switch) sıfırlar

Ayarlar: ayarlar.env (GitHub'da, gizli bilgi yok) + .env (sunucuda; API anahtarı ve Telegram bilgileri).
Sunucuda systemd servisi olarak çalışır: deploy/tradebot.service, kurulum VPS_KURULUM.md.

Çıkış kodları (systemd servisi bunlara göre yeniden başlatıp başlatmayacağına karar verir):
    0 = normal durdurma (Ctrl+C veya Telegram /kapat)   → yeniden başlatılmaz
    1 = ayar / kullanıcı hatası                           → yeniden başlatılmaz
    2 = kill-switch tetiklendi                            → ASLA yeniden başlatılmaz
    3 = beklenmedik hata / çökme, Telegram'dan yeniden başlatma → yeniden başlatılır
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Optional

from config import (BASE_DIR, DATA_DIR, LOG_DIR, STRATEGY_NAMES, ConfigError, is_long_horizon, is_mean_reversion,
                    is_rotation, load_config)
from logger import redact, register_secrets, setup_logging

EXIT_OK, EXIT_CONFIG, EXIT_KILLED, EXIT_CRASH = 0, 1, 2, 3
NETWORK_RETRY_SEC = 30


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse varsayılanı 2 ile çıkar; 2 kill-switch'e ayrıldı
        self.print_usage(sys.stderr)
        self.exit(EXIT_CONFIG, f"❌ Hatalı komut: {message}\n")


def parse_args(argv=None) -> argparse.Namespace:
    parser = _Parser(description="Kripto Para Al-Sat Botu (Binance Spot)")
    parser.add_argument("--mode", choices=["paper", "live"], help="Çalışma modu (.env'deki TRADING_MODE'u ezer)")
    parser.add_argument("--selftest", action="store_true", help="Bağlantı ve kural testlerini çalıştırır")
    parser.add_argument("--istatistik", action="store_true", help="Performans istatistiklerini gösterir")
    parser.add_argument("--backtest", action="store_true", help="Stratejiyi geçmiş verilerle dener")
    parser.add_argument("--gun", type=int, default=None,
                        help="Backtest gün sayısı (varsayılan: 5 dk stratejilerinde 30, swing stratejilerinde 365, "
                             "rotation'da 1095)")
    parser.add_argument("--coin-sayisi", type=int, default=40, help="Backtest'te kullanılacak coin sayısı")
    parser.add_argument("--sermaye", type=float, default=None,
                        help="Backtest başlangıç sermayesi (.env'deki START_CAPITAL_USDT yerine)")
    parser.add_argument("--bitis", default=None,
                        help="Backtest döneminin bitiş tarihi YYYY-AA-GG (varsayılan: şimdi). Bulunan ayarları "
                             "test edilen dönemle çakışmayan daha eski bir dönemde doğrulamak için")
    parser.add_argument("--tara", action="store_true",
                        help="Backtest: aynı veriyle farklı Kâr Al / Zarar Kes / süre ayarlarını karşılaştırır")
    parser.add_argument("--tara-giris", action="store_true",
                        help="Backtest: aynı veriyle farklı giriş filtrelerini (piyasa rejimi, 24s düşüş, sinyal "
                             "puanı, hedef alanı) karşılaştırır; çıkış ayarları .env'deki gibi kalır")
    parser.add_argument("--tara-filtre", action="store_true",
                        help="Backtest (rebound): sinyal filtrelerini tek tek gevşetip işlem sayısına ve işlem başı "
                             "sonuca etkisini gösterir; çıkış ayarları .env'deki gibi kalır")
    parser.add_argument("--tara-ab", action="store_true",
                        help="A/B filtre testi: her giriş filtresini / risk sigortasını tek tek açıp kapatarak kazanma "
                             "oranına ve net kâra etkisini aynı veriyle karşılaştırır")
    parser.add_argument("--grafiksiz", action="store_true",
                        help="Backtest K/Z dağılım grafiğini çizme / Telegram'a gönderme")
    parser.add_argument("--turnuva", action="store_true",
                        help="Backtest: ortalamaya dönüş / swing / rotasyon stratejilerini farklı çıkış kuralları ve "
                             "piyasa filtresiyle aynı dönemde (varsayılan 365 gün) dener, kazanma oranı ve işlem başı "
                             "kâra göre sıralar")
    parser.add_argument("--strateji", choices=list(STRATEGY_NAMES), default=None,
                        help="Backtest için strateji (.env'deki STRATEGY yerine). Başka tür bir stratejiye "
                             "(5 dk → swing / rotation) geçilirse çıkışlar o tür için önerilen değerlerle denenir")
    parser.add_argument("--strateji-karsilastir", action="store_true",
                        help="Backtest: aynı mum aralığındaki stratejileri aynı veriyle karşılaştırır "
                             "(5 dk: rebound / trend / breakout, 1 saat: swing / swing_pullback, "
                             "mean_revert / rsi_divergence)")
    parser.add_argument("--tp", default=None, help="--tara için Kâr Al değerleri, örn. 2,3,5")
    parser.add_argument("--sl", default=None, help="--tara için Zarar Kes değerleri, örn. 2,3.5")
    parser.add_argument("--iz", default=None, help="--tara için izleyen stop başlangıçları, örn. 1,2")
    parser.add_argument("--sure", default=None,
                        help="--tara için maks. tutma süreleri (dk), örn. 60,120,240 (swing: 1440,4320,7200)")
    parser.add_argument("--alan", default=None, help="--tara için MIN_ROOM_RATIO değerleri, örn. 0,1")
    parser.add_argument("--ayar", action="append", default=[], metavar="ANAHTAR=DEĞER",
                        help="Backtest: bir ayarı YALNIZCA bu test için değiştirir (birden çok kez yazılabilir), örn. "
                             "--ayar TAKE_PROFIT_PCT=3 --ayar VWAP_FILTER=true. .env / ayarlar.env değişmez")
    parser.add_argument("--cikti-klasoru", default=None,
                        help="Backtest: CSV dosyalarını ve grafiği bu klasöre yazar, Telegram'a kendisi göndermez "
                             "(Telegram'dan başlatılan backtest'ler için)")
    parser.add_argument("--reset-state", action="store_true", help="Kayıtlı durumu sıfırlar (kill-switch dahil)")
    parser.add_argument("--telegram-chat-id", action="store_true", help="Telegram chat ID'nizi bulmanıza yardım eder")
    parser.add_argument("--yes", action="store_true", help="Onay sorularını atlar")
    parser.add_argument("--env", default=None, help="Farklı bir .env dosyası yolu")
    return parser.parse_args(argv)


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().upper() in {"EVET", "E", "YES"}
    except EOFError:
        return False


def find_chat_id(cfg) -> int:
    from telegram_bot import discover_chat_ids

    if not cfg.telegram_bot_token:
        print("❌ Önce .env dosyasına TELEGRAM_BOT_TOKEN yazın.")
        return EXIT_CONFIG
    print("🔍 Bota gelen mesajlar kontrol ediliyor...")
    try:
        chats = discover_chat_ids(cfg.telegram_bot_token)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Telegram'a bağlanılamadı: {redact(str(exc))}")
        return EXIT_CONFIG
    if not chats:
        print("⚠️ Hiç mesaj bulunamadı. Telegram'da botunuzu açıp /start yazın, sonra bu komutu tekrar çalıştırın.")
        return EXIT_CONFIG
    print("✅ Bulunan sohbetler:")
    for chat_id, name in chats:
        print(f"   TELEGRAM_CHAT_ID={chat_id}    ({name})")
    print("Bu satırı .env dosyanıza kopyalayın.")
    return EXIT_OK


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.env, mode_override=args.mode)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return EXIT_CONFIG

    register_secrets(cfg.binance_api_key, cfg.binance_api_secret, cfg.telegram_bot_token)
    # Backtest ayrı log dosyasına yazar: bot çalışırken aynı dosyayı iki süreç döndürmesin (RotatingFileHandler)
    log = setup_logging(cfg.log_level, LOG_DIR / ("backtest.log" if args.backtest else "bot.log"))
    for note in cfg.notes:
        log.warning("⚠️ %s", note)

    if args.telegram_chat_id:
        return find_chat_id(cfg)

    if args.selftest:
        from selftest import run_selftest
        return run_selftest(cfg)

    if args.istatistik:
        return show_stats(cfg)

    if args.backtest:
        return run_backtest_cli(cfg, args)

    from state import StateStore

    store = StateStore(DATA_DIR / f"state_{cfg.trading_mode}.json")

    if args.reset_state:
        if not store.exists():
            print(f"ℹ️ '{cfg.trading_mode}' modu için kayıtlı durum yok.")
            return EXIT_OK
        if not args.yes and not confirm(
                f"⚠️ '{cfg.trading_mode}' modunun kayıtlı durumu (K/Z, açık pozisyon kaydı, kill-switch) "
                f"sıfırlanacak. Devam için EVET yazın: "):
            print("İptal edildi.")
            return EXIT_CONFIG
        store.reset()
        print("✅ Durum sıfırlandı (eski dosya .bak.json olarak yedeklendi).")
        return EXIT_OK

    try:
        return run_bot(cfg, store, args, log)
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        return EXIT_OK
    except Exception:  # noqa: BLE001 - beklenmedik çökme: systemd yeniden başlatsın
        log.exception("Bot beklenmedik şekilde durdu")
        return EXIT_CRASH


def must_refuse_killed(state) -> bool:
    """Kill-switch tetiklenmiş ve tüm pozisyonlar satılmışsa bot başlamaz (kilit).
    Satılamamış pozisyon kaldıysa bot yalnızca onları satmak için başlar (yeni alım yapmaz)."""
    return bool(state.killed and not state.positions)


def show_stats(cfg) -> int:
    import stats
    from telegram_bot import _strip_html

    path = DATA_DIR / f"trades_{cfg.trading_mode}.csv"
    trades = stats.load_trades(path)
    if not trades:
        print(f"📭 Henüz kapanmış işlem yok ({path}).")
        return EXIT_OK
    from state import StateStore
    state = StateStore(DATA_DIR / f"state_{cfg.trading_mode}.json").load()
    realized = state.realized_pnl if state else sum(t["kar_zarar_usdt"] for t in trades)
    st = stats.compute_stats(trades)
    limit = (cfg.loss_limit(state.start_capital, state.capital_added) if state
             else cfg.loss_limit(cfg.start_capital))
    prob = stats.kill_switch_probability([t["kar_zarar_usdt"] for t in trades], realized, limit)
    print(_strip_html(stats.format_stats(st, cfg.quote_asset, realized, limit,
                                         0.0 if is_rotation(cfg.strategy) else cfg.take_profit_pct,
                                         cfg.stop_loss_pct, prob)))
    return EXIT_OK


def _number_list(text, name: str, cast=float, allow_zero: bool = False):
    """'2,3.5,5' → [2.0, 3.5, 5.0]; boşsa None."""
    if not text:
        return None
    try:
        values = [cast(float(x.strip())) for x in text.split(",") if x.strip()]
    except ValueError:
        raise ValueError(f"--{name} virgülle ayrılmış sayılar olmalı, örn. 2,3.5 (girilen: {text!r})") from None
    if not values or any(v < 0 or (v == 0 and not allow_zero) or v != v for v in values):
        raise ValueError(f"--{name} sıfırdan büyük sayılar içermeli (girilen: {text!r})")
    return values


def _end_date_ms(text) -> Optional[int]:
    """'2026-07-01' → o günün başlangıcı (yerel saat, ms); boşsa None (= şimdi)."""
    if not text:
        return None
    try:
        day = datetime.strptime(text.strip(), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"--bitis YYYY-AA-GG biçiminde olmalı, örn. 2026-07-01 (girilen: {text!r})") from None
    if day.timestamp() > time.time():
        raise ValueError(f"--bitis gelecekte olamaz (girilen: {text})")
    return int(day.timestamp() * 1000)


def run_backtest_cli(cfg, args) -> int:
    from dataclasses import replace

    import backtest
    from binance_client import BinanceClient
    from config import PROFILE_ATTRS, PROFILES, short_entry_filters, strategy_family, with_strategy

    from pathlib import Path

    from runtime_settings import apply_overrides, parse_override_args

    out_dir = Path(args.cikti_klasoru) if args.cikti_klasoru else DATA_DIR
    if args.sermaye is not None:
        if args.sermaye <= 0:
            print("❌ --sermaye sıfırdan büyük olmalı")
            return EXIT_CONFIG
        cfg = replace(cfg, start_capital=args.sermaye)
    if args.strateji:
        env_strategy = cfg.strategy
        cfg, profile = with_strategy(cfg, args.strateji)
        if profile:
            kind = ("haftalarca tutulan rotasyon işlemleri" if is_rotation(cfg.strategy) else
                    "saatler-2 gün süren küçük hedefli dönüş işlemleri" if is_mean_reversion(cfg.strategy)
                    else "1-5 gün süren işlemler")
            print(f"ℹ️ .env'deki çıkış ayarları {env_strategy} stratejisi içindir; {cfg.strategy}, {kind} "
                  f"için önerilen ayarlarla deneniyor:")
            print("   " + " ".join(f"{k}={getattr(cfg, PROFILE_ATTRS[k]):g}"
                                   for k in PROFILES[strategy_family(cfg.strategy)]))
            print(f"   Kendi çıkış ayarlarınızla denemek için .env'de STRATEGY={cfg.strategy} yazıp çıkışları "
                  f"değiştirin.")
        # Karşılaştırma puan / alan filtresini kapatır: orada yalnızca diğer engeller geçerlidir
        blocking = short_entry_filters(replace(cfg, min_room_ratio=0.0) if args.strateji_karsilastir else cfg)
        if is_long_horizon(cfg.strategy) and blocking:
            print("⚠️ Bu test şu giriş filtreleriyle yapılıyor: " + "; ".join(blocking))
        if strategy_family(env_strategy) != "short" and strategy_family(cfg.strategy) == "short":
            print(f"⚠️ .env'deki çıkış ayarları {env_strategy} stratejisi içindir; {cfg.strategy} (5 dk) bu "
                  f"çıkışlarla deneniyor.")
    if args.ayar:   # Strateji profilinden SONRA: kullanıcının yazdığı değer her zaman geçerli
        try:
            overrides = parse_override_args(args.ayar)
            cfg = apply_overrides(cfg, overrides)
        except ConfigError as exc:
            print(f"❌ {exc}")
            return EXIT_CONFIG
        print("⚙️ Yalnızca bu test için değiştirilen ayarlar: " + " ".join(f"{k}={v}" for k, v in overrides.items()))
    days = args.gun if args.gun is not None else (
        1095 if is_rotation(cfg.strategy) and not args.turnuva else
        365 if args.turnuva or is_long_horizon(cfg.strategy) or is_mean_reversion(cfg.strategy) else 30)
    if days <= 0:
        print("❌ --gun sıfırdan büyük olmalı")
        return EXIT_CONFIG
    try:
        grid = {"tps": _number_list(args.tp, "tp"), "sls": _number_list(args.sl, "sl"),
                "trails": _number_list(args.iz, "iz"),
                "holds": _number_list(args.sure, "sure", int, allow_zero=True),   # 0 = süre sınırı yok
                "rooms": _number_list(args.alan, "alan", allow_zero=True)}        # 0 = filtre kapalı
        end_ms = _end_date_ms(args.bitis)
    except ValueError as exc:
        print(f"❌ {exc}")
        return EXIT_CONFIG

    period = f"{args.bitis} tarihinde biten {days} günün" if end_ms else f"Son {days} günün"
    print(f"⏳ {period} verisi indiriliyor ve strateji deneniyor (ilk seferde birkaç dakika sürebilir)...")
    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)
    compare = args.strateji_karsilastir and not is_rotation(cfg.strategy)
    if args.strateji_karsilastir and not compare:
        print("ℹ️ Rotasyon tek stratejidir (günlük mumlarla başka strateji yok); karşılaştırma yerine normal rapor "
              "gösteriliyor (BTC'yi alıp tutmakla kıyaslı). Ayarları denemek için --tara ve --tara-giris kullanın.")
    if args.turnuva:
        print("🏆 Strateji turnuvası: saatlik veriyle 3 giriş stratejisi × 3 çıkış kuralı × piyasa filtresi ve günlük "
              "veriyle rotasyon aynı dönemde deneniyor (ilk seferde veri indirilir; 5-20 dk sürebilir)...")
        try:
            result = backtest.run_tournament(cfg, client, days=days, max_symbols=args.coin_sayisi, progress=print,
                                             end_time_ms=end_ms)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ Turnuva çalıştırılamadı: {exc}")
            return EXIT_CONFIG
        print(backtest.format_tournament_report(result, cfg))
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            path = backtest.save_sweep_csv([t.row for t in result.rows], out_dir / f"turnuva_{stamp}.csv")
            print(f"📄 Tüm {len(result.rows)} satır Excel ile açılabilir dosyada: {path}")
        except OSError as exc:
            print(f"⚠️ Turnuva dosyası yazılamadı: {exc}")
        return EXIT_OK
    try:
        filters = args.tara_filtre and cfg.strategy == "rebound"
        if args.tara_filtre and not filters:
            print(f"ℹ️ --tara-filtre yalnızca rebound stratejisinin filtrelerini dener ({cfg.strategy} için "
                  f"--tara-giris kullanın).")
        ab = args.tara_ab and not is_rotation(cfg.strategy)
        if args.tara_ab and not ab:
            print("ℹ️ --tara-ab giriş filtrelerini dener; rotasyonda bu filtreler kullanılmaz (--tara-giris kullanın).")
        if not (args.tara or args.tara_giris or compare or filters or ab):
            result = backtest.run_backtest(cfg, client, days=days, max_symbols=args.coin_sayisi, end_time_ms=end_ms)
            report = backtest.format_backtest_report(result, cfg)
            print(report)
            if not args.grafiksiz and getattr(result, "trades", None):
                _send_backtest_chart(cfg, result, report, out_dir, send=not args.cikti_klasoru)
            return EXIT_OK
        data = backtest.prepare_backtest(cfg, client, days=days, max_symbols=args.coin_sayisi, end_time_ms=end_ms)
        cache: dict = {}   # Giriş analizleri taramalar arasında da paylaşılır (anahtar stratejiyi içerir)
        reports = []
        if compare:
            combos = backtest.strategy_combos(cfg)
            print(f"🧪 Strateji karşılaştırması: {', '.join(st['STRATEGY'] for st, _ in combos)} aynı veriyle "
                  f"deneniyor...")
            reports.append(("strateji", backtest.run_sweep(cfg, data, progress=print, combos=combos,
                                                           signal_cache=cache)))
        if args.tara:
            combos = backtest.sweep_combos(cfg, **{k: v for k, v in grid.items() if v is not None})
            print(f"🧪 Çıkış taraması: {len(combos)} ayar aynı veriyle denenecek (giriş analizleri bir kez "
                  f"yapılır, sonra hızlanır)...")
            reports.append(("cikis", backtest.run_sweep(cfg, data, progress=print, combos=combos,
                                                        signal_cache=cache)))
        if args.tara_giris:
            if is_rotation(cfg.strategy):
                print("🧪 Giriş taraması: momentum süresi / BTC trend filtresi / tutulan coin sayısı deneniyor...")
            else:
                what = ("zirve penceresi" if is_long_horizon(cfg.strategy)
                        else "24s düşüş" if cfg.strategy == "rebound" else "24s yükseliş")
                print(f"🧪 Giriş taraması: piyasa rejimi / {what} / sinyal puanı / hedef alanı filtreleri "
                      f"deneniyor...")
            reports.append(("giris", backtest.run_entry_sweep(cfg, data, progress=print, signal_cache=cache)))
        if filters:
            print("🧪 Filtre taraması: her sinyal filtresi tek tek gevşetilip deneniyor (sinyal filtreleri değiştiği "
                  "için her satır analizleri yeniden yapar; birkaç dakika sürebilir)...")
            reports.append(("filtre", backtest.run_filter_sweep(cfg, data, progress=print, signal_cache=cache)))
        if ab:
            print("🧪 A/B filtre testi: her filtre / sigorta tek tek açılıp kapatılarak aynı veriyle deneniyor...")
            reports.append(("ab", backtest.run_ab_test(cfg, data, progress=print, signal_cache=cache)))
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Backtest çalıştırılamadı: {exc}")
        return EXIT_CONFIG
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    for kind, rows in reports:
        print(backtest.format_filter_report(rows, cfg, data) if kind == "filtre"
              else backtest.format_ab_report(rows, cfg, data) if kind == "ab"
              else backtest.format_sweep_report(rows, cfg, data, kind=kind))
        try:
            path = backtest.save_sweep_csv(rows, out_dir / f"tarama_{kind}_{stamp}.csv")
            print(f"📄 Tüm {len(rows)} satır Excel ile açılabilir dosyada: {path}")
        except OSError as exc:
            print(f"⚠️ Tarama dosyası yazılamadı: {exc}")
        print()
    return EXIT_OK


def _send_backtest_chart(cfg, result, report: str, out_dir=DATA_DIR, send: bool = True) -> None:
    """K/Z dağılım grafiğini `out_dir` klasörüne kaydeder; Telegram ayarlıysa (ve send) rapor ve grafiği oraya da
    gönderir (sunucuda resim açılamadığı için grafiği telefondan görmenin en kolay yolu). Telegram'dan başlatılan
    backtest'te (send=False) gönderimi bot yapar."""
    from charts import matplotlib_available, save_pnl_chart

    if not matplotlib_available():
        print("ℹ️ K/Z dağılım grafiği için matplotlib gerekir: .venv/bin/pip install matplotlib")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    rotation = is_rotation(cfg.strategy)
    try:
        path = save_pnl_chart(result.trades, out_dir / f"backtest_grafik_{stamp}.png",
                              f"Backtest: {cfg.strategy} — {result.days:g} gün",
                              None if rotation else cfg.take_profit_pct, cfg.stop_loss_pct)
    except Exception as exc:  # noqa: BLE001 - grafik hatası raporu engellemesin
        print(f"⚠️ Grafik çizilemedi: {exc}")
        return
    if path is None:
        return
    print(f"🖼️ K/Z dağılım grafiği: {path}")
    if not send or not cfg.has_telegram:
        return
    from telegram_bot import TelegramNotifier
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, async_mode=False)
    text_path = path.with_suffix(".txt")
    try:
        text_path.write_text(report, encoding="utf-8")
    except OSError:
        text_path = None
    try:
        sent = notifier.send_photo(path, f"📊 Backtest K/Z dağılımı — {cfg.strategy}, {result.days:g} gün, "
                                         f"{len(result.trades)} işlem")
        if text_path is not None:
            notifier.send_document(text_path, "📄 Backtest raporu")
    except Exception as exc:  # noqa: BLE001 - gönderim hatası backtest sonucunu etkilemesin
        print(f"⚠️ Grafik Telegram'a gönderilemedi: {exc}")
        return
    print("📤 Grafik ve rapor Telegram'a gönderildi." if sent else "⚠️ Grafik Telegram'a gönderilemedi.")


def is_transient_binance_error(exc) -> bool:
    """Binance bakım / aşırı yük gibi geçici sunucu hataları (ayar hatası DEĞİL)."""
    return (exc.status or 0) >= 500 or exc.code in (-1001, -1006, -1007, -1008)


def wait_for_network(fn, what: str, cfg, log):
    """İnternet yoksa kapanmak yerine bekler ve tekrar dener (WAIT_FOR_NETWORK=true)."""
    import requests

    from binance_client import BinanceAPIError, RateLimitError

    announced = False
    while True:
        try:
            return fn()
        except RateLimitError as exc:
            log.warning("%s: Binance istek limiti, %s sn bekleniyor", what, exc.retry_after)
            time.sleep(exc.retry_after)
        except (requests.RequestException, BinanceAPIError) as exc:
            if isinstance(exc, BinanceAPIError) and not is_transient_binance_error(exc):
                raise  # Gerçek ayar hatası (geçersiz anahtar, izin yok...)
            if not cfg.wait_for_network:
                raise
            log.warning("%s: Binance'e bağlanılamadı (%s)", what, exc)
            if not announced:
                print(f"📶 İnternet / Binance bağlantısı yok. {NETWORK_RETRY_SEC} sn'de bir tekrar denenecek "
                      f"(çıkmak için Ctrl+C)...")
                announced = True
            time.sleep(NETWORK_RETRY_SEC)


def run_bot(cfg, store, args, log) -> int:
    from binance_client import BinanceClient
    from telegram_bot import TelegramNotifier

    if not cfg.is_paper:
        if not cfg.has_binance_keys:
            print("❌ Canlı mod için .env dosyasına BINANCE_API_KEY ve BINANCE_API_SECRET yazmalısınız.")
            return EXIT_CONFIG
        print("\n" + "!" * 64)
        print("  ⚠️  CANLI MOD: Bot GERÇEK PARANIZLA Binance'te işlem yapacak!")
        kill = (f"-{cfg.legacy_loss_usdt:g} {cfg.quote_asset}" if cfg.legacy_loss_usdt is not None
                else f"sermayenin %{cfg.max_total_loss_pct:g}'i")
        exits = (f"Rotasyon: en güçlü {cfg.rotation_top_n} coin, felaket zarar kesi -%{cfg.stop_loss_pct:g}"
                 if is_rotation(cfg.strategy) else f"TP +%{cfg.take_profit_pct:g} | SL -%{cfg.stop_loss_pct:g}")
        print(f"  Sermaye: {cfg.start_capital:g} {cfg.quote_asset} | Kill-switch: {kill} | {exits}")
        print("!" * 64)
        if not args.yes and not confirm("Devam etmek için EVET yazın: "):
            print("İptal edildi.")
            return EXIT_CONFIG

    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)
    prefix = "🧪 <i>[TEST MODU]</i>\n" if cfg.is_paper else ""
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, prefix=prefix,
                                quiet_window=cfg.quiet_window, max_queue=cfg.telegram_queue_max,
                                mode=cfg.notify_mode)
    if not notifier.enabled:
        log.warning("Telegram ayarlı değil; bildirimler sadece terminale yazılacak.")

    from pathlib import Path
    env_path = Path(args.env) if args.env else BASE_DIR / ".env"   # Telegram menüsünden değişen ayarlar buraya yazılır
    try:
        return _start_bot(cfg, store, client, notifier, log, env_path)
    finally:
        notifier.flush()  # Kuyruktaki Telegram mesajlarının gitmesini bekle


def _build_optional_parts(cfg, client, notifier, log):
    """Anlık fiyat akışı, Telegram komutları ve ek filtreler (yoksa bot onlarsız da çalışır)."""
    price_stream = listener = coin_filter = regime = None
    if cfg.use_websocket:
        try:
            from price_stream import PriceStream
            stream = PriceStream(cfg.binance_ws_url, stale_after=5.0)
            if stream.available:
                price_stream = stream
            else:
                log.warning("websocket-client kurulu değil; fiyatlar REST ile sorgulanacak "
                            "(pip install -r requirements.txt)")
        except Exception as exc:  # noqa: BLE001
            log.warning("Anlık fiyat akışı başlatılamadı: %s", exc)
    if cfg.telegram_commands and notifier.enabled:
        from commands import TelegramCommandListener
        listener = TelegramCommandListener(cfg.telegram_bot_token, cfg.telegram_chat_id)
    if cfg.delist_filter:
        from coin_filters import CoinSafetyFilter
        coin_filter = CoinSafetyFilter(client, cfg)
    if cfg.regime_filter:
        from market_regime import MarketRegime
        regime = MarketRegime(client, cfg)
    return price_stream, listener, coin_filter, regime


def _start_bot(cfg, store, client, notifier, log, env_path=None) -> int:
    from exchange import LiveExchange, PaperExchange
    from state import BotState, EquityLog, TradeLog
    from strategy import make_strategy
    from telegram_bot import Messages
    from trader import TradingBot

    try:
        state = store.load()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Durum dosyası okunamadı ({store.path}): {exc}\n   Sıfırlamak için: python main.py --reset-state")
        return EXIT_CONFIG

    if state and must_refuse_killed(state):
        msg = (f"🛑 Kill-switch daha önce tetiklenmiş: {state.kill_reason}\n"
               f"   Bot güvenlik nedeniyle başlatılmadı. Yeniden başlatmak için: "
               f"python main.py --mode {cfg.trading_mode} --reset-state")
        print(msg)
        notifier.send(Messages.warning(msg), critical=True)
        return EXIT_KILLED

    if state is None:
        start_capital = cfg.start_capital
        if not cfg.is_paper:
            def first_balance():
                client.sync_time()
                return LiveExchange(client, cfg.quote_asset, cfg.fee_rate).free_balance(cfg.quote_asset)

            from binance_client import BinanceAPIError
            try:
                free = wait_for_network(first_balance, "Bakiye okunamadı", cfg, log)
            except BinanceAPIError as exc:   # Geçersiz anahtar / IP / izin: yeniden denemek düzeltmez
                msg = f"Binance bakiyesi okunamadı: {exc}"
                if exc.code == -2015:
                    msg += ("\nAPI anahtarını, 'Enable Spot & Margin Trading' iznini ve IP kısıtlamasını (sunucunun "
                            "IP adresi) kontrol edin; .env'deki BINANCE_API_KEY / BINANCE_API_SECRET doğru mu?")
                print(f"❌ {msg}")
                notifier.send(Messages.error(f"Bot başlatılamadı: {msg}", fatal=True), critical=True)
                return EXIT_CRASH if is_transient_binance_error(exc) else EXIT_CONFIG
            if free <= 0:
                print(f"❌ Binance SPOT cüzdanınızda serbest {cfg.quote_asset} bakiyesi yok.\n"
                      f"   Bot Spot piyasada işlem yapar. Paranız Futures/Funding cüzdanındaysa Binance'te\n"
                      f"   Cüzdan → Transfer ile Spot cüzdanına aktarın (ücretsiz ve anında).")
                return EXIT_CONFIG
            if free < start_capital:
                log.warning("Serbest bakiye (%.2f) START_CAPITAL_USDT'den (%.2f) az; sermaye %.2f alındı.",
                            free, start_capital, free)
                start_capital = free
        state = BotState.new(cfg.trading_mode, start_capital, cfg.quote_asset)
        store.save(state)

    if cfg.is_paper:
        exchange = PaperExchange(client, state.paper_balances, cfg.quote_asset, cfg.fee_rate)
    else:
        exchange = LiveExchange(client, cfg.quote_asset, cfg.fee_rate)

    price_stream, listener, coin_filter, regime = _build_optional_parts(cfg, client, notifier, log)
    ip_monitor = None
    if cfg.ip_check_minutes > 0:
        from ip_monitor import IpMonitor
        ip_monitor = IpMonitor(cfg.ip_check_minutes * 60)
    bot = TradingBot(cfg, exchange, make_strategy(client, cfg, coin_filter=coin_filter, regime=regime),
                     notifier, store, state, TradeLog(DATA_DIR / f"trades_{cfg.trading_mode}.csv"),
                     equity_log=EquityLog(DATA_DIR / f"equity_{cfg.trading_mode}.csv"),
                     price_stream=price_stream, command_listener=listener, settings_path=env_path,
                     ip_monitor=ip_monitor)

    from binance_client import BinanceAPIError
    from exchange import OrderError
    try:
        wait_for_network(bot.startup, "Açılış", cfg, log)
    except (OrderError, BinanceAPIError) as exc:  # Örn. API anahtarı geçersiz / işlem izni yok
        print(f"❌ {exc}")
        notifier.send(Messages.error(f"Bot başlatılamadı: {exc}", fatal=True), critical=True)
        if isinstance(exc, BinanceAPIError) and is_transient_binance_error(exc):
            return EXIT_CRASH  # Geçici sunucu hatası: bekçi yeniden denesin
        return EXIT_CONFIG
    return bot.run(do_startup=False)


def _entry() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:  # noqa: BLE001 - yakalanmamış her hata "çökme" sayılır → bekçi yeniden başlatır
        import traceback
        traceback.print_exc()
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(_entry())
