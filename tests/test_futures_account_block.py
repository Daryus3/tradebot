"""Hesap modu engeli (Binance -4168: hesap Çoklu Varlık / Multi-Assets modunda → yalıtılmış teminat reddedilir, bot hiçbir
pozisyon açamaz): açık hata mesajı, saatte bir kritik Telegram uyarısı, /tarama'da ve backtest raporunun canlı bot
karşılaştırmasında görünür satır. 10 Kasım 2026'da iki giriş bu yüzden reddedilmişti (aynı gün bot modu kendisi düzeltir
oldu); eski bir ret raporda yanlış alarm vermemeli."""
import json

import pytest

import futures_backtest as fb
import futures_trader as ft
from futures_exchange import FuturesError, FuturesExchange
from strategy import FUT_BAR_MS
from test_futures import BASE_MS, Clock, FakeCcxt, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_cascade import live_cfg
from test_futures_fast_entry import CLOSE, CLOSES, FastCcxt, fast_cfg, journal

E4168 = 'binance {"code":-4168,"msg":"Unable to adjust to isolated-margin mode under the Multi-Assets mode."}'


class StickyCcxt(FakeCcxt):
    """Binance mod değişikliği isteğini kabul eder ama hesap Çoklu Varlık modunda kalır (ör. Kredi İşlem Modu)."""

    def fapiPrivatePostMultiAssetsMargin(self, params):
        self.calls.append(("multi_assets", params["multiAssetsMargin"]))


# ====================================================================== borsa katmanı
def test_rejection_while_api_shows_single_asset_mode_is_explained(fcfg):
    """Rapor 15-18'deki durum: mod sorgusu Tek Varlık gösteriyor ama teminat modu isteği yine -4168 ile reddediliyor."""
    fake = FakeCcxt()
    fake.margin_error = E4168
    with pytest.raises(FuturesError) as err:
        FuturesExchange(fcfg, exchange=fake).prepare_symbol("BTCUSDT", 5)
    text = str(err.value)
    assert err.value.code == "MULTI_ASSETS"
    assert text.startswith("Binance hesabı Çoklu Varlık (Multi-Assets) modunda") and "-4168" in text[:110]
    assert "Tek Varlık Modu" in text and "Kredi İşlem Modu" in text and "API yanıtı" in text
    assert [c[0] for c in fake.calls] == ["set_margin_mode", "set_margin_mode"]      # Kaldıraç / emir denenmez


def test_switch_that_binance_accepts_but_does_not_apply_is_reported(fcfg):
    fake = StickyCcxt()
    fake.multi_assets = True
    ex = FuturesExchange(fcfg, exchange=fake)
    with pytest.raises(FuturesError) as err:
        ex.check_single_asset()
    assert err.value.code == "MULTI_ASSETS" and "hâlâ Çoklu Varlık modunda" in str(err.value)
    with pytest.raises(FuturesError) as err:
        ex.prepare_symbol("BTCUSDT", 5)
    assert err.value.code == "MULTI_ASSETS" and ("set_leverage", 5, "BTC/USDT:USDT") not in fake.calls


def test_switch_that_works_still_opens_normally(fcfg):
    fake = FakeCcxt()
    fake.multi_assets = True
    ex = FuturesExchange(fcfg, exchange=fake)
    ex.prepare_symbol("BTCUSDT", 5)
    assert fake.calls[-1] == ("set_leverage", 5, "BTC/USDT:USDT") and not fake.multi_assets


# ====================================================================== canlı bot
def test_entry_blocked_by_account_mode_warns_once_an_hour_and_shows_in_scan_report(fcfg, tmp_path):
    clock = Clock(CLOSE - 200)
    fake = FastCcxt(CLOSES, clock=clock)
    bot, fake, notes, clock = make_fbot(live_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    fake.margin_error = E4168
    notes.messages.clear()
    clock.t = bot.next_scan_at
    bot.tick()
    assert bot.state.positions == {} and "create_order" not in [c[0] for c in fake.calls]
    warned = [m for m in notes.messages if "Çoklu Varlık" in m]
    assert len(warned) == 1 and "SOLUSDT sinyali açılamadı" in warned[0] and "Tek Varlık Modu" in warned[0]
    row = journal(bot)[-1]
    assert row["s"]["SOL"].startswith("açılamadı: Binance hesabı Çoklu Varlık") and "-4168" in row["s"]["SOL"]
    assert bot.account_block and "⛔" in bot.scan_report() and "Tek Varlık Modu" in bot.scan_report()
    # Sinyal 5 dk sonra yine gelir: aynı uzun uyarı bir saat içinde yinelenmez, bir saat sonra yinelenir
    notes.messages.clear()
    assert bot._note_account_block(FuturesError("x", "MULTI_ASSETS"), clock.t + 300, "SOLUSDT") is True
    assert notes.messages == []
    assert bot._note_account_block(FuturesError("x", "MULTI_ASSETS"), clock.t + ft.ACCOUNT_WARN_EVERY_SEC + 1) is True
    assert len(notes.messages) == 1
    assert bot._note_account_block(FuturesError("başka hata", "-2019"), clock.t) is False       # Başka hatalar: eski yol
    # Hesap düzelince (Tek Varlık modu) sıradaki sinyal açılır ve engel kalkar
    fake.margin_error = None
    bot.state.cooldowns.clear()
    clock.t = bot.next_scan_at
    bot.tick()
    assert set(bot.state.positions) == {"SOLUSDT"} and bot.account_block == "" and "⛔" not in bot.scan_report()


def test_fast_entry_preparation_detects_the_block_before_any_signal(fcfg, tmp_path):
    """Hızlı girişte teminat modu taramalar arasında önceden ayarlanır: engel sinyal beklenmeden fark edilir, tek uyarı
    yollanır ve diğer coinler boşuna denenmez (hesap düzeyinde)."""
    clock = Clock(CLOSE - 200)
    fake = FastCcxt(CLOSES, clock=clock)
    bot, fake, notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    fake.margin_error = E4168
    fake.calls.clear()
    notes.messages.clear()
    bot.tick()
    tried = [c for c in fake.calls if c[0] == "set_margin_mode"]
    assert len({c[2] for c in tried}) == 1                               # İlk coinde anlaşıldı: diğerleri denenmedi
    assert len([m for m in notes.messages if "Çoklu Varlık" in m]) == 1 and bot.account_block
    assert bot._prepare_todo == 0
    clock.t += 1
    bot.tick()
    assert len([c for c in fake.calls if c[0] == "set_margin_mode"]) == len(tried)     # Bir saat yeniden denenmez
    assert len([m for m in notes.messages if "Çoklu Varlık" in m]) == 1


def test_startup_warning_keeps_the_block_visible(fcfg, tmp_path):
    fake = StickyCcxt()
    fake.multi_assets = True
    bot, fake, notes, _clock = make_fbot(live_cfg(fcfg), tmp_path, live=True, fake=fake)
    assert any("hâlâ Çoklu Varlık modunda" in m for m in notes.messages)
    assert "Çoklu Varlık" in bot.account_block


# ====================================================================== backtest raporu
def test_report_mentions_account_mode_rejections_without_false_alarm(fcfg, tmp_path):
    t0 = BASE_MS
    path = tmp_path / "futures_scans_paper.jsonl"
    old = ('açılamadı: ZECUSDT teminat modu ISOLATED yapılamadı: binance {"code":-4168,"msg":"Unable to adjust to '
           'isolated-margin mo')
    rows = [{"t": t0, "u": ["ZEC", "NEAR"], "a": ["ZEC", "NEAR"], "s": {"ZEC": old, "NEAR": "önceki girişte hata"}},
            {"t": t0 + FUT_BAR_MS, "a": ["ZEC", "NEAR"], "s": {"ZEC": old}},
            {"t": t0 + 2 * FUT_BAR_MS, "a": ["ZEC", "NEAR"]}]

    def report(rows, end=6):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        data = fb.FutData(t0, t0 + end * FUT_BAR_MS, [fb.Segment(t0 - 1, t0 + end * FUT_BAR_MS, ("ZEC", "NEAR"))], {},
                          ((), (), ()), (None, None), {}, None, "5m")
        return "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    # Ret kaydın sonuna yakın: hâlâ sürüyor olabilir → uyarı
    text = report(rows)
    assert "⛔ 2 taramada giriş reddedildi" in text and "Tek Varlık Modu" in text and "-4168" in text
    assert text.index("⛔") < text.index("Bu aralıkta backtestte işlem yok")
    # Ret eski (rapor 15-18: hata 10 Kasım'da, bot aynı gün modu kendisi düzeltir oldu): yanlış alarm yok, yalnız bilgi
    late = rows + [{"t": t0 + 100 * FUT_BAR_MS, "a": ["ZEC", "NEAR"]}]
    text = report(late, end=101)
    assert "⛔ 2 taramada" not in text and "ℹ️ 2 taramada giriş reddedildi" in text and "sorun kalmamıştır" in text
    # Yeni mesaj biçimi de tanınır; sonradan pozisyon açılabildiyse engel kalkmış sayılır
    rows[1]["s"] = {"ZEC": "açılamadı: Binance hesabı Çoklu Varlık (Multi-Assets) modunda: yalıtılmış teminat"}
    rows[2]["s"] = {"NEAR": "açıldı"}
    text = report(rows)
    assert "⛔" not in text and "engel kalkmış" in text
    # Ret yoksa satır da yok
    assert "Çoklu Varlık" not in report([{"t": t0, "u": ["ZEC"], "a": ["ZEC"], "s": {"ZEC": "açıldı"}}])
    assert fb.live_account_block([]) is None
