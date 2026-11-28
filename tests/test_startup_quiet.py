"""Açılışta ayar notları Telegram'a gönderilmez (yalnızca bot.log'a yazılır): başlangıç mesajı dışında ayar / risk
uyarısı gelmez."""
from dataclasses import replace

from test_bot_flow import make_bot
from test_futures import fcfg, make_fbot  # noqa: F401 - pytest fikstürü

NOTE = "Deneme notu: bu ayar etkisiz."


def test_futures_startup_sends_no_setting_notes(fcfg, tmp_path):
    _, _, notes, _ = make_fbot(replace(fcfg, notes=(NOTE,)), tmp_path)
    assert "Vadeli Scalping Botu Başlatıldı" in notes.messages[0]
    assert not any(NOTE in m for m in notes.messages)
    # Sahte borsada olmayan coinler bildirilir (gerçek veri sorunu, ayar notu değil); başka uyarı yok
    assert all("bulunamayan (atlanan) coinler" in m for m in notes.messages[1:])


def test_spot_startup_sends_no_setting_notes(cfg, fake_client, tmp_path):
    _, _, notifier = make_bot(replace(cfg, notes=(NOTE,)), fake_client, tmp_path)
    assert notifier.messages and not any(NOTE in m for m in notifier.messages)
