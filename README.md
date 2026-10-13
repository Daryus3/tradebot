# 🤖 Kripto Para Al-Sat Botu (Binance Spot)

Kendi bilgisayarınızda çalışan, modüler yapıda, Python ile yazılmış bir al-sat botu.
Düşmüş ama toparlanmaya başlamış coinleri bulur, kısa vadeli (15-60 dk) toparlanma
işlemleri açar ve her şeyi emojili mesajlarla Telegram'ınıza bildirir. Telegram'dan
komutla yönetilebilir.

> ⚠️ **Risk uyarısı:** Kripto para alım-satımı yüksek risk içerir. Bu bot kâr garantisi
> vermez; kaybetmeyi göze alamayacağınız parayla kullanmayın. Önce mutlaka **test modunda**
> (sanal bakiye) çalıştırın.

---

## ✨ Özellikler

### Ana kurallar (11 USDT örneği)

| Kural | Ayrıntı |
|---|---|
| 💰 Sermaye | 11 USDT. Binance'in min. emir tutarı (~5 USDT) nedeniyle 11 USDT'de **tek pozisyon** açılır, bakiyenin tamamı kullanılır |
| 📦 Çoklu pozisyon | Bakiye yettikçe aynı anda **en fazla 3 coin** (ayarlanabilir): ~12 USDT → 2, ~17.5 USDT → 3. Sermaye eşit bölünür |
| 🎯 Kâr Al | Net kâr **+%15** → ≈ **12.65 USDT** olunca satar |
| 🛑 Zarar Kes | Net zarar **−%10** → ≈ **9.90 USDT** olunca satar |
| 📈 İzleyen Stop | Net kâr bir kez **+%10** (≈12.10) görülür, sonra **+%5**'e (≈11.55) düşerse anında satar |
| 💸 Komisyon | Alış ve satışta **%0.1** (BNB ile ödenirse %0.075). Tüm kararlar komisyon **düşüldükten sonraki net** paraya göre verilir |
| ☠️ Kill-switch | Toplam zarar sermayenin **%45**'ine (11 USDT'de ≈ **4.95 USDT**) ulaşırsa tüm açık pozisyonları satar, botu kapatır ve kilitler. Yüzde olduğu için sermaye büyüdükçe limit de büyür |
| 🔍 Tarama | Piyasa **30 sn**'de bir taranır; açık pozisyon **~1 sn**'de (anlık fiyat akışı) bir kontrol edilir |
| 📨 Telegram | Alım, satım, kâr/zarar, bakiye, hatalar → emojili anlık mesajlar |
| 🧪 Test modu | 11 USDT **sanal** bakiye ile canlı fiyatlardan simülasyon (gerçek emir yok) |

### Koruma ve güvenilirlik

| Özellik | Ne yapar? |
|---|---|
| 🧷 Binance'te yedek zarar-kes | Her alımdan sonra Binance'e −%12'de bekleyen bir stop emri bırakır. **Bilgisayar kapalı, uykuda veya internetsizken bile** pozisyon korunur |
| 🔄 Hesap mutabakatı | Açılışta Binance ile karşılaştırma yapar: bot kapalıyken stop çalıştıysa veya coini elle sattıysanız **gerçek satış fiyatını** kaydeder; internet kesintisinde sonucu belirsiz kalan alımı bulup takibe alır; gerçek komisyon oranınızı (BNB indirimi dahil) ve BNB bakiyenizi saatte bir kontrol eder |
| ♻️ Otomatik yeniden başlatma | `watchdog.py` (veya `BASLAT.bat`) ile bot çökerse kendiliğinden yeniden açılır. İsterseniz Windows açılışında başlar |
| ⏰ Kesinti uyarısı | Bilgisayar uykuya geçip uyanınca veya bot uzun süre kapalı kalınca "Bot 47 dk çalışmadı" uyarısı |
| 📶 İnternet kesintisi | Telegram mesajları kaybolmaz, bağlantı gelince gönderilir; açılışta internet yoksa bot bekler |
| 🔒 Başabaş koruması *(varsayılan açık)* | +%5 görmüş işlem geri dönerse zarara geçmeden ~+%0.3'te satar |
| 📉 BTC çöküş çıkışı *(isteğe bağlı)* | BTC 15 dk'da %2'den fazla düşerse açık pozisyonları erkenden satar |
| ⚡ Anlık fiyat akışı | Fiyatlar Binance WebSocket'ten anında gelir; bağlantı koparsa otomatik olarak normal sorguya döner |
| 🔐 Gizli bilgi koruması | Bot token'ı ve API anahtarları log dosyalarına ve mesajlara asla açık yazılmaz |

### Daha iyi alım seçimi

| Filtre | Ne yapar? |
|---|---|
| ↔️ Spread filtresi | Alış-satış farkı %0.3'ten büyük coinlere girmez (11 USDT'de bu fark doğrudan zarardır) |
| ☣️ Tehlikeli coin filtresi | Binance delist takvimindeki, 14 günden yeni listelenmiş ve son 3 günde %50+ pompalanıp sönen coinleri eler |
| 🌡️ Piyasa rejimi | Genel çöküşte alım yapmaz; zayıf piyasada kurallar sıkılaşır ve en fazla 1 pozisyon açılır |
| 📏 Oynaklık (ATR) filtresi | Hedefe gidemeyecek kadar sakin veya zarar kesi gürültüyle tetikleyecek kadar oynak coinleri eler |
| 🏅 Sinyal puanı / teyit *(isteğe bağlı)* | Alt puan sınırı ve sinyalin art arda 2 taramada görülmesi şartı |

### Telegram'dan kontrol ve raporlar

`/durum` `/pozisyon` `/bakiye` (TL karşılığıyla) `/islemler` `/csv` `/istatistik` `/rapor` `/tarama`
`/durdur` `/devam` `/sat` `/hepsinisat` `/kapat` `/ayarlar` `/ping` — ayrıntılar aşağıda.
Her gece günlük, pazar günleri haftalık özet gelir. Sessiz saatlerde rutin mesajlar sessiz gelir.

---

## 🚀 En kolay kurulum (Windows, çift tıkla)

1. [python.org/downloads](https://www.python.org/downloads/) adresinden **Python 3.10 veya üstünü** kurun.
   Kurulumun ilk ekranında **"Add Python to PATH"** kutusunu mutlaka işaretleyin.
2. Projeyi indirin: GitHub'da **Code → Download ZIP**, sonra ZIP'i bir klasöre çıkarın.
3. Klasördeki dosyalara **çift tıklayın**:

| Dosya | Ne yapar? |
|---|---|
| `KURULUM.bat` | Türkçe kurulum sihirbazı: Telegram'ı bağlar, (isterseniz) Binance anahtarlarını test eder, `.env` dosyasını sizin yerinize yazar, sonunda self-test çalıştırır |
| `TEST_ET.bat` | Self-test: bağlantıları ve kuralları kontrol eder |
| `BASLAT.bat` | Botu **test modunda** (sanal 11 USDT) başlatır; çökerse kendiliğinden yeniden açar |
| `CANLI_BASLAT.bat` | Botu **gerçek parayla** başlatır (EVET onayı ister) |
| `ISTATISTIK.bat` | İşlem geçmişinden performans istatistikleri |
| `BACKTEST.bat` | Stratejiyi son 30 günün verisiyle dener |

İlk çalıştırmada gerekli kütüphaneler otomatik kurulur. Pencereyi kapatmak botu durdurur.

---

## 🛠️ Elle kurulum (adım adım, tüm işletim sistemleri)

### 1) Python'u kurun
- [python.org/downloads](https://www.python.org/downloads/) adresinden **Python 3.10 veya üstünü** indirin.
- **Windows:** Kurulumun ilk ekranında **"Add Python to PATH"** kutusunu mutlaka işaretleyin.
- Kontrol: Terminali açıp yazın: `python --version` (macOS/Linux'ta `python3 --version`)

### 2) Projeyi indirin
- GitHub sayfasında **Code → Download ZIP** ile indirip bir klasöre çıkarın,
  veya git yüklüyse: `git clone https://github.com/Daryus3/tradebot.git`

### 3) Terminali proje klasöründe açın
- **Windows:** Klasörü açın, adres çubuğuna `cmd` yazıp Enter'a basın.
- **macOS/Linux:** Terminal'de `cd` ile klasöre gidin, örn. `cd ~/Downloads/tradebot`

### 4) Sanal ortam oluşturun ve etkinleştirin (önerilir)
**Windows:**
```bat
python -m venv venv
venv\Scripts\activate
```
**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```
Satırın başında `(venv)` görürseniz tamamdır. Terminali her yeni açışınızda bu
**etkinleştirme** komutunu tekrar çalıştırın.

### 5) Gereksinimleri kurun
```bash
pip install -r requirements.txt
```

### 6) Ayarları yapın — sihirbazla (önerilir)
```bash
python main.py --kurulum
```
Sihirbaz Telegram token'ınızı ister, chat ID'nizi kendisi bulur, isterseniz Binance anahtarlarınızı
test eder ve `.env` dosyasını yazar. Kâr al / zarar kes / kill-switch kurallarınız değiştirilmez.

**Elle yapmak isterseniz:** `.env.example` dosyasını `.env` adıyla kopyalayın
(Windows: `copy .env.example .env`, macOS/Linux: `cp .env.example .env`) ve Telegram bilgilerini girin:
1. Telegram'da **@BotFather**'ı açın → `/newbot` yazın → isim verin.
2. Size verilen **token**'ı `.env` içinde `TELEGRAM_BOT_TOKEN=` satırına yapıştırın.
3. Telegram'da yeni botunuzu açıp **`/start`** yazın (bu adım şart!).
4. Chat ID'nizi bulmak için `python main.py --telegram-chat-id` çalıştırıp çıkan
   `TELEGRAM_CHAT_ID=...` satırını `.env` dosyasına kopyalayın.

### 7) Self-test çalıştırın ✅
```bash
python main.py --selftest
```
Kontrol edilenler: ayarlar, risk kuralları (kâr al / zarar kes / izleyen stop / başabaş / kill-switch
senaryoları, yedek stop tutarı), Binance bağlantısı, deneme taraması, 11 USDT sanal bakiye ile canlı
fiyattan örnek al-sat, anlık fiyat akışı, API anahtarı izinleri ve komisyon oranı, Telegram.
Sonuçlar Telegram'a da gönderilir.

### 8) Test modunda (paper trading) çalıştırın 🧪
```bash
python watchdog.py --mode paper     # Önerilen: çökerse kendiliğinden yeniden başlar
python main.py                      # veya doğrudan
```
`.env` içinde `TRADING_MODE=paper` olduğu sürece bot **11 USDT sanal bakiye** ile canlı
verileri kullanarak al-sat yapar; gerçek emir göndermez. Telegram mesajlarının başında
`🧪 [TEST MODU]` yazar. Durdurmak için **Ctrl + C** (veya Telegram'dan `/kapat`).

Birkaç gün test modunda çalıştırıp sonuçları izlemenizi öneririz (`/istatistik`).
İşlem geçmişi: `data/trades_paper.csv` (Excel ile açılabilir).

### 9) Canlı moda geçiş 💵 (gerçek para)
1. **Binance API anahtarı oluşturun:** Binance → Profil → **API Yönetimi** → API Oluştur.
   - ✅ **Enable Reading** (Okuma)
   - ✅ **Enable Spot & Margin Trading** (Spot işlem)
   - ❌ **Enable Withdrawals** (Para çekme) — **ASLA açmayın!**
   - Mümkünse **IP kısıtlaması** ekleyin (evinizin IP adresi).
2. Anahtarları `.env` içindeki `BINANCE_API_KEY` ve `BINANCE_API_SECRET` satırlarına yazın
   (veya `python main.py --kurulum`).
3. USDT'nizin **Spot** cüzdanında olduğundan emin olun (Futures/Funding'deyse Binance'te
   **Cüzdan → Transfer** ile Spot'a aktarın).
4. Self-test'i tekrar çalıştırın (API izinlerini ve komisyonu kontrol eder): `python main.py --selftest`
5. Canlı modu başlatın:
   ```bash
   python watchdog.py --mode live
   ```
   Bot sizden onay için **EVET** yazmanızı ister. Çökerse yeniden başlatmalarda onay tekrar sorulmaz.

> ℹ️ Bot **binance.com** (Global) API'sini kullanır. Binance TR (trbinance) farklı bir
> borsadır ve desteklenmez.

> 💡 Test ve canlı modu **aynı anda** çalıştıracaksanız iki ayrı Telegram botu kullanın;
> aynı token'la iki bot komut alamaz.

---

## 🧭 Bilgisayar komutları

| Komut | Açıklama |
|---|---|
| `python watchdog.py --mode paper` | Test modu; çökerse otomatik yeniden başlatır (önerilen) |
| `python watchdog.py --mode live` | Canlı mod; çökerse otomatik yeniden başlatır |
| `python main.py` | `.env`'deki moda göre doğrudan çalışır (yeniden başlatma yok) |
| `python main.py --kurulum` | Türkçe kurulum sihirbazı |
| `python main.py --selftest` | Tüm sistemi test eder |
| `python main.py --istatistik` | Performans istatistikleri |
| `python main.py --backtest --gun 30` | Stratejiyi geçmiş verilerle dener (`--coin-sayisi 40`, `--sermaye 25`) |
| `python main.py --backtest --gun 90 --tara` | Aynı veriyle onlarca Kâr Al / Zarar Kes / süre ayarını karşılaştırır |
| `python main.py --backtest --gun 90 --bitis 2026-07-01` | O tarihte biten (daha eski) bir dönemi dener: bulunan ayarı bağımsız dönemde doğrulamak için |
| `python main.py --telegram-chat-id` | Telegram chat ID'nizi bulur |
| `python main.py --reset-state` | Kayıtlı durumu sıfırlar (kill-switch kilidi dahil) |
| `python watchdog.py --install-autostart --mode paper` | Windows açılışında otomatik başlatmayı kurar |
| `python watchdog.py --remove-autostart` | Otomatik başlatmayı kaldırır |
| `Ctrl + C` | Botu durdurur |

**Windows açılışında canlı mod:** Güvenlik için ayrıca onay bayrağı ister:
`python watchdog.py --install-autostart --mode live --allow-live-autostart`

---

## 📱 Telegram komutları

Bot **yalnızca sizin sohbetinizden** (`TELEGRAM_CHAT_ID`) gelen komutları kabul eder.
Telegram'da `/` yazınca komut listesi çıkar.

| Komut | Ne yapar? |
|---|---|
| `/durum` | Genel durum, bakiye, açık pozisyonlar |
| `/pozisyon` | Her pozisyonun güncel fiyatı, net K/Z, hedef ve stop fiyatları, yedek stop |
| `/bakiye` | Bakiye ve **TL karşılığı** (bilgi amaçlı) |
| `/islemler` | Son 10 işlem |
| `/csv` | İşlem geçmişi dosyasını gönderir |
| `/istatistik` | Kazanma oranı, ortalama kâr/zarar, en uzun kayıp serisi, kill-switch'e ulaşma olasılığı |
| `/rapor gun` · `/rapor hafta` · `/rapor ay` | Dönem özeti |
| `/tarama` | "Neden alım yapmıyor?" — son taramada kaç coine bakıldı, en sık eleme nedenleri |
| `/durdur` · `/devam` | Yeni alımları durdurur / devam ettirir (açık pozisyon takibi sürer) |
| `/sat ETH` · `/hepsinisat` | Pozisyonu hedefi beklemeden hemen satar (onay ister) |
| `/kapat` | Botu güvenle kapatır (onay ister) |
| `/sermayeekle 10` | Binance hesabınıza eklediğiniz parayı bota tanıtır; sermaye, kill-switch limiti ve pozisyon sayısı buna göre büyür (onay ister) |
| `/ayarlar` · `/ping` · `/yardim` | Ayarlar, "çalışıyor musun?", komut listesi |

Riskli komutlar `/evet` onayı ister (60 sn içinde). `.env`'de `COMMAND_PIN=1234` gibi bir PIN
belirlerseniz onay `/evet 1234` şeklinde olur. Elle satıştaki zarar da kill-switch toplamına sayılır.

---

## 📈 Strateji nasıl çalışır?

**Her 30 saniyede bir:**
1. **Ön eleme:** Binance'teki tüm USDT pariteleri arasından son 24 saatte **düşmüş**
   (−%2 ile −%30 arası), yeterli **hacmi** (≥ 3M USDT) ve **oynaklığı** olan coinler seçilir.
   Stabil coinler, tehlikeli coinler (delist takvimi, yeni listelenen, pompa sonrası) ve
   11 USDT ile işlem yapmaya uygun olmayan pariteler elenir.
2. **Ayrıntılı analiz (5 dk mumlar):**
   - Son 3 saatte en az **%3'lük bir düşüş** (dip) olmuş,
   - Dip en az 10 dk önce oluşmuş ve fiyat **toparlanmaya başlamış** (düşüşün %15-60'ı geri alınmış),
   - **RSI** dipte aşırı satımdaydı (≤35) ve şimdi **yükseliyor** (40-65),
   - Fiyat **EMA(9)** üzerinde, son mum yeşil, toparlanma mumlarında **hacim** artmış,
   - Oynaklık (ATR) makul aralıkta.
3. **Puanlama:** Düşüş derinliği, kalan toparlanma alanı, hacim, RSI ivmesi ve oynaklığa göre
   en iyi aday seçilir; spread kontrolünden geçerse alım yapılır.
4. **Piyasa filtreleri:** BTC son 1 saatte %1.5'ten fazla düşmüşse veya piyasa genel çöküş
   modundaysa alım yapılmaz. Zayıf piyasada kurallar sıkılaşır ve en fazla 1 pozisyon açılır.

**Pozisyon açıkken:** Her pozisyonun net kâr/zararı ayrı ayrı hesaplanır; kâr al, zarar kes ve
izleyen stop her coin için bağımsız çalışır. Kill-switch tüm pozisyonların toplamına bakar.

### 🧷 Binance'teki yedek zarar-kes emri
Bot normalde zarar kesi kendisi yapar (−%10). Ama bilgisayar kapanır, uykuya geçer veya internet
kesilirse bot satış yapamaz. Bu yüzden her alımdan hemen sonra Binance'e, biraz daha aşağıda
(**−%12**) bekleyen bir stop emri bırakılır. Bu emir **Binance'in sunucusunda** durur:

- Bot çalışırken fiyat −%10'a inerse bot önce bu emri iptal eder, sonra kendisi satar.
- Bot ulaşılamıyorsa ve fiyat −%12'ye inerse Binance satışı kendisi yapar.
- Bot yeniden açılınca bunu fark eder ve gerçek satış fiyatıyla kaydeder.

Not: Emir beklerken coinler Binance'te "kilitli" görünür; bu normaldir. Bazı paritelerde
piyasa tipi stop yoktur, o zaman limitli stop kullanılır ve çok sert çöküşte dolmayabilir.
Kapatmak için: `BACKUP_STOP_ENABLED=false`.

### 📦 Aynı anda kaç coin? (`MAX_OPEN_POSITIONS`)
Bot sermayeyi pozisyonlara **eşit** böler. Ancak her pozisyon, fiyat stop-loss'a (−%10) düşse bile
Binance'in minimum satış tutarının (~5 USDT) üstünde kalmalıdır; aksi halde coin satılamaz ve
zarar kes çalışmaz. Bu yüzden pozisyon başına en az **≈5.83 USDT** gerekir ve bot pozisyon sayısını
bakiyeye göre **otomatik** ayarlar:

| Bakiye | Aynı anda pozisyon |
|---|---|
| 11 USDT | 1 |
| ~12 – 17.4 USDT | 2 |
| ~17.5 USDT ve üstü | 3 (`MAX_OPEN_POSITIONS=3` ise) |

Kâr ettikçe pozisyon sayısı artar, zarar ettikçe azalır. Her zaman tek pozisyon için
`.env` içinde `MAX_OPEN_POSITIONS=1` yapın.

> ⚠️ Daha çok pozisyon riski dağıtmaz: piyasa düşünce coinler genelde **birlikte** düşer ve
> kill-switch limitine daha hızlı yaklaşılabilir. Küçük pozisyonlarda satılamayan "toz" payı da
> oransal olarak büyür, bu yüzden uygun coin sayısı azalabilir.

### 💸 Komisyon ve "toz" hesabı
- Binance alışta komisyonu **aldığınız coinden** keser (%0.1), satışta **USDT'den** keser (%0.1).
- Satılabilecek miktar Binance'in LOT adımına yuvarlanır; artan çok küçük kısım ("toz")
  cüzdanda kalır. Bot bunu da zarar olarak hesaba katar ve tozu büyük olacak pariteleri
  (örn. 11 USDT ile BTC) hiç seçmez.
- Bu yüzden fiyat tam %15 artınca değil, komisyonlar karşılandıktan sonra **net %15** olunca satılır
  (fiyat olarak yaklaşık %15.2-15.3 artış gerekir).

### 🪙 BNB ile komisyon indirimi (isteğe bağlı)
- Binance'te komisyonu BNB ile öderseniz **%25 indirim** alırsınız (%0.1 → **%0.075**). Açmak için Binance'te
  Kontrol Paneli / Ücret Oranı sayfasındaki **"BNB ile komisyon öde"** (Pay fees with BNB) anahtarını açın
  ve Spot cüzdanınızda biraz BNB tutun. Ardından `.env` içinde `FEE_RATE=0.00075` yapın.
- BNB biterse Binance uyarı vermeden komisyonu yine %0.1 olarak alınan coinden / USDT'den keser.
  Bot bunu **yalnızca izler**: açılışta ve saatte bir gerçek oranı okur; `FEE_RATE` gerçek orandan düşükse
  (net kâr al / zarar kes hesabı fazla iyimser kalır) Telegram'dan uyarır. BNB'niz `BNB_MIN_TRADES` (20)
  al-sat işleminden azına yetecek kadar kaldıysa ya da bittiyse sessiz bir bilgi mesajı gönderir (günde en
  fazla bir). Binance kırıntı BNB'yi kullanmaz; bir işlemin komisyonuna yetmeyen BNB "bitmiş" sayılır.
  `FEE_RATE` gerçek orandan yüksekse yalnızca bilgi verir (bot temkinli hesaplar). Durumu `/ayarlar`
  komutunda ve self-test'te de görürsünüz.
- Bot Binance'teki bu ayarı **değiştirmez** ve **otomatik BNB almaz**: en küçük emir ~5 USDT olduğu için
  11 USDT'lik hesapta paranın yarısına yakını oynak bir coine bağlanırdı; kazanç ise taraf başına yalnızca
  %0.025 (11 USDT'lik bir al-satta ≈0.0055 USDT). BNB'yi isterseniz kendiniz, az miktarda alın.
- BNB'yi botun sermayesinden değil **ayrı bir parayla** alın (bot onu sermayesi saymaz). BNB ile komisyon
  ödeme açıkken bot **BNBUSDT paritesinde işlem açmaz**; yoksa komisyonlar pozisyonun kendisinden düşer.

### ☠️ Kill-switch
Toplam (gerçekleşen + tüm açık pozisyonlardaki) zarar, botun sermayesinin `MAX_TOTAL_LOSS_PCT` yüzdesine
(varsayılan **%45** → 11 USDT'de ≈4.95 USDT) ulaştığında bot açık pozisyonların hepsini satar,
Telegram'a bildirir ve kapanır. Bu durum `data/` klasörüne kaydedilir; botu yeniden başlatsanız
bile **kilitli** kalır (bekçi de yeniden başlatmaz). Bilerek tekrar başlatmak için:
```bash
python main.py --reset-state
```

### 💵 Hesaba para eklemek
Bot yalnızca kendisine verilen sermayeyle işlem yapar; Binance'e para yatırmak tek başına yetmez.
Parayı **Spot** cüzdanına aktardıktan sonra Telegram'dan örneğin `/sermayeekle 20` yazıp `/evet` ile
onaylayın. Botun sermayesi 20 USDT artar; kill-switch limiti (sermayenin %45'i) ve aynı anda açılabilecek
pozisyon sayısı da buna göre büyür. Kill-switch sayacı (şimdiye kadarki kâr/zarar) sıfırlanmaz.
Canlı modda bot, eklenen tutarın Spot'ta gerçekten serbest olup olmadığını kontrol eder.

> **Eski `.env` dosyaları:** `MAX_TOTAL_LOSS_USDT` hâlâ yazılıysa (ve `MAX_TOTAL_LOSS_PCT` boşsa) bot o
> tutarı aynen uygular (sermaye eklenirse aynı oranda büyütür) ve açılışta sizi uyarır. Yüzdeye geçmek
> için o satırı silip `MAX_TOTAL_LOSS_PCT=45` yazın. Aynı şekilde eski `.env`'de `BREAKEVEN_ENABLED=false`
> yazıyorsa yeni varsayılan (açık) uygulanmaz; başabaşı açmak için o satırı `true` yapın.

### ⏱️ 15-60 dakika hakkında
Strateji, kısa sürede toparlanma potansiyeli olan oynak coinleri seçer. Ancak %15'lik kâr hedefi
iddialı bir hedeftir; bazı işlemler 60 dakikadan uzun sürebilir. İsterseniz `.env` içinde
`MAX_HOLD_MINUTES=60` yaparak pozisyonun en fazla 60 dk tutulmasını sağlayabilirsiniz
(varsayılan: kapalı, yani sadece kâr al / zarar kes / izleyen stop ile çıkılır).

---

## 📊 İstatistik ve backtest

- **`/istatistik`** veya `python main.py --istatistik`: kazanma oranı, ortalama kâr ve zarar, en uzun
  kayıp serisi, toplam komisyon, ortalama süre, çıkış nedenlerine ve coinlere göre dağılım.
  +%15 / −%10 kuralında başabaş için yaklaşık **%40 kazanma oranı** gerekir; bot bunu sizin
  oranınızla karşılaştırır ve geçmiş işlemlerden "kill-switch'e ulaşma olasılığı" tahmini yapar.
  20-30 işlemden az veriyle sonuçlar yanıltıcı olabilir.
- **İşlem kaydı** (`data/trades_<mod>.csv`): her işlemin sinyal puanı, düşüş, RSI, hacim, spread,
  BTC durumu, piyasa rejimi ve işlem boyunca görülen en yüksek / en düşük kâr bilgisi de yazılır.
  Bakiye seyri `data/equity_<mod>.csv` dosyasında.
- **Backtest** (`python main.py --backtest --gun 30`): strateji ve tüm kurallarınız (+%15, −%10,
  izleyen stop, kill-switch, 11 USDT, %0.1 komisyon) son 30 günün verisiyle denenir. İlk çalıştırma
  veri indirdiği için birkaç dakika sürebilir; veriler `data/backtest_cache` klasörüne kaydedilir.
  **Geçmiş geleceği garanti etmez**: listeden çıkarılmış coinler eksik olduğu için sonuç iyimser
  çıkabilir; 5 dakikalık mumlarla çalışır ve aynı mumda hem hedef hem stop varsa kötümser varsayar.
  Başabaş, piyasa rejimi (yaklaşık), ATR ve hedef alanı filtreleri canlıdaki gibi uygulanır.
- **Teşhis bölümü** (backtest raporunun sonunda): işlem başına fiyat hareketi ile komisyon + spread
  maliyetini ayırır (sinyal mi kötü, komisyon mu yiyor?), işlemlerin yüzde kaçının +%1 / +%2 / Kâr Al
  seviyesine ulaştığını, süre dolan işlemlerin durumunu, hedef alanı geniş/dar sinyallerin sonucunu ve
  dönemin iki yarısının ayrı sonuçlarını gösterir.
- **Parametre taraması** (`--tara`): veri bir kez indirilir, giriş analizleri bir kez yapılır; sonra
  Kâr Al (2/3/5), Zarar Kes (2/3.5), izleyen stop (1/2), süre (60/120/240 dk) ve hedef alanı filtresi
  (kapalı/1) kombinasyonları aynı veriyle denenir. Değerleri değiştirmek için `--tp 2,3 --sl 2
  --iz 1 --sure 90,180 --alan 0,1`. Karşılaştırma adil olsun diye taramada kill-switch kapalıdır.
  **Çok ayar denendiğinde birkaçı tesadüfen kârlı çıkar**: yalnızca dönemin iki yarısında da kârlı
  (✅) ve yeterli işlemi olan ayarları ciddiye alın, sonra test edilen dönemle **çakışmayan** daha eski
  bir dönemde deneyin (`--bitis`; rapor tam komutu yazar). `--gun 30` / `--gun 60` aynı dönemin
  parçasıdır, bağımsız kanıt sayılmaz.

---

## ⚙️ Önemli ayarlar (`.env`)

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` = test, `live` = gerçek |
| `START_CAPITAL_USDT` | `11` | Botun yöneteceği sermaye |
| `MAX_OPEN_POSITIONS` | `3` | Aynı anda en fazla pozisyon (bakiye yetmezse otomatik azalır) |
| `MAX_TOTAL_LOSS_PCT` | `45` | Kill-switch: toplam zarar sermayenin bu yüzdesine ulaşınca bot durur |
| `TAKE_PROFIT_PCT` | `15` | Net kâr hedefi (%) |
| `STOP_LOSS_PCT` | `10` | Net zarar limiti (%) |
| `TRAILING_ACTIVATION_PCT` / `TRAILING_FLOOR_PCT` | `10` / `5` | İzleyen stop |
| `FEE_RATE` | `0.001` | Komisyon oranı (%0.1; komisyonu BNB ile ödüyorsanız `0.00075`) |
| `BNB_FEE_CHECK` / `BNB_MIN_TRADES` | `true` / `20` | BNB bakiyesi bildirimleri (yalnızca bilgi verir, BNB almaz). Yanlış `FEE_RATE` uyarısı her zaman açıktır |
| `BACKUP_STOP_ENABLED` / `BACKUP_STOP_PCT` | `true` / `12` | Binance'teki yedek zarar-kes emri |
| `BREAKEVEN_ENABLED` / `BREAKEVEN_TRIGGER_PCT` | `true` / `5` | Başabaş koruması (TRIGGER, izleyen stop başlangıcından küçük olmalı) |
| `MIN_ROOM_RATIO` | `0` | Hedef alanı filtresi: düşüş öncesi zirveye kalan alan Kâr Al için yetmiyorsa alma (0 = kapalı) |
| `BTC_CRASH_EXIT` | `false` | BTC çöküşünde erken çıkış |
| `USE_WEBSOCKET` | `true` | Anlık fiyat akışı |
| `TELEGRAM_COMMANDS` / `COMMAND_PIN` | `true` / boş | Telegram komutları / onay PIN'i |
| `DAILY_REPORT_TIME` / `WEEKLY_REPORT` | `23:55` / `true` | Günlük ve haftalık özet |
| `QUIET_HOURS` | boş | Sessiz saatler, örn. `23:00-08:00` |
| `MAX_SPREAD_PCT` | `0.3` | Spread filtresi |
| `DELIST_FILTER` | `true` | Tehlikeli coin filtresi |
| `REGIME_FILTER` | `true` | Piyasa rejimi filtresi |
| `MIN_SIGNAL_SCORE` / `SIGNAL_CONFIRM_SCANS` | `0` / `1` | Sinyal puanı alt sınırı / tarama teyidi (kapalı) |
| `MIN_ATR_PCT` / `MAX_ATR_PCT` | `0.25` / `4` | Oynaklık filtresi |
| `MAX_HOLD_MINUTES` | `0` | Maks. pozisyon süresi (0 = sınırsız) |

Tüm ayarlar ve açıklamaları `.env.example` dosyasındadır. Eski bir `.env` dosyanız varsa yeni
ayarları eklemeniz gerekmez; yazmadığınız ayarlar için yukarıdaki varsayılanlar kullanılır.

> Sermaye/kural ayarlarını değiştirdikten sonra temiz bir başlangıç için
> `python main.py --reset-state` çalıştırın (kayıtlı durum başlangıç sermayesini hatırlar).

---

## 📁 Dosya yapısı

```
tradebot/
├── main.py            # Giriş noktası (komut satırı, modlar)
├── watchdog.py        # Bekçi: bot çökerse yeniden başlatır
├── autostart.py       # Windows açılışında otomatik başlatma
├── config.py          # .env ayarlarını okur ve doğrular
├── trader.py          # Ana döngü: tarama, alım, pozisyon takibi, yedek stop, kill-switch, raporlar
├── strategy.py        # Düşüş sonrası toparlanma tarayıcısı
├── coin_filters.py    # Tehlikeli coin filtresi (delist, yeni listeleme, pompa)
├── market_regime.py   # Piyasa rejimi tespiti
├── indicators.py      # RSI, EMA, ATR hesaplamaları
├── risk.py            # Kâr al / zarar kes / izleyen stop / başabaş / boyutlandırma (komisyon dahil)
├── exchange.py        # Binance kuralları, canlı borsa ve sanal (paper) borsa, yedek stop emirleri
├── binance_client.py  # Binance REST API istemcisi
├── price_stream.py    # Anlık fiyat akışı (WebSocket)
├── telegram_bot.py    # Telegram bildirimleri ve emojili mesaj şablonları
├── commands.py        # Telegram komutları
├── stats.py           # İstatistikler ve dönem raporları
├── backtest.py        # Geçmiş veriyle strateji testi
├── state.py           # Kalıcı durum (data/state_*.json), işlem ve bakiye kayıtları (CSV)
├── selftest.py        # Self-test
├── setup_wizard.py    # Kurulum sihirbazı
├── logger.py          # Log ayarları (logs/bot.log), gizli bilgileri maskeleme
├── *.bat              # Windows için çift tıkla başlatma dosyaları
├── tests/             # Otomatik testler (pytest)
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🩺 Sorun giderme

| Sorun | Çözüm |
|---|---|
| `python` bulunamadı | Python'u "Add to PATH" işaretli tekrar kurun veya `py main.py` deneyin |
| Telegram mesajı gelmiyor | Bota `/start` yazdığınızdan emin olun; `--telegram-chat-id` ile ID'yi kontrol edin |
| Telegram komutlarına cevap yok | Aynı bot token'ını kullanan başka bir program (ör. ikinci bot) çalışıyor olabilir; logda "aynı bot token" uyarısına bakın |
| `Timestamp ... recvWindow` (-1021) | Bilgisayar saatinizi senkronize edin (Windows: Ayarlar → Saat → "Şimdi eşitle") |
| `Invalid API-key, IP, or permissions` (-2015) | Anahtarı, izinleri ve IP kısıtlamasını kontrol edin; ev IP'niz değişmiş olabilir |
| `Service unavailable from a restricted location` | Binance bulunduğunuz konumdan API erişimine izin vermiyor |
| "Serbest USDT bakiyesi yok" ama hesapta USDT var | Bot **Spot** cüzdanını kullanır. Futures/Funding cüzdanındaki USDT'yi Binance'te **Cüzdan → Transfer** ile **Spot**'a aktarın |
| Coinler Binance'te "kilitli" görünüyor | Normal: yedek zarar-kes emri bekliyor. Bot satarken önce iptal eder |
| Kill-switch kilidi | `python main.py --reset-state` |
| Bot hiç alım yapmıyor | Normal olabilir: kriterlere uyan fırsat yoksa bekler. Telegram'dan `/tarama` yazın |

**Önemli:** Bot sadece bilgisayarınız açıkken ve internete bağlıyken **kâr al / izleyen stop** yapabilir.
Bilgisayar kapalıyken açık pozisyonu yalnızca Binance'teki yedek stop emri (−%12) korur.
Windows'ta **Ayarlar → Sistem → Güç** bölümünden uyku modunu kapatmanızı öneririz. Bot tekrar
başlatıldığında açık pozisyonu kaldığı yerden takip eder ve kapalıyken ne olduğunu size bildirir.

---

## 🧪 Geliştiriciler için: otomatik testler
```bash
pip install pytest
python -m pytest
```
