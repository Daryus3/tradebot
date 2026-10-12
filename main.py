"""
Kripto Al-Sat Botu — giriş noktası.

Kullanım:
    python main.py                     # .env'deki TRADING_MODE ile çalışır (varsayılan: paper = test)
    python main.py --mode paper        # 11 USDT sanal bakiye, canlı verilerle simülasyon
    python main.py --mode live         # GERÇEK para ile işlem (onay ister)
    python main.py --selftest          # Bağlantı ve risk kurallarını test eder
    python main.py --kurulum           # Türkçe kurulum sihirbazı (.env dosyasını hazırlar)
    python main.py --istatistik        # İşlem geçmişinden performans istatistikleri
    python main.py --backtest --gun 30 # Stratejiyi geçmiş verilerle dener
    python main.py --telegram-chat-id  # Telegram chat ID'nizi bulur
    python main.py --reset-state       # Kayıtlı durumu (K/Z, kill-switch) sıfırlar

Çıkış kodları (watchdog.py bunlara göre yeniden başlatıp başlatmayacağına karar verir):
    0 = normal durdurma (Ctrl+C veya Telegram /kapat)   → yeniden başlatılmaz
    1 = ayar / kullanıcı hatası                           → yeniden başlatılmaz
    2 = kill-switch tetiklendi                            → ASLA yeniden başlatılmaz
    3 = beklenmedik hata / çökme                          → yeniden başlatılır
"""
from __future__ import annotations

import argparse
import sys
import time

from config import BASE_DIR, DATA_DIR, ConfigError, load_config
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
    parser.add_argument("--kurulum", action="store_true", help="Türkçe kurulum sihirbazı (.env hazırlar)")
    parser.add_argument("--istatistik", action="store_true", help="Performans istatistiklerini gösterir")
    parser.add_argument("--backtest", action="store_true", help="Stratejiyi geçmiş verilerle dener")
    parser.add_argument("--gun", type=int, default=30, help="Backtest gün sayısı (varsayılan 30)")
    parser.add_argument("--coin-sayisi", type=int, default=40, help="Backtest'te kullanılacak coin sayısı")
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
        print("❌ Önce .env dosyasına TELEGRAM_BOT_TOKEN yazın (veya python main.py --kurulum).")
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

    if args.kurulum:
        from setup_wizard import run_wizard
        env_path = BASE_DIR / ".env" if args.env is None else args.env
        if not run_wizard(env_path=env_path, example_path=BASE_DIR / ".env.example"):
            return EXIT_CONFIG
        args.selftest = True  # Kurulumun sonunda her şeyi test et

    try:
        cfg = load_config(args.env, mode_override=args.mode)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return EXIT_CONFIG

    register_secrets(cfg.binance_api_key, cfg.binance_api_secret, cfg.telegram_bot_token)
    log = setup_logging(cfg.log_level)

    if args.telegram_chat_id:
        return find_chat_id(cfg)

    if args.selftest:
        from selftest import run_selftest
        return run_selftest(cfg)

    if args.istatistik:
        return show_stats(cfg)

    if args.backtest:
        return run_backtest_cli(cfg, args.gun, args.coin_sayisi)

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
    except Exception:  # noqa: BLE001 - beklenmedik çökme: watchdog yeniden başlatsın
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
    prob = stats.kill_switch_probability([t["kar_zarar_usdt"] for t in trades], realized, cfg.max_total_loss)
    print(_strip_html(stats.format_stats(st, cfg.quote_asset, realized, cfg.max_total_loss,
                                         cfg.take_profit_pct, cfg.stop_loss_pct, prob)))
    return EXIT_OK


def run_backtest_cli(cfg, days: int, max_symbols: int) -> int:
    from backtest import format_backtest_report, run_backtest
    from binance_client import BinanceClient

    print(f"⏳ Son {days} günün verisi indiriliyor ve strateji deneniyor (ilk seferde birkaç dakika sürebilir)...")
    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)
    try:
        result = run_backtest(cfg, client, days=days, max_symbols=max_symbols)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Backtest çalıştırılamadı: {exc}")
        return EXIT_CONFIG
    print(format_backtest_report(result, cfg))
    return EXIT_OK


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
        print(f"  Sermaye: {cfg.start_capital:g} {cfg.quote_asset} | Kill-switch: -{cfg.max_total_loss:g} "
              f"{cfg.quote_asset} | TP +%{cfg.take_profit_pct:g} | SL -%{cfg.stop_loss_pct:g}")
        print("!" * 64)
        if not args.yes and not confirm("Devam etmek için EVET yazın: "):
            print("İptal edildi.")
            return EXIT_CONFIG

    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)
    prefix = "🧪 <i>[TEST MODU]</i>\n" if cfg.is_paper else ""
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, prefix=prefix,
                                quiet_window=cfg.quiet_window, max_queue=cfg.telegram_queue_max)
    if not notifier.enabled:
        log.warning("Telegram ayarlı değil; bildirimler sadece terminale yazılacak.")

    try:
        return _start_bot(cfg, store, client, notifier, log)
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


def _start_bot(cfg, store, client, notifier, log) -> int:
    from exchange import LiveExchange, PaperExchange
    from state import BotState, EquityLog, TradeLog
    from strategy import ReboundStrategy
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

            free = wait_for_network(first_balance, "Bakiye okunamadı", cfg, log)
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
    bot = TradingBot(cfg, exchange, ReboundStrategy(client, cfg, coin_filter=coin_filter, regime=regime),
                     notifier, store, state, TradeLog(DATA_DIR / f"trades_{cfg.trading_mode}.csv"),
                     equity_log=EquityLog(DATA_DIR / f"equity_{cfg.trading_mode}.csv"),
                     price_stream=price_stream, command_listener=listener)

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
