"""
Self-test: gerçek parayı riske atmadan önce her şeyin çalıştığını doğrular.

    python main.py --selftest

Kontroller:
  1. Ayarlar (.env) okunabiliyor mu?
  2. Risk motoru senaryoları (kâr al, zarar kes, izleyen stop, kill-switch, bakiye optimizasyonu)
  3. Binance bağlantısı (ping, saat farkı, parite kuralları, 24s veriler)
  4. Strateji deneme taraması (alım yapmaz, sadece sinyalleri listeler)
  5. 11 USDT sanal bakiye ile canlı fiyattan örnek al-sat (komisyon etkisini gösterir)
  6. Binance API anahtarı (varsa): okuma, işlem izni, çekim izni KAPALI mı?
  7. Telegram'a test mesajı
Sonuçlar terminale yazılır ve Telegram'a gönderilir.
"""
from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from typing import List, Tuple

from binance_client import BinanceClient
from exchange import LiveExchange, PaperExchange, SymbolRules, parse_symbol_rules
from logger import get_logger
from risk import (ExitReason, Position, evaluate_exit, kill_switch_hit, min_position_usdt, net_exit_value,
                  plan_entry, pnl_pct, position_slots, price_for_pnl, update_breakeven, update_trailing)
from strategy import ReboundStrategy
from telegram_bot import LINE, Messages, TelegramNotifier, esc, fmt_price, fmt_rate, fmt_usdt

log = get_logger()

OK, WARN, FAIL = "✅", "⚠️", "❌"
Result = Tuple[str, str, str]  # (durum, başlık, ayrıntı)


def _fake_rules() -> SymbolRules:
    d = Decimal
    return SymbolRules(symbol="TESTUSDT", base_asset="TEST", quote_asset="USDT", tick_size=d("0.0001"),
                       step_size=d("0.01"), min_qty=d("0.01"), market_step_size=d("0.01"),
                       market_min_qty=d("0.01"), market_max_qty=d("1000000"), min_notional=d("5"))


def _run_path(cfg, prices: List[float], qty: float, cost: float):
    """Fiyat yolunu risk motorundan geçirir; (çıkış_nedeni, çıkış_adımı, son_net_kz) döner."""
    now = time.time()
    pos = Position("TESTUSDT", "TEST", qty, 1.0, cost, now)
    pct = 0.0
    for i, price in enumerate(prices):
        pct = pnl_pct(net_exit_value(qty, price, cfg.fee_rate), cost)
        update_trailing(pos, pct, cfg)
        reason = evaluate_exit(pos, pct, now, cfg)
        if reason:
            return reason, i, pct
    return None, -1, pct


def run_risk_scenarios(cfg) -> List[Tuple[str, bool, str]]:
    """Risk kurallarını sentetik fiyat yollarıyla test eder (internet gerekmez)."""
    cost = cfg.start_capital
    qty = cost / 1.0 * (1 - cfg.fee_rate)  # 1.00 fiyattan alış, komisyon coinden kesildi

    def p(target_pct: float) -> float:
        return price_for_pnl(target_pct, cost, qty, cfg.fee_rate)

    tp, sl = cfg.take_profit_pct, cfg.stop_loss_pct
    act, floor = cfg.trailing_activation_pct, cfg.trailing_floor_pct
    checks: List[Tuple[str, bool, str]] = []

    r, i, pct = _run_path(cfg, [p(0), p(tp / 2), p(tp + 0.1)], qty, cost)
    checks.append(("Kâr Al", r == ExitReason.TAKE_PROFIT and i == 2, f"+%{pct:.2f}'de satış"))

    r, i, pct = _run_path(cfg, [p(0), p(-sl / 2), p(-sl - 0.1)], qty, cost)
    checks.append(("Zarar Kes", r == ExitReason.STOP_LOSS and i == 2, f"%{pct:.2f}'de satış"))

    r, i, pct = _run_path(cfg, [p(0), p(act + 0.5), p((act + floor) / 2 + 0.5), p(floor - 0.1)], qty, cost)
    checks.append(("İzleyen Stop", r == ExitReason.TRAILING_STOP and i == 3,
                   f"+%{act:g} görüldü, +%{pct:.2f}'de satış"))

    r, i, pct = _run_path(cfg, [p(0), p(act - 1), p(floor - 1)], qty, cost)
    checks.append(("İzleyen stop erken çalışmıyor", r is None or r == ExitReason.MAX_HOLD,
                   f"+%{act - 1:g} zirvede aktifleşmedi"))

    m = cfg.loss_limit(cfg.start_capital)
    hit = kill_switch_hit(-(m - 0.5), -0.6, m)
    no_hit = not kill_switch_hit(-(m - 0.5), -0.4, m)
    grown = cfg.loss_limit(cfg.start_capital * 2, cfg.start_capital)   # Sermaye eklenince limit de büyümeli
    checks.append(("Kill-switch", hit and no_hit and abs(grown - 2 * m) < 1e-9,
                   f"sermayenin %{cfg.loss_pct(cfg.start_capital):.4g}'i: {cfg.start_capital:g} USDT'de -{m:.2f}, "
                   f"{cfg.start_capital * 2:g} USDT'de -{grown:.2f} USDT'de tetikleniyor"))

    rules = _fake_rules()
    plan_ok = plan_entry(cfg.start_capital, rules, 1.0, cfg)
    min_pos = min_position_usdt(5.0, cfg)
    small = round(min_pos - 0.05, 2)   # Stop-loss'ta min. emrin altına düşecek kadar küçük tutar
    plan_small = plan_entry(small, rules, 1.0, cfg)
    checks.append(("Min. emir tutarı optimizasyonu", plan_ok.ok and not plan_small.ok,
                   f"{cfg.start_capital:g} USDT ile {plan_ok.quote_amount:.2f} USDT'lik işlem; "
                   f"{small:.2f} USDT reddedildi (stop-loss'ta 5 USDT altına düşer)"))
    slots = {c: position_slots(c, 5.0, cfg) for c in (cfg.start_capital, 12.0, 25.0)}
    safe = all(1 <= n <= cfg.max_open_positions and (n == 1 or c / n >= min_pos) for c, n in slots.items())
    checks.append(("Pozisyon sayısı bakiyeye göre", safe,
                   " | ".join(f"{c:g} USDT → {n} pozisyon" for c, n in slots.items())
                   + f" (pozisyon başı en az {min_pos:.2f} USDT)"))

    # Başabaş koruması (ayar kapalı olsa da mantığı doğrulanır)
    # Kâr Al / izleyen stop başabaştan önce devreye girmesin diye bu testte uzaklaştırılır
    be_cfg = replace(cfg, breakeven_enabled=True,
                     take_profit_pct=max(cfg.take_profit_pct, cfg.breakeven_trigger_pct + 5),
                     trailing_activation_pct=max(cfg.trailing_activation_pct, cfg.breakeven_trigger_pct + 5))
    pos = Position("TESTUSDT", "TEST", qty, 1.0, cost, time.time())
    for target in (be_cfg.breakeven_trigger_pct + 0.5, be_cfg.breakeven_floor_pct - 0.1):
        pct = pnl_pct(net_exit_value(qty, p(target), cfg.fee_rate), cost)
        update_trailing(pos, pct, be_cfg)
        update_breakeven(pos, be_cfg)
    reason = evaluate_exit(pos, pct, time.time(), be_cfg)
    checks.append(("Başabaş koruması", reason == ExitReason.BREAKEVEN,
                   f"+%{be_cfg.breakeven_trigger_pct:g} görüldükten sonra +%{pct:.2f}'de satış "
                   f"({'açık' if cfg.breakeven_enabled else 'ayarda kapalı'})"))

    # Yedek stop emri: en küçük pozisyonda bile Binance minimumunun üstünde kalmalı
    smallest = max(min_pos, cfg.start_capital / max(1, position_slots(cfg.start_capital, 5.0, cfg)))
    backup_value = smallest * (1 - cfg.backup_stop_pct / 100) * (1 - cfg.backup_stop_limit_gap_pct / 100)
    checks.append(("Binance yedek stop tutarı", not cfg.backup_stop_enabled or backup_value >= 5.0,
                   f"-%{cfg.backup_stop_pct:g} seviyesinde emir ≈{backup_value:.2f} USDT (min. 5)"
                   if cfg.backup_stop_enabled else "kapalı"))
    return checks


def _example_levels(cfg) -> str:
    c = cfg.start_capital
    return (f"{fmt_usdt(c)} USDT işlemde net hedefler → 🎯 {fmt_usdt(c * (1 + cfg.take_profit_pct / 100))} | "
            f"📈 {fmt_usdt(c * (1 + cfg.trailing_activation_pct / 100))} | "
            f"🛡️ {fmt_usdt(c * (1 + cfg.trailing_floor_pct / 100))} | "
            f"🛑 {fmt_usdt(c * (1 - cfg.stop_loss_pct / 100))}")


def run_selftest(cfg) -> int:
    results: List[Result] = []
    client = BinanceClient(cfg.binance_api_key, cfg.binance_api_secret, cfg.binance_base_url)

    # 1) Ayarlar
    mode = "TEST (paper)" if cfg.is_paper else "CANLI"
    results.append((WARN if cfg.notes else OK, "Ayarlar",
                    f"Mod: {mode}, sermaye {cfg.start_capital:g} {cfg.quote_asset}, "
                    f"komisyon {fmt_rate(cfg.fee_rate)}, en fazla {cfg.max_open_positions} pozisyon"
                    + "".join(f"\n     ⚠️ {note}" for note in cfg.notes)))

    # 2) Risk motoru
    checks = run_risk_scenarios(cfg)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        log.info("  %s %s: %s", OK if ok else FAIL, name, detail)
    failed = [name for _, ok, _ in checks if not ok]
    results.append((OK if not failed else FAIL, f"Risk motoru ({passed}/{len(checks)})",
                    _example_levels(cfg) + "".join(
                        f"\n     ❌ {name}: {detail}" for name, ok, detail in checks if not ok)))

    # 3) Binance bağlantısı
    rules = {}
    tickers = []
    try:
        t0 = time.time()
        client.ping()
        latency = (time.time() - t0) * 1000
        offset = client.sync_time()
        rules = parse_symbol_rules(client.exchange_info(), cfg.quote_asset)
        tickers = client.tickers_24h()
        status = OK if abs(offset) < 5000 else WARN
        results.append((status, "Binance bağlantısı",
                        f"gecikme {latency:.0f} ms, saat farkı {offset} ms, {len(rules)} {cfg.quote_asset} paritesi"
                        + ("" if status == OK else " (bilgisayar saatinizi senkronize edin!)")))
    except Exception as exc:  # noqa: BLE001
        results.append((FAIL, "Binance bağlantısı", str(exc)))

    # 4) Strateji deneme taraması
    if rules:
        try:
            strategy = ReboundStrategy(client, cfg)
            scan = strategy.scan(rules, set(), cfg.start_capital)
            detail = scan.summary()
            results.append((OK, "Strateji taraması (alım yapılmadı)", detail))
        except Exception as exc:  # noqa: BLE001
            results.append((FAIL, "Strateji taraması", str(exc)))

    # 5) Sanal bakiye ile örnek al-sat
    if rules and tickers:
        results.append(_paper_round_trip(cfg, client, rules, tickers))

    # 6) Anlık fiyat akışı (WebSocket)
    if cfg.use_websocket:
        results.append(_check_websocket(cfg))

    # 7) API anahtarı
    results.append(_check_api_keys(cfg, client))

    # 7) Telegram
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, async_mode=False)
    if notifier.enabled:
        sent = notifier.send("🧪 <b>Self-test</b>: Telegram bağlantısı çalışıyor! 🎉")
        results.append((OK if sent else FAIL, "Telegram",
                        "Test mesajı gönderildi" if sent else "Mesaj gönderilemedi (token/chat ID kontrol edin)"))
    else:
        results.append((WARN, "Telegram", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil"))

    # Özet
    print("\n" + "=" * 60)
    print("SELF-TEST SONUÇLARI")
    print("=" * 60)
    for status, title, detail in results:
        print(f"{status} {title}\n     {detail}")
    print("=" * 60)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    if failed:
        print(f"❌ {failed} kontrol başarısız. Lütfen yukarıdaki hataları düzeltin.")
    else:
        print("✅ Tüm kritik kontroller başarılı. Sonraki adım: BASLAT.bat (Windows) veya python watchdog.py --mode paper")

    if notifier.enabled:
        lines = "\n".join(f"{s} <b>{esc(t)}</b>\n<i>{esc(d)}</i>" for s, t, d in results)
        footer = "❌ Bazı kontroller başarısız!" if failed else "✅ Bot çalışmaya hazır."
        notifier.send(f"🧪 <b>Self-Test Sonuçları</b>\n{LINE}\n{lines}\n{LINE}\n{footer}")
    return 1 if failed else 0


def _paper_round_trip(cfg, client: BinanceClient, rules: dict, tickers: list) -> Result:
    """En likit uygun paritede sanal alış + anında satış yapıp komisyon/spread etkisini gösterir."""
    title = f"Sanal al-sat ({cfg.start_capital:g} USDT)"
    try:
        liquid = sorted(tickers, key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
        for t in liquid:
            r = rules.get(t["symbol"])
            if r is None or r.base_asset in cfg.excluded_assets:
                continue
            if not plan_entry(cfg.start_capital, r, float(t["lastPrice"]), cfg).ok:
                continue
            paper = PaperExchange(client, {cfg.quote_asset: cfg.start_capital}, cfg.quote_asset, cfg.fee_rate)
            paper.rules = rules
            buy = paper.market_buy(r.symbol, cfg.start_capital)
            sell = paper.market_sell(r.symbol, buy.qty)
            net = sell.usdt - buy.usdt
            return (OK, title,
                    f"{r.symbol}: {fmt_usdt(buy.usdt)} USDT'ye {fmt_price(buy.avg_price)}'dan alındı, "
                    f"{fmt_price(sell.avg_price)}'dan satıldı → net {fmt_usdt(sell.usdt)} USDT "
                    f"({net:+.4f}; komisyon {buy.fee_usdt + sell.fee_usdt:.4f} + spread/toz)")
        return (WARN, title, "Uygun parite bulunamadı")
    except Exception as exc:  # noqa: BLE001
        return (FAIL, title, str(exc))


def _check_websocket(cfg) -> Result:
    title = "Anlık fiyat akışı (WebSocket)"
    try:
        from price_stream import PriceStream
    except ImportError:
        return (WARN, title, "price_stream modülü yok; fiyatlar 5 sn'de bir sorgulanacak")
    stream = PriceStream(cfg.binance_ws_url, stale_after=10.0)
    if not stream.available:
        return (WARN, title, "websocket-client kurulu değil (pip install -r requirements.txt); REST kullanılacak")
    try:
        stream.start()
        stream.set_symbols({"BTCUSDT"})
        deadline = time.time() + 10
        while time.time() < deadline:
            quote = stream.get_bid_ask("BTCUSDT")
            if quote:
                return (OK, title, f"BTCUSDT anlık fiyat alındı ({quote[0]:.2f})")
            time.sleep(0.25)
        return (WARN, title, "10 sn içinde fiyat gelmedi; bot gerekirse REST ile devam eder")
    finally:
        stream.stop()


def _check_api_keys(cfg, client: BinanceClient) -> Result:
    title = "Binance API anahtarı"
    if not cfg.has_binance_keys:
        return (WARN, title, "Tanımlı değil (test modu için gerekmez, canlı mod için gerekli)")
    try:
        acc = client.account()
        free = next((float(b["free"]) for b in acc.get("balances", []) if b["asset"] == cfg.quote_asset), 0.0)
        problems = []
        if not acc.get("canTrade", False):
            problems.append("işlem izni YOK")
        try:
            restr = client.api_restrictions()
            if restr.get("enableWithdrawals"):
                problems.append("ÇEKİM İZNİ AÇIK — güvenlik için mutlaka kapatın!")
            if not restr.get("enableSpotAndMarginTrading"):
                problems.append("'Spot & Margin Trading' izni kapalı")
            if not restr.get("ipRestrict"):
                problems.append("IP kısıtlaması yok (önerilir)")
        except Exception as exc:  # noqa: BLE001
            log.debug("API kısıtlamaları okunamadı: %s", exc)
        detail = f"Serbest bakiye: {free:.2f} {cfg.quote_asset}"
        try:
            detail += _check_fees(cfg, client, problems)
        except Exception as exc:  # noqa: BLE001
            log.debug("Komisyon oranı okunamadı: %s", exc)
        if free < cfg.start_capital:
            problems.append(f"Spot bakiyesi START_CAPITAL_USDT ({cfg.start_capital:g}) altında — para Futures/Funding "
                            f"cüzdanındaysa Binance'te Transfer ile Spot'a aktarın")
        if problems:
            critical = any("ÇEKİM" in p or "YOK" in p for p in problems)
            return (FAIL if critical else WARN, title, detail + " | " + "; ".join(problems))
        return (OK, title, detail + " | işlem izni var, çekim izni kapalı")
    except Exception as exc:  # noqa: BLE001
        return (FAIL, title, str(exc))


def _check_fees(cfg, client: BinanceClient, problems: List[str]) -> str:
    """Komisyon ve BNB indirimi (yalnızca okur). FEE_RATE kuralı botla aynı: ayar gerçek orandan
    düşükse uyarı (hesap fazla iyimser), yüksekse yalnızca bilgi (hesap temkinli)."""
    info = LiveExchange(client, cfg.quote_asset, cfg.fee_rate).fee_info(f"BTC{cfg.quote_asset}")
    position = cfg.start_capital / position_slots(cfg.start_capital, 5.0, cfg)
    detail = f" | komisyon {fmt_rate(info.taker)} | {Messages.bnb_status(info, position, cfg.quote_asset)}"
    status, real = info.status_for(cfg.fee_rate, position), info.rate_for(position)
    if status == "optimistic":
        fix = (f".env'de FEE_RATE={real:g} yapın" if info.covers(position)
               else f".env'de FEE_RATE={info.taker:g} yapın veya Binance'te biraz BNB alın")
        problems.append(f"gerçek komisyon {fmt_rate(real)}, FEE_RATE ayarı {fmt_rate(cfg.fee_rate)}: net kâr/zarar "
                        f"hesabı fazla iyimser ({fix})")
    elif status == "conservative":
        detail += f" | FEE_RATE {fmt_rate(cfg.fee_rate)}, temkinli hesap (isterseniz FEE_RATE={real:g})"
    if cfg.bnb_fee_check and cfg.bnb_min_trades > 0 and info.covers(position) and info.discount > 0:
        left = info.bnb_trades(position)
        if left is not None and left < cfg.bnb_min_trades:
            problems.append(f"BNB yalnızca ~{int(left)} işleme yetiyor; bitince komisyon {fmt_rate(info.taker)}'a "
                            f"döner (bot otomatik BNB almaz, Binance'ten kendiniz alın)")
    return detail
