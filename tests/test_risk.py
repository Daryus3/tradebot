import time
from decimal import Decimal

import pytest

from exchange import SymbolRules
from risk import (ExitReason, Position, evaluate_exit, kill_switch_hit, net_exit_value, plan_entry,
                  pnl_pct, price_for_pnl, update_trailing)
from selftest import run_risk_scenarios


def make_rules(step="0.01", min_notional="5"):
    d = Decimal
    return SymbolRules("TESTUSDT", "TEST", "USDT", d("0.0001"), d(step), d(step), d(step), d(step),
                       d("1000000"), d(min_notional))


def make_pos(cfg, cost=11.0, price=1.0):
    qty = cost / price * (1 - cfg.fee_rate)
    return Position("TESTUSDT", "TEST", qty, price, cost, time.time()), qty


def pnl_at(cfg, pos, qty, price):
    return pnl_pct(net_exit_value(qty, price, cfg.fee_rate), pos.cost_usdt)


def test_selftest_scenarios_pass(cfg):
    for name, ok, detail in run_risk_scenarios(cfg):
        assert ok, f"{name}: {detail}"


def test_user_example_levels(cfg):
    """Kullanıcının verdiği örnek: 11 USDT → 12.65 / 12.10 / 11.55 / 9.90 (net, komisyon sonrası)."""
    pos, qty = make_pos(cfg)
    for pct, expected in [(15, 12.65), (10, 12.10), (5, 11.55), (-10, 9.90)]:
        price = price_for_pnl(pct, pos.cost_usdt, qty, cfg.fee_rate)
        assert net_exit_value(qty, price, cfg.fee_rate) == pytest.approx(expected, abs=1e-9)


def test_fees_included_in_decision(cfg):
    """Fiyat tam %15 artınca komisyonlar yüzünden net kâr %15'in altında kalır → henüz satma."""
    pos, qty = make_pos(cfg)
    pct = pnl_at(cfg, pos, qty, 1.15)
    assert pct < 15
    assert evaluate_exit(pos, pct, time.time(), cfg) is None
    # Komisyonlar yüzünden stop-loss, fiyat tam %10 düşmeden (~%9.8'de) çalışır
    assert evaluate_exit(pos, pnl_at(cfg, pos, qty, 0.903), time.time(), cfg) is None
    assert evaluate_exit(pos, pnl_at(cfg, pos, qty, 0.901), time.time(), cfg) == ExitReason.STOP_LOSS


def test_trailing_requires_activation_and_sells_at_floor(cfg):
    pos, qty = make_pos(cfg)
    now = time.time()
    for price, expected in [(1.05, None), (1.09, None), (1.06, None)]:
        pct = pnl_at(cfg, pos, qty, price)
        update_trailing(pos, pct, cfg)
        assert evaluate_exit(pos, pct, now, cfg) is expected
    assert not pos.trailing_active

    pct = pnl_at(cfg, pos, qty, 1.115)  # net ~%11.3 → izleyen stop aktif
    assert update_trailing(pos, pct, cfg) is True
    assert pos.trailing_active
    assert evaluate_exit(pos, pnl_at(cfg, pos, qty, 1.07), now, cfg) is None   # ~%6.8: tut
    assert evaluate_exit(pos, pnl_at(cfg, pos, qty, 1.051), now, cfg) == ExitReason.TRAILING_STOP


def test_dynamic_trailing_keeps_half_of_peak(cfg):
    from dataclasses import replace
    dyn = replace(cfg, trailing_dynamic=True)
    pos, qty = make_pos(dyn)
    update_trailing(pos, 14.0, dyn)
    assert evaluate_exit(pos, 7.5, time.time(), dyn) is None
    assert evaluate_exit(pos, 6.9, time.time(), dyn) == ExitReason.TRAILING_STOP


def test_max_hold_optional(cfg):
    from dataclasses import replace
    pos, _ = make_pos(cfg)
    pos.opened_at = time.time() - 3 * 3600
    assert evaluate_exit(pos, 1.0, time.time(), cfg) is None  # Varsayılan: kapalı
    timed = replace(cfg, max_hold_minutes=60)
    assert evaluate_exit(pos, 1.0, time.time(), timed) == ExitReason.MAX_HOLD


def test_kill_switch_threshold(cfg):
    assert kill_switch_hit(-4.0, -1.0, 5.0)
    assert kill_switch_hit(-5.2, 0.0, 5.0)
    assert not kill_switch_hit(-4.0, -0.99, 5.0)
    assert not kill_switch_hit(3.0, -7.5, 5.0)  # Önceki kâr zararı dengeliyor


def test_plan_entry_uses_full_balance(cfg):
    plan = plan_entry(11.0, make_rules(), 0.5, cfg)
    assert plan.ok and plan.quote_amount == 11.0


def test_plan_entry_rejects_when_stop_loss_would_be_unsellable(cfg):
    assert not plan_entry(5.5, make_rules(), 1.0, cfg).ok   # SL'de 4.95 < 5
    assert plan_entry(6.0, make_rules(), 1.0, cfg).ok       # SL'de 5.40 ≥ 5.25


def test_plan_entry_rejects_large_lot_step(cfg):
    # BTC benzeri: adım 0.00001 @ 100000 → bir adım 1 USDT = 11 USDT'nin ~%9'u satılamaz toz olur
    plan = plan_entry(11.0, make_rules(step="0.00001"), 100_000, cfg)
    assert not plan.ok and "toz" in plan.reason
