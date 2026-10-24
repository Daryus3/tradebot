# 🖥️ Botu bir sunucuda (VPS) 7/24 çalıştırma — sıfırdan, adım adım

Bu rehber VPS ve GitHub'ı **hiç kullanmamış** biri için yazıldı. Acele etmeyin: her adımın sonunda bir
**✅ Kontrol** var. Orada yazanı görmediyseniz durun ve ekranda yazanı Claude'a gönderin
(içinde şifre veya token varsa önce o kısmı silin).

İlk kurulum yaklaşık **1 saat** sürer. Sunucunun maliyeti aylık yaklaşık **4-7 € / $**.

---

## 📚 Başlamadan önce: dört kavram

- **VPS (sanal sunucu):** İnternette kiraladığınız, hiç kapanmayan küçük bir bilgisayar. Ekranı ve faresi
  yoktur; Windows bilgisayarınızdan ona **yazı komutlarıyla** bağlanırsınız. Bu bağlantının adı **SSH**'tır.
  Bot orada çalışırken kendi bilgisayarınızı kapatabilirsiniz.
- **GitHub:** Botun kodunun internetteki kopyası. Kodu sunucuya oradan **tek bir komutla** indireceğiz.
  GitHub hesabı açmanıza gerek yok.
- **systemd:** Sunucunun (Ubuntu'nun) "bu programı hep açık tut" sistemi. Bot çökerse veya sunucu yeniden
  başlarsa botu kendisi tekrar açar.
- **İki ayar dosyası:** Bütün ayarlarınız (strateji, kâr al, zarar kes, filtreler...) `ayarlar.env` dosyasında
  **hazır olarak** GitHub'dan gelir. Gizli bilgiler (Telegram token'ı, chat ID, ileride Binance anahtarı)
  GitHub'a **konmaz**; onları sunucuda `.env` adlı küçük bir dosyaya siz yazacaksınız (Adım 6).

Rehberde iki tür pencere kullanacağız:

| Pencere | Satırın başında görünen | Komutlar nerede çalışır? |
|---|---|---|
| Sunucuya bağlı PowerShell | `root@...:~#`, `ubuntu@...:~$` veya `tradebot@...:~$` | **Sunucuda** |
| Normal PowerShell | `PS C:\Users\inyer>` | **Bilgisayarınızda** |

> 💡 **Komut nasıl yapıştırılır?** Rehberdeki gri kutudaki komutu kopyalayın (kutunun sağ üstündeki
> kopyalama düğmesi), PowerShell penceresine **sağ tıklayın** (sağ tık = yapıştır), sonra **Enter**'a basın.
> Bir komut bitmeden (satırın başında yeniden `#`, `$` veya `PS ...>` çıkmadan) yenisini yapıştırmayın.

---

## Adım 1 — Güvenlik: sızan anahtarları iptal edin (telefon / bilgisayar, 10 dk)

Daha önce sohbete gönderdiğiniz `.env` dosyasında gerçek anahtarlarınız vardı. Bunları değiştiriyoruz.

**1.1 Binance API anahtarını silin.**
Binance (uygulama veya web) → Profil → **API Yönetimi** → anahtarın yanındaki **Sil** (Delete).
Test modunda bot API anahtarı kullanmaz; yenisini şimdilik **oluşturmayın**. Canlıya geçerken birlikte,
yalnızca sunucunun IP adresinden çalışacak şekilde oluştururuz.

**1.2 Telegram token'ını yenileyin.**
Telegram'da **@BotFather**'a yazın: `/revoke` → listeden botunuzu seçin. BotFather size **yeni bir token**
verir (`1234567890:AAH...` gibi uzun bir yazı). Eski token artık çalışmaz. Chat ID'niz değişmez.

**1.3 Chat ID'nizi bulun.** Bilgisayarınızda `C:\Users\inyer\PycharmProjects\TRADE2` klasöründeki `.env`
dosyasına sağ tıklayın → **Birlikte aç** → **Not Defteri**. `TELEGRAM_CHAT_ID=` satırındaki sayıyı not edin
(örn. `TELEGRAM_CHAT_ID=1234567890` ise `1234567890`). Dosyayı **değiştirmeden** kapatın.

**✅ Kontrol:** İki şeyi not ettiniz: BotFather'ın verdiği **yeni token** ve **chat ID**'niz. Bunları
kimseyle (Claude dahil) paylaşmayın.

---

## Adım 2 — Bir VPS kiralayın (15 dk)

Birkaç bilinen sağlayıcı (hangisini seçtiğiniz fark etmez, hepsinde adımlar benzer):

- **Hetzner Cloud** (hetzner.com/cloud) — genelde en ucuzu (~4-5 € / ay)
- **DigitalOcean** (digitalocean.com) — ~6 $ / ay
- **Vultr** (vultr.com) — ~5-6 $ / ay

E-posta ve kartla üye olun (bazıları kimlik doğrulaması isteyebilir). Sonra **yeni sunucu oluştur**
düğmesine basın (adı sağlayıcıya göre değişir: *Create Server*, *Create Droplet*, *Deploy Instance*) ve
şunları seçin:

| Seçenek | Ne seçmeli |
|---|---|
| Konum (*Location / Region*) | **Frankfurt (Almanya)**. ⚠️ **ABD'deki bir konumu seçmeyin** — Binance ABD'den gelen bağlantıları engeller |
| İşletim sistemi (*Image / OS*) | **Ubuntu 24.04 LTS** (yoksa 22.04 LTS) |
| Boyut (*Plan / Type / Size*) | **En küçük / en ucuz** paket (1 işlemci, 1-2 GB RAM botun ihtiyacından fazlası) |
| Giriş yöntemi (*Authentication*) | **Password** (şifre) — kendiniz belirliyorsanız güçlü bir şifre yazıp bir yere not edin. Sağlayıcı şifreyi e-postayla gönderiyorsa o e-postayı saklayın |
| Diğer her şey (yedek, ek disk, IPv6...) | Olduğu gibi bırakın |

**✅ Kontrol:** Panelde sunucunuz **Running** (çalışıyor) görünüyor ve bir **IP adresi** var: noktalarla
ayrılmış dört sayı, örneğin `203.0.113.25`. Bu adresi not edin.
**Bundan sonra `SUNUCU_IP` gördüğünüz her yere bu adresi yazacaksınız.**

---

## Adım 3 — Sunucuya bağlanın (5 dk)

**3.1** Windows'ta **Başlat**'a tıklayın, `PowerShell` yazın ve **Windows PowerShell**'i açın.

**3.2** Şunu yazın ve Enter'a basın. `KULLANICI` yerine sağlayıcının verdiği kullanıcı adını (panelde veya
e-postada yazar; çoğunlukla `root` ya da `ubuntu`), `SUNUCU_IP` yerine sunucunun adresini yazın:

```
ssh KULLANICI@SUNUCU_IP
```

Örnek: `ssh ubuntu@203.0.113.25` veya `ssh root@203.0.113.25`.

**3.3** İlk seferde şunu sorar:
`Are you sure you want to continue connecting (yes/no/[fingerprint])?`
→ `yes` yazıp Enter. ("Bu sunucuyu tanıyorum" demektir; yalnızca bir kez sorulur.)

**3.4** `password:` yazınca sunucu şifrenizi girip Enter'a basın.
⚠️ **Şifreyi yazarken ekranda hiçbir şey görünmez** (yıldız bile çıkmaz). Bu normaldir; yazmaya devam edin.
Kopyaladıysanız sağ tık ile yapıştırabilirsiniz.

**3.5** Bazı sağlayıcılar ilk girişte şifre değiştirmenizi ister (*You are required to change your password*):
önce mevcut şifreyi, sonra yeni şifreyi iki kez yazın.

**✅ Kontrol:** Satırın başında `ubuntu@...:~$` veya `root@...:~#` gibi bir yazı var. Artık bu pencereye
yazdığınız her komut **sunucuda** çalışır. Bu pencereyi açık bırakın.

> 🔑 **`sudo` hakkında:** Rehberdeki yönetici komutları `sudo` ile başlar ("bunu yönetici olarak yap"). İlk
> `sudo` komutunda `[sudo] password for ubuntu:` diye **kendi giriş şifrenizi** sorabilir (yine görünmez);
> yazıp Enter'a basın. Birkaç dakika boyunca tekrar sormaz. `root` olarak bağlandıysanız hiç sormaz.
>
> Bağlantı koparsa ya da pencereyi kapatırsanız aynı `ssh KULLANICI@SUNUCU_IP` komutuyla yeniden bağlanırsınız.

---

## Adım 4 — Sunucuyu hazırlayın (10 dk)

Komutları **sırayla, tek tek** yapıştırın.

**4.1 Güncellemeleri yükleyin** (birkaç dakika sürer, ekrandan çok yazı akar):

```
sudo apt update && sudo apt -y upgrade
```

Mor/mavi bir pencere açılıp bir şey sorarsa (örneğin hangi servislerin yeniden başlatılacağı) **Enter**'a
basarak geçin.

**4.2 Gerekli programları kurun** (Python ve Git):

```
sudo apt -y install python3 python3-venv git
```

**4.3 Saat dilimini Türkiye yapın** (günlük rapor 23:55'te Türkiye saatiyle gelsin; sunucunun varsayılan
saati UTC'dir, Türkiye'nin 3 saat gerisi):

```
sudo timedatectl set-timezone Europe/Istanbul
```

**4.4 Bot için ayrı bir kullanıcı oluşturun.** Bot, sunucunun tam yetkili yöneticisi olan `root` olarak
çalışmasın; bir sorun olursa etkisi sınırlı kalsın:

```
sudo useradd -m -s /bin/bash tradebot
```

**✅ Kontrol:**

```
python3 --version
```

`Python 3.12...` (veya `3.10...`) yazmalı.

---

## Adım 5 — Botu indirip kurun (5 dk)

**5.1 Bot kullanıcısına geçin:**

```
sudo -iu tradebot
```

Satırın başı `tradebot@...:~$` olur. Şifre sorarsa kendi giriş şifrenizdir (`tradebot` kullanıcısının
şifresi yoktur; `su - tradebot` bu yüzden çalışmaz).

**5.2 Kodu GitHub'dan indirin:**

```
git clone -b claude/crypto-trading-bot-larh4t https://github.com/Daryus3/tradebot.git
```

Bu, botun en güncel halini `tradebot` adlı bir klasöre kopyalar. Kullanıcı adı veya şifre **sormaz**;
sorarsa adresi yanlış yazmışsınızdır.

**5.3 Klasöre girin, "sanal ortam" oluşturun ve kütüphaneleri yükleyin.** Sanal ortam (`.venv`), botun
kullandığı kütüphaneleri sunucunun geri kalanından ayrı tutan bir kutudur:

```
cd ~/tradebot
```

```
python3 -m venv .venv
```

```
.venv/bin/pip install -r requirements.txt
```

Son komutun sonunda `Successfully installed ... python-dotenv ... requests ... websocket-client ...` yazar.

**✅ Kontrol:**

```
ls
```

Listede `main.py`, `ayarlar.env` (sizin ayarlarınız), `requirements.txt` ve `deploy` görünmeli.

**5.4 Kendi kullanıcınıza geri dönün:**

```
exit
```

Satırın başı yine `ubuntu@...:~$` (veya `root@...:~#`) olur.

---

## Adım 6 — Bilgisayardaki botu kapatın ve gizli bilgileri sunucuya yazın (10 dk)

**6.1 Bilgisayarınızdaki botu kapatın.** İki bot aynı Telegram token'ıyla çalışırsa birbirini bozar.

- Bot penceresi açıksa içinde **Ctrl+C**'ye basın (veya pencereyi kapatın).
- Windows açılışında kendiliğinden başlamasın: klavyede **Windows tuşu + R** → `shell:startup` yazıp Enter →
  açılan klasörde `kripto_bot_baslat.bat` varsa **silin**.

**6.2 Token'ı kopyalamaya hazırlanın.** Token uzun olduğu için elle yazmak yerine kopyalayın: bilgisayarda
Telegram'ı açın (**web.telegram.org** veya Telegram Desktop) ve BotFather'ın gönderdiği mesajı bulun.

**6.3 `.env` dosyasını oluşturun.** Sunucuya bağlı pencerede (`ubuntu@...` veya `root@...`):

```
sudo nano /home/tradebot/tradebot/.env
```

Pencere bir metin düzenleyiciye (**nano**) dönüşür: üstte dosya adı, altta `^G Help  ^O Write Out ...` gibi
kısayollar görünür. Fare burada çalışmaz; **ok tuşlarıyla** gezinirsiniz.

1. Şu dört satırı kopyalayıp pencereye **sağ tıklayarak** yapıştırın:

   ```
   BINANCE_API_KEY=
   BINANCE_API_SECRET=
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_CHAT_ID=
   ```

2. **Ok tuşlarıyla** `TELEGRAM_BOT_TOKEN=` satırının en sonuna gidin. Telegram'dan token'ı kopyalayıp
   **sağ tıklayın**; `=` işaretinin hemen arkasına yapışır (arada boşluk olmasın).
3. Bir alt satıra, `TELEGRAM_CHAT_ID=` satırının sonuna gidip chat ID'nizi yazın.
4. İlk iki satır (Binance) **boş kalır**: test modunda gerekmez.
5. Kaydedin: **Ctrl+O** → altta `File Name to Write: ...` yazar → **Enter**.
6. Çıkın: **Ctrl+X**.

**6.4 (İsteğe bağlı) Test geçmişinizi taşıyın.** Atlarsanız bot sunucuda 32 USDT sanal bakiyeyle sıfırdan
başlar. Taşımak için **ikinci bir PowerShell penceresi** açın (Başlat → PowerShell; bu pencere sunucuya değil
**bilgisayarınıza** komut verir) ve yazın (`KULLANICI` ve `SUNUCU_IP` yerine kendi bilgileriniz, şifreyi sorar):

```
scp -r "C:\Users\inyer\PycharmProjects\TRADE2\data" KULLANICI@SUNUCU_IP:
```

(Sondaki `:` işareti önemli: klasör sunucuda kendi ev klasörünüze kopyalanır.) Sonra sunucuya bağlı pencerede
bot klasörüne taşıyın:

```
sudo cp -r ~/data /home/tradebot/tradebot/
```

**6.5 Sunucuya bağlı pencerede** dosyaların sahibini ve izinlerini ayarlayın.
Dosyaları `sudo` (yönetici) oluşturduğu için sahibi root oldu; onları bot kullanıcısına veriyoruz. `.env`'yi de yalnızca
bot kullanıcısının okuyabileceği hale getiriyoruz:

```
sudo chown -R tradebot:tradebot /home/tradebot/tradebot
```

```
sudo chmod 600 /home/tradebot/tradebot/.env
```

```
sudo chmod 700 /home/tradebot
```

**✅ Kontrol:**

```
sudo ls -l /home/tradebot/tradebot/.env
```

Satır `-rw------- 1 tradebot tradebot` ile başlamalı.

---

## Adım 7 — Test edin (2 dk)

```
sudo -iu tradebot bash -c 'cd ~/tradebot && .venv/bin/python main.py --selftest'
```

**✅ Kontrol:**

- **Binance bağlantısı ✅**, **Telegram ✅** ve telefonunuza "🧪 Self-Test Sonuçları" mesajı gelir.
- **Risk motoru ✅** (ayarlarınızla denedim, 9/9 geçiyor).
- **Binance API anahtarı ⚠️ Tanımlı değil** — normal; test modunda gerekmez.

**Sorun varsa:**

- **Binance bağlantısı ❌** ve hata içinde `restricted location` veya `451` geçiyorsa: sunucunun konumu
  Binance tarafından engelleniyor. Sağlayıcının panelinden sunucuyu silin ve başka bir Avrupa konumunda
  yeniden oluşturun (Adım 2'den devam).
- **Telegram ❌**: `.env`'deki `TELEGRAM_BOT_TOKEN` veya `TELEGRAM_CHAT_ID` yanlış. `sudo nano /home/tradebot/tradebot/.env`
  ile açıp düzeltin (Ctrl+O, Enter, Ctrl+X) ve testi tekrarlayın.

---

## Adım 8 — Botu servis olarak başlatın (3 dk)

Botla birlikte gelen servis dosyası (`deploy/tradebot.service`) sistemin servis klasörüne kopyalanır ve bot
başlatılır. Komutları tek tek yapıştırın:

```
sudo cp /home/tradebot/tradebot/deploy/tradebot.service /etc/systemd/system/
```

```
sudo systemctl daemon-reload
```

```
sudo systemctl enable --now tradebot
```

```
sudo systemctl status tradebot --no-pager
```

Ne yaptılar:

1. Servis dosyasını kopyaladı.
2. Sisteme yeni dosyayı okuttu.
3. Botu **şimdi** başlattı ve **sunucu her açıldığında** başlayacak şekilde ayarladı.
4. Botun durumunu gösterdi.

**✅ Kontrol:** Çıktıda yeşil **`Active: active (running)`** yazıyor ve Telegram'a
"🤖 Kripto Al-Sat Botu Başlatıldı" mesajı geliyor.

Servis botu **test modunda** (`--mode paper`) çalıştırır: gerçek emir göndermez.

---

## Adım 9 — Kendiliğinden kalktığını deneyin (5 dk)

**9.1 Çökmeyi taklit edin:**

```
sudo systemctl kill -s SIGKILL tradebot
```

20 saniye bekleyin, sonra:

```
sudo systemctl status tradebot --no-pager
```

Yine **`active (running)`** olmalı ve Telegram'a yeni bir "Başlatıldı" mesajı gelmeli.

**9.2 Sunucuyu yeniden başlatın:**

```
sudo reboot
```

Bağlantınız kopar (normal). 1-2 dakika bekleyin, `ssh KULLANICI@SUNUCU_IP` ile yeniden bağlanın ve:

```
sudo systemctl status tradebot --no-pager
```

Yine **`active (running)`** olmalı.

🎉 **Bu iki test geçtiyse kurulum bitti.** Artık bilgisayarınızı kapatabilirsiniz; bot sunucuda çalışır.
Telegram komutları (`/durum`, `/tarama`, `/pozisyon`...) eskisi gibi çalışır; çoğu zaman sunucuya
bağlanmanız bile gerekmez.

---

## 📋 Günlük kullanım (kopya kâğıdı)

Önce sunucuya bağlanın: normal PowerShell'de `ssh KULLANICI@SUNUCU_IP`.

| Ne istiyorsunuz? | Komut |
|---|---|
| Botun canlı loglarını izlemek | `sudo journalctl -u tradebot -f` (çıkmak için **Ctrl+C** — bot durmaz) |
| Son 100 satır log | `sudo journalctl -u tradebot -n 100 --no-pager` |
| Bot çalışıyor mu? | `sudo systemctl status tradebot --no-pager` |
| Botu durdurmak | `sudo systemctl stop tradebot` |
| Botu başlatmak | `sudo systemctl start tradebot` |
| Botu yeniden başlatmak | `sudo systemctl restart tradebot` |

**Botun kendiliğinden yeniden başlayıp başlamaması:**

| Durum | Bot ne yapar? |
|---|---|
| Çökerse | 15 saniye sonra kendiliğinden açılır |
| Sunucu yeniden başlarsa | Kendiliğinden açılır |
| `sudo systemctl stop` veya Telegram `/kapat` | Kapalı kalır (`sudo systemctl start tradebot` ile açarsınız) |
| Kill-switch tetiklenirse | Kapalı kalır (aşağıya bakın) |
| 15 dakikada 5 kez çökerse | Denemeyi bırakır: `sudo systemctl reset-failed tradebot && sudo systemctl start tradebot` |

### Ayar değiştirmek

Ayarlarınız `ayarlar.env` dosyasında. **Bu dosyayı sunucuda düzenlemeyin**: güncelleme sırasında çakışır.
Bunun yerine değiştirmek istediğiniz satırı `.env`'ye ekleyin; `.env`'deki değer her zaman geçerli olur.
Örneğin aynı anda 3 pozisyon için:

```
sudo nano /home/tradebot/tradebot/.env
```

En alta `MAX_OPEN_POSITIONS=3` satırını ekleyin → **Ctrl+O**, **Enter** → **Ctrl+X**. Sonra:

```
sudo systemctl restart tradebot
```

Telegram'da `/ayarlar` yazarak yeni değeri görebilirsiniz. Ayarın kalıcı olarak `ayarlar.env`'ye (GitHub'a)
yazılmasını isterseniz Claude'a söyleyin; sonra aşağıdaki güncelleme adımlarını yapın.

### Botu güncellemek (yeni bir özellik eklendiğinde)

```
sudo systemctl stop tradebot
```

```
sudo -iu tradebot bash -c 'cd ~/tradebot && git pull && .venv/bin/pip install -r requirements.txt'
```

```
sudo systemctl start tradebot
```

### Kill-switch tetiklenirse

Bot her şeyi satar, kapanır ve kendiliğinden açılmaz. Nedenini anladıktan sonra:

```
sudo -iu tradebot bash -c 'cd ~/tradebot && .venv/bin/python main.py --mode paper --reset-state --yes'
```

```
sudo systemctl start tradebot
```

### Sunucuda backtest

Bot çalışmaya devam ederken de yapılabilir (90 günlük filtre taraması birkaç dakika sürer):

```
sudo -iu tradebot bash -c 'cd ~/tradebot && .venv/bin/python main.py --backtest --gun 90 --sermaye 32 --tara-filtre'
```

---

## 🩺 Sık karşılaşılan sorunlar

| Gördüğünüz | Ne yapmalı |
|---|---|
| `ssh: connect to host ... Connection timed out` | IP yanlış ya da sunucu henüz açılmadı; paneli kontrol edin |
| `Permission denied (publickey)` | Sağlayıcı şifreyle girişe izin vermiyor. Panelde "root şifresini sıfırla" (*Reset root password*) kullanın ya da sunucuyu "Password" seçerek yeniden oluşturun |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | Sunucuyu silip yeniden kurduysanız normaldir. Bilgisayarınızda `ssh-keygen -R SUNUCU_IP` çalıştırıp tekrar bağlanın |
| `sudo systemctl status` → `failed` veya `activating (auto-restart)` | `sudo journalctl -u tradebot -n 50 --no-pager` çıktısını Claude'a gönderin |
| Telegram'a mesajlar iki kez geliyor, komutlar karışıyor | Bilgisayardaki bot hâlâ açık; kapatın (Adım 6.1) |
| `git clone` kullanıcı adı / şifre soruyor | Adres yanlış yazıldı; Adım 5.2'deki satırı aynen kopyalayın |

---

## 💵 Canlıya geçiş (şimdilik değil)

Backtestler kâr göstermediği için test modunda kalmanızı öneririm. Zamanı gelirse birlikte yapacağız:

1. Binance'te yeni bir API anahtarı oluşturmak: yalnızca okuma + spot işlem izni; **para çekme kapalı**;
   **yalnızca sunucunun IP adresinden** çalışacak şekilde.
2. Anahtarı `.env`'deki `BINANCE_API_KEY` / `BINANCE_API_SECRET` satırlarına yazmak.
3. `sudo nano /etc/systemd/system/tradebot.service` ile dosyayı açıp `--mode paper` yerine `--mode live --yes`
   yazmak.
4. `sudo systemctl daemon-reload && sudo systemctl restart tradebot` çalıştırmak.
