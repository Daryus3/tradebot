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

**Taktik (`FUTURES_TACTIC=crash`, varsayılan): çöküş dönüşü.** `FUTURES_COINS`'teki (varsayılan Kasım 2023'ün ilk 10'u)
bir coin son `FUTURES_CRASH_HOURS` (4) saatte en az `FUTURES_CRASH_DROP_PCT` (%10) düştüyse (likidasyon şelalesi),
5 dk mum kapanışında **long** açılır; aynı anda birden çok coin çökerse en derin düşen alınır. Çıkışlar (fiyat %):
**Kâr Al +%6** (Binance'te limit emir), **Zarar Kes −%4** (Binance'te STOP_MARKET; konamazsa pozisyon hemen kapatılır),
**başabaş** fiyat +%1.5'e gelince stop girişin +%0.3 üstüne, **zaman stopu 30 dk**; zararla kapanan coine **4 saat**
(`LOSS_COOLDOWN_MINUTES=240`) tekrar girilmez. **Kaldıraç 2x** (`FUTURES_LEVERAGE`, Telegram'da 1x-20x düğmeleri;
3x üstü önerilmez). `FUTURES_CRASH_LIQ=true`: çöküş mumunda likidasyon dalgası da şart (kazanma %76-79, daha az işlem).
`FUTURES_TACTIC=liq4`: ilk 4'lü onay sürümü (likidasyon + hacim + RSI uyumsuzluğu + BTC trendi; aşağıda neden
bırakıldığı).

**İşlem altyapısı:** her girişten HEMEN önce `set_margin_mode('isolated')` + `set_leverage`; vadeli cüzdandaki serbest
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
| Günlük 30, ≥30 gün, çöküş #2 (5 dk tarama) | **3.534** | %77.6 | +%0.13 | +%2.721 / eridi | %68 / %94 | 6/7 |
| Günlük 30, ≥30 gün, çöküş + düzeltme (5 dk) | 4.766 | %70.5 | +%0.05 | +%2.133 / eridi | %69 / %93 | 4/7 |
| Günlük 30, ≥30 gün, yalnız fonlama −%0.05 (5 dk) | 1.311 | %77.5 | +%0.13 | +%366 / eridi | %88 / %93 | 5/7 |
| Günlük 30, ≥30 gün, yalnız düzeltme RSI < 20 (1 dk) | 908 | %64.7 | +%0.02 | +%211 / eridi | %92 / %93 | 2/7 |

Günlük 30 coin, ≥3 yıllık olanlarla bile yıl yıl: 2020 +%135, 2021 +%15.549, 2022 +%5, 2023 +%10, **2024 −%88, 2025
−%66, 2026 −%91**. Yaş filtresi olmadan 7 yılda **627 farklı coin** günün ilk 30'una giriyor (FARTCOIN, PNUT, AIXBT,
USUAL, PUMP...): dünün hacim patlaması çoğu zaman bir hype / yeni listeleme dalgası; bu coinlerin çöküşleri
toparlanmıyor. "Hacim varsa kalite vardır" varsayımı tutmadı: 6 aylık ortalama hacim + 3 yıl yaş (şimdiki ayar)
hype'ı eliyor, günlük hacim eleyemiyor.

Sonuç: **dört yapıdan hiçbiri #2'yi geçemedi.** Limit emir maliyeti %0.05 düşürse de en iyi sıçramaları kaçırıyor;
fonlama sinyali çöküşle örtüşüyor; düzeltme ve günlük havuz işlem sayısını 1.000-6.000'e çıkarıyor ama işlem başı
sonuç maliyetin altına iniyor. Öneri: 🥈 #2 (liste ∪ köklü 20, piyasa emri), yeni modüller kapalı; denemek isterseniz
yalnızca test modunda (paper).

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
| "Serbest USDT bakiyesi yok" | USDT'yi Binance'te **Cüzdan → Transfer** ile **Spot**'a aktarın |
| Coinler Binance'te "kilitli" görünüyor | Normal: yedek zarar-kes emri bekliyor |
| Kill-switch kilidi | Servisi durdurun, `python main.py --mode paper --reset-state --yes`, servisi başlatın |
| Bot hiç alım yapmıyor | Telegram'dan `/tarama` yazın; hangi filtrenin elediğini gösterir |

**Geliştiriciler için testler:** `.venv/bin/pip install pytest && .venv/bin/python -m pytest`
