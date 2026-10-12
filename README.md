# 🤖 Kripto Para Al-Sat Botu (Binance Spot)

Kendi bilgisayarınızda çalışan, modüler yapıda, Python ile yazılmış bir al-sat botu.
Düşmüş ama toparlanmaya başlamış coinleri bulur, kısa vadeli (15-60 dk) toparlanma
işlemleri açar ve her şeyi emojili mesajlarla Telegram'ınıza bildirir.

> ⚠️ **Risk uyarısı:** Kripto para alım-satımı yüksek risk içerir. Bu bot kâr garantisi
> vermez; kaybetmeyi göze alamayacağınız parayla kullanmayın. Önce mutlaka **test modunda**
> (sanal bakiye) çalıştırın.

---

## ✨ Özellikler

| Kural | Ayrıntı (11 USDT örneği) |
|---|---|
| 💰 Sermaye | 11 USDT. Binance'in min. emir tutarı (~5 USDT) nedeniyle **tek pozisyon** açılır, bakiyenin tamamı kullanılır |
| 🎯 Kâr Al | Net kâr **+%15** → ≈ **12.65 USDT** olunca satar |
| 🛑 Zarar Kes | Net zarar **−%10** → ≈ **9.90 USDT** olunca satar |
| 📈 İzleyen Stop | Net kâr bir kez **+%10** (≈12.10) görülür, sonra **+%5**'e (≈11.55) düşerse anında satar |
| 💸 Komisyon | Alış ve satışta **%0.1** (BNB'siz). Tüm kararlar komisyon **düşüldükten sonraki net** paraya göre verilir |
| ☠️ Kill-switch | Toplam zarar **5 USDT**'ye ulaşırsa açık pozisyonu satar, botu kapatır ve kilitler |
| 🔍 Tarama | Piyasa **30 sn**'de bir taranır; açık pozisyon **5 sn**'de bir kontrol edilir |
| 📨 Telegram | Alım, satım, kâr/zarar, bakiye, hatalar → emojili anlık mesajlar |
| 🧪 Test modu | 11 USDT **sanal** bakiye ile canlı fiyatlardan simülasyon (gerçek emir yok) |

---

## 🛠️ Kurulum (adım adım)

### 1) Python'u kurun
- [python.org/downloads](https://www.python.org/downloads/) adresinden **Python 3.10 veya üstünü** indirin.
- **Windows:** Kurulumun ilk ekranında **"Add Python to PATH"** kutusunu mutlaka işaretleyin.
- Kontrol: Terminali açıp yazın:
  ```bash
  python --version
  ```
  (macOS/Linux'ta `python3 --version`)

### 2) Projeyi indirin
- GitHub sayfasında **Code → Download ZIP** ile indirip bir klasöre çıkarın,
  veya git yüklüyse:
  ```bash
  git clone https://github.com/Daryus3/tradebot.git
  ```

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

### 6) Ayar dosyasını oluşturun
**Windows:**
```bat
copy .env.example .env
```
**macOS / Linux:**
```bash
cp .env.example .env
```
`.env` dosyasını Not Defteri / herhangi bir metin editörüyle açın. Varsayılan değerler
isteğinizdeki kurallara göre ayarlıdır; şimdilik sadece Telegram bilgilerini girmeniz yeterli.

### 7) Telegram botunu bağlayın
1. Telegram'da **@BotFather**'ı açın → `/newbot` yazın → isim verin.
2. Size verilen **token**'ı `.env` içinde `TELEGRAM_BOT_TOKEN=` satırına yapıştırın.
3. Telegram'da yeni botunuzu açıp **`/start`** yazın (bu adım şart!).
4. Chat ID'nizi bulmak için:
   ```bash
   python main.py --telegram-chat-id
   ```
   Çıkan `TELEGRAM_CHAT_ID=...` satırını `.env` dosyasına kopyalayın.

### 8) Self-test çalıştırın ✅
```bash
python main.py --selftest
```
Kontrol edilenler: ayarlar, risk kuralları (kâr al / zarar kes / izleyen stop / kill-switch
senaryoları), Binance bağlantısı, deneme taraması, 11 USDT sanal bakiye ile canlı fiyattan
örnek al-sat, API anahtarı izinleri ve Telegram. Sonuçlar Telegram'a da gönderilir.

### 9) Test modunda (paper trading) çalıştırın 🧪
```bash
python main.py
```
`.env` içinde `TRADING_MODE=paper` olduğu sürece bot **11 USDT sanal bakiye** ile canlı
verileri kullanarak al-sat yapar; gerçek emir göndermez. Telegram mesajlarının başında
`🧪 [TEST MODU]` yazar. Durdurmak için **Ctrl + C**.

Birkaç gün test modunda çalıştırıp sonuçları izlemenizi öneririz.
İşlem geçmişi: `data/trades_paper.csv` (Excel ile açılabilir).

### 10) Canlı moda geçiş 💵 (gerçek para)
1. **Binance API anahtarı oluşturun:** Binance → Profil → **API Yönetimi** → API Oluştur.
   - ✅ **Enable Reading** (Okuma)
   - ✅ **Enable Spot & Margin Trading** (Spot işlem)
   - ❌ **Enable Withdrawals** (Para çekme) — **ASLA açmayın!**
   - Mümkünse **IP kısıtlaması** ekleyin (evinizin IP adresi).
2. Anahtarları `.env` içindeki `BINANCE_API_KEY` ve `BINANCE_API_SECRET` satırlarına yazın.
3. Self-test'i tekrar çalıştırın (API izinlerini kontrol eder): `python main.py --selftest`
4. Canlı modu başlatın:
   ```bash
   python main.py --mode live
   ```
   Bot sizden onay için **EVET** yazmanızı ister. (Kalıcı olarak canlı mod için `.env`
   içinde `TRADING_MODE=live` yapabilirsiniz.)

> ℹ️ Bot **binance.com** (Global) API'sini kullanır. Binance TR (trbinance) farklı bir
> borsadır ve desteklenmez.

---

## 🧭 Komutlar

| Komut | Açıklama |
|---|---|
| `python main.py` | `.env`'deki moda göre çalışır (varsayılan: test) |
| `python main.py --mode paper` | Test modu (sanal 11 USDT) |
| `python main.py --mode live` | Canlı mod (gerçek para, onay ister) |
| `python main.py --selftest` | Tüm sistemi test eder |
| `python main.py --telegram-chat-id` | Telegram chat ID'nizi bulur |
| `python main.py --reset-state` | Kayıtlı durumu sıfırlar (kill-switch kilidi dahil) |
| `Ctrl + C` | Botu durdurur |

---

## 📈 Strateji nasıl çalışır?

**Her 30 saniyede bir:**
1. **Ön eleme:** Binance'teki tüm USDT pariteleri arasından son 24 saatte **düşmüş**
   (−%2 ile −%30 arası), yeterli **hacmi** (≥ 3M USDT) ve **oynaklığı** olan coinler seçilir.
   Stabil coinler ve 11 USDT ile işlem yapmaya uygun olmayan pariteler elenir.
2. **Ayrıntılı analiz (5 dk mumlar):**
   - Son 3 saatte en az **%3'lük bir düşüş** (dip) olmuş,
   - Dip en az 10 dk önce oluşmuş ve fiyat **toparlanmaya başlamış** (düşüşün %15-60'ı geri alınmış),
   - **RSI** dipte aşırı satımdaydı (≤35) ve şimdi **yükseliyor** (40-65),
   - Fiyat **EMA(9)** üzerinde, son mum yeşil, toparlanma mumlarında **hacim** artmış.
3. **Puanlama:** Düşüş derinliği, kalan toparlanma alanı, hacim, RSI ivmesi ve oynaklığa göre
   en iyi aday seçilir ve alım yapılır.
4. **Piyasa filtresi:** BTC son 1 saatte %1.5'ten fazla düşmüşse (genel çöküş) alım yapılmaz.

**Pozisyon açıkken (her 5 saniyede):** Net kâr/zarar hesaplanır; kâr al, zarar kes,
izleyen stop ve kill-switch koşulları kontrol edilir.

### 💸 Komisyon ve "toz" hesabı
- Binance alışta komisyonu **aldığınız coinden** keser (%0.1), satışta **USDT'den** keser (%0.1).
- Satılabilecek miktar Binance'in LOT adımına yuvarlanır; artan çok küçük kısım ("toz")
  cüzdanda kalır. Bot bunu da zarar olarak hesaba katar ve tozu büyük olacak pariteleri
  (örn. 11 USDT ile BTC) hiç seçmez.
- Bu yüzden fiyat tam %15 artınca değil, komisyonlar karşılandıktan sonra **net %15** olunca satılır
  (fiyat olarak yaklaşık %15.2-15.3 artış gerekir).

### ☠️ Kill-switch
Toplam (gerçekleşen + açık pozisyondaki) zarar 5 USDT'ye ulaştığında bot açık pozisyonu satar,
Telegram'a bildirir ve kapanır. Bu durum `data/` klasörüne kaydedilir; botu yeniden başlatsanız
bile **kilitli** kalır. Bilerek tekrar başlatmak için:
```bash
python main.py --reset-state
```

### ⏱️ 15-60 dakika hakkında
Strateji, kısa sürede toparlanma potansiyeli olan oynak coinleri seçer. Ancak %15'lik kâr hedefi
iddialı bir hedeftir; bazı işlemler 60 dakikadan uzun sürebilir. İsterseniz `.env` içinde
`MAX_HOLD_MINUTES=60` yaparak pozisyonun en fazla 60 dk tutulmasını sağlayabilirsiniz
(varsayılan: kapalı, yani sadece kâr al / zarar kes / izleyen stop ile çıkılır).

---

## ⚙️ Önemli ayarlar (`.env`)

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` = test, `live` = gerçek |
| `START_CAPITAL_USDT` | `11` | Botun yöneteceği sermaye |
| `MAX_TOTAL_LOSS_USDT` | `5` | Kill-switch zarar limiti |
| `TAKE_PROFIT_PCT` | `15` | Net kâr hedefi (%) |
| `STOP_LOSS_PCT` | `10` | Net zarar limiti (%) |
| `TRAILING_ACTIVATION_PCT` | `10` | İzleyen stop başlangıcı (%) |
| `TRAILING_FLOOR_PCT` | `5` | İzleyen stop satış seviyesi (%) |
| `TRAILING_DYNAMIC` | `false` | `true`: koruma seviyesi zirve kârın yarısına çıkar |
| `FEE_RATE` | `0.001` | Komisyon oranı (%0.1) |
| `SCAN_INTERVAL_SEC` | `30` | Tarama aralığı (sn) |
| `POSITION_CHECK_SEC` | `5` | Açık pozisyon kontrol aralığı (sn) |
| `STATUS_REPORT_MINUTES` | `60` | Telegram durum raporu aralığı (0 = kapalı) |
| `MAX_HOLD_MINUTES` | `0` | Maks. pozisyon süresi (0 = sınırsız) |

Tüm ayarlar ve açıklamaları `.env.example` dosyasındadır.

> Sermaye/kural ayarlarını değiştirdikten sonra temiz bir başlangıç için
> `python main.py --reset-state` çalıştırın (kayıtlı durum başlangıç sermayesini hatırlar).

---

## 📁 Dosya yapısı

```
tradebot/
├── main.py            # Giriş noktası (komut satırı, modlar)
├── config.py          # .env ayarlarını okur ve doğrular
├── trader.py          # Ana döngü: tarama, alım, pozisyon takibi, kill-switch
├── strategy.py        # Düşüş sonrası toparlanma tarayıcısı
├── indicators.py      # RSI, EMA hesaplamaları
├── risk.py            # Kâr al / zarar kes / izleyen stop / boyutlandırma (komisyon dahil)
├── exchange.py        # Binance kuralları, canlı borsa ve sanal (paper) borsa
├── binance_client.py  # Binance REST API istemcisi
├── telegram_bot.py    # Telegram bildirimleri ve emojili mesaj şablonları
├── state.py           # Kalıcı durum (data/state_*.json) ve işlem geçmişi (CSV)
├── selftest.py        # Self-test
├── logger.py          # Log ayarları (logs/bot.log)
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
| `Timestamp ... recvWindow` (-1021) | Bilgisayar saatinizi senkronize edin (Windows: Ayarlar → Saat → "Şimdi eşitle") |
| `Invalid API-key, IP, or permissions` (-2015) | Anahtarı, izinleri ve IP kısıtlamasını kontrol edin |
| `Service unavailable from a restricted location` | Binance bulunduğunuz konumdan API erişimine izin vermiyor |
| Kill-switch kilidi | `python main.py --reset-state` |
| Bot hiç alım yapmıyor | Normal olabilir: kriterlere uyan fırsat yoksa bekler. Loglarda "Tarama:" satırlarına bakın |

**Önemli:** Bot sadece bilgisayarınız açıkken ve internete bağlıyken çalışır. Pozisyon açıkken
bilgisayar uyku moduna geçerse kâr al / zarar kes çalışmaz. Windows'ta **Ayarlar → Sistem →
Güç** bölümünden uyku modunu kapatın. Bot tekrar başlatıldığında açık pozisyonu kaldığı yerden
takip eder.

---

## 🧪 Geliştiriciler için: otomatik testler
```bash
pip install pytest
python -m pytest
```
