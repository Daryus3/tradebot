# 🤖 Kripto Para Al-Sat Botu (Binance Spot)

Ubuntu bir sunucuda (VPS) 7/24 çalışan, Python ile yazılmış bir Binance Spot al-sat botu. Düşmüş ama
toparlanmaya başlamış coinleri bulur, kısa vadeli işlemler açar, her şeyi Telegram'a bildirir ve Telegram'dan
komutla yönetilir. Test modunda (sanal bakiye) gerçek emir göndermez.

> ⚠️ **Risk uyarısı:** Kripto para alım-satımı yüksek risk içerir. Bu bot kâr garantisi vermez; kaybetmeyi göze
> alamayacağınız parayla kullanmayın. Önce mutlaka **test modunda** çalıştırın.

---

## 🚀 Kurulum

**[VPS_KURULUM.md](VPS_KURULUM.md)** sunucu kiralamaktan botun kendiliğinden yeniden başladığını denemeye kadar her
adımı, ekranda ne görmeniz gerektiğiyle birlikte anlatır. VPS veya GitHub'ı hiç kullanmamış biri için yazıldı.

Deneyimli kullanıcılar için özet (Ubuntu 22.04 / 24.04, sunucu ABD dışında olmalı — Binance ABD'yi engeller):
```bash
sudo apt update && sudo apt -y install python3 python3-venv git
sudo timedatectl set-timezone Europe/Istanbul
sudo useradd -m -s /bin/bash tradebot
sudo -iu tradebot git clone -b claude/crypto-trading-bot-larh4t https://github.com/Daryus3/tradebot.git
sudo -iu tradebot bash -c 'cd ~/tradebot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo -iu tradebot nano /home/tradebot/tradebot/.env    # Gizli bilgiler (aşağıda), Ctrl+O Enter Ctrl+X
sudo chmod 600 /home/tradebot/tradebot/.env
sudo -iu tradebot bash -c 'cd ~/tradebot && .venv/bin/python main.py --selftest'
sudo cp /home/tradebot/tradebot/deploy/tradebot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tradebot
```

---

## ⚙️ Ayarlar: iki dosya

| Dosya | İçinde ne var? | Nerede? |
|---|---|---|
| `ayarlar.env` | Strateji, risk ve filtre ayarlarının **tamamı**, açıklamalarıyla | GitHub'da (gizli bilgi **içermez**) |
| `.env` | API anahtarı ve Telegram bilgileri (4 satır) | **Yalnızca sunucuda**. GitHub'a hiç yüklenmez (`.gitignore`) |

Sunucudaki `.env`:
```
BINANCE_API_KEY=
BINANCE_API_SECRET=
TELEGRAM_BOT_TOKEN=BotFather'ın_verdiği_token
TELEGRAM_CHAT_ID=sizin_chat_id
```
Binance anahtarları yalnızca **canlı mod** için gerekir; test modunda boş kalır. Chat ID'nizi bilmiyorsanız
Telegram'da botunuza `/start` yazıp `python main.py --telegram-chat-id` çalıştırın.

**Öncelik:** `.env` > `ayarlar.env` > koddaki varsayılanlar. Bir ayarı sunucuda değiştirmek için o satırı `.env`'ye
ekleyin (örn. `MAX_OPEN_POSITIONS=3`) ve `sudo systemctl restart tradebot` çalıştırın. `ayarlar.env`'yi sunucuda
düzenlemeyin: güncelleme (`git pull`) sırasında çakışır.

---

## 🖥️ Sunucu komutları

**Servis** (bot arka planda; çökerse 15 sn sonra, sunucu yeniden başlarsa açılışta kendiliğinden başlar):

| Komut | Ne yapar? |
|---|---|
| `sudo systemctl status tradebot --no-pager` | Çalışıyor mu? |
| `sudo systemctl restart tradebot` | Yeniden başlat (ayar değişikliğinden sonra) |
| `sudo systemctl stop tradebot` / `start tradebot` | Durdur / başlat |
| `sudo journalctl -u tradebot -f` | Canlı log (Ctrl+C yalnızca izlemeyi bırakır) |
| `tail -f /home/tradebot/tradebot/logs/bot.log` | Botun kendi log dosyası |

Normal durdurma, Telegram `/kapat`, ayar hatası ve kill-switch'te bot yeniden **başlatılmaz**.

**Bot komutları** — sunucuda `sudo -iu tradebot`, `cd ~/tradebot`, `source .venv/bin/activate` sonrası:

| Komut | Ne yapar? |
|---|---|
| `python main.py --selftest` | Ayarlar, risk kuralları, Binance, Telegram ve anlık fiyat akışını test eder |
| `python main.py --istatistik` | Performans istatistikleri |
| `python main.py --backtest --gun 90` | Stratejiyi geçmiş verilerle dener (`--sermaye 32`, `--coin-sayisi 40`) |
| `python main.py --backtest --gun 90 --tara` | Aynı veriyle onlarca Kâr Al / Zarar Kes / süre ayarını karşılaştırır |
| `python main.py --backtest --gun 90 --tara-giris` | Giriş filtrelerini (piyasa rejimi, 24s düşüş, sinyal puanı, hedef alanı) karşılaştırır |
| `python main.py --backtest --gun 90 --tara-filtre` | rebound filtrelerini tek tek gevşetip işlem sayısına ve işlem başı sonuca etkisini gösterir |
| `python main.py --backtest --turnuva` | Ortalamaya dönüş / RSI uyumsuzluğu / swing / rotasyon stratejilerini farklı çıkış kurallarıyla aynı yılda dener, sıralar |
| `python main.py --backtest --strateji rsi_divergence` | Pozitif RSI uyumsuzluğu + dönüş teyidiyle alım stratejisini dener (1 saatlik mumlar, 365 gün) |
| `python main.py --backtest --deney --gun 540 --coin-sayisi 100` | **Zarar azaltma deneyi:** mevcut ayarlar, önerilen ayar listesi ve takılan işlem / zarardan sonra bekleme / önceki trend birleşimlerini **kill-switch açıkken** (canlıdaki gibi) aynı veriyle dener; en iyisi için `/ayar` satırlarını verir |
| `python main.py --backtest --gun 90 --strateji-karsilastir` | rebound / trend / breakout stratejilerini karşılaştırır |
| `python main.py --backtest --gun 90 --bitis 2026-07-01` | O tarihte biten (daha eski) bir dönemi dener: bulunan ayarı bağımsız dönemde doğrulamak için |
| `python main.py --telegram-chat-id` | Telegram chat ID'nizi bulur |
| `python main.py --mode paper --reset-state --yes` | Kayıtlı durumu sıfırlar (kill-switch kilidi dahil). **Önce servisi durdurun** |

⚠️ Servis çalışırken `python main.py`'yi (botun kendisini) ayrıca başlatmayın: iki bot aynı anda çalışır.
Self-test, backtest ve istatistik ise servisle birlikte çalıştırılabilir.

**Güncelleme:**
```bash
sudo systemctl stop tradebot
sudo -iu tradebot bash -c 'cd ~/tradebot && git pull && .venv/bin/pip install -r requirements.txt'
sudo systemctl start tradebot
```

**Canlı mod (gerçek para):** Binance'te yalnızca okuma + spot işlem izinli, **para çekme izni kapalı** ve
**yalnızca sunucunun IP adresinden** çalışan bir API anahtarı oluşturup `.env`'ye yazın.
`/etc/systemd/system/tradebot.service` içindeki `--mode paper`'ı `--mode live --yes` yapıp
`sudo systemctl daemon-reload && sudo systemctl restart tradebot` çalıştırın. USDT'niz Binance **Spot**
cüzdanında olmalıdır. Bot **binance.com** API'sini kullanır; Binance TR desteklenmez.

---

## ✨ Özellikler

**Kurallar** (değerler `ayarlar.env`'de): kâr al, zarar kes, izleyen stop ve başabaş **komisyon düşüldükten
sonraki net** kâra göre çalışır. Sermaye pozisyonlara eşit bölünür; her pozisyon zarar kesten sonra bile Binance'in
~5 USDT minimum emir tutarının üstünde kalacak şekilde pozisyon sayısı bakiyeye göre otomatik ayarlanır.
**Kill-switch:** toplam zarar sermayenin `MAX_TOTAL_LOSS_PCT` yüzdesine ulaşırsa bot her şeyi satar, kapanır ve
kilitli kalır (`--reset-state` ile açılır).

| Koruma | Ne yapar? |
|---|---|
| 🧷 Binance'te yedek zarar-kes | Her alımdan sonra Binance'e `BACKUP_STOP_PCT`'de bekleyen bir stop emri bırakır; bot ulaşılamazken de pozisyon korunur |
| 🔄 Hesap mutabakatı | Açılışta Binance ile karşılaştırır: bot kapalıyken stop çalıştıysa veya coini elle sattıysanız gerçek fiyatı kaydeder; sonucu belirsiz kalan emri bulup takibe alır |
| 📶 Kesinti dayanıklılığı | İnternet kesilirse Telegram mesajları kaybolmaz; açılışta internet yoksa bot bekler; uzun kesintide uyarır |
| ⚡ Anlık fiyat akışı | Açık pozisyon ~1 sn'de bir kontrol edilir (WebSocket); bağlantı koparsa normal sorguya döner |
| ☣️ Tehlikeli coin filtresi | Delist takvimindeki, yeni listelenmiş ve son günlerde pompalanıp sönen coinleri eler |
| 🌡️ Piyasa rejimi | Genel çöküşte alım yapmaz; zayıf piyasada kurallar sıkılaşır ve en fazla 1 pozisyon açılır |
| ↔️ Spread / ATR / BTC filtresi | Alış-satış farkı geniş, çok sakin ya da aşırı oynak coinlere ve BTC sert düşerken alım yapmaz |
| 🪙 BNB komisyon takibi | Gerçek komisyon oranını ve BNB bakiyesini izler, `FEE_RATE` yanlışsa uyarır (BNB almaz) |
| 🔐 Gizli bilgi koruması | Token ve API anahtarları loglara ve mesajlara açık yazılmaz |
| 🌐 IP alarmı | Sunucunun dış IP'sini 10 dk'da bir kontrol eder (api.ipify.org); değişirse uyarır (Binance IP kısıtlaması) |
| 🧯 Risk sigortaları | Üst üste 3 zarar keste 4 saat, günlük zarar limitinde gün sonuna kadar yeni alım yapmaz; aynı coinde 3 zarar keste kara liste önerir; ortalama kayma artarsa uyarır; isterseniz zararla kapanan coine bir süre (`LOSS_COOLDOWN_MINUTES`) tekrar girmez |
| 🛡️ Ek giriş filtreleri | Sahte yükseliş (üst fitil), VWAP, aşırı ısınma, RSI uyumsuzluğu teyidi, önceki trend (düşüşten önce de düşen coini almaz); canlıda satış duvarı, emir defteri dengesizliği, tape teyidi (varsayılan kapalı, `/menu` → Filtreler) |
| 🧠 Akıllı yönetim | ATR'ye göre hedefler, Kâr Al'da hedef uzatma, DCA / piramit (yedek akçeyle), süre sorusu, erken kaçış, kademeli alım, sinyal onayı (varsayılan kapalı, `/menu` → Akıllı Yönetim) |

---

## 📱 Telegram komutları

Bot **yalnızca sizin sohbetinizden** (`TELEGRAM_CHAT_ID`) gelen komutları ve menü düğmelerini kabul eder.

### 🎛️ Kontrol paneli: `/menu`
`/menu` yazınca düğmeli bir panel açılır:

| Düğme | Ne yapar? |
|---|---|
| 📊 Durum · ⏸️ Durdur / ▶️ Devam | `/durum`, `/durdur`, `/devam` ile aynı |
| ⚙️ Ayarlar | Kâr Al, Zarar Kes, izleyen stop (±0.5) ve en fazla pozisyon (±1); hemen uygulanır |
| 💰 Sermaye (yedek akçe) | İlk giriş yüzdesi: %100 (kapalı) / %75 / %50 / %33 — kalan pay USDT olarak yedekte bekler |
| 🧭 Strateji | Hazır stratejiler arasında geçiş (onay ister, bot yeniden başlar; grubun çıkış ayarları yüklenir, eski grubunkiler saklanır) |
| 🔔 Bildirim | Tümü / Sadece Kapanışlar / Sessiz Mod (komut cevapları ve acil uyarılar her zaman gelir) |
| 🛡️ Filtreler · 🧯 Risk | Ek giriş filtrelerini ve risk sigortalarını açar / kapatır |
| 🧠 Akıllı Yönetim | Aşağıdaki tablodaki özellikleri açar / kapatır; DCA, piramit, süre ve kaçış için kapalı / sorarak / otomatik |
| 📈 Canlı Pano · 🧹 Toz → BNB | 5 sn'de bir güncellenen tek K/Z mesajı; küçük bakiyeleri BNB'ye çevirme (canlı mod) |
| 🧪 Backtest | Backtest'i Telegram'dan düğmelerle kurup başlatır, sonuçları (rapor, grafik, CSV) Telegram'a gönderir (aşağıda) |
| 🔎 Piyasayı Tara | En hacimli 40 coinde "dipte olup hacmi artan"ları listeler (bilgi amaçlı, alım yapmaz) |
| 📶 Gecikme · 📜 Son Loglar · 🌐 IP | Binance'e ms gecikme, son 15 log satırı, sunucunun dış IP'si |
| 🔄 Sunucu Reset | Bot servisini yeniden başlatır (onay ister; ayrıntı VPS_KURULUM.md) |

Menüden değiştirilen her ayar doğrulanır, hemen uygulanır ve sunucudaki `.env`'nin sonuna yazılır (bot yeniden
başlasa da kalır; `.env`, `ayarlar.env`'yi ezer). Diğer ayarlar için `/ayar ANAHTAR DEĞER` (örn. `/ayar tp 2.5`,
`/ayar DAILY_LOSS_LIMIT_PCT 3`, giriş taramasının önerdiği `/ayar MIN_ROOM_RATIO 0.5`, `/ayar MAX_24H_DROP_PCT 15`,
`/ayar MIN_SIGNAL_SCORE 16.3`). API anahtarları, mod, sermaye ve kill-switch Telegram'dan değiştirilemez.

| Komut | Ne yapar? |
|---|---|
| `/durum` · `/pozisyon` · `/bakiye` | Genel durum; pozisyonların K/Z, hedef ve stop fiyatları; bakiye (TL karşılığıyla) |
| `/islemler` · `/csv` · `/istatistik` | Son işlemler, işlem geçmişi dosyası, istatistikler |
| `/rapor gun` · `/rapor hafta` · `/rapor ay` | Dönem özeti |
| `/tarama` | "Neden alım yapmıyor?" — son tarama ve en sık eleme nedenleri |
| `/durdur` · `/devam` | Yeni alımları durdurur / devam ettirir (açık pozisyon takibi sürer) |
| `/sat ETH` · `/hepsinisat` | Pozisyonu hemen satar (onay ister) |
| `/kapat` | Botu güvenle kapatır (onay ister; servis yeniden başlatmaz) |
| `/sermayeekle 10` | Hesaba eklediğiniz parayı bota tanıtır (onay ister) |
| `/ayarlar` · `/ping` · `/yardim` | Ayarlar, "çalışıyor musun?", komut listesi |
| `/menu` · `/ayar tp 2.5` | Kontrol paneli; tek bir ayarı değiştirme |
| `/piyasa` · `/gecikme` · `/loglar` · `/ip` | Piyasa taraması, ağ gecikmesi, son loglar, dış IP |
| `/pano` · `/toz` | Canlı K/Z panosu (aç / kapat); tozları BNB'ye çevir |
| `/backtest` | Backtest menüsü (düğmelerle ayarla, başlat, sonuçları al) |

Riskli komutlar 60 sn içinde `/evet` (veya ✅ düğmesi) onayı ister (`COMMAND_PIN` ayarlıysa düğme gösterilmez,
`/evet PIN` yazılır). Her gece günlük, pazar
günleri haftalık özet gelir.

### 🧪 Telegram'dan backtest (`/backtest` veya `/menu` → 🧪 Backtest)

Sunucuya bağlanmadan, düğmelerle:

| Düğme | Seçenekler |
|---|---|
| 📋 Tür | Normal backtest, çıkış taraması (`--tara`), giriş taraması, filtre gevşetme, A/B filtre testi, strateji karşılaştırma, strateji turnuvası, zarar azaltma deneyi, tüm taramalar birden |
| 🧭 Strateji | Botun stratejisi veya herhangi bir strateji (yalnızca bu test için) |
| 🗓️ Süre · 📅 Bitiş | 7 günden 3 yıla; bitiş bugün, N gün önce veya **önceki dönem** (bulunan ayarları başka bir dönemde doğrulamak için) |
| 💼 Sermaye · 🪙 Coin | Ayardaki / botun güncel sermayesi veya 11-1000 USDT; en hacimli 10-100 coin |
| ⚙️ Test ayarları | Kâr Al, Zarar Kes, izleyen stop, başabaş, maks. süre, pozisyon sayısı, ilk giriş %, hedef alanı (`MIN_ROOM_RATIO`), 24s en fazla düşüş, en düşük sinyal puanı, filtreler, sigortalar, akıllı yönetim — **yalnızca bu test için**, botun ayarları değişmez (giriş taramasında bulunan birleşimi kill-switch açık normal backtest'le doğrulamak için) |
| 🎚️ Tarama değerleri | Çıkış taramasında denenecek Kâr Al / Zarar Kes / izleyen stop / süre / alan değerleri |
| ▶️ Başlat · ⏹ Durdur · 📄 Son sonuç | Çalıştırır (ilerleme mesajı güncellenir), durdurur, son sonucu tekrar gönderir |

Backtest botun içinde değil **ayrı bir süreçte, düşük öncelikle** çalışır: bot bu sırada pozisyonları izlemeye
devam eder, bellek yetmezse önce backtest kapanır. Aynı anda bir backtest çalışır; süre sınırı yoktur, saatler sürse de bitene kadar çalışır (menüden elle
durdurulabilir; bot yeniden başlatılırsa çalışan backtest de durur).
Sonuçta rapor Telegram'da eşit aralıklı yazıyla (tablolar düzgün görünür), tamamı `rapor.txt` dosyası olarak,
grafik resim, tablolar CSV olarak gelir; son 5 sonuç `data/backtest_runs/` klasöründe saklanır. Aynı şeyi komut
satırında yapmak için: `--ayar ANAHTAR=DEĞER` (örn. `--ayar TAKE_PROFIT_PCT=3 --ayar VWAP_FILTER=true`).

### 🧠 Akıllı yönetim (`/menu` → Akıllı Yönetim, hepsi varsayılan kapalı)

| Özellik | Ne yapar? | Backtest |
|---|---|---|
| 📏 ATR hedefleri | Kâr Al / Zarar Kes'i coinin 1 saatlik ATR'sine göre belirler (zarar kes yalnızca daralır) | ✅ |
| 🚀 Hedef uzatma | Kâr Al'da momentum güçlüyse satmaz, zirveden %0.5 geri dönünce satar | ✅ |
| ➕ DCA | Net -%5'te pozisyonun yedek akçesiyle bir kez ekler (sorarak / otomatik) | ✅ otomatik |
| 🔺 Piramit | Net +%3 ve momentum güçlüyse yedekle ekler; sonra birleşik pozisyon zarara dönmeden satılır | ✅ otomatik |
| ⏳ Süre sorusu | 3 saat yatay kalan pozisyonda "başabaşta satılsın mı?" (Başabaşta sat / Hemen sat / Bekle) | ✅ otomatik |
| 🏃 Erken kaçış | Net -%4'te hacimli satış baskısı varsa zarar kesi beklemeden çıkış | ✅ otomatik |
| 🪜 Kademeli alım | Sinyalden sonra fiyat düştükçe bekler, dipten %0.3 dönünce alır | ✅ (mumlarla yaklaşık) |
| 🧊 Takılan işlem çıkışı | `STALE_EXIT_MINUTES` dakikada net kâr bir kez bile +%1'i görmediyse (toparlanma gelmedi) maks. süreyi beklemeden satar | ✅ (A/B: 60 / 120 / 240 dk) |
| 🔔 Sinyal onayı | Oynak piyasada (1s ATR yüksek / likidasyon dalgası) veya her sinyalde almadan önce sorar | — |
| 💥 Likidasyon · 📐 RSI uyumsuzluğu | Vadeli işlemler likidasyon dalgası ve pozitif RSI uyumsuzluğu bildirimleri (bilgi amaçlı) | — |
| 📐 Uyumsuzluk alımı | Ana stratejinin **yanında**, her saatlik mum kapanışından sonra en hacimli 30 coinde uyumsuzluk + dönüş teyidi görülünce alır (sorarak = "✅ Al" düğmesiyle, 15 dk; otomatik = sormadan). İsterseniz bu işlemlere ayrı Kâr Al / Zarar Kes: `RSI_DIV_TP_PCT`, `RSI_DIV_SL_PCT` | `--strateji rsi_divergence` (aynı kurallar) |
| 🪙 BNB takviyesi | BNB azalınca "6 USDT'lik BNB alınsın mı?" (bota ayrılmamış bakiyeden; Binance'in en küçük emri ~5 USDT) | — |

DCA ve piramit **yedek akçe** ister (`/menu` → Sermaye → ilk giriş %50 gibi). Sorular pozisyon başına bir kez
sorulur, 15 dk sonra geçersiz olur; `COMMAND_PIN` ayarlıysa düğme yetmez, `/evet PIN` istenir. "Otomatik" hallerin
geçmiş veriyle etkisini `--tara-ab` gösterir; "sorarak" hali geçmiş veriyle denenemez.

---

## 📈 Strateji

**`rebound` (varsayılan), her 30 saniyede bir:**
1. **Ön eleme:** Son 24 saatte düşmüş (`MIN_24H_DROP_PCT`…`MAX_24H_DROP_PCT`), yeterli hacmi ve oynaklığı olan
   USDT pariteleri; en çok düşen `MAX_CANDIDATES` tanesi incelenir.
2. **5 dk mumlarla analiz:** Son 3 saatte en az `MIN_DIP_PCT` düşüş; dip en az 10 dk önce oluşmuş ve düşüşün
   `MIN_RETRACE`…`MAX_RETRACE` kadarı geri alınmış; RSI dipte `RSI_OVERSOLD` altını görmüş, şimdi
   `RSI_ENTRY_MIN`…`RSI_ENTRY_MAX` arasında ve yükseliyor; fiyat EMA(9) üstünde, son mum yeşil, hacim artmış.
3. **Puanlama ve alım:** En iyi aday seçilir, spread uygunsa alınır.

**Diğer stratejiler** (`STRATEGY`; tarama, filtreler ve çıkış kuralları aynı):

| `STRATEGY` | Ne yapar? |
|---|---|
| `trend` | 24 saatte yükselmiş, 5 dk EMA50 > EMA200 coinde fiyat EMA20'ye geri çekilip yukarı dönünce alır |
| `breakout` | Yükselen trendde son 4 saatin zirvesi yüksek hacimle kırılınca alır |
| `swing` / `swing_pullback` | 1 saatlik mumlarla 1-5 günlük, %8-15'lik hareketler (çıkış ayarları da uzun vadeye göre olmalı: örn. TP 12, SL 6, süre 7200 dk) |
| `mean_revert` | **Ortalamaya dönüş:** yalnızca en hacimli `MR_UNIVERSE` (20) coinde, yükseliş trendindeki (1 saatlik EMA50 > EMA200) coinin saatlik kapanışı 20 saatlik ortalamanın `MR_BAND_STD` (2) standart sapma altına inince alır; küçük hedefle satar (önerilen: TP 1.5, SL 5, süre 2880 dk, izleyen stop 1 → 0.5, başabaş 0.7 → 0.2). Kazanma oranı yüksek olur; kâr, kayıpların kazançları silmemesine bağlıdır |
| `rsi_divergence` | **Pozitif RSI uyumsuzluğu:** en hacimli `RSI_DIV_UNIVERSE` (30) coinde fiyat daha düşük dip yaparken RSI daha yüksek dip yapınca (ilk dipte RSI ≤ `RSI_DIV_OVERSOLD`, ikinci dipte en az `RSI_DIV_RISE` puan yüksek) ve son saatlik mum dönüşü **teyit edince** (yeşil, dipten beri görülen en yüksek seviyenin üstünde, ortalamanın üstünde hacimle) alır. `RSI_DIV_TREND_FILTER=true`: yalnızca fiyat 1 saatlik EMA200'ün üstündeyken. Çıkışlar `mean_revert` ile aynı grup (küçük hedef, 2 gün) |
| `rotation` | Haftalık momentum rotasyonu: en hacimli 30 coinden son 4 haftanın en güçlü 3'ünü tutar, BTC 50 günlük ortalamasının altına inince USDT'ye geçer (`STOP_LOSS_PCT=20`, `BACKUP_STOP_PCT=25` önerilir) |
| `rotation` + `ROTATION_COINS` | **Büyük coin trendi:** yalnızca seçilen coinler. `ROTATION_COINS=BTC`, `ROTATION_TOP_N=1`: BTC'nin son 4 haftası artıda ve BTC 50 günlük ortalamasının üstündeyken BTC'yi tutar, değilse USDT'de bekler (haftada bir bakar; trend bozulunca ertesi gün satar). `ROTATION_COINS=BTC,ETH`: ikisinden güçlü olanı (`ROTATION_TOP_N=2`: ikisini birden). Yılda birkaç işlem yapar; amaç sık al-sat değil, büyük düşüşlerin dışında kalmak |

`--strateji` yalnızca o backtest için geçerlidir; botun kullandığı strateji `STRATEGY` ayarıdır. Swing,
mean_revert ve rsi_divergence'ta backtest varsayılanı 365 gün, rotasyonda 3 yıldır (`--strateji swing
--strateji-karsilastir`, `--strateji mean_revert --strateji-karsilastir` = mean_revert / rsi_divergence,
`--strateji rotation`). Rotasyon sonucu her zaman "BTC'yi alıp tutmak"la kıyaslanır.

### 🏆 Strateji turnuvası (`--turnuva`)
Hangi strateji türünün sizin dönemde işe yaradığını **tek komutla** görmek için:
```bash
python main.py --backtest --turnuva --sermaye 32
```
Aynı bir yılda (varsayılan 365 gün; `--gun`, `--bitis` ile değişir) şu birleşimleri, her biri kendi çıkış
kurallarıyla dener ve kazanma oranı, ortalama kazanç / kayıp, işlem başı sonuç ve dönemin iki yarısıyla sıralar:
- **Giriş:** `mean_revert`, `rsi_divergence`, `swing_pullback`, `swing` (1 saatlik mumlar)
- **Çıkış:** küçük hedef (+%1.5 / −%5, 2 gün — kazanma oranı yüksek), dengeli (+%3 / −%3, 3 gün),
  trend (+%12 / −%6, izleyen stop, 5 gün)
- **Piyasa filtresi:** zayıf piyasada alım yap / yapma (`REGIME_SKIP_WEAK`)
- **Rotasyon** (günlük mumlar): haftalık momentum rotasyonu, **BTC trendi** ve **BTC/ETH trendi** (büyük coin trendi)
- Kıyas: aynı dönemde BTC'yi alıp tutmak

✅ işaretli satır (iki yarıda kârlı, ≥30 işlem, şanstan ayırt edilebiliyor) varsa rapor `.env`'ye yazılacak satırları
verir. **Kazanma oranı tek başına kârlılık değildir:** %70 kazanan işlem, kayıplar kazançların 3 katıysa yine zarar
ettirir; kararı "İşl.başı" sütunu verir. Seçilen birleşimi `--bitis` ile önceki yılda doğrulayın ve test modunda izleyin.

Bir birleşimi kill-switch açık **normal backtest**le (işlem listesi, çıkış nedenleri, grafik) tekrar görmek için
Telegram'da: 🧭 Strateji → ör. `mean_revert`, ⚙️ Test ayarları → 🧩 Çıkış profili → `trend`, gerekirse "Zayıfta alma".
Strateji 5 dk'dan 1 saatlik gruba geçince `MAX_24H_GAIN_PCT` turnuvadaki gibi en az 25 olur.

Hiç işlem açmayan satırın nedeni raporda yazılır. Küçük sermayede en sık neden **toz sınırıdır**: komisyon alınan
coinden kesilince satışta LOT adımının (BTC'de 0.00001 BTC) altında bir kalıntı kalır; bu adımın değeri sermayenin
`MAX_DUST_PCT`'sinden (%0.5) büyükse coin alınmaz (canlıda da). Örneğin BTC ~78.000 $ iken bunun için ~160 USDT
gerekir. Komisyonu **BNB ile** ödüyorsanız coin azalmaz, toz kalmaz: `BNB_FEES=true` ile bot (canlı, test modu ve
backtest) BTC / ETH'yi küçük sermayeyle de alır. Telegram'da teste özel: ⚙️ Test ayarları → "Komisyon BNB'den";
büyük coin trendi için 🧭 Strateji → `rotation`, ⚙️ Test ayarları → 🪙 Rotasyon: büyük coin trendi.

---

## 📊 Backtest nasıl okunur?

- **Teşhis bölümü:** işlem başına fiyat hareketiyle komisyon + spread maliyetini ayırır, işlemlerin ne kadarının
  hedefe ulaştığını ve dönemin iki yarısının ayrı sonuçlarını gösterir.
- **K/Z dağılım grafiği:** normal backtest her işlemin net K/Z'sini nokta grafiği olarak `data/backtest_grafik_*.png`
  dosyasına çizer (matplotlib gerekir) ve Telegram ayarlıysa grafiği + raporu Telegram'a gönderir (`--grafiksiz` ile kapanır).
- **A/B filtre testi** (`--tara-ab`): her giriş filtresini / risk sigortasını (BTC sigortası, piyasa rejimi, üst
  fitil, VWAP, aşırı ısınma, RSI uyumsuzluğu teyidi, önceki trend, zarar serisi, günlük limit, zarardan sonra
  bekleme, takılan işlem çıkışı; rsi_divergence'ta trend filtresi)
  tek tek açıp kapatarak aynı veriyle kazanma oranına ve net kâra etkisini gösterir. Yalnızca dönemin **iki
  yarısında da** kârı artıran değişikliği önerir (🟢); tek yarıda işe yarayan 🟡 (şans olabilir), sermayenin
  %0.5'inden küçük fark ≈ (önemsiz) işaretlenir. "Yedek akçe %50" yalnızca kıyas satırıdır: DCA / piramit ona göre ölçülür (DCA tetiği zarar kesten büyükse,
  örn. tetik -%5 / zarar kes -%4, DCA hiç çalışmayacağı için satır zarar kesin yarısıyla denenir). Takılan işlem
  satırları yalnızca maks. süreden kısa değerlerle denenir. Emir defteri / tape filtreleri geçmiş veriyle denenemez.
- **Taramalar** (`--tara`, `--tara-giris`, `--tara-filtre`): veri bir kez indirilir, ayarlar aynı veriyle denenir;
  karşılaştırma adil olsun diye kill-switch kapalıdır; canlıda kill-switch'e takılacak ayarlar MaksDD yanında `!` ile
  işaretlenir ve raporun altında ☠️ ile tarihiyle yazılır (o tarihten sonraki sonuçlar canlıda hiç gerçekleşmez). Çok ayar denendiğinde birkaçı tesadüfen kârlı çıkar:
  yalnızca **iki yarıda da kârlı (✅) ve ≥30 işlemli** satırları ciddiye alın, sonra çakışmayan daha eski bir
  dönemde (`--bitis`) doğrulayın. Tüm satırlar `data/tarama_*.csv` dosyasına yazılır.
- **İşlem başı sonuç negatifse işlem sayısını artırmak zararı da artırır.**
- Geçmiş geleceği garanti etmez: backtest bugünün en hacimli coinleriyle yapılır (listeden çıkan coinler yok),
  kayma yoktur.

---

## 📁 Dosya yapısı

```
tradebot/
├── ayarlar.env        # Tüm ayarlar (GitHub'da, gizli bilgi yok)
├── .env               # Gizli bilgiler — yalnızca sunucuda, GitHub'a yüklenmez
├── VPS_KURULUM.md     # Sunucu kurulumu: sıfırdan, adım adım
├── deploy/tradebot.service  # systemd servisi
├── main.py            # Giriş noktası (komut satırı, modlar)
├── config.py          # Ayarları okur ve doğrular
├── trader.py          # Ana döngü: tarama, alım, pozisyon takibi, yedek stop, kill-switch, raporlar
├── strategy.py        # Giriş stratejileri
├── rotation.py        # Momentum rotasyonu karar fonksiyonları
├── coin_filters.py    # Tehlikeli coin filtresi
├── market_regime.py   # Piyasa rejimi tespiti
├── indicators.py      # RSI, EMA, ATR
├── risk.py            # Kâr al / zarar kes / izleyen stop / başabaş / boyutlandırma
├── exchange.py        # Binance kuralları, canlı ve sanal (paper) borsa
├── binance_client.py  # Binance REST API istemcisi
├── price_stream.py    # Anlık fiyat akışı (WebSocket)
├── telegram_bot.py    # Telegram bildirimleri
├── commands.py        # Telegram komutları
├── control_panel.py   # Telegram kontrol paneli (/menu): düğmeler, ayar değiştirme, strateji seçimi, reset
├── runtime_settings.py  # Telegram'dan değiştirilebilen ayarlar ve .env'ye güvenli yazma
├── entry_filters.py   # Ek giriş filtreleri (fitil, VWAP, aşırı ısınma, RSI uyumsuzluğu, emir defteri, tape)
├── ip_monitor.py      # Sunucunun dış IP'sini izler, değişirse alarm
├── market_scan.py     # "Piyasayı Tara" ve pozitif RSI uyumsuzluğu taraması
├── smart_manager.py   # Akıllı yönetim (canlı): hedef uzatma, DCA, piramit, sorular, kademeli alım, pano
├── position_rules.py  # Akıllı yönetim kuralları ve RSI uyumsuzluğu tespiti (canlı ve backtest ortak)
├── liquidations.py    # Binance vadeli işlemler likidasyon akışı
├── charts.py          # Backtest K/Z dağılım grafiği (matplotlib)
├── backtest_runner.py # Telegram'dan backtest: düğmeli menü, ayrı süreçte çalıştırma, sonuçları gönderme
├── stats.py           # İstatistikler ve raporlar
├── backtest.py        # Geçmiş veriyle strateji testi ve taramalar
├── state.py           # Kalıcı durum ve işlem kayıtları (data/)
├── selftest.py        # Self-test
├── logger.py          # Log ayarları (logs/bot.log), gizli bilgileri maskeleme
├── tests/             # Otomatik testler (sunucuda gerekmez)
└── requirements.txt
```

İşlem geçmişi `data/trades_<mod>.csv`, bakiye seyri `data/equity_<mod>.csv`, durum `data/state_<mod>.json`.

---

## 🩺 Sorun giderme

| Sorun | Çözüm |
|---|---|
| `systemctl status` → `failed` / `activating (auto-restart)` | `sudo journalctl -u tradebot -n 50 --no-pager` ile nedenine bakın |
| `Service unavailable from a restricted location` (451) | Sunucunun konumu Binance tarafından engelleniyor; başka bir (ABD dışı) konumda sunucu açın |
| Telegram mesajı gelmiyor | Bota `/start` yazdığınızdan emin olun; `.env`'deki token ve chat ID'yi kontrol edin |
| Telegram komutlarına cevap yok / mesajlar iki kez geliyor | Aynı token'la başka bir bot (ör. bilgisayardaki) çalışıyor; onu kapatın |
| `Timestamp ... recvWindow` (-1021) | `timedatectl` ile saatin senkronize olduğunu kontrol edin |
| `Invalid API-key, IP, or permissions` (-2015) | Anahtarı, izinleri ve IP kısıtlamasını (sunucu IP'si) kontrol edin |
| "Serbest USDT bakiyesi yok" | USDT'yi Binance'te **Cüzdan → Transfer** ile **Spot**'a aktarın |
| Coinler Binance'te "kilitli" görünüyor | Normal: yedek zarar-kes emri bekliyor |
| Kill-switch kilidi | Servisi durdurun, `python main.py --mode paper --reset-state --yes`, servisi başlatın |
| Bot hiç alım yapmıyor | Telegram'dan `/tarama` yazın; hangi filtrenin elediğini gösterir |

**Geliştiriciler için testler:** `.venv/bin/pip install pytest && .venv/bin/python -m pytest`
