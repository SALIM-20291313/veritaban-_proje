-- Futbol Ligi ve İstatistik Yönetim Sistemi DDL Script

CREATE DATABASE IF NOT EXISTS futbol_ligi CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE futbol_ligi;

-- 1. Stadyumlar (Stadiums)
CREATE TABLE IF NOT EXISTS Stadyumlar (
    Stadyum_ID INT AUTO_INCREMENT,
    Ad VARCHAR(100) NOT NULL UNIQUE,
    Kapasite INT NOT NULL,
    Sehir VARCHAR(80) NOT NULL,
    PRIMARY KEY (Stadyum_ID)
) ENGINE=InnoDB;

-- 2. Takımlar (Teams)
CREATE TABLE IF NOT EXISTS Takimlar (
    Takim_ID INT AUTO_INCREMENT,
    Ad VARCHAR(100) NOT NULL UNIQUE,
    Kurulus_Yili YEAR NOT NULL,
    Sehir VARCHAR(80) NOT NULL,
    Stadyum_ID INT NULL,
    PRIMARY KEY (Takim_ID),
    FOREIGN KEY (Stadyum_ID) REFERENCES Stadyumlar(Stadyum_ID) ON DELETE SET NULL
) ENGINE=InnoDB;

-- 3. Oyuncular (Players)
CREATE TABLE IF NOT EXISTS Oyuncular (
    Oyuncu_ID INT AUTO_INCREMENT,
    Ad VARCHAR(60) NOT NULL,
    Soyad VARCHAR(60) NOT NULL,
    Dogum_Tarihi DATE NOT NULL,
    Uyruk VARCHAR(50) NOT NULL,
    Mevki ENUM('KL', 'DF', 'OS', 'FV') NOT NULL,
    Takim_ID INT NULL,
    PRIMARY KEY (Oyuncu_ID),
    FOREIGN KEY (Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE SET NULL,
    UNIQUE KEY uq_oyuncu (Ad, Soyad, Dogum_Tarihi)
) ENGINE=InnoDB;

-- 4. Maçlar (Matches)
CREATE TABLE IF NOT EXISTS Maclar (
    Mac_ID INT AUTO_INCREMENT,
    Ev_Sahibi_Takim_ID INT NOT NULL,
    Deplasman_Takim_ID INT NOT NULL,
    Tarih_Saat DATETIME NOT NULL,
    Stadyum_ID INT NULL,
    Ev_Sahibi_Skor TINYINT NULL DEFAULT NULL,
    Deplasman_Skor TINYINT NULL DEFAULT NULL,
    PRIMARY KEY (Mac_ID),
    FOREIGN KEY (Ev_Sahibi_Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE CASCADE,
    FOREIGN KEY (Deplasman_Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE CASCADE,
    FOREIGN KEY (Stadyum_ID) REFERENCES Stadyumlar(Stadyum_ID) ON DELETE SET NULL
) ENGINE=InnoDB;

-- 5. Maç Olayları (Match Events)
CREATE TABLE IF NOT EXISTS Mac_Olaylari (
    Olay_ID INT AUTO_INCREMENT,
    Mac_ID INT NOT NULL,
    Oyuncu_ID INT NOT NULL,
    Olay_Tipi ENUM('Gol', 'Asist', 'Sari_Kart', 'Kirmizi_Kart', 'KKG') NOT NULL,
    Dakika TINYINT UNSIGNED NOT NULL,
    PRIMARY KEY (Olay_ID),
    FOREIGN KEY (Mac_ID) REFERENCES Maclar(Mac_ID) ON DELETE CASCADE,
    FOREIGN KEY (Oyuncu_ID) REFERENCES Oyuncular(Oyuncu_ID) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 6. Transfer Geçmişi (Transfers)
CREATE TABLE IF NOT EXISTS Transferler (
    Transfer_ID INT AUTO_INCREMENT,
    Oyuncu_ID INT NOT NULL,
    Eski_Takim_ID INT NULL,
    Yeni_Takim_ID INT NULL,
    Tarih DATE NOT NULL,
    Bonservis_Bedeli DECIMAL(15,2) NOT NULL,
    Para_Birimi VARCHAR(3) NOT NULL DEFAULT 'EUR',
    PRIMARY KEY (Transfer_ID),
    FOREIGN KEY (Oyuncu_ID) REFERENCES Oyuncular(Oyuncu_ID) ON DELETE CASCADE,
    FOREIGN KEY (Eski_Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE SET NULL,
    FOREIGN KEY (Yeni_Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE SET NULL
) ENGINE=InnoDB;

-- 7. Teknik Direktörler (Managers)
CREATE TABLE IF NOT EXISTS Teknik_Direktorler (
    Direktor_ID INT AUTO_INCREMENT,
    Ad VARCHAR(60) NOT NULL,
    Soyad VARCHAR(60) NOT NULL,
    Takim_ID INT NULL UNIQUE, -- 1:1 ilişkiyi garanti etmek için UNIQUE eklenmiştir
    PRIMARY KEY (Direktor_ID),
    FOREIGN KEY (Takim_ID) REFERENCES Takimlar(Takim_ID) ON DELETE SET NULL
) ENGINE=InnoDB;
