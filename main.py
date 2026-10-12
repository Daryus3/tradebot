"""
Kripto Al-Sat Botu — giriş noktası.

Kullanım:
    python main.py                     # .env'deki TRADING_MODE ile çalışır (varsayılan: paper = test)
    python main.py --mode paper        # 11 USDT sanal bakiye, canlı verilerle simülasyon
    python main.py --mode live         # GERÇEK para ile işlem (onay ister)
    python main.py --selftest          # Bağlantı ve risk kurallarını test eder
    python main.py --telegram-chat-id  # Telegram chat ID'nizi bulur
    python main.py --reset-state       # Kayıtlı durumu (K/Z, kill-switch) sıfırlar
"""
from __future__ import annotations

import argparse
import sys

from config import DATA_DIR, ConfigError, load_config
from logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kripto Para Al-Sat Botu (Binance Spot)")
    parser.add_argument("--mode", choices=["paper", "live"], help="Çalışma modu (.env'deki TRADING_MODE'u ezer)")
    parser.add_argument("--selftest", action="store_true", help="Bağlantı ve kural testlerini çalıştırır")
    parser.add_argument("--reset-state", action="store_true", help="Kayıtlı durumu sıfırlar (kill-switch dahil)")
    parser.add_argument("--telegram-chat-id", action="store_true", help="Telegram chat ID'nizi bulmanıza yardım eder")
    parser.add_argument("--yes", action="store_true", help="Onay sorularını atlar")
    parser.add_argument("--env", default=None, help="Farklı bir .env dosyası yolu")
    return parser.parse_args()


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().upper() in {"EVET", "E", "YES"}
    except EOFError:
        return False


def find_chat_id(cfg) -> int:
    from telegram_bot import discover_chat_ids

    if not cfg.telegram_bot_token:
        print("❌ Önce .env dosyasına TELEGRAM_BOT_TOKEN yazın.")
        return 1
    print("🔍 Bota gelen mesajlar kontrol ediliyor...")
    try:
        chats = discover_chat_ids(cfg.telegram_bot_token)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Telegram'a bağlanılamadı: {exc}")
        return 1
    if not chats:
        print("⚠️ Hiç mesaj bulunamadı. Telegram'da botunuzu açıp /start yazın, sonra bu komutu tekrar çalıştırın.")
        return 1
    print("✅ Bulunan sohbetler:")
    for chat_id, name in chats:
        print(f"   TELEGRAM_CHAT_ID={chat_id}    ({name})")
    print("Bu satırı .env dosyanıza kopyalayın.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(args.env, mode_override=args.mode)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 1

    log = setup_logging(cfg.log_level)

    if args.telegram_chat_id:
        return find_chat_id(cfg)

    if args.selftest:
        from selftest import run_selftest
        return run_selftest(cfg)

    from state import StateStore

    store = StateStore(DATA_DIR / f"state_{cfg.trading_mode}.json")

    if args.reset_state:
        if not store.exists():
            print(f"ℹ️ '{cfg.trading_mode}' modu için kayıtlı durum yok.")
            return 0
        if not args.yes and not confirm(
                f"⚠️ '{cfg.trading_mode}' modunun kayıtlı durumu (K/Z, açık pozisyon kaydı, kill-switch) "
                f"sıfırlanacak. Devam için EVET yazın: "):
            print("İptal edildi.")
            return 1
        store.reset()
        print("✅ Durum sıfırlandı (eski dosya .bak.json olarak yedeklendi).")
        return 0

    return run_bot(cfg, store, args, log)


def run_bot(cfg, store, args, log) -> int:
    from binance_client import BinanceClient
    from telegram_bot import TelegramNotifier

    if not cfg.is_paper:
        if not cfg.has_binance_keys:
            print("❌ Canlı mod için .env dosyasına BINANCE_API_KEY ve BINANCE_API_SECRET yazmalısınız.")
            return 1
        print("\n" + "!" * 64)
        print("  ⚠️  CANLI MOD: Bot GERÇEK PARANIZLA Binance'te işlem yapacak!")
        print(f"  Sermaye: {cfg.start_capital:g} {cfg.quote_asset} | Kill-switch: -{cfg.max_total_loss:g} "
              f"{cfg.quote_asset} | TP +%{cfg.take_profit_pct:g} | SL -%{cfg.stop_loss_pct:g}")
        print("!" * 64)
        if not args.yes and not confirm("Devam etmek için EVET yazın: "):
            print("İptal edildi.")
            return 1

    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)
    prefix = "🧪 <i>[TEST MODU]</i>\n" if cfg.is_paper else ""
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, prefix=prefix)
    if not notifier.enabled:
        log.warning("Telegram ayarlı değil; bildirimler sadece terminale yazılacak.")

    try:
        return _start_bot(cfg, store, client, notifier, log)
    finally:
        notifier.flush()  # Kuyruktaki Telegram mesajlarının gitmesini bekle


def _start_bot(cfg, store, client, notifier, log) -> int:
    import requests

    from exchange import LiveExchange, PaperExchange
    from state import BotState, TradeLog
    from strategy import ReboundStrategy
    from telegram_bot import Messages
    from trader import TradingBot

    try:
        state = store.load()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Durum dosyası okunamadı ({store.path}): {exc}\n   Sıfırlamak için: python main.py --reset-state")
        return 1

    if state and state.killed:
        msg = (f"🛑 Kill-switch daha önce tetiklenmiş: {state.kill_reason}\n"
               f"   Bot güvenlik nedeniyle başlatılmadı. Yeniden başlatmak için: "
               f"python main.py --mode {cfg.trading_mode} --reset-state")
        print(msg)
        notifier.send(Messages.warning(msg))
        return 2

    try:
        if state is None:
            start_capital = cfg.start_capital
            if not cfg.is_paper:
                client.sync_time()
                free = LiveExchange(client, cfg.quote_asset, cfg.fee_rate).free_balance(cfg.quote_asset)
                if free <= 0:
                    print(f"❌ Binance SPOT cüzdanınızda serbest {cfg.quote_asset} bakiyesi yok.\n"
                          f"   Bot Spot piyasada işlem yapar. Paranız Futures/Funding cüzdanındaysa Binance'te\n"
                          f"   Cüzdan → Transfer ile Spot cüzdanına aktarın (ücretsiz ve anında).")
                    return 1
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

        bot = TradingBot(cfg, exchange, ReboundStrategy(client, cfg), notifier, store, state,
                         TradeLog(DATA_DIR / f"trades_{cfg.trading_mode}.csv"))
        return bot.run()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        return 0
    except requests.RequestException as exc:
        log.error("Binance'e bağlanılamadı: %s", exc)
        print("❌ Binance'e bağlanılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.\n"
              "   (Ayrıntı logs/bot.log dosyasında. 'python main.py --selftest' ile bağlantıyı test edebilirsiniz.)")
        notifier.send(Messages.error(f"Bot başlatılamadı (bağlantı hatası): {exc}", fatal=True))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("Bot başlatılamadı")
        notifier.send(Messages.error(f"Bot başlatılamadı: {type(exc).__name__}: {exc}", fatal=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
