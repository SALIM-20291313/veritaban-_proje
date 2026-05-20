# ✈️ Antigravity - Gemini 3.5 Destekli Canlı Futbol Analiz ve Yönetim Sistemi

Ankara Üniversitesi Bilgisayar Mühendisliği Bölümü **BLM2058 Veritabanı Yönetim Sistemleri** dersi projesi kapsamında geliştirilmiş, canlı verilerle beslenen ve yapay zeka analitiği içeren ilişkisel bir veritabanı web uygulamasıdır.

Bu sistem, tasarlanan 7 tabloluk ilişkisel veri modelini (Teams, Players, Stadiums, Matches, Match Events, Transfers, Managers) fiziksel olarak MySQL üzerinde ayağa kaldırır, dinamik SQL sorgularıyla puan durumunu ve istatistikleri hesaplar, ve **Gemini 3.5** ile verileri analiz ederek kullanıcılara yapay zeka destekli Türkçe yorumlar sunar.

---

## 🏛️ Sistem Mimarisi

```
                          [ Canlı Futbol API ]
                                   │
                                   ▼ (JSON Data)
[ Frontend (Arayüz) ] ◄──► [ Backend (Python/Flask) ] ◄──► [ Veritabanı (MySQL) ]
  Tailwind CSS & Charts       "Antigravity Core"
                                   ▲
                                   │ (Prompt & Data Context)
                                   ▼
                        [ Gemini 3.5 API (AI Analyst) ]
```

1. **Veritabanı Katmanı:** ER modelindeki 7 ilişkisel tablo MySQL üzerinde fiziksel olarak oluşturulmuştur. Referential Integrity (Cascade/Set Null) kuralları uygulanmıştır.
2. **Backend (Antigravity Core):** Python + Flask. MySQL veritabanına bağlanır, verileri sorgular, simülasyonları yürütür ve Gemini API'si için bağlam (context) hazırlar.
3. **AI Analiz Katmanı:** Gemini API entegrasyonu ile SQL'den dönen puan durumunu, en pahalı transferleri ve teknik direktör galibiyet oranlarını analiz eder ve Türkçe doğal dilde yorum raporu üretir.
4. **Frontend Katmanı:** Tailwind CSS ile hazırlanmış, Chart.js grafik entegrasyonuna sahip karanlık modda çalışan dinamik bir yönetim paneli.

---

## 🚀 Proje Geliştirme ve Sunum Adımları (Milestones)

### 📅 Adım 1: Kavramsal Tasarımın Doğrulanması (20 Mayıs 2026)
* Aşama 1 ve Aşama 2 kapsamında hazırlanan 7 tabloluk (Takımlar, Oyuncular, Stadyumlar, Maçlar, Maç Olayları, Transferler, Teknik Direktörler) ER şeması doğrulandı.
* Tablolar arası `CASCADE` ve `SET NULL` gibi bilgi bütünlüğü (Referential Integrity) stratejileri netleştirildi.

### 📅 Adım 2: Fiziksel Veritabanı ve DDL Kurulumu (21 Mayıs 2026)
* İlişkisel modeli canlıya almak için MySQL uyumlu DDL (Data Definition Language) scriptleri yazıldı ve veritabanı ayağa kaldırıldı.
* Teknik Direktör - Takım arasındaki 1:1 kısıtı `UNIQUE` anahtarı ile, oyuncuların transfer geçmişi ise çoklu Foreign Key referansları ile veritabanı seviyesinde güvenceye alındı.

### 📅 Adım 3: Canlı API Entegrasyonu (21 Mayıs 2026)
* `API-Football` (REST API) entegrasyonu Flask mimarisine dahil edildi. Gerçek dünyadaki lig verileri ve anlık maç olayları (gol, kart vb.) canlı JSON istekleriyle çekilip lokal MySQL veritabanına asenkron olarak yazılmaya başlandı.
* API anahtarı olmadığında veya kısıtlı olduğunda sunumun aksamaması için "Canlı Rastgele Maç Olayları & Transfer Simülatörü" geliştirildi.

### 📅 Adım 4: Gemini 3.5 AI Entegrasyonu (22 Mayıs 2026)
* Projenin "Antigravity Core" yapısına **Gemini 3.5 API** entegrasyonu yapıldı. 
* SQL'den dönen ham istatistik ve finansal transfer verileri, Gemini 3.5 modeline "Context (Bağlam)" olarak beslendi. Yapay zekanın ham SQL çıktılarını yorumlayarak doğal dilde Türkçe analitik raporlar üretmesi sağlandı.

### 📅 Adım 5: Frontend Tasarımı ve Bulut Dağıtımı (23 Mayıs 2026)
* Tailwind CSS ve Chart.js kullanılarak dinamik bir Dashboard arayüzü kodlandı. Ham tabloların yanına Gemini 3.5 Analiz Kutusu eklendi.
* Projenin tüm kaynak kodları GitHub reposuna pushlandı ve web uygulaması bulut platformuna dağıtılarak canlıya alındı.

---

## 🛠️ Kullanılan Teknolojiler

- **Yapay Zeka Motoru:** Gemini 3.5 API (google-generativeai)
- **Veritabanı:** MySQL 8.0 (MySQL80 Windows Servisi)
- **Backend:** Python 3.11 / Flask
- **Arayüz:** HTML5, Tailwind CSS (CDN), Chart.js (CDN)
- **Paket Yönetimi:** pip (python-dotenv, mysql-connector-python, markdown)

---

## 💻 Kurulum ve Çalıştırma Kılavuzu

### 1. Bağımlılıkların Kurulması
Terminalde proje klasörüne giderek gerekli Python kütüphanelerini kurun:
```bash
pip install -r requirements.txt
```

### 2. Çevre Değişkenlerinin Yapılandırılması
Proje dizininde yer alan `.env` dosyasını düzenleyin:
```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_mysql_password
DB_NAME=futbol_ligi
GEMINI_API_KEY=your_gemini_api_key_here
```

### 3. Uygulamanın Başlatılması
Flask sunucusunu ayağa kaldırmak için terminalde şu komutu çalıştırın:
```bash
python app.py
```
Sunucu çalıştıktan sonra tarayıcınızda [http://localhost:5000](http://localhost:5000) adresine gidin.

*Not: Eğer veritabanı bağlantısı kurulamıyorsa, tarayıcıda otomatik olarak **Kurulum Sihirbazı** açılacaktır. Buradan MySQL şifrenizi girip bağlantıyı doğrulayabilirsiniz. Bağlantı sağlandığında tablolar (`schema.sql`) otomatik oluşturulacak ve lige örnek başlangıç verileri (6 Türk takımı, oyuncuları, teknik direktörleri, maçları ve transferleri) yüklenecektir.*
