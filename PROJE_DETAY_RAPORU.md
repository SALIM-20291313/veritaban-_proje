# ScoutX Pro - Kapsamlı Proje Raporu

## 1. Proje Tanımı ve Amacı
**ScoutX Pro**, futbol dünyasındaki dinamikleri dijital bir veritabanı altyapısıyla buluşturan, yapay zeka destekli, modern bir lig yönetimi ve scouting (yetenek avcılığı) platformudur. Ankara Üniversitesi Bilgisayar Mühendisliği Bölümü **BLM2058 Veritabanı Yönetim Sistemleri** dersi kapsamında geliştirilmiştir.

Projenin temel amacı; takımların, oyuncuların, teknik direktörlerin ve maç istatistiklerinin salt bir veri yığını olmaktan çıkarılıp ilişkisel bir veritabanı (RDBMS) üzerinde anlamlı bilgilere dönüştürülmesidir. Buna ek olarak entegre Yapay Zeka (AI) sistemi ile verilerin yorumlanması ve teknik analizlerin insan dilinde raporlanması hedeflenmiştir.

---

## 2. Kullanılan Teknolojiler ve Mimari
Proje, tamamen modern web standartları ve sağlam bir backend altyapısı üzerine inşa edilmiştir.

* **Veritabanı:** MySQL 8.0 (İlişkisel Veritabanı Yönetim Sistemi)
* **Backend:** Python 3.11 ve Flask Framework
* **Frontend:** HTML5, Tailwind CSS (Typography eklentisi ile birlikte) ve Vanilla JavaScript
* **Grafik & Görselleştirme:** Chart.js
* **Yapay Zeka (AI):** DeepSeek API (Gelişmiş Prompt Engineering ve Markdown formatında veri görselleştirmesi)
* **Veri İletişimi:** AJAX Fetch API (Asenkron JSON veri transferi)

---

## 3. Veritabanı Tasarımı (ER Modeli)
Proje, veritabanı normalizasyon kurallarına (3NF) uygun olarak tasarlanmış 7 ana tablodan oluşur:

1. **Takimlar:** Ligdeki futbol kulüplerinin temel bilgileri (Kuruluş, Şehir vb.)
2. **Stadyumlar:** Kulüplerin oynadığı stadyumların kapasite ve zemin verileri. (Takimlar ile 1:1 veya 1:N ilişki)
3. **Oyuncular:** Futbolcuların demografik verileri (Yaş, Mevki, Uyruk vb.). Takim_ID ile `Takimlar` tablosuna (N:1) bağlıdır.
4. **Teknik_Direktorler:** Takımların hocaları. Bir takımın sadece bir hocası olabilir (1:1 ilişki, UNIQUE Constraint ile güvence altına alınmıştır).
5. **Maclar:** Fikstürdeki karşılaşmalar. Ev_Sahibi ve Deplasman olarak `Takimlar` tablosuna çifte referans içerir.
6. **Mac_Olaylari:** Maçlarda yaşanan dakika bazlı olaylar (Sarı/Kırmızı Kart, Gol, Asist). `Oyuncular` ve `Maclar` tablolarına N:1 bağlıdır.
7. **Transferler:** Oyuncuların eski takımından yeni takımına geçişlerindeki ekonomik verileri (Bonservis) barındırır.

> **Veri Bütünlüğü (Referential Integrity):** Tablolar arası ilişkilerde `ON DELETE CASCADE` ve `ON DELETE SET NULL` gibi kısıtlamalar kullanılarak, bir takım silindiğinde ona bağlı oyuncuların takım ID'lerinin sıfırlanması veya maçların silinmesi gibi senaryolar SQL seviyesinde güvenceye alınmıştır.

---

## 4. Gelişmiş Özellikler ve Modüller

### 4.1. Dinamik Lig Durumu ve Canlı Maç Simülasyonu
Sistem arka planda SQL sorgularıyla takımların attığı gol, yediği gol, averaj ve puanlarını hesaplayarak canlı bir puan durumu oluşturur. "Maç Simüle Et" butonu, veritabanına rastgele yeni maç skorları ve maç olayları enjekte ederek tüm ligin (gol krallığı dahil) anında güncellenmesini sağlar.

### 4.2. Karşılaştırma Modülü (Compare Engine)
Kullanıcılar; Oyuncuları, Takımları veya Teknik Direktörleri seçerek detaylı karşılaştırma paneline alabilir. 
* **Takım Karşılaştırması:** Seçilen takımların kadro genişliği, yaş ortalaması, puanları ve kart disiplinleri yan yana gösterilir.
* **Oyuncu Karşılaştırması:** Gol, asist oranları ve maç başı verimlilikleri kıyaslanır.

### 4.3. Yapay Zeka (AI) Scout Analisti
Projenin en yenilikçi yanlarından biridir. Sistemdeki ham SQL istatistikleri (puanlar, transferler, golcüler) derlenip, arka plandaki DeepSeek Yapay Zeka modeline "Context" olarak beslenir.
* AI modeli, *Tiki-taka*, *Gegenpressing* gibi futbol jargonu kullanarak rapor üretmek üzere özel Prompt (talimat) ile eğitilmiştir.
* Yanıtlar düz metin değil; emoji, ASCII bar grafikleri, Markdown tablolar ve alıntılar kullanılarak Tailwind Typography eklentisiyle görsel bir spor dergisi formatında render edilir.

### 4.4. Dinamik SQL Görüntüleyici (Live SQL)
Projenin akademik amacına uygun olarak, arayüzdeki hemen hemen her istatistik tablosunun ve karşılaştırma panelinin yanında **"SQL"** butonu yer alır. Bu buton, ekranda görünen verilerin arka planda hangi karmaşık `JOIN`, `GROUP BY`, veya alt sorgular (Subquery) ile çekildiğini anlık olarak öğrenciye veya kullanıcıya gösterir.

### 4.5. Yönetim (CRUD) Paneli
Kapsamlı bir arayüz ile kullanıcı veritabanındaki her bir satıra müdahale edebilir. Tüm Ekleme (Create), Okuma (Read), Güncelleme (Update) ve Silme (Delete) işlemleri anlık olarak AJAX üzerinden yapılır ve sayfa yenilenmesine gerek kalmadan listeler güncellenir.

---

## 5. Kurulum Süreci
Sistem geliştirici ve kullanıcı dostu bir yaklaşımla tasarlanmıştır.

1. Proje bağımlılıkları `pip install -r requirements.txt` ile kurulur.
2. `python app.py` komutuyla sunucu ayağa kaldırılır.
3. Tarayıcıdan `http://localhost:5000` adresine girildiğinde, sistem veritabanı bağlantısını kontrol eder.
4. Eğer bağlantı yoksa, ekranda şık bir **Kurulum Sihirbazı (Setup)** açılır ve MySQL bilgileri istenir.
5. Bilgiler girildiğinde sistem otomatik olarak `futbol_ligi` veritabanını oluşturur, tabloları inşa eder ve 6 takım, düzinelerce oyuncu, maç ve transferden oluşan örnek "Seed Data"yı veritabanına işleyerek sistemi saniyeler içinde kullanıma hazır hale getirir.

---

## 6. Sonuç
ScoutX Pro, salt bir veritabanı ödevinin ötesine geçerek; modern web teknolojileri (Tailwind, AJAX), ilişkisel veritabanı mimarisi (MySQL) ve ileri düzey yapay zeka (DeepSeek AI) entegrasyonunu harmanlayan profesyonel çapta bir yönetim paneli (Dashboard) uygulaması olmayı başarmıştır. Gerek kullanıcı deneyimi (UX) gerekse arka plandaki sorgu verimliliği ile eksiksiz bir mimari sunmaktadır.
