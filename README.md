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
| `python main.py --backtest --strateji futures --izgara` | **Vadeli ızgara testi:** çöküş süresi × oranı × teyit + mikro/makro + σ eşiği (96 kural), en iyilerle çıkış denemeleri, 1 dk ve 3 kat kaymayla doğrulama; en iyi 2 yapılandırmanın ayarlarını verir (Ocak 2020'den bugüne, 10-30 dk) |
| `python main.py --backtest --strateji futures --modul-izgara` | **Vadeli modül ızgarası (Faz 3):** çöküş + pompa short / Bollinger + RSI / hacimli kırılım × en hacimli 20 / 120 / 150 coin, sabit çıkışlar, kayma ×3 ve 1 dk doğrulama (30-90 dk) |
| `python main.py --backtest --strateji futures --faz4-izgara` | **Vadeli Faz 4 ızgarası:** yalnızca long; çöküş + fonlama sıkışması / trend içi düzeltme × piyasa ve limit (maker) giriş × kayma ×1 / ×3, ayarlarınızdaki evren ve günlük en hacimli 30 (≥30 gün / ≥3 yıl); 1 dk doğrulama (30-120 dk) |
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
| ⚙️ Ayarlar | Kâr Al, Zarar Kes, izleyen stop (±0.5) ve en fazla pozisyon (±1); **🔧 Çıkış ayrıntıları**: izleyen stop tabanı, maks. süre, başabaş kalkanı (aç / kapa, tetik, taban), Binance yedek stopu. Vadeli stratejide ayrı sayfa (aşağıda) |
| 💰 Sermaye (yedek akçe) | İlk giriş yüzdesi: %100 (kapalı) / %75 / %50 / %33 — kalan pay USDT olarak yedekte bekler |
| 🧭 Strateji | Hazır stratejiler arasında geçiş (onay ister, bot yeniden başlar; grubun çıkış ayarları yüklenir, eski grubunkiler saklanır) |
| 🔔 Bildirim | Tümü / Sadece Kapanışlar / Sessiz Mod (komut cevapları ve acil uyarılar her zaman gelir); durum raporu aralığı (kapalı / 1 / 4 / 12 saat), günlük özet saati, haftalık özet, sessiz saatler |
| 🛡️ Filtreler · 🧯 Risk / Kill-switch | Ek giriş filtreleri; **☠️ kill-switch limiti** (%10-60, ±5 puan), günlük zarar limiti ve kâr hedefi, zarar serisi sigortası ve bekleme süresi, her çıkıştan / zarardan sonra aynı coine bekleme, coin karnesi, kayma alarmı |
| 🧠 Akıllı Yönetim | Aşağıdaki tablodaki özellikleri açar / kapatır; DCA, piramit, süre ve kaçış için kapalı / sorarak / otomatik |
| 📈 Canlı Pano · 🧹 Toz → BNB | 5 sn'de bir güncellenen tek K/Z mesajı; küçük bakiyeleri BNB'ye çevirme (canlı mod) |
| 🧪 Backtest | Backtest'i Telegram'dan düğmelerle kurup başlatır, sonuçları (rapor, grafik, CSV) Telegram'a gönderir (aşağıda) |
| 🔎 Piyasayı Tara | En hacimli 40 coinde "dipte olup hacmi artan"ları listeler (bilgi amaçlı, alım yapmaz) |
| 📶 Gecikme · 📜 Son Loglar · 🌐 IP | Binance'e ms gecikme, son 15 log satırı, sunucunun dış IP'si |
| 🔄 Sunucu Reset | Bot servisini yeniden başlatır (onay ister; ayrıntı VPS_KURULUM.md) |

**⚡ Vadeli stratejide ⚙️ Ayarlar:** kaldıraç (1x-20x hazır + ±1x), teminat (serbest bakiyenin %25 / 50 / 75 / 95'i)
ve alt sayfalar: **🧭 Giriş sinyali** (taktik; çöküş eşiği %6-15, ölçüm süresi 1-24 saat, ➕ ek pencereler (VEYA: 1 sa %5-7, 12 sa %15), 📐 oynaklık eşiği (σ), ✅ teyit (sadece kapanış / hacim zirvesi / ilk yeşil mum, izleme süresi), 🏆 / 🥈 ızgara testinin en iyi iki seti, likidasyon dalgası şartı;
4'lü onayda yön, hacim ve likidasyon eşikleri, ölçüm kaynağı, BTC trendi), **🎯 Çıkış ve emirler** (Kâr Al / Zarar Kes
hazır değerler + ±, zaman stopu 15 dk-4 saat, Kâr Al emri limit / piyasa, stop güvencesi), **🔒 Başabaş ve izleyen
stop** (başabaş tetiği +%1-5, tabanı +%0.1-3, ± adımlar; izleyen stop aç / kapa, başlangıç +%1-6, mesafe %0.5-3),
**🪙 Coinler** (16 büyük coini tek tek aç / kapa, 🔝 en hacimli 10-100 köklü coin, 📋 listeyi de ekle; varsayılan liste ∪ köklü 20 — aşağıdaki araştırmaya bakın),
**☠️ Kill-switch**, **⭐ Önerilen ayarlar** (araştırmada en iyi çıkan set, tek düğme + onay). Her sayfada seçili değer ✅ ile görünür; sayfalarda backtest sonuçlarının özeti yazar.

Menüden değiştirilen her ayar doğrulanır, hemen uygulanır ve sunucudaki `.env`'nin sonuna yazılır (bot yeniden
başlasa da kalır; `.env`, `ayarlar.env`'yi ezer). Diğer ayarlar için `/ayar ANAHTAR DEĞER` (örn. `/ayar tp 2.5`,
`/ayar DAILY_LOSS_LIMIT_PCT 3`, `/ayar kill 30`, `/ayar QUIET_HOURS 01:00-09:00`, giriş taramasının önerdiği
`/ayar MIN_ROOM_RATIO 0.5`). API anahtarları, mod ve sermaye Telegram'dan değiştirilemez. **Onay isteyenler:**
kill-switch'i gevşetmek (limiti büyütmek; sıkılaştırmak hemen uygulanır, şu anki toplam zararın altında bir limit hiç
seçilemez), vadelide stop güvencesini kapatmak ve vadeli taktiği değiştirmek (o taktiğin çıkış ayarları da yüklenir).

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
| 📋 Tür | Normal backtest, çıkış taraması (`--tara`), giriş taraması, filtre gevşetme, A/B filtre testi, strateji karşılaştırma, strateji turnuvası, zarar azaltma deneyi, tüm taramalar birden, vadeli ızgara testi (`--izgara`), vadeli modül ızgarası (`--modul-izgara`), vadeli Faz 4 ızgarası (`--faz4-izgara`) |
| 🧭 Strateji | Botun stratejisi veya herhangi bir strateji (yalnızca bu test için) |
| 🗓️ Süre · 📅 Bitiş | 7 günden 3 yıla; bitiş bugün, N gün önce, **önceki dönem** ya da **iki dönem önce** (bulunan ayarları başka dönemlerde doğrulamak için; 3 yılda iki dönem önce = 2017-2020, 2018 düşüşü dahil) |
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
| 🛟 Zarar kes uzatma | Zarar kese gelindiğinde **dönüş işareti** varsa (pozitif RSI uyumsuzluğu ya da aşırı satımdan dönen RSI + yeşil mum + alıcı ağırlıklı hacim; hacimli satış baskısında asla) satmaz, zarar kesi **bir kez** `SL_EXTEND_PCT` (varsayılan 1.5 puan) genişletip dönüşü bekler; oraya da gelirse satar. Dönüş gelmezse zarar o kadar büyür: önce A/B testiyle bakın | ✅ (A/B satırı) |
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
| `intraday` | **Gün içi al-sat, yalnızca en büyük coinler:** `INTRADAY_COINS` (varsayılan Kasım 2023'ün ilk 10'u: BTC, ETH, XRP, SOL, ADA, DOGE, TRX, LINK, DOT, LTC; boşsa en hacimli `INTRADAY_UNIVERSE` coin), 5 dk mumlar. Giriş taktiği `INTRADAY_TACTIC`: `vwap` (gün başından beri oluşan VWAP'ın `INTRADAY_BAND_STD` standart sapma altından yeşil mumla dönüş), `bollinger` (alt banttan dönüş), `rsi2` (RSI(2) `INTRADAY_RSI2_MAX` altı), `orb` (gün başının ilk `INTRADAY_ORB_MINUTES` dakikasının zirvesi hacimle kırılınca), `ema_cross` (EMA 9/21 kesişimi), `squeeze` (bantlar daraldıktan sonra kırılım), `donchian` (son `INTRADAY_BREAKOUT_BARS` mumun zirvesi kırılınca), `pullback` (trendde EMA20'ye geri çekilme), `dip` (sert düşüş sonrası dönüş); virgülle birkaçı yazılabilir (ilk sinyal veren alınır). `INTRADAY_TREND_FILTER=true`: yalnızca 5 dk EMA50 > EMA200 iken. Çıkışlar küçük: TP 1.2, SL 2.5, izleyen stop 1 → 0.5, başabaş 0.6 → 0.1, en fazla 6 saat. Telegram'da `/ayar INTRADAY_TACTIC rsi2` gibi değişir. ⚠️ **9 yıllık testte (2017-2026, her dönem kendi başındaki ilk 10 coinle) hiçbir taktik kâr etmedi:** en iyisi `vwap` + trend filtresi 9 yılda −%28 (aynı sürede BTC al-tut ≈ +%990); sık işlem yapan taktikler −%97…−100. İşlem başına ortalama sonuç alış-satış maliyetine (komisyon + spread ≈ %0.2) eşit: komisyonsuz denendiğinde bile avantaj en fazla +%0.13 / işlem. Canlıda gerçek parayla kullanmayın; yalnızca test modunda deneyin |
| `swing` / `swing_pullback` | 1 saatlik mumlarla 1-5 günlük, %8-15'lik hareketler (çıkış ayarları da uzun vadeye göre olmalı: örn. TP 12, SL 6, süre 7200 dk) |
| `mean_revert` | **Ortalamaya dönüş:** yalnızca en hacimli `MR_UNIVERSE` (20) coinde, yükseliş trendindeki (1 saatlik EMA50 > EMA200) coinin saatlik kapanışı 20 saatlik ortalamanın `MR_BAND_STD` (2) standart sapma altına inince alır; küçük hedefle satar (önerilen: TP 1.5, SL 5, süre 2880 dk, izleyen stop 1 → 0.5, başabaş 0.7 → 0.2). Kazanma oranı yüksek olur; kâr, kayıpların kazançları silmemesine bağlıdır |
| `rsi_divergence` | **Pozitif RSI uyumsuzluğu:** en hacimli `RSI_DIV_UNIVERSE` (30) coinde fiyat daha düşük dip yaparken RSI daha yüksek dip yapınca (ilk dipte RSI ≤ `RSI_DIV_OVERSOLD`, ikinci dipte en az `RSI_DIV_RISE` puan yüksek) ve son saatlik mum dönüşü **teyit edince** (yeşil, dipten beri görülen en yüksek seviyenin üstünde, ortalamanın üstünde hacimle) alır. `RSI_DIV_TREND_FILTER=true`: yalnızca fiyat 1 saatlik EMA200'ün üstündeyken. Çıkışlar `mean_revert` ile aynı grup (küçük hedef, 2 gün) |
| `rotation` | Haftalık momentum rotasyonu: en hacimli 30 coinden son 4 haftanın en güçlü 3'ünü tutar, BTC 50 günlük ortalamasının altına inince USDT'ye geçer (`STOP_LOSS_PCT=20`, `BACKUP_STOP_PCT=25` önerilir) |
| `futures` | **Vadeli işlem, çöküş dönüşü (Binance USDⓈ-M, 2x):** 4 saatte %10 düşen coinde long, TP +%6 / SL −%4 / 30 dk; 2020-2026 testinde seçim ve doğrulama dönemlerinde kârlı, kazanma %70. Ayrıntılar aşağıda (⚡ Vadeli işlem) |
| `rotation` + `ROTATION_COINS` | **Büyük coin trendi:** yalnızca seçilen coinler. `ROTATION_COINS=BTC`, `ROTATION_TOP_N=1`: BTC'nin son 4 haftası artıda ve BTC 50 günlük ortalamasının üstündeyken BTC'yi tutar, değilse USDT'de bekler (haftada bir bakar; trend bozulunca ertesi gün satar). `ROTATION_COINS=BTC,ETH`: ikisinden güçlü olanı (`ROTATION_TOP_N=2`: ikisini birden). Yılda birkaç işlem yapar; amaç sık al-sat değil, büyük düşüşlerin dışında kalmak |

Gün içi taktiklerini aynı veriyle karşılaştırmak için `python main.py --backtest --strateji intraday --gun 365
--strateji-karsilastir` (her taktik aynı çıkışlarla) ya da `--tara-giris` (taktik × zayıf piyasada alım × hedef alanı).
9 yılı adil sınamak için her dönemi kendi başındaki 10 büyük coinle deneyin: Telegram'da 🧭 Strateji → `intraday`,
⚙️ Test ayarları → 🪙 Gün içi coinler → "10 büyük (Kas 2023 / Kas 2020 / Kas 2017)" ve 📅 Bitiş → bugün / önceki
dönem / iki dönem önce (3 yıl × 3).

`--strateji` yalnızca o backtest için geçerlidir; botun kullandığı strateji `STRATEGY` ayarıdır. Swing,
mean_revert ve rsi_divergence'ta backtest varsayılanı 365 gün, rotasyonda 3 yıldır (`--strateji swing
--strateji-karsilastir`, `--strateji mean_revert --strateji-karsilastir` = mean_revert / rsi_divergence,
`--strateji rotation`). Rotasyon sonucu her zaman "BTC'yi alıp tutmak"la kıyaslanır.

### ⚡ Vadeli işlem (`STRATEGY=futures`): çöküş dönüşü
Binance **USDⓈ-M vadeli** işlemlerde (CCXT, `defaultType: future`) kaldıraçlı işlem. Ayrı bir motor
(`futures_trader.FuturesBot`) çalışır ama Telegram menüleri, `/durum`, `/pano`, raporlar ve kill-switch aynıdır.

**Kurulum:** Binance'te USDT'yi Spot'tan *USDⓈ-M Vadeli* cüzdanına aktarın; API anahtarında **Enable Futures** izni
açık olsun (IP kısıtlaması: sunucunun IP'si). `.env`'de `STRATEGY=futures` (veya Telegram → 🧭 Strateji → Vadeli).
Önce test modunda (`TRADING_MODE=paper`) çalıştırın: piyasa verisi gerçek, emirler sanal. Kontrol:
`.venv/bin/python main.py --selftest` (CCXT bağlantısı, pozisyon modu, vadeli bakiye).

**Sermaye.** Bot yalnızca kendisine verilen sermayeyle işlem yapar: canlı modda pozisyonlar vadeli cüzdandaki serbest
bakiyeyle açılır ama botun sermayesini (başlangıç `START_CAPITAL_USDT` + K/Z + `/sermayeekle`) aşmaz; kill-switch de
bu sermayenin `MAX_TOTAL_LOSS_PCT`'si. Cüzdana para ekleyince Telegram'dan **`/sermayeekle <tutar>`** yazın (onay ister):
sermaye ve kill-switch limiti birlikte büyür. Tanıtılmayan para kullanılmaz (tanıtılmadan kullanılsaydı pozisyonlar
büyür ama kill-switch eski sermayeye göre kalır, bot ilk dalgalanmada kendini kilitlerdi). Açılışta cüzdan ile sermaye
belirgin farklıysa bot uyarır. Canlıya **ilk kez** geçerken (`state_futures_live.json` yokken) sermaye
`START_CAPITAL_USDT`'den alınır: önce bu değeri cüzdandaki tutara eşitleyin. Test modunda `/sermayeekle` sanal bakiyeye ekler.

**Taktik (`FUTURES_TACTIC=crash`, varsayılan): çöküş dönüşü.** `FUTURES_COINS`'teki (varsayılan Kasım 2023'ün ilk 10'u)
bir coin son `FUTURES_CRASH_HOURS` (4) saatte en az `FUTURES_CRASH_DROP_PCT` (%10) düştüyse (likidasyon şelalesi),
5 dk mum kapanışında **long** açılır; aynı anda birden çok coin çökerse en derin düşen alınır. Çıkışlar (fiyat %):
**Kâr Al +%6** (Binance'te limit emir), **Zarar Kes −%4** (Binance'te STOP_MARKET; konamazsa pozisyon hemen kapatılır),
**başabaş** fiyat +%1.5'e gelince stop girişin +%0.3 üstüne, **zaman stopu 30 dk**; zararla kapanan coine **4 saat**
(`LOSS_COOLDOWN_MINUTES=240`) tekrar girilmez. **Kaldıraç 2x** (`FUTURES_LEVERAGE`, Telegram'da 1x-20x düğmeleri;
3x üstü önerilmez). `FUTURES_CRASH_LIQ=true`: çöküş mumunda likidasyon dalgası da şart (kazanma %76-79, daha az işlem).
`FUTURES_TACTIC=liq4`: ilk 4'lü onay sürümü (likidasyon + hacim + RSI uyumsuzluğu + BTC trendi; aşağıda neden
bırakıldığı).

**İşlem altyapısı:** her girişten HEMEN önce `set_margin_mode('isolated')` + `set_leverage` (hızlı girişte önceden
ayarlanıp her taramadan önce doğrulanır, bkz. "Canlı giriş gecikmesi"); vadeli cüzdandaki serbest
bakiyenin `FUTURES_MARGIN_USAGE_PCT`'si (95) teminat × kaldıraç (bileşik); miktar / fiyat CCXT `amount_to_precision` /
`price_to_precision` ile; tutar Binance'in en küçük emir tutarının altındaysa işlem pas geçilir ve Telegram'a yazılır
(bot çökmez). Hesap tek yönlü (One-Way) olmalı: açılışta API'den teyit edilir. Telegram: pozisyonlarda `[LONG 2X]`
ve anlık ROE (`/pano`, `/durum`, `/pozisyon`); ⚙️ Ayarlar'da kaldıraç, teminat, taktik, eşikler, çıkışlar, coinler ve
kill-switch düğmeleri (yukarıda). Durum ve işlem kaydı
spot'tan ayrıdır: `data/state_futures_<mod>.json`, `data/trades_futures_<mod>.csv`.

**Nasıl bulundu (Kasım 2026 araştırması):** 15 coin, Ocak 2020 – Kasım 2026, 5 dk mumlar; ayarlar yalnızca
**seçim dönemi** (2020-01 → 2023-10) verisiyle seçildi, **doğrulama dönemi** (2023-11 → 2026-11, farklı coin listesi)
hiç kullanılmadan sonra sınandı.
- 5 dk – 1 saat ufkunda (scalping) 37 farklı sinyal ailesinden hiçbiri maliyeti (~%0.14 / işlem) aşmadı; ilk 4'lü onay
  sürümü (liq4) 7 yılın 7'sinde zarar etti. Short tarafında hiçbir ailede avantaj yok.
- Büyük çöküşlerden (4 saatte ≥ %7-10) sonra fiyat ortalama toparlanıyor: rastgele long girişler 24 saatte ≈ %0 net
  getirirken bu girişler her yıl ve coinlerin çoğunda pozitif; denenen 1.944 çıkış ayarının %97'sinden fazlası iki
  dönemde de kârlı.

| Kaldıraç | Seçim 2020-01 → 2023-10 | Doğrulama 2023-11 → 2026-11 |
|---|---|---|
| 1x | 30 → 189 USDT, yıllık +%62, en büyük düşüş %33 | 30 → 65 USDT, yıllık +%29, düşüş %12 |
| **2x** | **30 → 694 USDT, yıllık +%127, düşüş %56** | **30 → 127 USDT, yıllık +%61, düşüş %25** |
| 3x | 30 → 1.679 USDT, yıllık +%186, düşüş %72 | 30 → 225 USDT, yıllık +%95, düşüş %38 |

Kazanma oranı %70-71, 1 dk çıkışlar, maker / taker komisyonu, kayma, gerçek fonlama oranları, en küçük emir tutarı,
coin beklemeleri ve art arda zarar molası (canlıyla aynı kurallar) dahil; likidasyon yok. Tüm dönem (2x, tek hesap):
30 USDT → ~2.900 USDT; işlem başı sonuç 7 yılın 7'sinde pozitif. 10x'te aynı strateji Mart 2020'de hesabı eritti.
Stres testlerinde (5 kat kayma, 2 kat komisyon, 1-3 dk gecikmeli giriş, stopun fitilin yarısında dolması) kârlı kaldı.

⚠️ **Riskler:** en büyük düşüş COVID çöküşünde (Mart 2020) 2x'te %56: kill-switch (−%40) o gün botu durdururdu. Ani
çöküşlerde (ör. 10 Ekim 2025) zarar kes emri stop fiyatının çok ötesinde dolabilir, borsa gecikebilir; listedeki bir
coin tamamen çökerse (LUNA, FTT gibi; test listelerinde yoktu) art arda kayıp yaşanır. Geçmiş sonuç gelecek garantisi
değildir: önce test modunda izleyin, küçük tutarla başlayın.

**Backtest:** `python main.py --backtest --strateji futures --tum-donem` (Telegram: 🧪 Backtest → Strateji: Vadeli →
Süre: Tüm dönem). Veri Binance'in herkese açık arşivinden indirilir. Rapor: (A) 30 USDT ile tek hesap, (B) her yıl
yeniden başlama, (C) sabit pozisyonla stratejinin kendisi; yıllara göre kazanma oranı, maks. düşüş, kayıp serisi,
likidasyon riski, kaldıraç / TP-SL duyarlılığı, tek kural değişince; grafik Telegram'a gelir.

#### Daha çok coin, izleyen stop, başabaş (Kasım 2026 araştırması)
Aynı kurallar (2x, Kâr Al %6, Zarar Kes %4, başabaş +1.5 → +0.3, 30 dk, 30 USDT), Binance arşivindeki **913 USDT
süresiz paritesinin tamamı** (sonradan kaldırılanlar dahil; stabil coin, endeks ve 2026'da açılan hisse / emtia
vadelileri hariç). Her ay, bir önceki aya kadarki hacme göre evren seçildi (geleceği görmeden); ayarlar seçim
döneminde (2020-01 → 2023-10) seçildi, doğrulama döneminde (2023-11 → 2026-10) sınandı.

| Coinler | Seçim dönemi | Doğrulama dönemi |
|---|---|---|
| **Sabit liste (varsayılan, 10 büyük coin)** | +%110/yıl, düşüş %56 | **+%62/yıl, düşüş %25** |
| Son ayın en hacimli 10 / 20 / 40 / 60 / 80'i | +%226 … +%697/yıl | **hepsinde hesap eridi** (30 USDT → 7 USDT ya da sıfır) |
| En az 3 yıllık, 6 ay ort. hacim: ilk 10 | +%262/yıl, düşüş %55 | +%55/yıl, düşüş %28 |
| … ilk 20 | +%200/yıl, düşüş %59 | +%81/yıl, düşüş %33 |
| … ilk 40 | +%224/yıl | +%11/yıl, düşüş %78 |
| … ilk 60 / 80 | +%247/yıl | −%8 / −%42/yıl |

Ham hacim sıralaması 2024-2026'da yeni çıkan hype / meme coinleri (TRUMP, FARTCOIN, WIF, 1000PEPE, RIVER, MYX ...)
öne çıkarıyor: bunların çöküşleri toparlanmadı. Bu yüzden `FUTURES_UNIVERSE=N` "en hacimli N **köklü** coin"dir: en az
`FUTURES_UNIVERSE_MIN_AGE_DAYS` (1095 gün = 3 yıl) gündür listelenen kripto paritelerden son `FUTURES_UNIVERSE_AVG_DAYS`
(180) günün ortalama USDT hacmine göre ilk N (canlıda sıralama günde bir yenilenir; çöküş taktiğinde her taramada tek
istekle 24 saatlik özet alınır, yalnızca tepesinden yeterince düşmüş coinlerin mumları istenir; kaldırılması planlanan
ve 30 günden yeni pariteler alınmaz). Telegram: ⚙️ Ayarlar → 🪙 Coinler → 📋 Liste / 🔝 10 · 20 · 40 · 60 · 80 · 100,
🎂 en az 1-4 yıl. Backtest aynı kuralı geçmişe uygular (ilk çalıştırmada ~22 bin küçük aylık hacim dosyası iner).
Canlı sıralama durum dosyasında saklanır (yeniden başlatmada hemen hazır); birkaç paritenin hacmi alınamazsa onlar atlanır,
sıralama hiç alınamazsa önceki sıralama, o da yoksa geçici olarak köklü paritelerin 24 saatlik hacmi kullanılır, 10 dk
sonra yeniden denenir ve Telegram'dan uyarılır (yalnız listeye düşen bot, backtestin liste dışı coinlerde açtığı
işlemleri kaçırır). `/tarama` taranan evreni ("🪙 Evren: 22 coin (en hacimli 20 + liste): BTC, ETH, ...") gösterir.

**Canlı bot karşılaştırması.** Vadeli bot her 5 dk mum için `data/futures_scans_<mod>.jsonl` dosyasına bir satır yazar
(taranan / analiz edilen coinler, genişlik, sinyaller ve sonuçları, beklemedeki coinler ya da taramanın neden yapılmadığı:
durdurulmuş, kill-switch, dondurulmuş, boş yer yok, gecikme; 30 gün tutulur). Vadeli backtest raporu bu kayıtla örtüşen
dönemde backtestin her işlemini canlı botun aynı mumdaki kaydıyla karşılaştırır ("🔍 CANLI BOT KARŞILAŞTIRMASI"): canlı da
açtı mı, açmadıysa neden (kayıt yok = bot kapalıydı, taranmadı, coin canlı evrende yoktu, coin beklemedeydi, sinyal
çıkmadı, sinyal vardı ama boş yer yoktu / minimum emir / hata). Telegram'dan 🧪 Backtest → 7 gün ile kontrol edilir.
Aynı bölüm canlı botun **ölçtüğü giriş gecikmesini** de gösterir (bkz. "Canlı giriş gecikmesi: ölçüm ve hızlı giriş").

- **İzleyen stop** (`FUTURES_TRAILING`, varsayılan kapalı; fiyat +`FUTURES_TRAILING_ACT_PCT` gidince zarar kes en iyi
  fiyatın `FUTURES_TRAILING_GAP_PCT` gerisine çekilir, Binance'teki stop emri de taşınır): seçim döneminde çoğu ayarda
  sonucu iyileştirdi ama doğrulama döneminde iyileştirmedi (sabit listede mevcut çıkışlara eklenince +%62 → +%27-48/yıl;
  köklü ilk 20'de tüm 7 ayar daha kötü). Bu stratejide getiriyi sabit Kâr Al'ın 30 dk içinde yakaladığı ilk sıçrama
  sağlıyor; izleyen stop gürültüde erken çıkarıyor.
- **Başabaş** (Telegram'da tetik +%5'e, taban +%3'e kadar): +1.5 → +0.3 en düşük düşüşü verdi (%25). Tetik +4 / +5
  getiri kazandırmadı (+%41-62/yıl), düşüşü %39'a çıkardı; köklü ilk 20'de getiriyi +%1-10/yıla indirdi.
- Zaman stopu 60 / 120 dk (izleyensiz): doğrulama döneminde +%51 / +%54/yıl (30 dk: +%62).

#### İşlem sayısını artırmak (Kasım 2026 araştırması, varsayılanlar buna göre)
Aynı veri ve dönemler; önce 5 dk çıkışlarla ~150 senaryo tarandı, finalistler 1 dk çıkışlarla doğrulandı. Önceki
varsayılanla en büyük kayıp "fırsat kaçırma"ydı: tüm dönemde 17.885 sinyal mumunun 11.861'i açık pozisyon, 3.797'si
coin beklemesi, 1.138'i zarar serisi molası yüzünden atlanıyordu.

| Ayar | 2020-01 → 2023-10 | 2023-11 → 2026-10 |
|---|---|---|
| Önceki (liste, coin bekleme 30 dk, zarar sonrası 240 dk, 3 zarar kes → 4 sa mola) | 161 işlem/yıl, +%123/yıl, düşüş %56 | 53 işlem/yıl, +%62/yıl, düşüş %25 |
| **Yeni: liste ∪ köklü 20, kârdan sonra beklemesiz, zarar sonrası 240 dk, mola yok** | **242/yıl, +%379/yıl, düşüş %57** | **137/yıl, +%99/yıl, düşüş %30** |
| Yeni + "1 saatte ≥%6" ek kuralı (eklenmedi) | 366/yıl, +%189/yıl, düşüş %74 | 235/yıl, +%182/yıl, düşüş %38 |
| Yeni + aynı anda 2 pozisyon (eklenmedi) | 333/yıl, +%277/yıl, düşüş %81 | 184/yıl, +%147/yıl, düşüş %45 |

Yıl yıl (her yıl 30 USDT ile): yeni ayarlar 7/7 yıl kârlı (2020 +%190, 2021 +%4.157, 2022 +%114, 2023 +%117,
2024 +%229, 2025 +%46, 2026 +%13); önceki ayarlar 2020'de −%11'di. Tüm dönem tek hesap: 30 → ~101.000 USDT (önceki
~2.800), 1.337 işlem (önceki 778); kill-switch hiç tetiklenmedi. Stres: 5 kat kayma +%38/yıl (önceki +%40), 2 kat
komisyon +%54/yıl; stopların fitilin yarısında dolduğu kötümser varsayımda −%1/yıl (önceki −%5).

Denenip **alınmayanlar**: aynı anda birden çok pozisyon (bakiye bölünerek: işlem ↑ ama getiri ↓; tam büyüklükte:
düşüş %80'e çıktı), daha düşük eşik (4 sa ≥ %7-9: seçim döneminde kâr düştü), 8 / 12 / 24 saatlik pencereler (zayıf),
1 saatlik ek kural (≥ %6 doğrulamada iyi ama ≥ %7 iki dönemde de kötü: tutarsız), likidasyon şartıyla daha düşük eşik,
daha kısa zaman stopu (kâr ↓), köklü 30 ve üstü (doğrulamada bozuldu), short / pompa sönmesi (çoğu ayarda zarar).

Canlı bota uygulamak için: Telegram → ⚙️ Ayarlar → **⭐ Önerilen ayarlar** (onay ister; bekleme, mola, evren ve
çıkışları birlikte yazar). Bekleme ve mola değerleri vadeli profilindedir: spot bir stratejiye dönünce spot
varsayılanları (30 dk, 3 zarar kes) geri gelir.

#### Dinamik çöküş kuralları ve ızgara testi (`--izgara`, Kasım 2026)
Mevcut ana kural (`FUTURES_CRASH_HOURS` / `FUTURES_CRASH_DROP_PCT`) aynen duruyor; aşağıdakiler ona **eklenir** ve
hepsi varsayılan kapalıdır (açılmadıkça bot eskisi gibi çalışır). Canlı bot ve backtest aynı fonksiyonları kullanır
(`strategy.crash_state` / `crash_entries`). Telegram: ⚙️ Ayarlar → 🧭 Giriş sinyali.

| Ayar | Ne yapar |
|---|---|
| `FUTURES_CRASH_EXTRA` | Ana kurala **VEYA** ile eklenen pencereler `saat:%`, ör. `1:5,12:15` = mikro (1 saatte %5) / standart (ana kural, 4 saatte %10) / makro (12 saatte %15) çöküş: herhangi biri yeter |
| `FUTURES_CRASH_Z` (+ `_MIN_PCT`) | Sabit yüzdeler yerine **oynaklık eşiği**: düşüş, coinin o süredeki olağan hareketinin (pencereden önceki 24 saatin 5 dk getirilerinin standart sapması × √mum sayısı) en az Z katı olmalı ("son 4 saatte 3 standart sapma"); en az `FUTURES_CRASH_Z_MIN_PCT` (%3) düşüş de aranır |
| `FUTURES_CONFIRM` (+ `_BARS`, `_VOLUME_RATIO`) | **İzleme modu / kapitülasyon teyidi**: `close` = çöküş mumunun kapanışında hemen al (önceki davranış); `volume` = çöküşten sonraki `FUTURES_CONFIRM_BARS` (12 = 1 saat) mum içinde hacmi önceki 20 mumun 3 katı olan mumda; `green` = ilk yeşil mumda; `either` = hangisi önce gelirse |

**Izgara testi:** `python main.py --backtest --strateji futures --izgara` (Telegram: 🧪 Backtest → Tür: Vadeli ızgara
testi). 1) 96 giriş kuralı: düşüş süreleri [1, 2, 4, 8 saat] × oranları [%5, 8, 10, 15] × teyit [sadece kapanış, hacim
zirvesi, ilk yeşil mum] + mikro/standart/makro × 4 teyit + oynaklık eşiği [1, 2, 4, 8 saat] × [2.5, 3, 4σ] × 3 teyit +
ana kurala eklenen pencereler; 2) en iyi giriş kurallarıyla 48 çıkış (TP / SL / başabaş / zaman stopu); 3) adaylar
1 dk mumlarla ve 3 kat kaymayla doğrulanır, en iyi 2 yapılandırma ayar satırlarıyla raporlanır (10-30 dk). Veri:
2020-01 → 2026-11, liste ∪ köklü 20, 2x, 30 USDT; seçim dönemi 2020-01 → 2023-10, doğrulama 2023-11 → 2026-11.

Bulgular (önce 5 dk mumlarla, sonra 1 dk ile doğrulandı):
- **"İlk yeşil mum" teyidi her pencerede kârı düşürdü** (ör. 4 sa %10: işlem başı +%0.46 → +%0.05; düşüş durup ilk
  yeşil mum geldiğinde dönüşün çoğu kaçmış oluyor). **Hacim zirvesi** işlem başı sonucu artırdı ama işlemi %60-90
  azalttı (4 sa %10: 189 → 73/yıl): hedefin tersi.
- **Oynaklık (σ) eşiği** aynı işlem sayısında sabit yüzdeden kötü (1 sa 4σ: 316/yıl, işlem başı +%0.12; 1 sa %5:
  365/yıl, +%0.19). Likidasyon şelaleleri kaldıraç seviyelerine bağlı sabit yüzdelerde tetikleniyor, σ'da değil.
- Tek pencerede eşiği düşürmek işlemi çok artırıyor ama işlem başı sonucu sıfıra indiriyor (4 sa %5: 1.104/yıl,
  −%0.04; 8 sa %8: 649/yıl, +%0.04). Mikro/standart/makro VEYA mevcut çıkışlarla yılda 479 işlem ama kazanma %65.
- **Asıl kazanç çıkışlarda:** ek 1 saatlik pencerenin sinyalleri, başabaşın +1 → +0.3'e, SL'nin 5'e, TP'nin 8'e ve
  zaman stopunun 45-60 dk'ya çekilmesiyle kârlı hâle geliyor. Başabaş tabanı +0.2 daha da iyi görünüyor ama maliyete
  (komisyon + kayma ≈ %0.14) çok yakın: kayma 3-5 katına çıkınca bu küçük kazançlar zarara dönüyor (kazanma %85 → %20).

| 1 dk çıkışlarla | İşlem/yıl (seçim / doğr.) | Kazanma | Toplam (sabit poz.) | Yıllık getiri seçim / doğr. | Maks. düşüş seçim / doğr. | Kârlı yıl | Kayma ×3: yıllık |
|---|---|---|---|---|---|---|---|
| Mevcut (4 sa %10, TP 6 / SL 4 / BE 1.5 → 0.3 / 30 dk) | 195 (241 / 137) | %70.0 | +%526 | +%385 / +%99 | %57 / %30 | 7/7 | +%216 / +%73 |
| **🏆 #1: + 1 sa %6 + 12 sa %15**, TP 8 / SL 5 / BE 1 → 0.3 / 45 dk | **410 (489 / 308)** | %79.7 | +%726 | +%721 / +%146 | %66 / %54 | 5/7 | +%220 / +%62 |
| **🥈 #2: + 1 sa %6**, TP 8 / SL 5 / BE 1 → 0.3 / 45 dk | **343 (411 / 257)** | %81.2 | +%719 | +%587 / +%213 | %52 / %39 | 7/7 | +%236 / +%100 |
| En çok işlem (kırılgan): + 1 sa %5 + 12 sa %15, TP 8 / SL 5 / BE 0.7 → 0.2 / 45 dk | 608 (721 / 464) | %83.8 | +%603 | +%394 / +%108 | %76 / %63 | 6/7 | +%94 / +%2 |

Yıl yıl (her yıl 30 USDT ile, tek hesap): mevcut 2020 +%190, 2021 +%4.157, 2022 +%114, 2023 +%117, 2024 +%229, 2025
+%46, 2026 +%13; **#1** +%2.789, +%7.285, **−%1, −%11**, +%444, +%511, +%73; **#2** +%840, +%7.290, +%44, +%9, +%545,
+%642, +%66. #1 yılda 400 işlem hedefini tutturuyor ama durgun 2022-2023'te kazanamadı; **#2 her yıl kârlı ve maliyet
artışına daha dayanıklı** (önerilen). Kazanma oranının yükselmesi büyük ölçüde başabaş stopuyla küçük kârla kapanan
işlemlerden (çıkışların ~%60'ı): işlem başı net sonuç #2'de +%0.31 (mevcut +%0.39), ama işlem sayısı arttığı için toplam
kâr daha yüksek. Kâr korunarak ulaşılabilen en yüksek işlem sayısı sağlam ayarlarda 1.8-2.1 kat (yılda 343-410); 3-4
kat yalnızca maliyet varsayımına çok duyarlı ayarlarla mümkün oldu. `--izgara` komutu bu iki yapılandırmayı kendiliğinden
bulur (aynı veriyle aynı sonuç).

Uygulamak için: Telegram → 🧭 Giriş sinyali → **🏆 Izgara #1** ya da **🥈 Izgara #2** (onay ister; testteki evren ve
bekleme ayarlarını da yazar) ya da `.env`'de `FUTURES_CRASH_EXTRA=1:6` + `TAKE_PROFIT_PCT=8 STOP_LOSS_PCT=5
BREAKEVEN_TRIGGER_PCT=1 BREAKEVEN_FLOOR_PCT=0.3 MAX_HOLD_MINUTES=45` (#2; #1 için `FUTURES_CRASH_EXTRA=1:6,12:15`).
Önce test modunda (paper) izleyin.

#### Faz 3: ek giriş modülleri ve geniş evren (`--modul-izgara`, Kasım 2026)
Çöküş kuralına **VEYA** ile eklenebilen üç modül; hepsi varsayılan kapalı (Telegram: 🧭 Giriş sinyali → 🧩 Ek
modüller). Tek pozisyon kuralı aynen geçerli; aynı anda birden çok sinyal varsa öncelik çöküş > pompa > kırılım >
Bollinger. Canlı bot ve backtest aynı fonksiyonları kullanır; Bollinger / kırılım açıkken her coinin mumları her 5
dakikada (çok coinde paralel) istenir, pompa açıkken 24 saatlik dibinin yeterince üstündeki coinler de taranır.

| Ayar | Ne yapar |
|---|---|
| `FUTURES_PUMP_RULES` | **Pompa dönüşü SHORT:** son H saatte en az %X YÜKSELEN coinde 5 dk mum kapanışında short, ör. `1:6,4:10,12:15` |
| `FUTURES_BB` (+ `_STD`, `_RSI_LOW`, `_RSI_HIGH`) | **Bollinger (20, 2) + RSI(14):** kapanış alt bandın altında ve RSI < 25 → long; üst bandın üstünde ve RSI > 75 → short (`off` / `both` / `long` / `short`) |
| `FUTURES_BREAKOUT` (+ `_VOLUME_RATIO`, `_HOURS`) | **Hacimli kırılım LONG:** hacim önceki 20 mumun 3 katından fazla ve kapanış son 4 saatin zirvesinin üstünde |
| `FUTURES_UNIVERSE` | Telegram'da artık 120 / 150 seçenekleri de var (en fazla 150) |

**Test:** `python main.py --backtest --strateji futures --modul-izgara` (Telegram: 🧪 Backtest → Tür: Vadeli modül
ızgarası). Çıkışlar sabit: TP 8 / SL 5 / başabaş +1 → +0.3 / 45 dk (🥈 #2), 2x, 30 USDT; 2020-01 → 2026-11; önce 5 dk
mumlarla tarama, sonra 1 dk ile doğrulama ve 3 kat kayma. Not: 3 yıllık yaş kuralıyla 2020-2022'de yalnızca ~11 köklü
coin vardı; 120-150 coinlik evren ancak 2024'ten itibaren oluşuyor.

| Liste ∪ köklü 20 coin | İşlem/yıl | Kazanma | İşlem başı | Yıllık getiri seçim / doğr. | Maks. düşüş | Kayma ×3 |
|---|---|---|---|---|---|---|
| Çöküş #2 (mevcut en iyi, 1 dk) | 343 | %81.2 | +%0.31 | +%587 / +%213 | %52 / %39 | +%236 / +%100 |
| Çöküş + pompa short (1 dk) | **1.008** | %74.1 | +%0.03 | −%32 / −%23 | %96 / %90 | eridi |
| Yalnız pompa short (1 dk) | 739 | %71.4 | −%0.11 | eridi | %98 / %95 | eridi |
| Yalnız Bollinger + RSI, long + short (5 dk) | 3.844 | %51.5 | −%0.13 | eridi | %92 / %91 | eridi |
| Yalnız hacimli kırılım (5 dk) | 3.477 | %41.0 | −%0.12 | eridi | %92 / %95 | eridi |
| HEPSİ: çöküş + pompa + Bollinger + kırılım (5 dk) | 5.520 | %47.6 | −%0.09 | eridi | %95 / %92 | eridi |
| HEPSİ, 1 dk ile yalnızca 2025 | 5.395 | %42.9 | −%0.13 | 2025'te tek hesap −%91 | %91 | −%91 |

Yıl yıl (her yıl 30 USDT ile, 1 dk): çöküş #2 2020 +%840, 2021 +%7.290, 2022 +%44, 2023 +%9, 2024 +%545, 2025 +%642,
2026 +%66 (7/7); **çöküş + pompa** +%89, +%40, −%66, −%61, −%26, +%148, −%67 (3/7, tek hesap 30 → 3 USDT); yalnız
pompa short 7 yılın 6'sında zararda (2021 −%92, 2023 −%85, 2024 −%85).

| Çöküş #2, evren genişliği | İşlem/yıl (2024-26) | İşlem başı (2024-26) | Doğrulama yıllık | Kayma ×3 doğr. |
|---|---|---|---|---|
| 20 köklü + liste (1 dk) | 343 (257) | +%0.29 | +%213 | +%100 |
| 30 (1 dk) | 397 (368) | +%0.12 | +%64 | −%4 |
| 40 (1 dk) | 458 (506) | +%0.03 | −%19 (2024 −%52) | eridi |
| 60 (5 dk) | 590 (812) | +%0.03 | −%28 | eridi |
| 120 (5 dk) | 973 (1.682) | −%0.02 | eridi | eridi |
| 150 (1 dk) | 1.118 (1.974) | −%0.07 | eridi (2024 −%92, 2025 −%92, 2026 −%91) | eridi |

Sonuç: **yılda 1.000+ işleme kârla ve kayma stresini geçerek ulaşan birleşim yok.** İşlem sayısı kolayca artıyor
(pompa ekleyince 1.008, 150 coinde 1.118, hepsi birlikte 5.000-8.000/yıl) ama işlem başı net sonuç maliyetin
(komisyon + kayma ≈ %0.14) altına iniyor. Modüller kendi tipik çıkışlarıyla da (TP 0.6-3, SL 0.6-2, 30-240 dk; 7 çıkış
× 4 modül) denendi: 28 birleşimin 28'i zararda. Pompa short'ta kazanma oranı %70-79 olsa da sürmekte olan pompalar
zarar kes'e gidiyor; Bollinger ve kırılımın 5 dk hareketi maliyeti karşılamıyor (önceki araştırmadaki 37 sinyal
ailesiyle aynı sonuç). Çöküş long'unun avantajı likidasyon zincirinin zorla satışından geliyor; bunun simetriği
pompalarda yok, geniş evrende de küçük / yeni coinlerin çöküşleri toparlanmıyor. Öneri: 🥈 #2 (liste ∪ köklü 20).

#### Faz 4: limit (maker) giriş, fonlama sıkışması, trend içi düzeltme, günlük hacim evreni (`--faz4-izgara`)
Dört yeni yapı; hepsi varsayılan kapalı, mevcut ayarlar değişmedi. Canlı bot ve backtest aynı fonksiyonları kullanır.

| Ayar | Ne yapar |
|---|---|
| `FUTURES_ENTRY_ORDER=limit` (+ `FUTURES_LIMIT_OFFSET_PCT`, `FUTURES_LIMIT_TTL_MIN`) | **Limit (maker) giriş:** sinyalde piyasa emri yerine en iyi alış (bid) fiyatının %0.05 altına **post-only** (GTX) limit emir; 3 dk içinde dolmazsa iptal (fırsat kaçar). Kısmen dolarsa dolan kadarı pozisyon olur; bot kapanırken (Ctrl+C / `systemctl stop`) bekleyen emir iptal edilir. Telegram: 🎯 Çıkışlar ve Emirler |
| `FUTURES_FUNDING=true` (+ `_RATE_PCT`, `_RULES`) | **Fonlama sıkışması LONG:** son ödenen fonlama oranı (8 saate oranlanmış; 4 / 1 saatte bir ödenen coinlerde ×2 / ×8) −%0.10 veya altında VE son 1 saatte %5 çöküş. Canlıda oranlar yalnızca çöküş şartını sağlayan coinler için istenir. Telegram: 🧩 Ek modüller |
| `FUTURES_PULLBACK=true` (+ `_RSI`, `_EMA`) | **Trend içi düzeltme LONG:** son tamamlanan 1 saatlik kapanış 1 saatlik EMA(200) üstünde VE 5 dk RSI(14) < 25. Canlıda 1 saatlik mumlar coin başına saatte bir istenir. Telegram: 🧩 Ek modüller |
| `FUTURES_UNIVERSE_MODE=daily` | **Günlük hacim havuzu:** her gün 00:00 UTC'de bir önceki günün USDT hacmine göre en hacimli N coin (`FUTURES_UNIVERSE=30`, yaş için `FUTURES_UNIVERSE_MIN_AGE_DAYS`; Telegram'da ≥30 gün seçeneği). Telegram: 🪙 Coinler |

Öncelik (aynı anda birden çok sinyal): çöküş > fonlama > pompa > düzeltme > kırılım > Bollinger.

**Test:** `python main.py --backtest --strateji futures --faz4-izgara` (Telegram: 🧪 Backtest → Tür: Vadeli Faz 4
ızgarası). Yalnızca long; çıkışlar sabit: TP 8 / SL 5 / başabaş +1 → +0.3 / 45 dk (🥈 #2), 2x, 30 USDT; 2020-01 →
2026-11; 5 dk mumlarla tarama, öne çıkanlar 1 dk mumlarla doğrulandı; kayma ×1 ve ×3. Komisyon Binance VIP0: piyasa
(taker) %0.05, limit (maker) %0.02 (normal hesapta maker iadesi / sıfır komisyon yok). Limit emir modeli: referans
sinyal anındaki fiyat, 3 dk içinde fiyat limitin %0.02 ötesine geçerse limit fiyattan dolar (yalnızca dokunması yetmez:
kuyrukta önümüzdeki emirler önce dolar), geçmezse işlem yok.

**1) Limit (maker) giriş, çöküş #2, liste ∪ köklü 20 (1 dk):**

| Giriş | İşlem/yıl | Kazanma | İşlem başı | Yıllık seçim / doğr. | Maks. düşüş | Kayma ×3: işlem başı, yıllık |
|---|---|---|---|---|---|---|
| Piyasa emri (şimdiki) | 343 | %81.2 | **+%0.31** | +%587 / +%213 | %52 / %39 | +%0.22, +%236 / +%100 |
| Limit bid −%0.02 | 315 | %78.6 | +%0.21 | +%149 / +%111 | %80 / %43 | +%0.18, +%98 / +%77 |
| Limit bid −%0.05 (önerilen deneme) | 307 | %78.6 | +%0.23 | +%234 / +%105 | %82 / %44 | +%0.20, +%158 / +%73 |
| Limit bid −%0.10 | 298 | %78.6 | +%0.22 | +%201 / +%73 | %69 / %47 | +%0.18, +%131 / +%47 |
| Limit −%0.05, dolmazsa piyasa emri | 322 | %77.8 | +%0.21 | +%190 / +%86 | %82 / %58 | +%0.16, +%105 / +%47 |
| Limit −%0.05, maker komisyonu %0 olsaydı | 307 | %78.6 | +%0.26 | +%286 / +%124 | %80 / %43 | +%0.22, +%195 / +%89 |

Limit emir maliyeti gerçekten düşürüyor: işlem başı komisyon %0.099 → %0.069 ve girişte kayma yok; kayma 3 katına
çıkınca piyasa emrinde işlem başı −%0.09 kaybedilirken limitte yalnızca −%0.04 (yalnızca çıkış kayıyor). Ama emirlerin
%11'i (−%0.10'da %16) dolmuyor ve **dolmayanlar en iyi işlemler:** aynı sinyaller piyasa emriyle ortalama **+%1.06**
kazanıyordu (%93'ü kârlı; çöküşten anında sıçrayanlar), dolanlar ise +%0.22 (düşüşü sürenler). Bu "ters seçim"
tasarruftan büyük: işlem başı +%0.31 → +%0.23, yıllık getiri yaklaşık yarıya iniyor; kayma 3 katına çıksa bile piyasa
emri önde. Dolmayınca piyasa emriyle girmek (yedek) de kötü: sıçramayı geç ve pahalı yakalıyor.

**2) Fonlama sıkışması ve trend içi düzeltme, liste ∪ köklü 20 (1 dk):**

| Kural | İşlem/yıl | Kazanma | İşlem başı | Yıllık seçim / doğr. | Maks. düşüş | Kayma ×3 | Kârlı yıl |
|---|---|---|---|---|---|---|---|
| Çöküş #2 (şimdiki) | 343 | %81.2 | +%0.31 | +%587 / +%213 | %52 / %39 | +%0.22 | 7/7 |
| Çöküş + fonlama (−%0.10, 1 sa %5) | 347 | %81.2 | +%0.30 | +%555 / +%232 | %55 / %46 | +%0.21 | 6/7 |
| Yalnız fonlama (−%0.05, 1 sa %5) | 44 | %82.9 | +%0.16 | +%9 / +%7 | %47 / %43 | +%0.02 | 4/7 |
| Çöküş + düzeltme (EMA200, RSI < 25) | **1.242** | %56.3 | −%0.01 | +%40 / eridi | %99 / %91 | −%0.09 | 2/7 |
| Yalnız düzeltme (RSI < 20) | 384 | %51.6 | −%0.01 | +%22 / −%49 | %89 / %92 | −%0.09 | 4/7 |

Fonlama: büyük coinlerde 8 saatlik fonlama nadiren −%0.10'un altına iner (ETH 2023-2024'te en düşük −%0.02); iner ve
aynı anda 1 saatte %5 çöküş olursa da bu genellikle zaten çöküş kuralına (1 saatte %6 / 4 saatte %10) giriyor: modül
yılda yalnızca ~4 işlem ekledi, sonuç neredeyse aynı (2023 +%9 → −%8). Tek başına seyrek (yılda ~45) ve 3 kat kaymada
başabaş. Düzeltme: işlem sayısını 343'ten **1.242'ye** çıkarıyor ama 5 dk RSI < 25 dipleri yükseliş trendinde de çoğu
zaman sürüyor (kazanma %56); çöküş + düzeltme yıl yıl 2020 +%131, 2021 +%6.969, 2022 −%92, 2023 −%81, 2024 −%58,
2025 −%66, 2026 −%91 (tek hesap 30 → 3 USDT).

**3) Günlük "en hacimli 30" havuzu (yaş filtresi olmadan ve ≥3 yıl):**

| Evren, kural | İşlem/yıl | Kazanma | İşlem başı | Yıllık seçim / doğr. | Maks. düşüş | Kârlı yıl |
|---|---|---|---|---|---|---|
| Liste ∪ köklü 20 (aylık, 6 ay ort. hacim), çöküş #2 (1 dk) | 343 | %81.2 | +%0.31 | +%587 / +%213 | %52 / %39 | 7/7 |
| Günlük 30, ≥3 yıl, çöküş #2 (1 dk) | 731 | %77.7 | +%0.05 | +%405 / eridi | %69 / %95 | 4/7 |
| Günlük 30, ≥3 yıl, çöküş + fonlama (1 dk) | 780 | %77.3 | +%0.07 | +%459 / eridi | %69 / %94 | 3/7 |
| Günlük 30, ≥3 yıl, çöküş + düzeltme (1 dk) | 1.787 | %59.9 | −%0.05 | +%5 / eridi | %99 / %92 | 1/7 |
| Günlük 30, ≥30 gün, çöküş #2 (1 dk) | **3.699** | %77.8 | −%0.03 | +%1.620 / eridi | %77 / %92 | 2/7 |
| Günlük 30, ≥30 gün, çöküş #2, limit giriş (1 dk) | 3.379 | %75.9 | −%0.10 | +%60 / eridi | %100 / %94 | 2/7 |
| Günlük 30, ≥30 gün, çöküş + düzeltme (1 dk) | **4.937** | %70.9 | −%0.06 | eridi / eridi | %100 / %92 | 2/7 |
| Günlük 30, ≥30 gün, yalnız fonlama −%0.05 (1 dk) | 1.364 | %77.6 | −%0.04 | −%4 / eridi | %92 / %92 | 2/7 |
| Günlük 30, ≥30 gün, yalnız düzeltme RSI < 20 (1 dk) | 908 | %64.7 | +%0.02 | +%211 / eridi | %92 / %93 | 2/7 |

Günlük 30 coin, ≥3 yıllık olanlarla bile yıl yıl: 2020 +%135, 2021 +%15.549, 2022 +%5, 2023 +%10, **2024 −%88, 2025
−%66, 2026 −%91**. Yaş filtresi olmadan çöküş #2 yılda 3.699 işlem yapıyor: 2021 boğasında hype coinlerle tek yılda
+%460.000 (!), sonra 2022 −%92, 2023 −%67, 2024 −%91, 2025 −%91, 2026 −%92. Yaş filtresi olmadan 7 yılda **627 farklı coin** günün ilk 30'una giriyor (FARTCOIN, PNUT, AIXBT,
USUAL, PUMP...): dünün hacim patlaması çoğu zaman bir hype / yeni listeleme dalgası; bu coinlerin çöküşleri
toparlanmıyor. "Hacim varsa kalite vardır" varsayımı tutmadı: 6 aylık ortalama hacim + 3 yıl yaş (şimdiki ayar)
hype'ı eliyor, günlük hacim eleyemiyor.

Sonuç: **dört yapıdan hiçbiri #2'yi geçemedi.** Limit emir maliyeti %0.05 düşürse de en iyi sıçramaları kaçırıyor;
fonlama sinyali çöküşle örtüşüyor; düzeltme ve günlük havuz işlem sayısını 1.000-6.000'e çıkarıyor ama işlem başı
sonuç maliyetin altına iniyor. Öneri: 🥈 #2 (liste ∪ köklü 20, piyasa emri), yeni modüller kapalı; denemek isterseniz
yalnızca test modunda (paper).

#### Faz 5: dalga modu — kârı azaltmadan daha çok işlem (Kasım 2026)
**Bulgu:** tek pozisyon kuralı yüzünden #2 sinyallerin çoğunu atıyor. Her coini kendi başına sayınca yılda ~1.500
işlem fırsatı var, #2 bunların ~%20'sini alabiliyor; atlananların %91'i "başka coinde açık pozisyon var" yüzünden ve
alınanlar kadar kârlı (ortalama +%0.32, kazanma %80). Kâr piyasa geneli likidasyon dalgalarında toplanıyor:

| Son 1 saatte %3+ düşen coin (genişlik) | Bağımsız işlem | Ortalama | Seçim dönemi | Doğrulama dönemi |
|---|---|---|---|---|
| 0-1 (yalnız çöküş) | 2.400 | −%0.08 | −%0.13 | −%0.02 |
| 2-7 | 1.418 | +%0.25 | +%0.20 | +%0.41 |
| 8+ (piyasa geneli dalga) | 6.559 | +%0.46 | +%0.45 | +%0.49 |

Buna göre iki ayar eklendi (varsayılan kapalı; Telegram: 🧭 Giriş sinyali → 🌊 Dalga modu, ⭐ Faz 5 seti tek dokunuş):

| Ayar | Ne yapar |
|---|---|
| `FUTURES_MAX_POSITIONS` (+ `FUTURES_EXTRA_BREADTH`) | Aynı anda en fazla N pozisyon (farklı coinler); her pozisyonun teminatı bakiyenin `FUTURES_MARGIN_USAGE_PCT / N`'i. İlk pozisyon her sinyalde, sonrakiler yalnızca genişlik ≥ `FUTURES_EXTRA_BREADTH` (8) iken açılır. Bot pozisyon açıkken de her 5 dk'da tarar. |
| `FUTURES_ISOLATED_SIZE_PCT` (+ `FUTURES_ISOLATED_BREADTH`) | Genişlik `FUTURES_ISOLATED_BREADTH`'in (2) altındaysa (yalnız çöküş) pozisyon bu büyüklükte (%; 50 = yarım). |

Genişlik canlı botta her taramada taranan tüm coinlerden (beklemedeki ve açık pozisyondaki coinler dahil), backtestte
o tarihteki evrenin tüm coinlerinden aynı kuralla ölçülür. Rapor: genişliğe göre işlem başı döküm ve "🌊 Dalga modu" /
"Tek pozisyon" karşılaştırma satırları (`python main.py --backtest --strateji futures --tum-donem`).

Sonuç (liste ∪ köklü 20, 🥈 #2 girişi ve çıkışları, 30 USDT, 2020-2026, 1 dk):

| Ayar | İşlem/yıl | Kazanma | İşlem başı | Yıllık seçim / doğr. | Maks. düşüş | Kayma ×3: yıllık | Kötümser stop: 30 USDT → |
|---|---|---|---|---|---|---|---|
| #2 (tek pozisyon, 2x) | 343 | %81.2 | +%0.31 | +%587 / +%212 | %52 / %39 | +%236 / +%100 | 374 USDT |
| #2 + yalnız çöküş %50 | 343 | %81.2 | +%0.31 | +%571 / +%212 | **%46 / %34** | +%282 / +%127 | **699 USDT** |
| **Faz 5 seti: 2 pozisyon, 3x, ek ≥8, yalnız çöküş %50** | **449** | **%81.7** | **+%0.34** | **+%690 / +%233** | %71 / %42 | **+%322 / +%134** | **Mayıs 2021'de eridi** |
| Aynı set 2x | 449 | %81.7 | +%0.34 | +%350 / +%136 | %55 / %30 | +%194 / +%86 | 57 USDT |
| Aynı set 4x | 449 | %81.7 | +%0.34 | +%1.122 / +%346 | %82 / %54 | — | — |

Yıl yıl (her yıl 30 USDT ile): Faz 5 seti 2020 +%547, 2021 +%14.005, 2022 +%50, 2023 +%91, 2024 +%278, 2025 +%499,
2026 +%106 (#2: +%840, +%7.290, +%44, +%9, +%545, +%642, +%66). İkinci pozisyonda açılan 1.059 işlem ortalama +%0.42.
Bedeli: en büyük düşüş 12-13 Mart 2020'de %71 (iki pozisyon birlikte zarar kesti; #2'de %52) ve "kötümser stop
dolumu" stresinde (zarar kes, stopun ötesindeki fitilin yarısından dolarsa) 19-20 Mayıs 2021'de hesap eridi; #2
erimedi. "Yalnız çöküş %50" ise her senaryoda #2'ye eşit ya da daha iyi (daha az düşüş), ama işlem sayısını artırmaz.
Denenen ve elenenler: sermayeyi N'e bölüp aynı kaldıraçla çok pozisyon (5 pozisyon: yılda 688 işlem ama yıllık getiri
+%90 / +%40), dalga şartı olmadan aynı büyüklükte 2-3 pozisyon (düşüş %84-93), zarardan sonra beklemeyi kaldırmak
(yılda 444 işlem ama doğrulamada düşüş %67, 3 zararlı yıl), dalgada çöküş eşiğini düşürmek (işlem başı en iyi hâlde
#2 düzeyinde, getiri artmıyor), ilk pozisyon kârdayken ikinciyi açmak (düşüş azalıyor ama ek işlemlerin çoğu kayboluyor).

#### Faz 6: dalgada yeniden giriş, dalga eşikleri, küçük ek pozisyonlar (Kasım 2026)
Faz 5'in üzerine (Faz 5 ayarları ve ⭐ Faz 5 seti değişmedi) üç bulgu:

1. **Dalgada zarar sonrası yeniden giriş en kârlı işlem.** Piyasa geneli çöküşte zarar kesen coin genelde düşmeye devam
   edip daha derinden dönüyor; 240 dk zarar sonrası bekleme bu ikinci girişi kaçırıyordu. Beklemesiz koşuda (#2 + yalnız
   çöküş %50, sabit tutar) zarardan sonraki 240 dk içinde açılan işlemler:

   | Son 1 saatte %3+ düşen coin | Sıradan işlem | Zarar sonrası yeniden giriş |
   |---|---|---|
   | 0-1 (yalnız çöküş) | 613 işlem, +%0.04 | 632 işlem, +%0.02 |
   | 2-7 | 490 işlem, +%0.23 | 231 işlem, +%0.17 |
   | 8-11 | 259 işlem, +%0.23 | 96 işlem, +%0.61 |
   | 12+ | 515 işlem, +%0.69 | 207 işlem, +%1.10 |

   Bekleme yalnız çöküşte doğru, dalgada zarar ettiriyor. Kural: genişlik ≥ 3 iken bekleme atlanır (2-12 arası her eşik
   iyileştirdi); bekleme süresi bitene kadar coine yalnızca dalgada girilir (araya kârlı bir yeniden giriş girse de).
2. **Dalga eşikleri.** 12+ coin düşerken 1 saatte %5 veya 4 saatte %8 düşüş de sinyal. Faz 5'te tek başına getiriyi
   artırmamıştı; yeniden girişle birlikte işlem sayısını da getiriyi de artırıyor (erken girip stop olan coine dipte
   tekrar girilebiliyor).
3. **Küçük ek pozisyonlar.** Faz 5'te iki pozisyon eşit: işlemlerin çoğu tek pozisyon olduğundan dalga dışında da ilk
   pozisyon #2'nin %75'i kadardı. Faz 6'da ilk pozisyon #2 boyunda (3x, bakiyenin %63.3'ü teminat = bakiyenin 1.9 katı),
   dalgada açılan 3 ek pozisyonun her biri bunun 1/6'sı; hepsi açıkken toplam bakiyenin 2.85 katı (Faz 5 kadar).

| Ayar | Ne yapar |
|---|---|
| `FUTURES_REENTRY_BREADTH` | Genişlik bu değer veya üstündeyken zarar sonrası bekleme (`LOSS_COOLDOWN_MINUTES`) atlanır; `SYMBOL_COOLDOWN_MINUTES` ve kara liste (coin karnesi) yine geçerli. 0 = kapalı. |
| `FUTURES_WAVE_RULES` + `FUTURES_WAVE_BREADTH` | Genişlik ≥ `FUTURES_WAVE_BREADTH` (12) iken ek çöküş pencereleri ("saat:%", `1:5,4:8`); ana kuralın sinyal vermediği coinlerde denenir, sıralamada derinliğe göre ana kural sinyallerinin arkasında kalır. Boş = kapalı. |
| `FUTURES_EXTRA_SIZE_PCT` | Dalga modunda ek pozisyonların büyüklüğü, ilk pozisyonun yüzdesi (100 = eşit, Faz 5). Teminat ağırlıkları 1, x, x, ...: ilk pozisyon kullanılabilir bakiyenin 1 / (1 + (N-1)·x)'i. |

Telegram: 🧭 Giriş sinyali → 🌊 Dalga modu → **⭐ Faz 6 seti** (4 pozisyon, 3x, ek %16.7, yeniden giriş ≥3, dalga
eşikleri 1:5,4:8 ≥12, yalnız çöküş %50, zarar sonrası bekleme 240 dk) ya da **⭐ Faz 6 tek pozisyon** (aynı ekler tek
pozisyonla, 2x). ⭐ Faz 5 seti Faz 6 eklerini kapatır (Faz 5 tam olarak Faz 5 kalır); "⛔ Dalga modunu kapat" hepsini
kapatır. Backtest raporunda genişlik dökümüne "dalgada yeniden giriş" ve "dalga eşiği sinyali" satırları, karşılaştırmaya
Faz 6 setleri ve eklerin tek tek kapatılması eklendi.

Sonuç (liste ∪ köklü 20, 🥈 #2 girişi ve çıkışları, 30 USDT, 2020-2026, 1 dk; motorla doğrulandı):

| Ayar | İşlem/yıl | Kazanma | İşlem başı | Yıllık seçim / doğr. | Maks. düşüş | Kayma ×3: yıllık | Kötümser stop: 30 USDT → |
|---|---|---|---|---|---|---|---|
| #2 (tek pozisyon, 2x) | 343 | %81.2 | +%0.31 | +%587 / +%212 | %52 / %39 | +%236 / +%100 | 374 USDT |
| Faz 5 seti | 449 | %81.7 | +%0.34 | +%690 / +%233 | %71 / %42 | +%322 / +%134 | Mayıs 2021'de eridi |
| **Faz 6 tek pozisyon** | **388** | %81.1 | **+%0.36** | **+%1.103 / +%356** | %50 / %45 | **+%517 / +%206** | **4.628 USDT** |
| **Faz 6 seti** | **782** | %81.7 | **+%0.36** | **+%1.001 / +%353** | **%53 / %38** | **+%385 / +%224** | **647 USDT** |

Yıl yıl (her yıl 30 USDT ile): Faz 6 seti 2020 +%1.673, 2021 +%10.495, 2022 +%157, 2023 +%84, 2024 +%588, 2025 +%642,
2026 +%123 (#2: +%840, +%7.290, +%44, +%9, +%545, +%642, +%66): 7 yılın 7'sinde #2'den iyi ya da eşit. Faz 6 setinde ek
pozisyonlar yılda ~477 işlem, işlem başı +%0.38 (seçim +%0.41, doğrulama +%0.32); dalgada yeniden giriş yılda ~200
işlem, +%0.56; dalga eşiği sinyalleri yılda ~140 işlem, +%0.17. En kötü gün 12 Mart 2020: -%37 (#2: -%43). Komşu
ayarlar da (ek pozisyon kapısı 5-12, yeniden giriş eşiği 2-8, dalga eşiği genişliği 10-15 ya da kapalı, ek boyu %12-20,
3 ya da 5 pozisyon) iki dönemde de #2'den kârlı çıktı. Kötümser stop dolumu ve 3 kat kayma **birlikte** olursa her ayar
para kaybediyor: 30 USDT ile Faz 6 seti 29 USDT'de bitiyor (#2 ve Faz 5 eriyor), 300 USDT ile #2 44, Faz 5 17, Faz 6
seti 52 USDT. 40'tan fazla ayar denendi ve iki dönemin sonucuna da bakıldı; seçim yanlılığı olabilir, önce test modunda
(paper) izleyin.
Denenen ve elenenler: ek coinleri (köklü 40) yalnızca dalgada taramak (doğrulama getirisi +%212 → +%176..+%224, kötümser
stop 699 → 219..407 USDT), piyasa geneli pompada short (işlem başı ~%0; 3 kat kaymada getiriyi düşürdü), beklemeyi
yalnızca zarar kesten sonra uygulamak (işlem arttı ama kâr ve stres sonucu düştü), zarar kes %3 / %4 / %6, zaman stopu
30 / 60 dk, kâr al %6 (kâr düştü), 4x kaldıraçla büyük ek pozisyonlar (daha kârlı ama kötümser stopta 485 / 142 USDT).

#### Rapor 13 (Kasım 2026): 12 saat penceresi, kaldıraç, ek pozisyon kapısı
Bir kullanıcı raporu (Faz 6 seti + 🏆 Izgara #1 girişi `1:6,12:15` + 5x, Kâr Al piyasa emri, BNB komisyon indirimi,
33.3 USDT) motorla birebir yeniden üretildi (5.824 işlem, işlem başı +%0.320, tek hesap 8.756.340 USDT) ve ~200 ayar
seçim (2023-11 öncesi) / doğrulama (sonrası) dönemi, 3 kat kayma ve kötümser stop dolumuyla denendi.

1. **12 saat penceresi kârı düşürüyor.** Yalnız bu pencerenin bulduğu işlemler yılda ~100, kazanma %72, işlem başı
   -%0.04 (diğerleri +%0.37); 691 işlemin 483'ü tam boy ilk pozisyon olarak açılıp daha iyi sinyallerin yerini aldı.
   Kapalıyken 7 yılın 6'sında getiri daha yüksek (yalnız 2020'de açık olan önde). Aynı sonuç tek pozisyonda da var
   (🥈 #2 doğrulamada +%213, 🏆 #1 +%146). Komşu kurallar da (4 saat %8 / %9 / %11 / %12, 1 saat %5 / %5.5 / %6.5 /
   %7, ek pencereler 2:8, 8:12, 8:14, 12:12, 12:18, 24:20) `1:6` + 4 saat %10'dan kötü ya da aynı çıktı.
2. **Kaldıraç kâr / risk ayarı.** 12 saat penceresi kapalı Faz 6 setinde doğrulama dönemi yıllık getirisi 2x +%199,
   3x +%368, 4x +%593, 5x +%862, 6x +%1.153; en büyük düşüş (seçim / doğrulama) %37 / %27, %53 / %38, %66 / %48,
   %80 / %57, %87 / %64; kötümser stopta 33 USDT → 633, 781, 543, 134 USDT, 6x'te Mayıs 2021'de eridi.
3. **Ek pozisyon kapısı ≥10 coin** (`FUTURES_EXTRA_BREADTH=10`, 8 yerine): 3x / 4x / 5x'te iki dönemde de getiri biraz
   daha yüksek, en büyük düşüş ~9 puan daha az, yılda ~%4 daha az işlem; kötümser stopta 4x-5x'te daha kötü. Ek
   pozisyonları küçültmek (%7.5-12.5) doğrulamada getiriyi artırıp seçim döneminde düşürdü (ilk pozisyon büyüyor):
   sağlam değil.
4. **Kâr Al limit emir** (`FUTURES_TP_ORDER=limit`): işlem başı +%0.002, kötümser stopta biraz daha iyi.

| Ayar (33.3 USDT, BNB indirimi) | İşlem/yıl | İşlem başı | Yıllık seçim / doğr. | Son 12 ay | Maks. düşüş | Kayma ×3: doğr. | Kötümser stop: 33 USDT → |
|---|---|---|---|---|---|---|---|
| Rapordaki (5x, 1:6 + 12:15, TP piyasa) | 850 | +%0.320 | +%1.947 / +%417 | +%268 | %77 / %72 | +%192 | 32 USDT |
| 5x, 12 saat kapalı, TP limit | 781 | +%0.372 | +%1.925 / +%870 | +%376 | %80 / %57 | +%454 | 145 USDT |
| **+ ek kapısı ≥10** | 752 | +%0.382 | **+%1.953 / +%884** | **+%378** | %70 / %54 | +%464 | 108 USDT |
| 4x, 12 saat kapalı, TP limit | 781 | +%0.372 | +%1.672 / +%597 | +%272 | %66 / %48 | +%346 | 871 USDT |
| **+ ek kapısı ≥10** | 752 | +%0.382 | +%1.722 / +%606 | +%274 | **%57 / %45** | +%352 | 467 USDT |
| 3x (Faz 6 seti) + ek kapısı ≥10 | 752 | +%0.382 | +%1.169 / +%374 | +%182 | %44 / %36 | +%243 | 851 USDT |

Elenenler (aynı sette): kâr al %6-15, zarar kes %4-7, başabaş +%0.5-3 (0.5 yılda ~1.000 işlem ama doğrulamada aynı
getiri, kaymaya daha duyarlı), zaman stopu 30-180 dk (60 dk: işlem başı yüksek ama getiri düşük), izleyen stop, limit
giriş (yılda ~500 işlem, getiri üçte birin altına iner), 2 / 3 / 5 / 6 / 8 pozisyon (3 pozisyon doğrulamada daha kârlı
ama 4x-5x'te düşüş %80+), yalnız çöküş %10-100 ve eşik 1-4 coin, yeniden giriş eşiği 0 / 2 / 5 / 8, zarar sonrası bekleme 0-720 dk, coin
beklemesi 30-60 dk (kâr yarıya ya da daha aşağı iner), teminat %90, dalga eşiklerinin komşuları (≥8-20 coin; 1:4,4:7 doğrulamada
yüksek ama 5x kötümser stopta eridi). Yüzlerce ayar denendiği için seçim yanlılığı olabilir; önce test modunda izleyin.
Backtest raporu uzun pencere varsa "12 sa %15 penceresi kapalı" satırını, ayar notu da bu uyarıyı gösterir.

#### Faz 8: ek pozisyonlarda erken başabaş (Kasım 2026, rapor 14)
> ⚠️ **Düzeltme (rapor 15):** bu bölümdeki kâr artışı backtest motorunun iyimserliğinden geliyordu. Saniyelik veriyle
> düzeltilmiş testte Faz 8 işlem sayısını artırıyor ama kârı artırmıyor (aşağıdaki "Saniyelik veriyle gerçekçilik
> düzeltmesi" bölümüne bakın). Öneri: kapalı. Bölüm, ne denendiği görülsün diye olduğu gibi bırakıldı.

Soru yine "kârı azaltmadan daha çok işlem" idi; başlangıç ayarı Faz 6 seti + 5x, giriş `1:6` + 4 saat %10, ek kapısı
≥10 (rapor 14: 5.155 işlem, tek hesap 9.435.019 USDT; motorla birebir üretildi). ~150 ayar seçim / doğrulama dönemi,
3 ve 5 kat kayma, kötümser stop dolumu ve bileşiksiz (sabit boy) yıllık getiriyle denendi.

**Tıkanan yer yuvalar.** 33.399 sinyal mumundan 5.155'i işleme dönüşüyor; 14.803'ü "açık pozisyon vardı" diye
atlanıyor (dalgada 4 yuva hep dolu). Daha çok yuva açmak işe yaramadı: aynı boylarla 6 yuva (6x) yılda 945 işlem ama
yıllık getiri +%1.942 / +%875 → +%1.762 / +%612, en büyük düşüş %71 → %87 (aynı dalgadaki pozisyonlar birlikte zarar
kesiyor); ek boyunu küçülterek 5-8 yuva da kârı düşürdü.

**İşe yarayan: ek pozisyonlar yuvayı daha çabuk boşaltsın.** `FUTURES_EXTRA_BE_TRIGGER_PCT=0.5`: dalga modunda başka
pozisyon açıkken açılan (ek) pozisyonların başabaş kalkanı fiyat +%0.5'te devreye girer (stop yine girişin
+`BREAKEVEN_FLOOR_PCT` üstüne); ilk pozisyon ana ayarda (+%1) kalır. Ek pozisyon küçük kârını kilitleyip kapanınca
yuvası hemen yeni sinyale geçer: ek pozisyon sayısı 3.038 → 3.760 (zarar kesle bitenler %11 → %6), defter daha sık
boşaldığı için ilk (büyük) pozisyon sayısı da 2.117 → 2.257. İlk pozisyonda aynı ayar doğrulama döneminde kârı
düşürdü (o kapanınca yerine gelen sinyal küçük ek pozisyon olarak açılıyor), bu yüzden yalnızca eklere uygulanır.
Tetik 0.75 → 0.6 → 0.5 → 0.4 indikçe işlem de getiri de düzenli arttı (tek bir şanslı değer değil).

| Ayar (33.3 USDT, BNB indirimi) | İşlem/yıl | Yıllık seçim / doğr. | Maks. düşüş | Kayma ×3: yıllık | Kötümser stop: 33 USDT → | Son 12 ay |
|---|---|---|---|---|---|---|
| Rapor 14 (5x) | 752 | +%1.942 / +%875 | %71 / %54 | +%1.059 / +%454 | 105 USDT | +%353 |
| **+ Faz 8 (5x)** | **877** | **+%2.102 / +%1.152** | %66 / %59 | **+%1.358 / +%543** | **431 USDT** | +%307 |
| 4x | 752 | +%1.706 / +%600 | %57 / %45 | +%813 / +%346 | 427 USDT | +%257 |
| **4x + Faz 8** | **877** | **+%1.830 / +%758** | %53 / %51 | **+%1.032 / +%417** | **1.039 USDT** | +%231 |
| 3x | 752 | +%1.147 / +%371 | %44 / %36 | +%508 / +%239 | 790 USDT | +%173 |
| **3x + Faz 8** | **877** | **+%1.364 / +%462** | %42 / %40 | **+%642 / +%277** | **1.322 USDT** | +%160 |
| ⭐ Faz 6 seti (3x, kapı 8, 30 USDT, BNB yok) | 781 | +%1.001 / +%351 | %53 / %38 | +%385 / +%223 | 647 USDT | +%164 |
| **⭐ Faz 6 seti + Faz 8** | **915** | **+%1.098 / +%426** | %53 / %46 | **+%470 / +%262** | **757 USDT** | +%150 |

Rapor 14 ayarında tek hesap 9.435.019 → 11.183.255 USDT (6.014 işlem), en kötü gün (sabit boy) -%54 → -%37. Yıl yıl
(her yıl 33.3 USDT ile): 2020 +%7.881 → +%9.708, 2021 +%152.436 → +%258.238, 2022 +%205 → +%252, 2023 +%161 → +%131,
2024 +%1.786 → +%2.514, 2025 +%1.977 → +%3.708, 2026 +%244 → +%217.

**Zayıf yanları (dürüst tablo).** (1) Kazanç büyük dalga yıllarından geliyor; sakin yıllarda (2023, 2026; Faz 6 setinde
2022 de) %5-19 geride. Son 12 ayda bileşiksiz getiri aynı (+%188 / +%187, işlem 381 → 470) ama bileşik getiri +%353 →
+%307 ve en büyük düşüş %46 → %59: fark tek günden (5 Haziran 2026: defter daha sık boşaldığı için aynı düşen coine
bir kez daha büyük pozisyonla girilip zarar kesildi). (2) 5 kat kaymada doğrulama dönemi getirisi biraz geride (+%204 →
+%170), tek hesap yine önde. (3) Tetik ile stop arası dar (%0.2). Canlı bot stopu taşırken fiyat stopun gerisine
düşmüşse (Binance -2021) beklemeden piyasa emriyle çıkar; ek pozisyonların başabaş çıkışları ortalama %0.1 kötü dolsa
da kapalı hâlinden kârlı (+%523 / +%315'e karşı +%498 / +%306 bileşiksiz yıllık getiri), %0.2'de doğrulamada değil.

Telegram: 🧭 Giriş sinyali → 🌊 Dalga modu → 🔒 **ek başabaş: +%0.5** (ana ayar / 0.5 / 0.6 / 0.75; yalnızca dalga
modunda ve başabaş açıkken görünür). ⭐ Faz 5 / ⭐ Faz 6 setleri ve "dalga modunu kapat" Faz 8'i kapatır (setler
olduğu gibi kalır). Backtest raporunda "Ek pozisyonlarda başabaş +%0.5 (Faz 8)" satırı (açıksa kapalı hâli) çıkar.

Denenen ve elenenler: daha çok yuva (yukarıda); başabaşı her pozisyonda erkene almak (yılda ~1.000 işlem, seçim
döneminde çok iyi ama doğrulamada -%8, 3 kat kaymada -%39); kâra geçmiş pozisyonu 10-30 dk sonra kapatmak (+%1'i görmüş
pozisyonun 20. dakikadan sonra kalan beklentisi ~%0 ölçüldü ama tam simülasyonda doğrulama getirisi %9-37 düştü);
zarardaki pozisyonu erken kapatmak (zarardakinin kalan beklentisi 30. dakikada hâlâ +%0.2: kâr düştü); yuvalar doluyken
eski pozisyonu kapatıp yeni sinyale geçmek (işlem +%2-10, kâr aynı ya da düşük); dalga eşiği genişliği ≥10 (işlem +%6,
kâr aynı ama 2022 zayıf; Faz 8 ile birlikte 5 kat kaymada tek hesap yarıya iniyor); dalga eşikleri 1:4,4:7 /
1:4.5,4:7.5 (işlem +%6-10, doğrulamada iyi, seçimde kötü; 1:4,4:7 5x kötümser stopta eridi); 2 saat %8 penceresi (7
yılın 4'ünde geride); aynı coinde üst üste 2-3 zarardan sonra dalgada yeniden girişi kesmek (5 Haziran 2026'yı
düzeltiyor ama 2025'te kârı %20-30 düşürüyor: tek güne göre ayar; 1 zararda kesmek kârı yarıya indiriyor). ~150 ayar denendi; seçim yanlılığı olabilir, önce test modunda izleyin.

#### Saniyelik veriyle gerçekçilik düzeltmesi (Kasım 2026, rapor 15)
Soru yine "kârı azaltmadan daha çok işlem" idi (başlangıç: rapor 15, Faz 8 açık, 5x; 6.017 işlem, tek hesap 10.647.290
USDT, motorla birebir üretildi). Bu kez adaylar yalnız 1 dk mumlarla değil, Binance'in **saniyelik işlem verisiyle**
(aggTrades) de sınandı ve asıl bulgu backtestin kendisiyle ilgili çıktı.

**Ne yapıldı.** Örnek 920 coin-gün (2020-2026 her yıldan; 2.563 işlem) için işlem verisi indirildi ve canlı botun çıkış
mantığı birebir oynatıldı: 2 saniyede bir son fiyat yoklaması, başabaş tetiklenince stopun tabana taşınması (fiyat tabanı
geçmişse piyasa emriyle çıkış), borsada duran zarar kes, zaman stopu. Ayrıca 380 coin-günlük ikinci bir örnekte (1.043
işlem) aynı şey tick düzeyinde yapıldı.

**Bulgu 1: mum içi başabaş.** 1 dk mumun içindeki sıra bilinmez. Eski motor yükselen mumda "dip → tepe → kapanış"
varsayıyordu: başabaş o mumda tetiklenir ve kapanış tabanın üstündeyse işlem sürüyor sayılıyordu. Gerçekte fiyat tetiğe
değdikten sonra aynı dakikada çoğu zaman tabana geri dönüyor ve stop çalışıyor; tetik ile taban ne kadar yakınsa o kadar
sık. İşlem başına ortalama brüt hareket (%; aynı işlemler, aynı giriş):

| Başabaş (tetik → taban) | Eski motor | Canlı (saniyelik) | Düzeltilmiş motor |
|---|---|---|---|
| Ek pozisyonlar, +%1 → +%0.3 | +0.49 | +0.39 | +0.43 |
| Ek pozisyonlar, +%0.5 → +%0.3 (Faz 8) | +0.47 | +0.30 | +0.32 |
| Ek pozisyonlar, +%0.4 → +%0.3 | +0.46 | +0.25 | +0.23 |
| İlk pozisyonlar, +%1 → +%0.3 | +0.44 | +0.42 | +0.39 |
| İlk pozisyonlar, +%0.5 → +%0.3 | +0.46 | +0.33 | +0.28 |

Eski motorda işlem başı kazanç başabaş tetiğinden neredeyse bağımsız görünüyordu (+0.46 ile +0.49 arası); bu yüzden dar
tetik "bedava" daha çok işlem gibi çıkıyordu. Gerçekte tetik daraldıkça işlem başı kazanç düşüyor.

**Düzeltme.** `futures_backtest.be_retouch_prob`: başabaşın tetiklendiği mumda mumun dibi tabanın altında, kapanışı
üstündeyse fiyatın tetikten sonra tabana dönmüş olma olasılığı `1 / (1 + 45 · x)`, `x = (tetik − taban) · (kapanış −
taban) / mum_aralığı²`. Çekiliş işlem başına sabittir (coin + giriş zamanı), sonuçlar çalıştırmadan çalıştırmaya
değişmez. Katsayı örneğin yarısıyla ayarlandı, diğer yarısında ve tick düzeyindeki ikinci örnekte doğrulandı: 13 başabaş
ayarı × 2 zarar kes × ilk / ek pozisyonlarda fark çoğunda ±%0.04, en kötüsünde %0.06 (eski motorda +%0.22'ye kadar).

**Bulgu 2: giriş gecikmesi.** Backtest sinyal mumundan sonraki mumun açılışından girer; canlı bot mum kapandıktan ~5 sn
sonra emir verebilir. Çöküş mumunun kapanışından sonra fiyat ortalama 3 sn'de %0.04, 5 sn'de %0.05, 10 sn'de %0.08
yükseliyor (2.563 işlem, ± %0.01). Piyasa emriyle girişlere `ENTRY_LAG_PCT = 0.05` eklendi (limit girişte yok).

**Sonuç: rakamlar küçüldü.** Kullanıcının ayarı (5x, Faz 6 ekleri, 28.93 USDT, BNB ile komisyon), 2020-2026:

| | Eski motor (rapor 15) | Düzeltilmiş, Faz 8 açık | Düzeltilmiş, Faz 8 kapalı |
|---|---|---|---|
| İşlem / yıl | 877 | 888 | 753 |
| İşlem başı net | +%0.36 | +%0.17 | +%0.24 |
| Yıllık getiri (seçim / doğrulama) | +%2.124 / +%1.033 | +%463 / +%313 | +%599 / +%293 |
| En büyük düşüş | %66 / %59 | %87 / %74 | %84 / %63 |
| Tek hesap (2020 →) | 10.6 milyon | 1.23 milyon | 1.87 milyon |
| Kayma 3 kat | +%1.393 / +%483 | +%120 / +%68 | +%170 / +%134 |
| Kayma 5 kat: 28.93 USDT → | 310.032 | 11 (zarar) | 42 |
| Kötümser stop dolumu | 261 USDT | eridi (Mayıs 2021) | eridi (Mayıs 2021) |
| Mayıs 2021 haftası (17-25) | −%21 | −%77 | −%69 |

Her yıl yeniden başlansa (Faz 8 açık): +%332, +%6.406, +%86, +%46, +%525, +%963, +%62 (7 yılın 7'si kârlı); yıl içi en
büyük düşüşler %87, %86, %54, %49, %74, %52, %52. Kaldıraca göre (Faz 8 kapalı): 3x +%347 / +%174, düşüş %62 / %42; 2x
+%205 / +%108, düşüş %45 / %29. Hazır setler (30 USDT, TP limit): Faz 6 seti 3x +%270 / +%166, düşüş %66 / %45 (eski:
+%1.001 / +%353, %53 / %38); Faz 6 tek pozisyon 2x +%379 / +%159, %59 / %45; #2 tek pozisyon 2x +%193 / +%90, %72 / %60.

**Faz 8 için düzeltme.** Faz 8 (ek pozisyonlarda başabaş +%0.5) işlem sayısını %18 artırıyor ama kârı artırmıyor: sabit
boyda yıllık getiri aynı (+%332 / +%217 ve +%333 / +%212), bileşikte seçim döneminde düşük, doğrulamada biraz yüksek;
düşüş doğrulamada %63 → %74, maliyet stresinde belirgin kötü. Öneri: kapalı (`FUTURES_EXTRA_BE_TRIGGER_PCT=0`).

**"Daha çok işlem" adayları (hepsi elendi).** ~200 ayar denendi; eski motorla umut verenler de ayakta kalmadı:
- Ek pozisyonlarda zarar kes −%3 (eski motorda işlem +%5, kâr iki dönemde de yüksekti): düzeltilmiş motorda seçim
  döneminde düşük (+%424), düşüş %93. Eski motordaki kazancı da yuvalar çabuk boşalınca büyük ilk pozisyonun çöküş
  sürerken daha sık açılmasından geliyordu (orada bile Mayıs 2021 haftası −%21 yerine −%51).
- Büyük yuvanın yeniden dolması (ilk pozisyon kapanınca, ekler açıkken gelen sinyal yine büyük açılır) ve bununla 5-6
  yuva (işlem +%13-33): eski motorda bile kâr yalnız seçim döneminde yüksek, en kötü gün −%37'den −%55/−%60'a, düşüş
  %64-72'ye çıkıyor.
- Ek başabaş +%0.4, taban +%0.4, daha çok yuva (5-8), ek kapısı ≥8, dalga eşiği genişliği ≥10, 2 saat %8 penceresi, 4
  saat eşiği %8-9, 1 saat eşiği %5-5.5, zarar sonrası beklemeyi kısaltmak / kaldırmak, eklere kısa zaman stopu ya da
  düşük kâr al, eklere izleyen stop, son 15-120 dakikada zarar kes olduysa ilk pozisyonu küçültmek: ya bir dönemde kârı
  düşürüyor ya da maliyet stresine dayanmıyor.
- Genel desen: bu stratejide işlem sayısı dalgadaki yuva sayısıyla sınırlı; eklenen her işlem ortalamanın altında
  kazandırıyor ve en kötü günleri büyütüyor.

**Raporda.** Başlıkta "Gerçekçilik payları" satırı; 🧪 "Tek kural değişince" tablosunun sonunda "Eski (iyimser) hesap"
satırı (payları kapatır; Kasım 2026 öncesi raporlarla karşılaştırmak için). Ayar notları ve Telegram açıklamaları
düzeltilmiş rakamlarla güncellendi; README'nin Faz 3-8 bölümlerindeki tablolar eski motorla üretilmiştir ve iyimserdir
(sıralamalar çoğunlukla aynı, düzeyler 2-4 kat yüksek). Ayrıca düzeltildi: Faz 8 açıkken "Başabaş +%5 → +%2" satırı ek
tetiği (+%0.5) tabanın (+%2) altında bıraktığı için hayali kâr gösteriyordu; ek tetik tabandan büyük değilse artık ana
tetik kullanılır.

**Sınırlar.** Düzeltme ortalamaları tutturur, tek tek işlemleri değil; örnekte ek pozisyonlarda ~%0.02-0.04 iyimser,
ilk pozisyonlarda ~%0.03-0.06 kötümser kaldı. Giriş gecikmesi 5 sn varsayar; sunucu yavaşsa pay daha büyüktür. Emir
defteri derinliği (büyük bakiyede kayma) hâlâ modellenmiyor. Geçmiş sonuç gelecek garantisi değildir.

#### Canlı giriş gecikmesi: ölçüm ve hızlı giriş (Kasım 2026)
Backtest girişi mum kapanışından sayar; canlı bot mumu kapandıktan sonra tarar ve emri birkaç saniye sonra verir. Çöküş
mumunun kapanışından sonra fiyat ortalama şu kadar yükseliyor (rapor 15'in saniyelik verisi, 2.563 işlem, ± %0.01):

| Mum kapanışından sonra | 1 sn | 2 sn | 3 sn | 5 sn | 8 sn | 10 sn | 15 sn |
|---|---|---|---|---|---|---|---|
| Fiyat (ortalama) | +%0.015 | +%0.023 | +%0.039 | +%0.050 | +%0.074 | +%0.081 | +%0.101 |

Backtest 5 sn (`ENTRY_LAG_PCT = 0.05`) varsayar. İşlem başı net kazanç +%0.17-0.24 olduğundan her saniye önemlidir.

**Gecikme nereden geliyor (eski yol).** Koddan ve CCXT'nin ağ olmadan ölçülen beklemelerinden; ağın payı sunucunun
Binance'e uzaklığına bağlıdır (her istek bir gidiş-dönüş) ve yalnızca canlıda ölçülür:

| Adım | İstek | Not |
|---|---|---|
| Döngünün uyanması | - | Tarama anı uykunun ortasına denk gelirse 1 sn'ye, açık pozisyon varken 2 sn'ye kadar geç |
| Bekleme (`SCAN_DELAY_SEC`) | - | 3 sn |
| Açık pozisyonların kontrolü | pozisyon başına 2 | Yalnızca pozisyon açıkken (ek pozisyonlar), taramadan önce |
| 24 saatlik ön eleme | 1 | CCXT bu ağır istekten önce, son 2 sn'de başka istek atıldıysa 2 sn'ye kadar bekler (ölçüldü: 2.0 sn) |
| Mumlar (24 coin) | 24 | 6 işçi = 4 dalga; CCXT dalga başına 100 ms bekletir (ölçüldü: 0.4 sn) |
| Teminat modu + kaldıraç | 2 | Her girişte, sıralı |
| Bakiye, fiyat, emir | 3 | CCXT fiyat isteğinden önce 100, emirden önce 200 ms'ye kadar bekletir |

Yani ilk pozisyonda 3 sn + yaklaşık 10 sıralı gidiş-dönüş; pozisyon açıkken (dalgadaki ek pozisyonlar) üstüne 2 sn'ye
varan uyku, pozisyon kontrolü ve 2 sn'lik CCXT beklemesi biner.

**Ölçüm (her zaman açık, davranışı değiştirmez).** Her taramada aşamaların mum kapanışından kaç ms sonra bittiği ve
açılan her pozisyonda emrin gönderildiği an, yanıtın geldiği an, Binance'in emri işlediği an (yanıttaki `updateTime`:
borsa saati, sunucunun saatinden bağımsız) ve dolum fiyatının sinyal mumunun kapanışına göre farkı kaydedilir:
- `data/futures_scans_<mod>.jsonl`: `"ms": [tarama başladı, mumlar alındı, sinyaller hazır]`, `"f": {"SOL": {"o": emir
  gönderildi, "r": yanıt geldi, "x": Binance'in işlem zamanı, "p": kapanışa göre dolum farkı %}}`, `"kt"` (mumlar kaç
  denemede alındı), `"co"` (Binance saati − sunucu saati, ms), `"fast": 1` (hızlı giriş açıktı).
- Telegram: pozisyon açılış mesajında "⚡ Giriş gecikmesi: mum kapanışından 2.1 sn sonra doldu; kapanış fiyatından
  %0.031 pahalıya alındı"; `/tarama` ve 🎯 Çıkışlar ve Emirler sayfasında son taramanın zamanlaması.
- Backtest raporu, 🔍 CANLI BOT KARŞILAŞTIRMASI: "Tarama zamanlaması (… ortanca): başlangıç → mumlar alındı → sinyaller
  hazır" ve "Giriş gecikmesi (mum kapanışı → dolum): ortalama / ortanca / en uzun | dolum, kapanış fiyatına göre ortalama
  +%… (backtest varsayımı +%0.07: gecikme %0.05 + kayma)". Hızlı giriş açık ve kapalı dönemler ayrı satırlarda.

**Hızlı giriş (`FUTURES_FAST_ENTRY=true`; varsayılan kapalı; Telegram → 🎯 Çıkışlar ve Emirler → ⚡ Hızlı giriş).**
Sinyal kuralları, pozisyon boyları ve çıkışlar aynıdır; yalnızca emir daha erken gider:
- **Tarama mum kapanışından 1 sn sonra başlar** (3 yerine) ve döngü tam o anda uyanır. Güvence: yanıtta az önce kapanan
  mumun ardından **yeni (süren) mum da görünmelidir**; bu, Binance'in o mumu kapattığını gösterir. Yeni mumu görünmeyen
  **her** coin 0.25 sn arayla yeniden istenir (yalnızca o coinler); en geç eski tarama anında (kapanış + 3 sn) eski
  davranışa dönülür: az önce kapanan mum yanıttaysa yeterlidir. En kötü durum eski zamanlamadır.
- **Mumlar tek dalgada**: 24 işçi, CCXT beklemesi olmadan (bağlantı havuzu 32'ye büyütüldü). İstek ağırlığı tarama başına
  ~24 coin × 2 = 48; Binance'in sınırı dakikada 2.400. Küçük evrende (≤ 40 coin) 24 saatlik ön eleme yapılmaz: birkaç mum
  isteği kazandırırken taramanın başına 2 sn'ye varan bekleme ekliyordu; yalnızca sinyal veremeyecek coinleri atladığı
  için sinyaller ve genişlik değişmez.
- **Ön hazırlık (taramadan 2.5 sn önce, mum kapanmadan)**: paralel hafif isteklerle bağlantılar açılır (mum istekleri TLS
  el sıkışması beklemez; aynı yanıtlardan Binance ile saat farkı ölçülür, 750 ms'yi aşarsa günlüğe uyarı yazılır) ve
  Binance'ten **parite ayarları okunur** (tek istek): teminat modu ISOLATED ve kaldıracı ayardakiyle aynı olan coinler
  "hazır" sayılır.
- **Teminat modu + kaldıraç önceden**: taramalar arasında, hazır olmayan coinler azar azar ayarlanır (adım başına 2
  coin, taramaya 20 sn kala durur, açık pozisyonlu coine dokunulmaz). Sinyal anında coin az önce doğrulanmışsa bu iki
  istek atlanır; doğrulanmamışsa (elle değiştirilmiş, okunamamış, doğrulama 30 sn'den eski, kaldıraç ayarı değişmiş)
  eski yol çalışır: girişten hemen önce ayarlanır. Bot yalnızca Binance'ten okuduğuna güvenir, kendi ayarladığını da
  oradan teyit eder.
- **Girişte CCXT beklemesi yok**: bakiye → fiyat → emir (3 istek) arka arkaya gider.

Kalan yol: mumların kesinleşmesi (aşağıdaki ölçüme göre kapanıştan ~1-2.5 sn sonra) + bakiye, fiyat, emir (3
gidiş-dönüş). Açık pozisyon varken pozisyon kontrolü yine taramadan önce yapılır (sıra değişmedi: kill-switch ve güncel
pozisyon durumu önce gelir); ne kadar sürdüğü kayıttaki `"ms"` alanının ilk değerinden görülür.

**Mum sınırı ölçümü (11 Kasım 2026).** Binance'in spot herkese açık veri ucunda (`data-api.binance.vision`; vadeli API
geliştirme ortamından erişilemiyor) 22 coin, 3 mum sınırı, 632 yanıt:

| | Ortanca | En geç (3 sınır) | Kapanıştan 1 sn sonra |
|---|---|---|---|
| Kapanan mum son hâlini aldı | +0.3 sn | +1.9 / +2.5 / +2.3 sn | 22 coinin 18-20'sinde |
| Yanıtta yeni mum göründü | +1.0 - +1.5 sn | +2.0 / +2.5 / +2.4 sn | 22 coinin 5-12'sinde |

Yeni mumun göründüğü hiçbir yanıtta kapanan mum eksik değildi (0 / 632): kural tutuyor. Ama kapanan mum ETH, SOL, BNB
gibi likit coinlerde bile 2-2.5 sn'ye kadar değişebiliyor; bu yüzden "coinlerin bir kısmında yeni mum göründüyse
gecikenleri kabul et" kısayolu (ilk sürümde vardı) kesinleşmemiş mumu taramaya sokabiliyordu ve kaldırıldı. Sonuç: hızlı
girişte tarama 1 sn'de başlar ama mumlar pratikte ~2-2.5 sn'de tamamlanır (eski yol: 3 sn + ön eleme + 4 dalga). Vadeli
uç farklı davranabilir; sunucudaki kayıtta `"ms"` (mumlar ne zaman alındı) ve `"kt"` (kaç deneme) bunu gösterir.

**Kullanım.** Önce kapalıyken birkaç gün çalıştırıp raporda / Telegram'da gecikmeyi görün, sonra açıp aynı satırlarla
karşılaştırın. Açıkken dikkat: taranan coinlerin **hepsinde** teminat modu ISOLATED ve kaldıraç `FUTURES_LEVERAGE` olarak
önceden ayarlanır (kapalıyken yalnızca girilen coinde ayarlanırdı); aynı hesapta bu coinlerde elle farklı kaldıraçla işlem
yapıyorsanız ayarınız değişir. Test modunda hesap ayarı yoktur; tarama zamanlaması ve mumlar aynı şekilde hızlanır.

**Sınırlar.** Hızlı yol sahte borsa ve sahte saatle test edildi (istek sırası, yeni mum kontrolü, doğrulama, geri dönüş
yolları; `tests/test_futures_fast_entry.py`); gerçek Binance'te yalnızca spot veri ucunun mum davranışı ölçüldü
(yukarıda). Vadeli API'ye karşı (parite ayarları ucu, emir yanıtındaki işlem zamanı, kazanılan süre) ölçüm sunucunuzda
yapılır. Backtestteki
`ENTRY_LAG_PCT` (5 sn = %0.05) değiştirilmedi: canlı ölçümler birikince yeniden ayarlanabilir; o zamana kadar backtest
hızlı girişe göre biraz kötümser kalır. Emir defteri derinliği (büyük bakiyede kayma) modellenmiyor.

**İlk canlı ölçüm (rapor 18, 9-12 Kasım 2026, gerçek vadeli hesap).** Hızlı giriş kapalıyken sinyaller mum kapanışından
ortanca 6.1 sn sonra hazırdı (backtestin varsaydığı 5 sn'den de geç); açıkken 103 taramada ortanca 3.8 sn (tarama 1.0
sn'de başladı, mumlar 3.7 sn'de alındı; sunucu saati ile Binance arası fark ortanca 185 ms). 103 taramanın 61'inde en az
bir coinin yeni mumu ilk yanıtta görünmediği için mumlar yeniden istendi ve tarama eski tarama anına (3 sn) kadar bekledi.
Bu taramaların neredeyse tamamı sakin piyasadaydı (aralıkta yalnızca 3 sinyal var): sakin piyasada az işlem gören coinde
yeni mumun ilk işlemi saniyeler sonra gelir; dalgada her coin saniyede birçok kez işlem gördüğü için bekleme kısalır.
Asıl ölçü pozisyon açıldığında yazılan "Giriş gecikmesi" satırıdır; bu aralıkta canlı bot pozisyon açmadığı için henüz
yok. (Aralıktaki sinyallerin ikisi 10 Kasım 15:40-15:45 UTC'de, hesap o an Çoklu Varlık modunda olduğu için `-4168` ile
reddedildi; bot aynı gün 16:02 UTC'deki güncellemeyle bu modu kendisi Tek Varlık'a alır oldu ve hesap o zamandan beri Tek
Varlık modunda. Sonrasında yeni sinyal gelmedi.)

**Sakin coin kuralı (12 Kasım 2026; bu ölçümün sonucu).** Yukarıdaki beklemeyi kaldırır. Yeni mumu henüz görünmeyen bir
coinin az önce kapanan mumu kesinleşmemiş olabilir; ama o coinin düşüşü hiçbir çöküş / dalga eşiğine 1 puandan, 1
saatlik düşüşü de genişlik eşiğine (%3) 0.5 puandan yakın değilse kapanışı biraz oynasa bile taramanın sonucu (sinyaller
ve genişlik) değişemez. Böyle coinler artık beklenmez; eşiğe yakın ya da eşiğin üstündeki coinler (sinyal adayları dahil)
eskisi gibi kendi yeni mumunu bekler. Tarama kaydına beklenmeyen coin sayısı yazılır (`kq`), rapordaki zamanlama satırı
"N taramada sakin coinlerin yeni mumu beklenmedi" der. Sinyal yalnızca kapanışlara bağlı değilse (ek modül, likidasyon
şartı, oynaklık eşiği, onay modu) kural uygulanmaz. Beklenen etki: sinyalli taramalarda mumların alınması 3.7 sn yerine
aday coinin yeni mumunun göründüğü ana (spot ölçümünde ortanca 1-1.5 sn) iner; kazanç canlıda "Giriş gecikmesi"
satırından ölçülecek. Tek sınırı: sınırın son bir saniyesinde eşiğe 1 puandan uzak bir coin o bir saniyede eşiği aşarsa
canlı bot o (en zayıf, eşiği kıl payı geçen) sinyali kaçırabilir; tersine yanlış sinyal üretemez.

#### Rapor 17 araştırması: kârı ve işlem sayısını birlikte artıran ayar var mı? (Kasım 2026)
Baz: rapor 17'nin ayarı (5x, 4 pozisyon, Faz 6 ekleri, ek kapısı ≥10, Faz 8 açık, 28.93 USDT), düzeltilmiş motor,
2020-01 → 2026-11, 1 dk çıkışlar; motorla birebir üretildi (1.234.021,97 USDT, 6.071 işlem). ~330 ayar denendi.

**Önce bir uyarı: tek raporun son bakiyesi şansa çok bağlı.** Düzeltilmiş motor, başabaşın tetiklendiği dakikada fiyatın
tabana dönüp dönmediğini bir olasılıkla çeker. 5x ve bileşik hesapta birkaç işlemin çekilişi sonucu katlarca oynatıyor:
aynı ayar 10 farklı çekilişle 275.539 ile 2.309.818 USDT arasında bitti (ortanca 1.352.814; rapordaki 1.234.022 bunlardan
biri). Bu yüzden adaylar tek raporla değil **10-12 çekilişin ortancasıyla** ve "kaç çekilişte bazdan iyi" sayısıyla
karşılaştırıldı. Rapora da eklendi: 💰 A) bölümündeki **"Çekiliş duyarlılığı"** satırı aynı ayarın 10 çekilişteki en
düşük / ortanca / en yüksek son bakiyesini yazar; iki rapor arasındaki fark bu aralığın içindeyse şans olabilir.

**Komşu değerlerde aynı yönü veren dört etki** (hepsi mevcut ayarlar):
- **İlk pozisyonun başabaş tetiği** (`BREAKEVEN_TRIGGER_PCT`; ekler `FUTURES_EXTRA_BE_TRIGGER_PCT` ile ayrı): yükseldikçe
  kâr artar, işlem azalır. 1.1-1.5 arasında sabit boy getiri seçim döneminde +%5-21, doğrulamada 0 ile +%5; işlem −%1-6.
  0.8'in altı doğrulamada çöker (0.75: −%17). Zayıf yanı: sakin yıllarda (2023, 2026) geride kalır.
- **Ek pozisyon tetiği 0.5 → 0.4**: işlem +%5, kâr aynı. 0.35 çöker (−%12).
- **Yalnız çöküş büyüklüğü 50 → 25** (`FUTURES_ISOLATED_SIZE_PCT`): işlem aynı; bu işlemlerin kazancı sıfıra yakın olduğu için
  küçülmeleri oynaklığı azaltır. Seçim döneminde bileşik getiri +%19, doğrulamada aynı, son 24 ayda biraz geride.
- **Dalga eşiklerinin genişliği 12 → 10** (`FUTURES_WAVE_BREADTH`): işlem +%7, tek başına kârı değiştirmez; ≥9 ve altı
  doğrulamada zayıflar.

**Adaylar** (çekilişlerin ortancası; seçim / doğrulama dönemi; "iyi" = bazdan iyi çıkan çekiliş sayısı):

| | Baz (rapor 17) | Faz 8 kapalı | A: 1.1 / 0.4 / %25 | B: 1.25 / 0.4 / %25 | C: B + dalga ≥10 |
|---|---|---|---|---|---|
| İşlem / yıl | 888 | 753 | 924 | 906 | 976 |
| Sabit boy yıllık getiri | +%332 / +%212 | +%346 / +%205 | +%352 / +%215 | +%382 / +%224 | +%375 / +%230 |
| ...bazdan iyi (10 çekilişte) | - | 8 / 3 | 10 / 9 | 10 / 10 | 10 / 10 |
| Bileşik yıllık getiri | +%493 / +%301 | +%691 / +%273 | +%655 / +%334 | +%853 / +%358 | +%819 / +%392 |
| ...bazdan iyi (10 çekilişte) | - | 10 / 3 | 10 / 10 | 10 / 10 | 10 / 10 |
| En büyük düşüş | %85 / %67 | %85 / %58 | %87 / %63 | %88 / %65 | %87 / %62 |
| Kayma 3 kat (bileşik) | +%123 / +%75 | +%175 / +%129 | +%132 / +%106 | +%224 / +%129 | +%176 / +%140 |
| Kayma 5 kat (bileşik) | −%0 / −%28 | +%33 / +%10 | +%6 / −%10 | +%28 / −%7 | −%9 / −%3 |
| Kötümser stop (5x) | eridi | eridi | eridi | eridi | eridi |
| Son 12 ay | +%81 | +%95 | +%85 | +%36 | +%35 |
| Son 24 ay | +%4.110 | +%2.590 | +%4.406 | +%5.526 | +%9.143 |
| Geride kaldığı yıllar | - | 2021, 2025 | 2022, 2025 | 2023, 2026 | 2022, 2023, 2026 |
| Tek rapor (kendi çekilişi) | 1.234.022 | 1.871.004 | 2.955.907 | 3.438.960 | 3.367.292 |
| ...10 çekilişte | 0.28-2.31 M | 0.55-2.86 M | 1.30-2.76 M | 2.43-3.70 M | 1.88-3.55 M |

Sütun başlıkları: ilk pozisyonun başabaş tetiği / ek pozisyon tetiği / yalnız çöküş büyüklüğü. Ayar satırları:
- A: `BREAKEVEN_TRIGGER_PCT=1.1`, `FUTURES_EXTRA_BE_TRIGGER_PCT=0.4`, `FUTURES_ISOLATED_SIZE_PCT=25`
- B: aynısı, `BREAKEVEN_TRIGGER_PCT=1.25`
- C: B + `FUTURES_WAVE_BREADTH=10`

**Yorum.** Üç aday da bazdan hem daha çok işlem yapıyor hem de iki dönemde, çekilişlerin neredeyse hepsinde daha çok
kazanıyor; 3 kat kaymada da önde. Ama kazanç küçük ve kesin değil: haftalık bloklarla önyüklemede (bootstrap) farkın
%5-95 aralığı sıfırı içeriyor (B için seçim −14..+111, doğrulama −18..+63 puan / yıl), **hiçbiri her yıl bazdan iyi
değil** ve B ile C son 12 ayda tüm çekilişlerde geride (+%35 / +%81). Yani "her testi geçen" bir ayar yine yok; A küçük ama
son dönemde de geride kalmayan adım, C uzun dönemde en çok işlem ve kârı veren ama sakin piyasada geride kalan seçenek.
Faz 8'i kapatmak (önceki öneri) seçim döneminde ve maliyet stresinde iyi, doğrulamada çekilişlerin çoğunda bazın gerisinde
ve işlem sayısını %15 düşürüyor. Düşüş ve en kötü gün hiçbir adayda azalmıyor: 5x'te kötümser stop dolumu hepsini
eritiyor; B 3x'te erimiyor (bileşik +%455 / +%198, düşüş %69 / %42). **İkinci tur (aşağıda, Faz 9) bu adayların yerini
aldı:** A'nın üstüne iki yeni kural ekleyen set hepsinden iyi çıktı.

**Denenip elenenler** (baza göre işlem / sabit boy getiri, seçim / doğrulama):
- Daha çok yuva (5, 6, 8; eklerin toplamı sabit): işlem +%16 / +%29 / +%53, getiri −%14 ile −%31. Dalgada sonradan gelen
  sinyaller belirgin kötü.
- Ek kapısı ≥8: işlem +%6 / +%1, getiri −%9 / 0. Zaman stopu 30 dk: +%4 / +%5, getiri +%2 / −%30. Eklere 15-20 dk zaman
  stopu: +%6-12, getiri −%1 ile −%17.
- Zarar kes 6-7 (Kâr Al 8-12 ile): seçimde +%12-21, doğrulamada −%17-25. Eklerde zarar kes 3: düşüş %93.
- Başabaş tabanı 0.4: getiri −%2 / −%21. Dalgada yeniden giriş ≥1-2: işlem +%1-8 ama doğrulamada bileşik getiri +%313 →
  +%274-281. Zarar beklemesi 60-120 dk: işlem +%2-6, doğrulama getirisi −%9-12. Her işlemden sonra bekleme: çöküyor.
- Düşük genişlikte hiç girmemek: işlem −%3-16, doğrulama getirisi −%13-22 (küçültmek yetiyor, atlamak zararlı).
- Ana eşik %9: işlem +%5, getiri −%6-12; %11 ve 1 sa %6.5-7: ikisi de düşüyor. 1 sa %5.5: işlem +%7 / +%4, getiri +%5 /
  −%3 (B ile birlikte en kötü gün −%80). 8 sa %13: doğrulamada −%21-24.
- B üstüne daha düşük dalga eşikleri (1:4.5,4:7.5): işlem +%10, getiri aynı ama 2023, 2025 ve 2026'da geride; dalga ≥10
  ile birlikte bileşik getiri +%598 / +%241'e düşüyor.
- Güçlü dalgada pozisyonu büyütmek: getiri +%14-18 ama düşüş %74 → %78-79 (örtülü kaldıraç).

**Hızlı giriş burada da en büyük kaldıraç.** Aynı bazda giriş gecikmesi payı %0.05 (5 sn) yerine %0.039 (3 sn) olursa
sabit boy getiri +%6 / +%9, %0.023 (2 sn) olursa +%12 / +%16 artıyor, işlem sayısı değişmiyor. Bu, canlıda ölçülen
gecikme düşerse geçerlidir (bkz. "Canlı giriş gecikmesi").

**Sınırlar.** Seçim yanlılığı: ~330 denemenin en iyileri raporlandı; adaylar tek tek gerekçesi olan etkilerin birleşimi
olsa da gerçek kazanç buradakinden küçük olabilir. Ek tetiği 0.4 ile taban 0.3 arası %0.1'dir: stop konana kadar fiyat
geri dönerse bot piyasa emriyle çıkar; motor bu ayarda saniyelik veriye göre hafif kötümser kalibre edildi (+%0.23 /
+%0.25) ama canlıda dolum daha kötü olabilir. Geçmiş sonuç gelecek garantisi değildir.

#### Faz 9: pozisyona göre zaman stopu (Kasım 2026, rapor 17 araştırmasının ikinci turu)
İkinci turda ~225 ayar daha denendi (toplam ~550), bu kez baştan 6-10 çekilişle ve son 12 ay ayrıca izlenerek. Mevcut
ayarlarla yeni bir kazanç çıkmadı; komşu değerlerde tutarlı çıkan iki **yeni kural** eklendi (ikisi de varsayılan kapalı;
Telegram → 🧭 Giriş sinyali → 🌊 Dalga modu):

- **Dalgada açılan ilk pozisyon daha uzun tutulur** (`FUTURES_WAVE_HOLD_MINUTES=60`, `FUTURES_WAVE_HOLD_BREADTH=8`):
  girişte son 1 saatte en az 8 coin %3+ düşüyorsa ilk (büyük) pozisyonun zaman stopu 45 yerine 60 dk. Piyasa geneli
  dalgada toparlanma daha uzun sürüyor; yalnız çöküşte uzun tutmak ise zarar ettiriyor (genişlik şartı bu yüzden).
- **Ek pozisyonlar daha erken kapatılır** (`FUTURES_EXTRA_HOLD_MINUTES=35`): dalgada ek yuvalar hep dolu; ek pozisyon 45
  yerine 35 dk'da kapanınca yuvası yeni sinyale geçer.

10 çekilişin ortancası (seçim / doğrulama dönemi; "iyi" = bazdan iyi çıkan çekiliş sayısı):

| | Baz (rapor 17) | Yalnız dalga kuralı | F: A + iki kural |
|---|---|---|---|
| İşlem / yıl | 888 | 883 | 929 |
| Sabit boy yıllık getiri | +%332 / +%212 | +%341 / +%222 | +%379 / +%236 |
| ...bazdan iyi (10 çekilişte) | - | 10 / 10 | 10 / 10 |
| Bileşik yıllık getiri | +%493 / +%301 | +%580 / +%339 | +%875 / +%412 |
| ...bazdan iyi (10 çekilişte) | - | 10 / 10 | 10 / 10 |
| En büyük düşüş | %85 / %67 | %85 / %68 | %87 / %68 |
| Kayma 3 kat (bileşik) | +%123 / +%75 | +%141 / +%88 | +%224 / +%147 |
| Kayma 5 kat (bileşik) | −%0 / −%28 | +%2 / −%21 | +%42 / −%9 |
| Kötümser stop (5x) | eridi | eridi | eridi |
| Son 12 ay | +%81 | +%120 | +%152 |
| Son 24 ay | +%4.110 | +%5.708 | +%6.252 |
| Geride kaldığı yıllar | - | yok (2023 eşit) | 2025 (+%582 / +%975) |
| Önyükleme: fark > 0 olasılığı (seçim / doğrulama / tümü) | - | 0.85 / 0.82 / 0.92 | 0.95 / 0.85 / 0.98 |
| Tek rapor (kendi çekilişi) | 1.234.022 | 1.820.651 | 3.871.663 |
| ...10 çekilişte | 0.28-2.31 M | 1.06-2.80 M | 2.32-3.89 M |

Ayar satırları (rapor 17 ayarının üstüne):
- Yalnız dalga kuralı: `FUTURES_WAVE_HOLD_MINUTES=60`
- F: `FUTURES_WAVE_HOLD_MINUTES=60`, `FUTURES_EXTRA_HOLD_MINUTES=35`, `BREAKEVEN_TRIGGER_PCT=1.1`,
  `FUTURES_EXTRA_BE_TRIGGER_PCT=0.4`, `FUTURES_ISOLATED_SIZE_PCT=25`

**Yorum.** Yalnız dalga kuralı işlem sayısını değiştirmeden kârı artırıyor ve bu araştırmada hiçbir yılda geride kalmayan
tek değişiklik. F hem işlemi (+%5) hem kârı artırıyor: iki dönemde de 10 çekilişin 10'unda bazdan iyi, 3 ve 5 kat kaymada
da önde, son 12 ve 24 ayda da; farkın tüm dönemde sıfırdan büyük olma olasılığı %98. Zayıf yanı 2025 (tüm çekilişlerde
geride) ve düşüşün azalmaması. Aynı set 4x'te bazın 5x'inden geri kalmıyor (bileşik +%676 / +%315; seçimde 10, doğrulamada
6 çekilişte bazın 5x'inden iyi), düşüş %79 / %58, ama kötümser stop 10 çekilişin 8'inde yine eritiyor. 3x'te bileşik
+%457 / +%217, düşüş %67 / %47 ve kötümser stopta erimiyor (bazın 3x'i: +%314 / +%176).

**Komşu değerler** (6 çekiliş). Dalga kuralı (baz üstünde): 60 dk iki dönemde de tüm çekilişlerde iyi; 65-70 dk
doğrulamada iyi, seçimde eşit ya da biraz geride; 50-55 dk doğrulamada geride; 75-120 dk seçimde geride. Genişlik eşiği
5-12 aynı sonucu veriyor, ≥3 doğrulamada kötü; genişlik şartı olmadan (her ilk pozisyon 60 dk) daha kötü. Ek zaman stopu
(A üstünde): 27-35 dk tüm çekilişlerde iyi, 25 dk kötü, 38-42 dk etkisiz. Baz üstünde tek başına etkisi küçük (son 12
ayda geride); ek pozisyon başabaşı 0.4 ile birlikte işe yarıyor.

**İkinci turda elenenler.** Limit (maker) giriş (işlem −%40, doğrulamada getiri düşük), zarar serisi molası (işlem
−%7-28, getiri iki dönemde de düşük), izleyen stop (seçimde iyi, doğrulamada kötü), tüm pozisyonlarda zaman stopu 30-90 dk,
zarar kes 4-6, dalgada yeniden giriş ≥1-2, ek kapısı ≥8 (seçimde kötü), 5 yuva, yalnız çöküş eşiği 3-4, genişliğe göre
başabaş tetiği (son 12 ayda kötü) ya da Kâr Al (etkisiz). Başabaş tabanı: 0.25 biraz iyi görünüyor ama komşuları tutarsız
(0.2 seçimde iyi, doğrulamada kararsız; 0.35-0.4 kötü) ve son 12 ayda geride; alınmadı. Nötr kalanlar: ek kapısı ≥9
(seçimde işlem +%3, kâr aynı), Kâr Al'ı limit emirle almak (çok küçük artı). Ek pozisyon boyunu küçültmek (%16.7 → %10)
sabit boy getiriyi artırıyor ama işlem sayısını değiştirmiyor ve düşüşü büyütüyor (para ilk pozisyona yığılıyor): bu bir
risk ayarı, iyileştirme değil.

Coin evreni de değişmemeli (6 çekiliş, doğrulama dönemi sabit boy getiri; baz +%217): en hacimli 15 +%88, 25 +%146,
30 +%107, 2 yıllık yaş sınırı +%155, 90 gün ortalama +%165. 25-30 coin doğrulamada işlem sayısını %19-38 artırıyor ama
getiriyi üçte bir ile yarı arasında düşürüyor. Listesiz en hacimli 20 doğrulamada eşit (+%213) ama seçimde çok kötü
(+%332 → +%224, işlem −%22).

**Canlıda.** Süre pozisyon açılırken belirlenir ve pozisyona yazılır; açılış mesajında "Zaman stopu: 60 dk (Faz 9; ana
ayar 45 dk)" görünür, açık pozisyon ayar sonradan değişse de kendi süresiyle kapanır. Dalga kuralı tek pozisyon modunda da
çalışır (genişlik ölçümünü açar). Backtest raporunda başlıkta "Faz 9: …", 🧪 tabloda açıkken "Faz 9 kapalı", kapalıyken
iki deneme satırı yer alır. Canlı bot ve backtest aynı kuralı kullanır (`config.futures_hold_minutes`).

**Sınırlar.** Seçim yanlılığı: ~550 denemenin en iyileri. Süreler 5 dk'lık tarama adımına duyarlı (ek pozisyonda 25 dk
kötü, 27-35 dk iyi): bu kadar keskin bir geçişin bir kısmı geçmişe özgü olabilir. Düşüş azalmıyor; 5x'te kötümser stop
dolumu her ayarı eritiyor. Geçmiş sonuç gelecek garantisi değildir.

#### Rapor 18 araştırması: üçüncü tur (Kasım 2026) — F setini geçen kural çıkmadı
Rapor 18 (F seti uygulanmış) üstünde ~110 kural / ayar daha denendi (toplam ~660). Bu turda ayar değerleri değil **yeni
kural aileleri** sınandı. Hiçbiri kabul ölçütünü (seçim ve doğrulama döneminde ayrı ayrı iyi ya da eşit, kayma stresinde
kötüleşmeme) geçemedi; **yeni ayar eklenmedi, öneri F setidir.** Rakamlar sabit boy yıllık getiri, seçim / doğrulama;
F: +%379 / +%236, yılda 929 işlem (6 çekiliş; 30-45 dk'lık dalga eşiği satırı 10 çekiliş).

| Fikir | Sonuç | Neden elendi |
|---|---|---|
| **Ana yuva yeniden dolsun**: büyük pozisyon kapanınca (ekler açıkken) sıradaki sinyal yine büyük boyla açılsın | +%412 / +%212; son 12 ay +%152 → +%33; düşüş %69 → %78 | Seçimde iyi, doğrulamada 6 çekilişin 6'sında kötü. Dalganın ilk sinyali özel; sonradan gelenler büyük boyu taşımıyor |
| **Daha çok yuva** (5 / 6 / 8; ek toplamı sabit) | İşlem +%16 / +%30 / +%54; getiri +%323 / +%196, +%307 / +%187, +%304 / +%191 | İşlem artıyor, kâr düşüyor (yeniden dolan ana yuvayla birlikte de: +%400 / +%206) |
| **Taze dip**: büyük pozisyon yalnızca sinyal mumu son 4 saatin dibini gördüyse | Tek tek sinyallerde çok güçlü (taze: işlem başı +%0.38 / +%0.29, 7 yılın 7'sinde artı; dip 3+ mum önceyse −%0.08 / +%0.18) ama yuva simülasyonunda bayatı atlamak +%293 / +%158, küçültmek +%250-322 / +%136-195 | Şimdiki kuralın seçtiği ilk pozisyonlar (dalgada işlem başı +%0.35-0.6) taze / bayat ayrımından daha iyi bir seçim yapıyor |
| **Büyük pozisyona eleme**: dalga eşiği sinyali, düşük puan, BTC son 5 dk'da yükseliyor, yeşil mum ilk pozisyon olamasın | +%316-369 / +%114-204 | Hepsi iki dönemde de kötü |
| **Eş zamanlı sinyallerin sırası**: düşüş yüzdesi, taze dip önce, satış hacmi, 1 sa / 4 sa kuralı önce | +%245-402 / +%135-231 | Hiçbiri iki dönemde birden iyi değil (taze dip önce: seçimde +%402, doğrulamada +%224). Ters sıra +%92 / +%96: şimdiki sıra (eşiğini en çok aşan önce) çok önemli, değiştirilmemeli |
| **15 / 30 dk'lık çöküş kuralı** (ana kural olarak: 15 dk %3-5, 30 dk %4-5.5) | İşlem +%3 ile +%34; getiri +%279-391 / +%172-213 | Doğrulamada hepsi kötü |
| **30-45 dk'lık dalga eşiği** (yalnızca ≥12 coin düşerken; en iyisi 30 dk %3.5) | İşlem +%5 (seçim +%2.5, doğrulama +%10); +%403 / +%235; bileşik +%1.159 / +%391 (F: +%875 / +%412); son 12 ay +%170 | Eklenen işlemlerin kendi kazancı sıfır (dalga eşiği işlemleri işlem başı +%0.036 → %0.000); 3 kat kaymada doğrulama +%147 → +%103; 2022, 2025 ve son 24 ayda geride; önyüklemede doğrulama farkının artı olma olasılığı %49. Komşular (%3.25, %3.75, 45 dk %4) daha zayıf |
| **Pozisyona göre Kâr Al / Zarar Kes** (eklerde Kâr Al 3-6, Zarar Kes 3-4; ilkte Kâr Al 10-12, Zarar Kes 4 / 6) | +%366-394 / +%204-242 | Etkisiz ya da karışık (eklerde Zarar Kes 3: doğrulamada iyi, seçimde bileşik getiri kötü, düşüş %87 → %90) |

**Faz 8 (ek pozisyon başabaşı) notu düzeltildi.** Eski "Öneri: kapalı" notu F setiyle çelişiyordu. F setinde Faz 8'i
kapatmak işlem sayısını %19-22 düşürdü, getiriyi değiştirmedi (+%379 / +%236 → +%376 / +%226), son 12 ayı +%152'den
+%36'ya indirdi; 0.5 yerine 0.4 son 12 ayda 6 çekilişin 6'sında iyi. Notlar buna göre güncellendi: daha çok işlem için
açık (0.4), maliyete dayanıklılık için kapalı.

**Ek denemeler (aynı gün, ~25 kural daha; toplam ~690).**
- **Dalga başında büyük pozisyonu aynı mumdaki en güçlü 2-3 sinyale bölmek** (toplam büyüklük aynı): 2'ye bölünce
  +%303-328 / +%188-206, 3'e bölünce +%276-282 / +%164-169; düşüş azalmıyor (%86 / %65). İkinci en güçlü sinyal birinciden
  belirgin kötü: bölmek kârı düşürüyor.
- **Kâr Al'ı limit emirle koymak** (`FUTURES_TP_ORDER=limit`): modelde 10 çekilişin 10'unda, her yıl ve iki dönemde F'den
  iyi ama fark çok küçük (+%379.4 / +%235.6 → +%382.6 / +%236.5; bileşik +%875 / +%412 → +%902 / +%416). Kazanç yalnızca
  Kâr Al'la kapanan işlemlerin (6.340 işlemin 161'i, %2.5) komisyon ve kayma farkından (işlem başı +%0.05) geliyor ve
  motor "fiyat dokundu = emir doldu" varsayıyor. Gerçekte fiyat hedefe dokunup dönerse limit emir dolmayabilir; 100
  dokunuştan 1'inden fazlası dolmazsa kazanç silinir. Bu yüzden önerilmiyor (zararsız, ama kâr beklenmemeli).
- **Ek kapısı ≥9**: işlem +%2 (yılda 929 → 950), doğrulamada 10 çekilişin 10'unda F'nin gerisinde. Kâr Al %7 / %9: kötü.
- Başka giriş modülleri (pompa short, Bollinger + RSI, kırılım, fonlama sıkışması, trend içi düzeltme) Faz 3-4'te
  denenmişti: 28 birleşimin 28'i zararda; yeniden denenmedi.

**Sonuç.** Backtestte bu stratejinin ayar ve kural uzayı büyük ölçüde tüketildi: işlem sayısını artıran her şey ya kârı
düşürüyor ya da sıfır kazançlı işlem ekliyor (komisyon ve risk artar, kâr artmaz). ~690 denemeden sonra aynı 7 yıllık
veride bulunacak yeni bir "iyileşme" büyük olasılıkla rastlantı olur. Kalan gerçek kazanç ayarlarda değil girişin
hızında: bu yüzden hızlı girişe sakin coin kuralı eklendi (bkz. "Canlı giriş gecikmesi"). (Güncelleme: bir sonraki tur
ayarların dışında kalan bir boyut buldu, tarama sıklığı; bkz. "Rapor 19 araştırması".)

#### Rapor 19 araştırması: her dakika tarama ve ızgara testi (Kasım 2026)
Beşinci turda ~100 kural / ölçüm daha denendi (toplam ~790) ve iki önemli sonuç çıktı: (1) giriş anı her şeydir, (2) şimdiki
raporlar ayarların denendiği 5 dakikalık ızgaraya göre iyimserdir. Bunlardan **`FUTURES_MINUTE_SCAN`** (her dakika
tarama; varsayılan kapalı) doğdu. Rakamlar sabit boy (bileşiksiz) yıllık getiri, seçim / doğrulama dönemi.

**1) Giriş anı.** Girişi yalnızca 1 dakika geciktirmek getiriyi +%379 / +%236'dan +%101 / +%50'ye indiriyor (2 dk:
+%108 / +%68). "Teyit bekle" türü her kural kötü: ilk yükselen 1 dk mumu beklemek +%103-144 / +%57-61, bir önceki 1 dk
mumun tepesi aşılınca girmek +%41-134 / +%32-68, yeni dip yapmayan ilk 1 dk mumda girmek +%63-70 / +%72-77. Toparlanma
çöküş mumunun kapanışını izleyen ilk dakikada başlıyor; bot ne kadar erken girerse o kadar iyi.

**2) Izgara testi: aynı strateji, 1-4 dakika kaydırılmış 5 dk mumlarla.** Bot kuralı :00, :05, :10... kapanışlarında
değerlendirir ve bütün ayarlar bu ızgaranın ürettiği işlem dizisi üstünde seçildi. Kural kapanıştan kapanışa düşüş
olduğu için 1 dk mumlarla başka bir ızgarada (:01, :06, :11...) da birebir uygulanabilir; piyasa aynı, işlem dizisi
farklıdır. F setiyle (5x; kaydırılmışlar 6, bugünkü 10 çekilişin ortancası):

| 5 dk ızgarası | İşlem / yıl | İşlem başı net | Sabit boy | Bileşik | En büyük düşüş | Son 12 ay | Kayma 3 kat (bileşik) |
|---|---|---|---|---|---|---|---|
| Saatle hizalı (bugünkü bot) | 929 | +%0.144 | +%391 / +%235 | +%939 / +%409 | %88 / %68 | +%147 | +%243 / +%144 |
| 1 dk kaydırılmış | 896 | +%0.021 | +%205 / +%63 | +%72 / eridi | %96 / %95 | −%9 | −%26 / eridi |
| 2 dk kaydırılmış | 913 | +%0.101 | +%343 / +%89 | +%657 / +%3 | %83 / %93 | +%21 | +%194 / eridi |
| 3 dk kaydırılmış | 908 | +%0.097 | +%322 / +%128 | +%429 / +%70 | %77 / %93 | −%4 | +%154 / −%20 |
| 4 dk kaydırılmış | 914 | +%0.116 | +%325 / +%140 | +%470 / +%110 | %78 / %88 | −%14 | +%117 / +%8 |

Farkın iki kaynağı var. Küçük olanı gerçek: sinyaller tek tek (yuva etkisi olmadan) ölçüldüğünde saatle hizalı
kapanışlardaki sinyal biraz daha iyi (işlem başı +%0.096 / +%0.177; diğer dakikalarda +%0.054 / +%0.141), çünkü piyasanın
geri kalanı da 5 dk kapanışlarında işlem yapıyor. Büyük olanı ise **seçim yanlılığı**: ~790 deneme ve önceki fazların
hepsi aynı işlem dizisine bakılarak seçildi; kaydırılmış ızgaralar ayarların hiç görmediği dizilerdir. **Sonuç: rapordaki
rakamlar iyimser uçtur.** Gerçekte beklenmesi gereken, bugünkü ızgara ile kaydırılmışlar arasında bir yerdir (kabaca
yarısı) ve 5x kaldıraçta görülmemiş dizilerde hesabın erime riski gerçektir (dört kaydırılmış ızgaranın birinde
doğrulama döneminde eridi; 3 kat kaymada ikisinde). Dalga eşiği sinyalleri (`FUTURES_WAVE_RULES`) kaydırılmış
ızgaralarda işlem başı −%0.05 ile −%0.12: bu ızgaraya uydurulmuş görünüyor.

**3) Tarama sıklığı.** Aynı kural 1 dk mumlarla farklı aralıklarda değerlendirildi; her aralık tüm faz kaymalarıyla
ölçülüp ortancası alındı (3 çekiliş):

| Tarama aralığı | 1 dk | 2 dk | 3 dk | 4 dk | 5 dk | 7 dk | 10 dk | 15 dk |
|---|---|---|---|---|---|---|---|---|
| İşlem / yıl (seçim / doğrulama) | 2.015 / 1.227 | 1.604 / 1.012 | 1.358 / 885 | 1.188 / 790 | 1.070 / 707 | 881 / 610 | 716 / 496 | 555 / 401 |
| Sabit boy yıllık getiri | +%526 / +%276 | +%433 / +%171 | +%397 / +%139 | +%354 / +%113 | +%319 / +%122 | +%253 / +%94 | +%182 / +%86 | +%91 / +%69 |

İlişki düzgün: ne kadar sık tarama, o kadar çok işlem ve o kadar çok kâr. Her dakika taramanın faz seçeneği olmadığı
için sonucu ızgara şansına bağlı değildir.

**4) Her dakika tarama, tam protokol** (10 çekilişin ortancası; "iyi" = bugünkü 5 dk botundan iyi çıkan çekiliş):

| | F, 5 dk (bugünkü) | F, her dakika | Baz (rapor 17), 5 dk | Baz, her dakika |
|---|---|---|---|---|
| İşlem / yıl | 929 | 1.662 (+%79) | 889 | 1.474 (+%66) |
| İşlem başı net | +%0.144 | +%0.149 | +%0.167 | +%0.201 |
| Sabit boy yıllık getiri | +%391 / +%235 | +%536 / +%278 (iyi 10 / 9) | +%344 / +%211 | +%452 / +%272 |
| Bileşik yıllık getiri | +%939 / +%409 | +%1.954 / +%423 (iyi 10 / 5) | +%569 / +%297 | +%1.475 / +%442 |
| En büyük düşüş | %88 / %68 | %87 / %77 | %86 / %67 | %85 / %73 |
| En kötü gün | −%72 | −%56 | −%71 | −%60 |
| Kayma 3 kat (bileşik) | +%243 / +%144 | +%526 / +%56 | +%139 / +%74 | +%248 / +%102 |
| Kayma 5 kat (bileşik) | +%63 / −%10 | +%90 / −%22 | %0 / −%29 | +%19 / −%15 |
| Kötümser stop (5x) | eridi | eridi | eridi | eridi |
| Son 12 ay | +%147 | +%112 (iyi 4) | +%75 | +%166 |
| Son 24 ay | +%6.130 | +%3.467 (iyi 2) | +%3.981 | +%3.710 |
| 2020 … 2026 | +781 +46.958 +84 +40 +1.093 +568 +102 | +6.829 +134.192 +46 +477 +265 +998 +79 | +732 +9.710 +39 +33 +576 +942 +33 | +2.852 +92.866 −25 +308 +352 +1.369 +91 |
| Tek rapor (kendi çekilişi) | 3.871.663 | 7.530.160 | 1.234.022 | - |

Önyükleme (haftalık blok), F her dakika − F 5 dk: farkın artı olma olasılığı seçimde 0.89, doğrulamada 0.74, tüm dönemde
0.92. **Değerlendirme:** işlem sayısı %66-79 artıyor, işlem başı net aynı kalıyor, sabit boy getiri iki dönemde de
artıyor; kaydırılmış ızgaralara (gerçekçi beklenti) göre fark çok büyük. Zayıf yanları: doğrulamada bileşik getiri eşit
(çekilişlerin yarısında iyi), en büyük düşüş %68 → %77, işlem çok olduğu için maliyete daha duyarlı (3 kat kaymada
doğrulama +%144 → +%56), 2022 ve 2024'te geride, son 12 ve 24 ayda geride. Yani bugünkü (iyimser) rapora göre "her
testi geçen" bir iyileşme değil; ızgara şansından bağımsız ve iki kat işlem yapan sağlam bir seçenek.

**Melezler ve uyarlamalar** (6 çekiliş): büyük pozisyonu yalnızca 5 dk kapanışına bırakıp ekleri her dakika taramak
kötü (+%286 / +%144). Her dakika taramada ek başabaşı 0.5 (`FUTURES_EXTRA_BE_TRIGGER_PCT=0.5`): işlem +%66 / +%45,
+%530 / +%278, düşüş %80 / %70, 3 kat kaymada doğrulama +%148 (5 dk ile eşit), son 12 ay +%164; dalga eşikleri kapalı
(`FUTURES_WAVE_RULES=`): +%550 / +%305, 3 kat kaymada +%168, işlem +%71 / +%26. İkisi de bu yolda seçildiği için aynı
yanlılığı taşır; karar verirken tabloya değil yöne bakın.

**Bu turda elenenler.** Süreye bağlı zarar kesme (girişten 10-40 dk sonra stopu −%1 / −%2 / −%3 / −%4'e çekmek): −%1 ve
−%2 doğrulamada kötü, 30 dk'da −%3 etkisiz (+%380 / +%238). Oynaklığa (ATR) göre zarar kes: seçimde +%412-419,
doğrulamada +%182-211 (kötü); Kâr Al = 4-5×ATR karışık; başabaş tetiği = 0.6×ATR [1.1-2.0]: +%453 / +%245, son 12 ayda
kararsız (ilk başabaşı yükseltmenin başka bir biçimi). Eş zamanlı sinyalleri oynaklığa, son 5 dk düşüşüne ya da 24 saatlik
tepeye uzaklığa göre sıralamak: hepsi kötü. Ana yuvayı yalnızca yeniden giriş sinyaliyle doldurmak: +%404-415 / +%219-231.

**`FUTURES_MINUTE_SCAN=true` nasıl çalışır** (Telegram → 🎯 Çıkışlar ve Emirler → ⏱️ Her dakika tara). 5 dk kapanışlarında
hiçbir şey değişmez. Aradaki dakikalarda bot coin başına 485 adet 1 dk mum ister (istek ağırlığı 5 dk taramayla aynı),
bunlardan "o dakikada kapanan" 5 dk mumları üretir (`futures_data.resample`, kaydırmalı) ve **aynı** strateji
fonksiyonlarından geçirir; genişlik de o dakika için sayılır. Backtest aynı fonksiyonu tüm geçmişe uygular
(`find_signals`), 5 dk sınırındaki sinyaller iki modda birebir aynıdır. Hızlı girişle birlikte çalışır (ara dakikada yeni
1 dk mum kontrolü ve sakin coin kuralı aynen). Yalnızca sinyal kapanışlara bağlıyken (sade çöküş taktiği: ek modül,
likidasyon şartı, oynaklık eşiği ve onay modu kapalı), piyasa emriyle girişte ve en uzun pencere 24 saati aşmıyorken
etkindir; değilse bot 5 dk'da bir taramaya devam eder ve bunu `/tarama`'da söyler.

**Backtestte.** İlk çalıştırma evrenin 1 dk mumlarını indirir (~2 GB disk) ve rapor birkaç kat uzun sürer (4 çekirdekli
makinede ~7 dk); en yüksek bellek ~2 GB. Küçük sunucuda backtest yerine yukarıdaki rakamlara bakın; canlı botun bellek
ve istek yükü artmaz. Raporda başlıkta "⏱️ Her dakika tarama", sinyal hunisinde "ara dakika sinyali", 🧪 tabloda "5 dk'da
bir tarama (dakikalık tarama kapalı)" satırı yer alır; yeni sinyal gerektiren satırlar (çöküş eşiği vb.) süre kısalsın
diye 5 dk taramayla hesaplanır ve "· 5 dk taramayla" diye işaretlenir.

**Sınırlar.** Giriş gecikmesi payı ara dakikalarda da 5 sn (%0.05) varsayıldı; ara dakikalar için ayrıca ölçülmedi.
Canlı yol sahte borsayla test edildi (`tests/test_futures_minute_scan.py`: ara dakikada dip, backtestle aynı sinyal,
sınırda eski davranış, hızlı giriş); gerçek Binance'te ilk kez sizin sunucunuzda çalışacak. İşlem sayısı iki katına
çıktığı için komisyon da iki katına çıkar: kayma varsayımı tutmazsa kazanç daha çabuk erir. Önce test modunda deneyin.

#### Rapor 20 araştırması: dakikalık taramada ayarlar, izleyen stopun saniyelik veriyle sınanması (Kasım 2026)
Dakikalık tarama (`FUTURES_MINUTE_SCAN`) açıkken ~110 ayar / kural yeniden denendi (toplam ~900). Üç sonuç:

**1) İzleyen stop: backtest iyimser, gerçekte zararlı.** Dakikalık taramada rapordaki "İzleyen stop +%3 / %1.5" satırı
çok iyi görünür (rapor 20: +%4.9e+07, tabanda +%2.6e+07) ve çok çekilişli testte 7 komşu ayarın 7'si tabandan iyi çıktı
(ör. +2/1: bileşiksiz getiri +%526 / +%276 → +%728 / +%390). Canlı bot izleyen stopu 2 saniyede bir fiyat yoklayarak
uygular; 1 dk mumda dakika içi geri çekilme görünmez. Bu yüzden 300 coin-gün (150 seçim + 150 doğrulama dönemi), 1.935
işlem için Binance'in saniyelik işlem kayıtları (aggTrades) indirildi ve canlı mantık birebir oynatıldı (2 sn yoklama,
stop taşıma gecikmesi 0.5 sn, borsadaki stop en sık 10 sn'de bir taşınır, geri çekilmede piyasa emriyle çıkış). Aynı
işlemlerde izleyen stoplu − izleyen stopsuz çıkış getirisi (%; ortalama ± standart hata):

| İzleyen stop | İlk pozisyon (757 işlem) | Ek pozisyon (1.178 işlem) | Motorun (1 dk) gösterdiği: ilk / ek |
|---|---|---|---|
| +3 / 1 | −0.060 ± 0.042 | −0.092 ± 0.033 | +0.038 / +0.181 |
| +2 / 1 | −0.104 ± 0.049 | −0.115 ± 0.037 | +0.034 / +0.263 |
| +3 / 1.5 | −0.045 ± 0.039 | −0.091 ± 0.031 | +0.031 / +0.157 |
| +2 / 0.75 | −0.072 ± 0.049 | −0.131 ± 0.038 | +0.058 / +0.288 |
| +4 / 2 | −0.035 ± 0.030 | −0.056 ± 0.026 | +0.012 / +0.098 |
| +1.5 / 0.75 | −0.068 ± 0.052 | −0.123 ± 0.040 | +0.088 / +0.343 |

İzleyen stop altı ayarın altısında işlem başı getiriyi düşürüyor; izleyen stopla kapanan işlemler, bırakılsaydı ilk
pozisyonda %0.15-0.48, eklerde %0.55-0.83 daha iyi kapanacaktı (çöküş dönüşlerinde fiyat dakika içinde sık sık geri
çekilip devam ediyor). Motorda en kötümser dakika içi sıra varsayımı (her mumda önce tepe, sonra dip) iyimserliği
azaltıyor ama gidermiyor. İzleyen stop olmadan aynı oynatma motorla uyumlu (fark: ilk −0.038 ± 0.081, ek −0.003 ±
0.087), yani motorun başabaş modeli dakikalık girişlerde de geçerli. **Sonuç: izleyen stop önerilmez**; ayar notu,
Telegram ipucu ve rapor satırı artık bunu söylüyor, izleyen stop açıkken rapor başlığına uyarı düşüyor.

**2) Giriş gecikmesinin maliyeti (aynı saniyelik veri, 2.001 giriş).** Sinyal dakikasının kapanışından sonra fiyat
ortalama 1 sn'de +%0.023, 2 sn'de +%0.053, 3 sn'de +%0.040, 5 sn'de +%0.066 (± 0.016), 10 sn'de +%0.080, 30 sn'de
+%0.142 yukarıda. 5 dk sınırındaki girişlerle (5 sn'de +%0.068) ara dakikalardaki girişler (+%0.065) aynı: motorun ara
dakikalar için de kullandığı %0.05 varsayımı ölçümle uyumlu (biraz düşük).

**3) Dakikalık taramada dalga eşikleri kapatılmalı.** 10 çekilişin ortancası; "iyi" = dakikalık tarama tabanından (F seti)
iyi çıkan çekiliş, seçim / doğrulama:

| | Taban (F, her dakika) | Dalga eşikleri kapalı | Ek kapısı 12 | Başabaş tabanı 0.2 | Dalga kapalı + taban 0.2 | Faz 8 kapalı |
|---|---|---|---|---|---|---|
| İşlem / yıl | 1.662 | 1.436 | 1.528 | 1.537 | 1.323 | 1.073 |
| İşlem başı net | +%0.149 | +%0.187 | +%0.163 | +%0.207 | +%0.252 | +%0.336 |
| Sabit boy yıllık getiri | +%536 / +%278 | +%550 / +%305 (9 / 10) | +%561 / +%289 (10 / 9) | +%567 / +%281 (9 / 8) | +%572 / +%310 (10 / 10) | +%511 / +%301 (4 / 10) |
| Bileşik yıllık getiri | +%1.954 / +%423 | +%2.071 / +%661 (10 / 10) | +%1.986 / +%481 (6 / 9) | +%2.270 / +%448 (10 / 7) | +%2.348 / +%708 (10 / 10) | +%2.159 / +%680 (10 / 10) |
| En büyük düşüş | %87 / %77 | %84 / %72 | %83 / %76 | %72 / %76 | %70 / %72 | %79 / %66 |
| Kayma 3 kat (bileşik) | +%526 / +%56 | +%574 / +%168 | +%659 / +%68 | +%772 / +%81 | +%965 / +%177 | +%769 / +%226 |
| Kayma 5 kat (bileşik) | +%90 / −%22 | +%147 / +%33 | +%84 / −%24 | +%135 / −%12 | +%194 / +%70 | +%116 / +%30 |
| Son 12 ay | +%112 | +%79 (iyi 2) | +%132 (iyi 8) | +%107 (iyi 4) | +%77 (iyi 4) | +%216 (iyi 8) |
| Son 24 ay | +%3.467 | +%6.222 (iyi 10) | +%4.301 (iyi 8) | +%4.111 (iyi 7) | +%8.566 (iyi 10) | +%5.275 (iyi 7) |
| Geride kaldığı yıllar | - | 2023, 2026 | yok | 2023 | 2023, 2026 | 2020, 2022, 2023 |

Bu kez adaylar yalnızca seçim / doğrulama ayrımıyla değil **başka tarama ızgaralarında** da sınandı (2 dk'da bir × 2
faz, 3 dk'da bir × 3 faz ve 1 dk: altı ayrı işlem dizisi; kaç ızgarada kendi tabanından iyi):

| | Seçim | Doğrulama | Kayma 3 kat (doğrulama) | Son 12 ay |
|---|---|---|---|---|
| Dalga eşikleri kapalı | 6 / 6 | 5 / 6 | 6 / 6 | 5 / 6 |
| Dalga kapalı + başabaş tabanı 0.2 | 5 / 6 | 6 / 6 | 5 / 6 | 6 / 6 |
| Dalga kapalı + ek kapısı 12 | 5 / 6 | 6 / 6 | 6 / 6 | 5 / 6 |
| Başabaş tabanı 0.2 | 6 / 6 | 5 / 6 | 4 / 6 | 2 / 6 |
| Ek kapısı 12 | 5 / 6 | 5 / 6 | 4 / 6 | 1 / 6 |
| Ek başabaş 0.5 / 0.6, Faz 8 kapalı | 4 / 6 | 3 / 6 | 3-5 / 6 | 2-5 / 6 |

Dalga eşiği sinyalleri (son 1 saatte ≥12 coin düşerken 1 sa %5 / 4 sa %8) önceki ızgaraya uydurulmuş çıktı: kaydırılmış
ızgaralarda işlem başı −%0.05 ile −%0.12, dakikalık taramada −%0.03 (rapor 20). Kapatmak her ızgarada kârı ve maliyete
dayanıklılığı artırıyor; bedeli %14 daha az işlem (yılda 1.436; eski 5 dk botunun 929'una göre hâlâ +%55) ve bu ızgarada
son 12 ayın geride kalması (diğer beş ızgarada önde). **Öneri (dakikalık tarama açıkken): `FUTURES_WAVE_RULES=` (boş;
Telegram → 🧭 Giriş sinyali → 🌊 Dalga modu → "Dalga eşikleri: kapalı").** Başabaş tabanını 0.2'ye indirmek ek küçük bir
kazanç: saniyelik veride eklerde +%0.058 ± 0.027 (motor +%0.091 diyor, biraz iyimser), ilk pozisyonda etkisiz.

**Bu turda etkisiz ya da tutarsız çıkanlar** (dakikalık taramada, 3 çekiliş): Kâr Al 5-12, Zarar Kes 4-6, ilk başabaş
0.9-1.5, zaman stopu 30 / 60, dalgada ilk pozisyon 45-90 dk, ek zaman stopu 25-45, 3 / 5 yuva, ek boyu %10 / %25,
dalgada yeniden giriş ≥2-8, zarar beklemesi 120 / 480 dk, yalnız çöküş %10 / %50, ek kapısı 8 / 15.

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
büyük coin trendi için 🧭 Strateji → `rotation`, ⚙️ Test ayarları → 🪙 Rotasyon coinleri.

**Rotasyon sonuçlarına dikkat:** backtest bugünün en hacimli coinleriyle yapılır; bunlar dönem içinde yükselmiş
(hayatta kalmış) coinlerdir. Momentum rotasyonu tam da yükselen coini aldığı için sonuç olduğundan çok iyi görünür
(sonradan çöken coinler listede yoktur). Adil bir sınama için 🪙 Rotasyon coinleri → "10 büyük (Kasım 2023)" (son 3
yıl), "10 büyük (Kasım 2020)" (⏮️ önceki dönem) ya da "10 büyük (Kasım 2017)" (⏮️⏮️ iki dönem önce) seçin: dönem
başındaki en büyük 10 coinden sabit bir liste. 2017 listesinde artık Binance'te olmayan NEM ve Monero yerine sıradaki
IOTA ve QTUM var; bu coinlerin çoğunun USDT paritesi 2018'de açıldı, backtest onları listelendikleri gün sıralamaya alır.

**Dengeleme günü şansı (🎲):** haftada bir dengelenen rotasyon az işlem yapar; sonuç, ilk dengelemenin hangi gün
yapıldığına çok bağlıdır (canlı bot başlatıldığı gün başlar). Dönemi bir gün kaydırmak bile sonucu ikiye katlayabilir.
Rotasyon raporları bu yüzden aynı ayarın 7 farklı başlangıç günündeki en kötü / ortanca / en iyi sonucunu da yazar;
karar için ortancaya ve en kötüye bakın. Çıkış taraması her gün dengelemeyi (`ROTATION_DAYS=1`, bu şans yok ama daha
çok al-sat) de dener.

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
├── settings_menu.py   # Ayrıntılı ayar sayfaları: vadeli işlem, kill-switch, risk sigortaları, raporlar
├── futures_universe.py # Vadeli "en hacimli N köklü coin" evreni (canlı uygunluk kuralları + geçmiş hacim sıralaması)
├── runtime_settings.py  # Telegram'dan değiştirilebilen ayarlar ve .env'ye güvenli yazma
├── entry_filters.py   # Ek giriş filtreleri (fitil, VWAP, aşırı ısınma, RSI uyumsuzluğu, emir defteri, tape)
├── ip_monitor.py      # Sunucunun dış IP'sini izler, değişirse alarm
├── market_scan.py     # "Piyasayı Tara" ve pozitif RSI uyumsuzluğu taraması
├── smart_manager.py   # Akıllı yönetim (canlı): hedef uzatma, DCA, piramit, sorular, kademeli alım, pano
├── position_rules.py  # Akıllı yönetim kuralları ve RSI uyumsuzluğu tespiti (canlı ve backtest ortak)
├── liquidations.py    # Binance vadeli işlemler likidasyon akışı
├── futures_trader.py  # Vadeli scalping motoru (FuturesBot): giriş, başabaş, zaman stopu, Binance'teki stop / hedef
├── futures_exchange.py  # Binance USDⓈ-M bağlantısı (CCXT): One-Way, ISOLATED, kaldıraç, emirler; test modu
├── futures_backtest.py  # Vadeli backtest: yıllara göre kazanma oranı, düşüş, likidasyon riski, grafik
├── futures_data.py    # Binance vadeli geçmiş verisi (data.binance.vision) indirme ve önbellek
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
| **Pozisyon açılamıyor: Çoklu Varlık modu** (`-4168`, "Unable to adjust to isolated-margin mode under the Multi-Assets mode"; Telegram'da ve `/tarama`'da ⛔, backtest raporunda "N taramada giriş reddedildi") | Binance vadeli hesabı **Çoklu Varlık (Multi-Assets)** modunda; bu modda yalıtılmış teminat kullanılamaz ve bot **hiçbir** sinyali açamaz. Açık vadeli pozisyon ve emirleri kapatıp Binance uygulaması → Vadeli → ⚙️ Tercihler → **Varlık Modu → Tek Varlık Modu** seçin; bot yeniden başlatılmadan devam eder. Bot bunu kendisi de dener; seçenek yoksa ya da değişmiyorsa hesap Kredi İşlem Modu'nda (Credits Trading Mode, BNFCR) olabilir: o modda yalıtılmış teminat yoktur, Binance desteğine danışın |
| "Serbest USDT bakiyesi yok" | USDT'yi Binance'te **Cüzdan → Transfer** ile **Spot**'a aktarın |
| Coinler Binance'te "kilitli" görünüyor | Normal: yedek zarar-kes emri bekliyor |
| Kill-switch kilidi | Servisi durdurun, `python main.py --mode paper --reset-state --yes`, servisi başlatın |
| Bot hiç alım yapmıyor | Telegram'dan `/tarama` yazın; hangi filtrenin elediğini gösterir |
| `Unable to adjust to isolated-margin mode under the Multi-Assets mode` (-4168) | Vadeli hesap Çoklu Varlık modunda; bu modda yalıtılmış teminat kullanılamaz. Bot açılışta ve ilk işlemde hesabı kendisi Tek Varlık moduna alır. Alamazsa: açık vadeli pozisyon ve emirleri kapatıp Binance → Vadeli → Ayarlar (Tercihler) → Varlık Modu → **Tek Varlık Modu** |
| Backtest işlem gösteriyor, canlı bot açmadı (vadeli) | 🧪 Backtest → 7 gün: raporun başındaki 🔍 CANLI BOT KARŞILAŞTIRMASI her işlemin neden kaçtığını yazar; `/tarama`'da 🪙 Evren satırına bakın |

**Geliştiriciler için testler:** `.venv/bin/pip install pytest && .venv/bin/python -m pytest`
