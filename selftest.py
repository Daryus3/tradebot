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
from decimal import Decimal
from typing import List, Tuple

from binance_client import BinanceClient
from exchange import PaperExchange, SymbolRules, parse_symbol_rules
from logger import get_logger
from risk import (ExitReason, Position, evaluate_exit, kill_switch_hit, net_exit_value, plan_entry,
                  pnl_pct, price_for_pnl, update_trailing)
from strategy import ReboundStrategy
from telegram_bot import LINE, TelegramNotifier, esc, fmt_price, fmt_usdt

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

    m = cfg.max_total_loss
    hit = kill_switch_hit(-(m - 0.5), -0.6, m)
    no_hit = not kill_switch_hit(-(m - 0.5), -0.4, m)
    checks.append(("Kill-switch", hit and no_hit, f"toplam -{m:g} USDT'de tetikleniyor"))

    rules = _fake_rules()
    plan_ok = plan_entry(cfg.start_capital, rules, 1.0, cfg)
    plan_small = plan_entry(5.5, rules, 1.0, cfg)
    checks.append(("Min. emir tutarı optimizasyonu", plan_ok.ok and not plan_small.ok,
                   f"{cfg.start_capital:g} USDT ile {plan_ok.quote_amount:.2f} USDT'lik işlem; "
                   f"5.50 USDT reddedildi (stop-loss'ta 5 USDT altına düşer)"))
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
    results.append((OK, "Ayarlar", f"Mod: {mode}, sermaye {cfg.start_capital:g} {cfg.quote_asset}, "
                                   f"komisyon %{cfg.fee_rate * 100:.2f}"))

    # 2) Risk motoru
    checks = run_risk_scenarios(cfg)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        log.info("  %s %s: %s", OK if ok else FAIL, name, detail)
    results.append((OK if passed == len(checks) else FAIL, f"Risk motoru ({passed}/{len(checks)})",
                    _example_levels(cfg)))

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

    # 6) API anahtarı
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
        print("✅ Tüm kritik kontroller başarılı. Sonraki adım: python main.py  (test modunda çalıştırır)")

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
        if free < cfg.start_capital:
            problems.append(f"Spot bakiyesi START_CAPITAL_USDT ({cfg.start_capital:g}) altında — para Futures/Funding "
                            f"cüzdanındaysa Binance'te Transfer ile Spot'a aktarın")
        if problems:
            critical = any("ÇEKİM" in p or "YOK" in p for p in problems)
            return (FAIL if critical else WARN, title, detail + " | " + "; ".join(problems))
        return (OK, title, detail + " | işlem izni var, çekim izni kapalı")
    except Exception as exc:  # noqa: BLE001
        return (FAIL, title, str(exc))
