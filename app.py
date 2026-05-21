import os
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, redirect, url_for
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
import google.generativeai as genai
import markdown
from seed_data import STADIUMS, TEAMS, MANAGERS, PLAYERS, TRANSFERS_RAW, MATCHES_RAW, MATCH_EVENTS_RAW

# Load environment variables
load_dotenv()

app = Flask(__name__)

# DB Connection status flag
db_connected = False
db_error_msg = ""

def get_db_config():
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': int(os.getenv('DB_PORT', 3306)),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', ''),
        'database': os.getenv('DB_NAME', 'futbol_ligi'),
        'charset': 'utf8mb4',
        'use_pure': True
    }

def get_mysql_connection(use_db=True):
    config = get_db_config()
    if not use_db:
        # Connect without database first to create it
        config_no_db = config.copy()
        config_no_db.pop('database', None)
        return mysql.connector.connect(**config_no_db)
    return mysql.connector.connect(**config)

def test_db_connection():
    global db_connected, db_error_msg
    conn = None
    try:
        # First try to connect without database
        conn = get_mysql_connection(use_db=False)
        cursor = conn.cursor()
        cursor.execute("CREATE DATABASE IF NOT EXISTS futbol_ligi CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        conn.commit()
        cursor.close()
        conn.close()

        # Now connect to the database
        conn = get_mysql_connection(use_db=True)
        db_connected = True
        db_error_msg = ""
        return True
    except Error as e:
        db_connected = False
        db_error_msg = str(e)
        print(f"Database connection error: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()

# Try initial connection
test_db_connection()

def initialize_database():
    if not db_connected:
        return False
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Read and execute schema.sql
        schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
        if os.path.exists(schema_path):
            with open(schema_path, 'r', encoding='utf-8') as f:
                schema_sql = f.read()
            
            # Execute queries separated by semicolon
            # We filter out empty commands
            queries = [q.strip() for q in schema_sql.split(';') if q.strip()]
            for query in queries:
                if "CREATE DATABASE" in query or "USE futbol_ligi" in query:
                    continue
                cursor.execute(query)
            conn.commit()
            print("Database schema successfully initialized.")
            return True
        else:
            print("schema.sql not found.")
            return False
    except Error as e:
        print(f"Error during schema initialization: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()

def is_database_empty():
    if not db_connected:
        return True
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM Takimlar")
        count = cursor.fetchone()[0]
        cursor.close()
        return count == 0
    except Error:
        return True
    finally:
        if conn and conn.is_connected():
            conn.close()

def seed_database():
    if not db_connected:
        return False
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        for tbl in ["Teknik_Direktorler","Transferler","Mac_Olaylari","Maclar","Oyuncular","Takimlar","Stadyumlar"]:
            cursor.execute(f"TRUNCATE TABLE {tbl};")
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")

        # 1. Stadiums
        cursor.executemany("INSERT INTO Stadyumlar (Ad, Kapasite, Sehir) VALUES (%s,%s,%s)", STADIUMS)
        conn.commit()
        cursor.execute("SELECT Stadyum_ID, Ad FROM Stadyumlar")
        stad_map = {r[1]: r[0] for r in cursor.fetchall()}

        # 2. Teams
        cursor.executemany(
            "INSERT INTO Takimlar (Ad, Kurulus_Yili, Sehir, Stadyum_ID) VALUES (%s,%s,%s,%s)",
            [(tn, yr, ct, stad_map[st]) for tn, yr, ct, st in TEAMS]
        )
        conn.commit()
        cursor.execute("SELECT Takim_ID, Ad FROM Takimlar")
        team_map = {r[1]: r[0] for r in cursor.fetchall()}

        # 3. Managers
        cursor.executemany(
            "INSERT INTO Teknik_Direktorler (Ad, Soyad, Takim_ID) VALUES (%s,%s,%s)",
            [(ad, soyad, team_map[takim]) for ad, soyad, takim in MANAGERS]
        )
        conn.commit()

        # 4. Players
        pq = "INSERT INTO Oyuncular (Ad, Soyad, Dogum_Tarihi, Uyruk, Mevki, Takim_ID) VALUES (%s,%s,%s,%s,%s,%s)"
        for team_name, roster in PLAYERS.items():
            tid = team_map[team_name]
            for p in roster:
                cursor.execute(pq, (p[0], p[1], p[2], p[3], p[4], tid))
        conn.commit()

        cursor.execute("SELECT Oyuncu_ID, Ad, Soyad FROM Oyuncular")
        player_map = {f"{r[1]} {r[2]}": r[0] for r in cursor.fetchall()}

        # 5. Transfers
        tq = "INSERT INTO Transferler (Oyuncu_ID, Eski_Takim_ID, Yeni_Takim_ID, Tarih, Bonservis_Bedeli, Para_Birimi) VALUES (%s,%s,%s,%s,%s,%s)"
        for pname, old_t, new_t, tarih, bedel, cur in TRANSFERS_RAW:
            pid = player_map.get(pname)
            if pid is None:
                continue
            old_id = team_map.get(old_t) if old_t else None
            new_id = team_map.get(new_t)
            cursor.execute(tq, (pid, old_id, new_id, tarih, bedel, cur))
        conn.commit()

        # 6. Matches
        mq = "INSERT INTO Maclar (Ev_Sahibi_Takim_ID, Deplasman_Takim_ID, Tarih_Saat, Stadyum_ID, Ev_Sahibi_Skor, Deplasman_Skor) VALUES (%s,%s,%s,%s,%s,%s)"
        match_id_map = {}
        for home_t, away_t, dt_str, stad_name, hs, as_ in MATCHES_RAW:
            cursor.execute(mq, (team_map[home_t], team_map[away_t], dt_str, stad_map[stad_name], hs, as_))
            mid = cursor.lastrowid
            date_key = dt_str[:10]
            match_id_map[(home_t, away_t, date_key)] = mid
        conn.commit()

        # 7. Match Events
        eq = "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s,%s,%s,%s)"
        for (home_t, away_t, date_key), event_list in MATCH_EVENTS_RAW.items():
            mid = match_id_map.get((home_t, away_t, date_key))
            if mid is None:
                continue
            for pname, etype, minute in event_list:
                pid = player_map.get(pname)
                if pid:
                    cursor.execute(eq, (mid, pid, etype, minute))
        conn.commit()

        print(f"Database seeded: {sum(len(v) for v in PLAYERS.values())} players, {len(TRANSFERS_RAW)} transfers, {len(MATCHES_RAW)} matches.")
        return True
    except Error as e:
        print(f"Seed error: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()

# Auto init & seed if database is connected and empty
if db_connected:
    initialize_database()
    if is_database_empty():
        seed_database()

# ----------------- Endpoints & Routes -----------------

@app.route('/')
def index():
    global db_connected, db_error_msg
    test_db_connection()
    if not db_connected:
        return render_template('setup.html', error=db_error_msg, config=get_db_config())
    return render_template('index.html')

@app.route('/setup')
def setup_page():
    return render_template('setup.html', error=db_error_msg, config=get_db_config())

@app.route('/api/save-config', methods=['POST'])
def save_config():
    global db_connected, db_error_msg
    
    # Get form parameters
    host = request.form.get('host', 'localhost')
    port = request.form.get('port', '3306')
    user = request.form.get('user', 'root')
    password = request.form.get('password', '')
    dbname = request.form.get('dbname', 'futbol_ligi')
    gemini_key = request.form.get('gemini_key', '')
    rapid_key = request.form.get('rapid_key', '')

    # Write back to .env
    try:
        env_content = f"""DB_HOST={host}
DB_PORT={port}
DB_USER={user}
DB_PASSWORD={password}
DB_NAME={dbname}
GEMINI_API_KEY={gemini_key}
RAPIDAPI_KEY={rapid_key}
"""
        with open('.env', 'w', encoding='utf-8') as f:
            f.write(env_content)
        
        # Reload environment
        load_dotenv(override=True)
        
        # Test connection
        if test_db_connection():
            initialize_database()
            if is_database_empty():
                seed_database()
            return redirect(url_for('index'))
        else:
            return render_template('setup.html', error=f"Baglanti Basarisiz: {db_error_msg}", config=get_db_config())
    except Exception as e:
        return render_template('setup.html', error=f"Dosya yazma hatasi: {e}", config=get_db_config())

@app.route('/api/stats')
def get_stats():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # 1. Puan Tablosu (Standings) Query
        standings_query = """
        SELECT 
            t.Takim_ID,
            t.Ad AS Takim_Ad,
            COUNT(m.Mac_ID) AS Oynanan_Mac,
            SUM(CASE 
                WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                     (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 
                ELSE 0 
            END) AS Galibiyet,
            SUM(CASE 
                WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 
                ELSE 0 
            END) AS Beraberlik,
            SUM(CASE 
                WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor < m.Deplasman_Skor) OR 
                     (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor < m.Ev_Sahibi_Skor) THEN 1 
                ELSE 0 
            END) AS Maglubiyet,
            SUM(CASE 
                WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Ev_Sahibi_Skor 
                WHEN m.Deplasman_Takim_ID = t.Takim_ID THEN m.Deplasman_Skor 
                ELSE 0 
            END) AS Atilan_Gol,
            SUM(CASE 
                WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Deplasman_Skor 
                WHEN m.Deplasman_Takim_ID = t.Takim_ID THEN m.Ev_Sahibi_Skor 
                ELSE 0 
            END) AS Yenilen_Gol,
            SUM(CASE 
                WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                     (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 3 
                WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 
                ELSE 0 
            END) AS Puan
        FROM Takimlar t
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID)
        AND (m.Ev_Sahibi_Skor IS NOT NULL AND m.Deplasman_Skor IS NOT NULL)
        GROUP BY t.Takim_ID, t.Ad
        ORDER BY Puan DESC, (Atilan_Gol - Yenilen_Gol) DESC, Atilan_Gol DESC;
        """
        cursor.execute(standings_query)
        standings = cursor.fetchall()
        
        for team in standings:
            team['Averaj'] = int(team.get('Atilan_Gol') or 0) - int(team.get('Yenilen_Gol') or 0)
            
            cursor.execute("""
                SELECT Ev_Sahibi_Takim_ID, Deplasman_Takim_ID, Ev_Sahibi_Skor, Deplasman_Skor
                FROM Maclar
                WHERE (Ev_Sahibi_Takim_ID = %s OR Deplasman_Takim_ID = %s)
                AND Ev_Sahibi_Skor IS NOT NULL AND Deplasman_Skor IS NOT NULL
                ORDER BY Tarih_Saat DESC, Mac_ID DESC
                LIMIT 5
            """, (team['Takim_ID'], team['Takim_ID']))
            recent_matches = cursor.fetchall()
            
            form = []
            for rm in recent_matches:
                if rm['Ev_Sahibi_Takim_ID'] == team['Takim_ID']:
                    if rm['Ev_Sahibi_Skor'] > rm['Deplasman_Skor']: form.append('G')
                    elif rm['Ev_Sahibi_Skor'] < rm['Deplasman_Skor']: form.append('M')
                    else: form.append('B')
                else:
                    if rm['Deplasman_Skor'] > rm['Ev_Sahibi_Skor']: form.append('G')
                    elif rm['Deplasman_Skor'] < rm['Ev_Sahibi_Skor']: form.append('M')
                    else: form.append('B')
            
            form.reverse()
            team['form_durumu'] = form
        
        # 2. Gol Kralligi Query (Haftalık ve Sezonluk)
        scorers_query = """
        SELECT 
            o.Oyuncu_ID,
            t.Takim_ID,
            o.Ad,
            o.Soyad,
            t.Ad AS Takim_Ad,
            SUM(CASE WHEN m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 ELSE 0 END) AS Haftalik_Gol,
            COUNT(mo.Olay_ID) AS Sezonluk_Gol
        FROM Oyuncular o
        JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
        JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        WHERE mo.Olay_Tipi = 'Gol'
        GROUP BY o.Oyuncu_ID, t.Takim_ID, o.Ad, o.Soyad, t.Ad
        ORDER BY Haftalik_Gol DESC, Sezonluk_Gol DESC
        LIMIT 10;
        """
        cursor.execute(scorers_query)
        scorers = cursor.fetchall()
        
        # Casting Decimal to int for JSON serialization
        for scorer in scorers:
            scorer['Haftalik_Gol'] = int(scorer['Haftalik_Gol']) if scorer['Haftalik_Gol'] is not None else 0
            scorer['Sezonluk_Gol'] = int(scorer['Sezonluk_Gol']) if scorer['Sezonluk_Gol'] is not None else 0
        
        # 3. Asist Kralligi Query
        assists_query = """
        SELECT 
            o.Oyuncu_ID,
            t.Takim_ID,
            o.Ad,
            o.Soyad,
            t.Ad AS Takim_Ad,
            COUNT(mo.Olay_ID) AS Asist_Sayisi
        FROM Oyuncular o
        JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        WHERE mo.Olay_Tipi = 'Asist'
        GROUP BY o.Oyuncu_ID, t.Takim_ID, o.Ad, o.Soyad, t.Ad
        ORDER BY Asist_Sayisi DESC
        LIMIT 10;
        """
        cursor.execute(assists_query)
        assists = cursor.fetchall()
        
        # 4. Transfer Harcamalari (Chart.js icin takim bazli bonservis)
        transfer_query = """
        SELECT 
            t.Ad AS Takim_Ad,
            SUM(tr.Bonservis_Bedeli) AS Toplam_Harcama
        FROM Takimlar t
        LEFT JOIN Transferler tr ON t.Takim_ID = tr.Yeni_Takim_ID
        GROUP BY t.Takim_ID, t.Ad
        ORDER BY Toplam_Harcama DESC;
        """
        cursor.execute(transfer_query)
        transfers_chart = cursor.fetchall()
        
        # 5. En Son Transferler
        recent_transfers_query = """
        SELECT 
            tr.Transfer_ID,
            o.Oyuncu_ID,
            o.Ad AS Oyuncu_Ad,
            o.Soyad AS Oyuncu_Soyad,
            o.Mevki,
            t_eski.Ad AS Eski_Takim,
            t_yeni.Ad AS Yeni_Takim,
            tr.Yeni_Takim_ID,
            tr.Tarih,
            tr.Bonservis_Bedeli,
            tr.Para_Birimi
        FROM Transferler tr
        JOIN Oyuncular o ON tr.Oyuncu_ID = o.Oyuncu_ID
        LEFT JOIN Takimlar t_eski ON tr.Eski_Takim_ID = t_eski.Takim_ID
        LEFT JOIN Takimlar t_yeni ON tr.Yeni_Takim_ID = t_yeni.Takim_ID
        ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC
        LIMIT 10;
        """
        cursor.execute(recent_transfers_query)
        recent_transfers_raw = cursor.fetchall()
        
        recent_transfers = []
        for t in recent_transfers_raw:
            eski_takim = t['Eski_Takim'] if t['Eski_Takim'] else 'Serbest'
            
            bonservis_val = float(t['Bonservis_Bedeli'] or 0.0)
            if bonservis_val == 0.0:
                bonservis_format = "Bedelsiz"
            else:
                formatted_num = f"{bonservis_val:,.0f}".replace(',', '.')
                bonservis_format = f"{formatted_num} {t['Para_Birimi']}"
                
            recent_transfers.append({
                "transfer_id": t['Transfer_ID'],
                "oyuncu_id": t['Oyuncu_ID'],
                "oyuncu_ad_soyad": f"{t['Oyuncu_Ad']} {t['Oyuncu_Soyad']}",
                "mevki": t['Mevki'],
                "eski_takim": eski_takim,
                "yeni_takim": t['Yeni_Takim'],
                "yeni_takim_id": t['Yeni_Takim_ID'],
                "tarih": str(t['Tarih']),
                "bonservis_raw": bonservis_val,
                "bonservis_format": bonservis_format
            })

        # 6. Teknik Direktorler Basari Tablosu
        managers_query = """
        SELECT 
            td.Direktor_ID,
            t.Takim_ID,
            td.Ad,
            td.Soyad,
            t.Ad AS Takim_Ad,
            COUNT(m.Mac_ID) AS Toplam_Mac,
            SUM(CASE 
                WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                     (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 
                ELSE 0 
            END) AS Galibiyetler,
            ROUND(
                (SUM(CASE 
                    WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                         (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 
                    ELSE 0 
                END) / NULLIF(COUNT(m.Mac_ID), 0)) * 100, 2
            ) AS Galibiyet_Yuzdesi
        FROM Teknik_Direktorler td
        JOIN Takimlar t ON td.Takim_ID = t.Takim_ID
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID)
        AND (m.Ev_Sahibi_Skor IS NOT NULL AND m.Deplasman_Skor IS NOT NULL)
        GROUP BY td.Direktor_ID, t.Takim_ID, td.Ad, td.Soyad, t.Ad
        ORDER BY Galibiyet_Yuzdesi DESC, Toplam_Mac DESC;
        """
        cursor.execute(managers_query)
        managers = cursor.fetchall()

        # 7. Tum Maclar (Son 15 Mac)
        all_matches_query = """
        SELECT 
            m.Mac_ID AS mac_id,
            t_ev.Ad AS ev_sahibi,
            t_dep.Ad AS deplasman,
            m.Tarih_Saat AS tarih_saat,
            m.Ev_Sahibi_Skor AS ev_sahibi_skor,
            m.Deplasman_Skor AS deplasman_skor,
            s.Stadyum_ID AS stadyum_id,
            s.Ad AS stadyum
        FROM Maclar m
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        LEFT JOIN Stadyumlar s ON m.Stadyum_ID = s.Stadyum_ID
        ORDER BY m.Tarih_Saat DESC, m.Mac_ID DESC
        LIMIT 15;
        """
        cursor.execute(all_matches_query)
        matches_raw = cursor.fetchall()
        
        matches = []
        now = datetime.now()
        for row in matches_raw:
            match_time = row['tarih_saat']
            if isinstance(match_time, str):
                try:
                    match_time = datetime.strptime(match_time, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    match_time = datetime.fromisoformat(match_time)
                    
            time_diff = (now - match_time).total_seconds() / 60.0
            
            if row['ev_sahibi_skor'] is not None and row['deplasman_skor'] is not None:
                if time_diff > 105:
                    durum = "Oynandı"
                else:
                    durum = "Devam Ediyor"
                skor = f"{row['ev_sahibi_skor']} - {row['deplasman_skor']}"
            else:
                if time_diff > 0:
                    durum = "Devam Ediyor"
                    skor = "0 - 0"
                else:
                    durum = "Başlamadı"
                    skor = "vs"
                
            matches.append({
                "mac_id": row['mac_id'],
                "ev_sahibi": row['ev_sahibi'],
                "deplasman": row['deplasman'],
                "tarih_saat": match_time.strftime('%Y-%m-%dT%H:%M:%S'),
                "stadyum_id": row['stadyum_id'],
                "stadyum": row['stadyum'] or "Bilinmiyor",
                "skor_gosterim": skor,
                "mac_durumu": durum
            })

        # 8. Haftalık Öne Çıkanlar (Highlights)
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad, COUNT(mo.Olay_ID) AS Gol_Sayisi
            FROM Mac_Olaylari mo
            JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Gol' AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY o.Oyuncu_ID, m.Mac_ID, o.Ad, o.Soyad, t.Ad
            HAVING Gol_Sayisi >= 3
        """)
        hat_tricks = cursor.fetchall()
        
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad
            FROM Mac_Olaylari mo
            JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Kirmizi_Kart' AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        """)
        red_cards = cursor.fetchall()
        
        cursor.execute("""
            SELECT m.Mac_ID, t_ev.Ad AS Ev_Sahibi, t_dep.Ad AS Deplasman,
                   SUM(CASE WHEN mo.Olay_Tipi IN ('Sari_Kart', 'Kirmizi_Kart') THEN 1 ELSE 0 END) AS Toplam_Kart
            FROM Mac_Olaylari mo
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
            JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
            WHERE m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY m.Mac_ID, t_ev.Ad, t_dep.Ad
            ORDER BY Toplam_Kart DESC
            LIMIT 1
        """)
        agresif_mac = cursor.fetchone()

        weekly_highlights = {
            "hat_tricks": hat_tricks,
            "red_cards": red_cards,
            "agresif_mac": agresif_mac
        }

        cursor.close()
        return jsonify({
            "success": True,
            "standings": standings,
            "weekly_scorers": scorers,
            "assists": assists,
            "transfers_chart": transfers_chart,
            "recent_transfers": recent_transfers,
            "managers": managers,
            "matches": matches,
            "weekly_highlights": weekly_highlights
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/simulate-match', methods=['POST'])
def simulate_match():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # 1. Select two random teams
        cursor.execute("SELECT Takim_ID, Ad, Stadyum_ID FROM Takimlar")
        teams = cursor.fetchall()
        if len(teams) < 2:
            return jsonify({"success": False, "error": "Ligde yeterli takim yok."}), 400
            
        home_team, away_team = random.sample(teams, 2)
        home_id, home_name, stadium_id = home_team
        away_id, away_name, _ = away_team
        
        # 2. Simulate score (weights for home advantage)
        home_score = random.choices([0, 1, 2, 3, 4, 5], weights=[15, 30, 25, 15, 10, 5])[0]
        away_score = random.choices([0, 1, 2, 3, 4], weights=[25, 35, 20, 15, 5])[0]
        
        match_time = datetime.now() - timedelta(days=random.randint(0, 3))
        
        # 3. Save match
        cursor.execute(
            "INSERT INTO Maclar (Ev_Sahibi_Takim_ID, Deplasman_Takim_ID, Tarih_Saat, Stadyum_ID, Ev_Sahibi_Skor, Deplasman_Skor) VALUES (%s, %s, %s, %s, %s, %s)",
            (home_id, away_id, match_time, stadium_id, home_score, away_score)
        )
        match_id = cursor.lastrowid
        
        # Get players for both teams
        cursor.execute("SELECT Oyuncu_ID, Ad, Soyad, Takim_ID, Mevki FROM Oyuncular WHERE Takim_ID IN (%s, %s)", (home_id, away_id))
        players = cursor.fetchall()
        
        home_players = [p for p in players if p[3] == home_id]
        away_players = [p for p in players if p[3] == away_id]
        
        events_summary = []
        
        # Helper to generate goals & assists
        def generate_goal_events(scoring_team_players, opposing_team_players, goals_count):
            for _ in range(goals_count):
                # Scorers are mostly forwards (FV) or midfielders (OS)
                fv_os = [p for p in scoring_team_players if p[4] in ('FV', 'OS')]
                all_scorers = fv_os if fv_os else scoring_team_players
                scorer = random.choice(all_scorers)
                minute = random.randint(1, 90)
                
                # Check for own goal (KKG) - 3% chance
                is_kkg = random.random() < 0.03
                if is_kkg and opposing_team_players:
                    scorer_og = random.choice(opposing_team_players)
                    cursor.execute(
                        "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s, %s, 'KKG', %s)",
                        (match_id, scorer_og[0], minute)
                    )
                    events_summary.append(f"{minute}' Kendi Kalesine Gol - {scorer_og[1]} {scorer_og[2]}")
                else:
                    cursor.execute(
                        "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s, %s, 'Gol', %s)",
                        (match_id, scorer[0], minute)
                    )
                    events_summary.append(f"{minute}' GOL! {scorer[1]} {scorer[2]}")
                    
                    # Assist chance - 70%
                    if random.random() < 0.70:
                        possible_assisters = [p for p in scoring_team_players if p[0] != scorer[0]]
                        if possible_assisters:
                            assister = random.choice(possible_assisters)
                            cursor.execute(
                                "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s, %s, 'Asist', %s)",
                                (match_id, assister[0], minute)
                            )
                            events_summary.append(f"{minute}' Asist: {assister[1]} {assister[2]}")
                            
        generate_goal_events(home_players, away_players, home_score)
        generate_goal_events(away_players, home_players, away_score)
        
        # Cards simulation (1 to 5 yellow cards, 0 to 1 red cards)
        yellow_cards = random.randint(1, 5)
        for _ in range(yellow_cards):
            card_player = random.choice(players)
            minute = random.randint(1, 90)
            cursor.execute(
                "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s, %s, 'Sari_Kart', %s)",
                (match_id, card_player[0], minute)
            )
            events_summary.append(f"{minute}' Sarı Kart: {card_player[1]} {card_player[2]} ({card_player[3]})")
            
        # Red card chance (15%)
        if random.random() < 0.15:
            red_player = random.choice(players)
            minute = random.randint(1, 90)
            cursor.execute(
                "INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika) VALUES (%s, %s, 'Kirmizi_Kart', %s)",
                (match_id, red_player[0], minute)
            )
            events_summary.append(f"{minute}' Kırmızı Kart! {red_player[1]} {red_player[2]} ({red_player[3]})")
            
        conn.commit()
        cursor.close()
        
        return jsonify({
            "success": True,
            "match": {
                "home": home_name,
                "away": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "events": events_summary
            }
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/simulate-transfer', methods=['POST'])
def simulate_transfer():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # 1. Find a random player and their current team
        cursor.execute("SELECT Oyuncu_ID, Ad, Soyad, Takim_ID FROM Oyuncular WHERE Takim_ID IS NOT NULL")
        players = cursor.fetchall()
        if not players:
            return jsonify({"success": False, "error": "Transfer edilecek oyuncu bulunamadi."}), 400
            
        player_id, p_ad, p_soyad, old_team_id = random.choice(players)
        
        # 2. Select a different destination team
        cursor.execute("SELECT Takim_ID, Ad FROM Takimlar WHERE Takim_ID != %s", (old_team_id,))
        other_teams = cursor.fetchall()
        if not other_teams:
            return jsonify({"success": False, "error": "Transfer yapilacak diger takim bulunamadi."}), 400
            
        new_team_id, new_team_name = random.choice(other_teams)
        
        cursor.execute("SELECT Ad FROM Takimlar WHERE Takim_ID = %s", (old_team_id,))
        old_team_name = cursor.fetchone()[0]
        
        # 3. Create transfer record
        value = float(random.randint(10, 80)) * 500000.0
        transfer_date = datetime.now().date()
        
        # Execute transfer insertion
        cursor.execute(
            "INSERT INTO Transferler (Oyuncu_ID, Eski_Takim_ID, Yeni_Takim_ID, Tarih, Bonservis_Bedeli, Para_Birimi) VALUES (%s, %s, %s, %s, %s, 'EUR')",
            (player_id, old_team_id, new_team_id, transfer_date, value)
        )
        
        # Update Player's active team
        cursor.execute(
            "UPDATE Oyuncular SET Takim_ID = %s WHERE Oyuncu_ID = %s",
            (new_team_id, player_id)
        )
        
        conn.commit()
        cursor.close()
        
        return jsonify({
            "success": True,
            "transfer": {
                "player": f"{p_ad} {p_soyad}",
                "from_team": old_team_name,
                "to_team": new_team_name,
                "fee": value,
                "currency": "EUR"
            }
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/reset-db', methods=['POST'])
def reset_database():
    test_db_connection()
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabanina baglanilamadi. Ayarlari kontrol edin."}), 500
    
    success = initialize_database()
    if success:
        seed_success = seed_database()
        if seed_success:
            return jsonify({"success": True, "message": "Veritabani sifirlandi ve ornek veriler yuklendi."})
    return jsonify({"success": False, "error": "Sifirlama sirasinda hata olustu."}), 500

@app.route('/api/ai-analysis')
def get_ai_analysis():
    # 1. Fetch current statistics context from DB
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabanı bağlantısı yok."}), 500
        
    conn = None
    stats_summary = {}
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # Get standings
        cursor.execute("""
            SELECT t.Ad AS Takim_Ad, 
            (SELECT SUM(CASE 
                WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                     (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 3 
                WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 
                ELSE 0 
            END) FROM Maclar m WHERE m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID) AS Puan
            FROM Takimlar t ORDER BY Puan DESC;
        """)
        standings = cursor.fetchall()
        stats_summary['standings'] = standings
        
        # Get scorers
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad, COUNT(mo.Olay_ID) AS Gol_Sayisi
            FROM Oyuncular o
            JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Gol'
            GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, t.Ad
            ORDER BY Gol_Sayisi DESC LIMIT 3;
        """)
        scorers = cursor.fetchall()
        stats_summary['scorers'] = scorers
        
        # Get transfers
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t_yeni.Ad AS Yeni_Takim, tr.Bonservis_Bedeli 
            FROM Transferler tr
            JOIN Oyuncular o ON tr.Oyuncu_ID = o.Oyuncu_ID
            JOIN Takimlar t_yeni ON tr.Yeni_Takim_ID = t_yeni.Takim_ID
            ORDER BY tr.Bonservis_Bedeli DESC LIMIT 3;
        """)
        transfers = cursor.fetchall()
        stats_summary['transfers'] = transfers

        # Get managers win rate
        cursor.execute("""
            SELECT td.Ad, td.Soyad, t.Ad AS Takim_Ad,
            ROUND(
                (SUM(CASE 
                    WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                         (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 
                    ELSE 0 
                END) / NULLIF(COUNT(m.Mac_ID), 0)) * 100, 2
            ) AS Galibiyet_Yuzdesi
            FROM Teknik_Direktorler td
            JOIN Takimlar t ON td.Takim_ID = t.Takim_ID
            LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID)
            AND (m.Ev_Sahibi_Skor IS NOT NULL AND m.Deplasman_Skor IS NOT NULL)
            GROUP BY td.Direktor_ID, td.Ad, td.Soyad, t.Ad
            ORDER BY Galibiyet_Yuzdesi DESC LIMIT 3;
        """)
        managers = cursor.fetchall()
        stats_summary['managers'] = managers
        
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad, COUNT(mo.Olay_ID) AS Gol_Sayisi
            FROM Mac_Olaylari mo
            JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Gol' AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY o.Oyuncu_ID, m.Mac_ID, o.Ad, o.Soyad, t.Ad
            HAVING Gol_Sayisi >= 3
        """)
        stats_summary['hat_tricks'] = cursor.fetchall()
        
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad
            FROM Mac_Olaylari mo
            JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Kirmizi_Kart' AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        """)
        stats_summary['red_cards'] = cursor.fetchall()
        
        cursor.execute("""
            SELECT m.Mac_ID, t_ev.Ad AS Ev_Sahibi, t_dep.Ad AS Deplasman,
                   SUM(CASE WHEN mo.Olay_Tipi IN ('Sari_Kart', 'Kirmizi_Kart') THEN 1 ELSE 0 END) AS Toplam_Kart
            FROM Mac_Olaylari mo
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
            JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
            WHERE m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY m.Mac_ID, t_ev.Ad, t_dep.Ad
            ORDER BY Toplam_Kart DESC
            LIMIT 1
        """)
        stats_summary['agresif_mac'] = cursor.fetchone()

        cursor.close()
    except Error as e:
        print(f"Error gathering stats for AI: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

    # Format data as text context
    context = "LIG PUAN DURUMU:\n"
    for idx, team in enumerate(stats_summary.get('standings', [])):
        puan = team.get('Puan') or 0
        context += f"{idx+1}. {team['Takim_Ad']}: {int(puan)} Puan\n"
        
    context += "\nGOL KRALLIGI (ILK 3):\n"
    for scorer in stats_summary.get('scorers', []):
        context += f"- {scorer['Ad']} {scorer['Soyad']} ({scorer['Takim_Ad']}): {scorer['Gol_Sayisi']} Gol\n"
        
    context += "\nEN PAHALI TRANSFERLER:\n"
    for tr in stats_summary.get('transfers', []):
        context += f"- {tr['Ad']} {tr['Soyad']} -> {tr['Yeni_Takim']}: {float(tr['Bonservis_Bedeli']):,.2f} EUR\n"
        
    context += "\nTEKNIK DIREKTOR GALIBIYET YUZDELERI (ILK 3):\n"
    for mgr in stats_summary.get('managers', []):
        pct = mgr.get('Galibiyet_Yuzdesi')
        pct_str = f"{pct}%" if pct is not None else "0.00%"
        context += f"- {mgr['Ad']} {mgr['Soyad']} ({mgr['Takim_Ad']}): {pct_str} galibiyet orani\n"

    context += "\nHAFTANIN ÖNE ÇIKAN SIRA DIŞI OLAYLARI (Hat-trickler ve Kartlar):\n"
    if stats_summary.get('hat_tricks'):
        for ht in stats_summary['hat_tricks']:
            context += f"- HAT-TRICK KAHRAMANI: {ht['Ad']} {ht['Soyad']} ({ht['Takim_Ad']}) tek macta {ht['Gol_Sayisi']} gol atti!\n"
    else:
        context += "- Bu hafta hat-trick yapan oyuncu olmadi.\n"
        
    if stats_summary.get('red_cards'):
        for rc in stats_summary['red_cards']:
            context += f"- KIRMIZI KART GOREN OYUNCU: {rc['Ad']} {rc['Soyad']} ({rc['Takim_Ad']}) takimini yalniz birakti.\n"
    else:
        context += "- Bu hafta kirmizi kart cikan oyuncu olmadi.\n"
        
    agresif = stats_summary.get('agresif_mac')
    if agresif:
        context += f"- HAFTANIN EN GERGIN MACI: {agresif['Ev_Sahibi']} vs {agresif['Deplasman']} (Toplam {int(agresif['Toplam_Kart'])} sari/kirmizi kart cikti!)\n"

    # 2. Call Gemini API
    api_key = os.getenv('GEMINI_API_KEY', '').strip()
    
    prompt = f"""
    Sen profesyonel bir Türk futbol analistisin. Sana verilen asagidaki ilişkisel veritabanı istatistiklerini incele ve bu haftanin lig durumunu, taktiksel basarisini, finansal harcamalarini ve teknik direktörlerin performansini yorumlayan taraftarlarin ve medyanin ilgisini cekecek bir rapor hazirla.
    
    Lütfen yanıtını UZUN PARAGRAFLAR YERİNE; kısa vurucu cümleler, emojiler, maddeler (bullet points) ve okuması keyifli alt başlıklar halinde tasarla. Kesinlikle sıkıcı blok metinler yazma. Sosyal medyada viral olacak tarzda, çok enerjik ve şık bir format kullan.
    
    Raporuna mutlaka ilgi çekici bir baslik ekle. Raporun icerisinde puan durumuna, gol kralligindaki isimlere, en pahali transfere ve en basarili teknik direktöre spesifik atiflarda bulun. Ayrica, sana verilen HAFTANIN ÖNE ÇIKAN SIRA DIŞI OLAYLARINI (Hat-trick yapan kahramanları, kırmızı kart görerek takımını yakan oyuncuları veya haftanın en gergin, en agresif maçını) da derinlemesine incele. Raporunda bu isimlere ve olaylara coşkulu, samimi, taraftarların ve medyanın ilgisini çekecek şekilde spesifik atıflarda bulun.
    
    Veritabanı İstatistikleri:
    {context}
    """
    
    if api_key:
        try:
            genai.configure(api_key=api_key)
            # Use gemini-1.5-flash as the fallback/active model
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            markdown_text = response.text
            html_text = markdown.markdown(markdown_text)
            return jsonify({
                "success": True,
                "analysis": html_text,
                "is_live_ai": True
            })
        except Exception as e:
            print(f"Gemini API Error, falling back to simulated analysis. Error: {e}")
            
    # Mock / Fallback Analysis Generator if key is missing or failed
    lider = stats_summary.get('standings', [{}])[0].get('Takim_Ad', 'Bilinmeyen Takım')
    lider_puan = stats_summary.get('standings', [{}])[0].get('Puan', 0)
    top_scorer = stats_summary.get('scorers', [{}])
    scorer_name = f"{top_scorer[0]['Ad']} {top_scorer[0]['Soyad']}" if top_scorer else "Bilinmeyen Oyuncu"
    scorer_goals = top_scorer[0]['Gol_Sayisi'] if top_scorer else 0
    top_trans = stats_summary.get('transfers', [{}])
    trans_name = f"{top_trans[0]['Ad']} {top_trans[0]['Soyad']}" if top_trans else "Bilinmeyen Oyuncu"
    trans_team = top_trans[0]['Yeni_Takim'] if top_trans else "Bilinmeyen Takım"
    trans_fee = float(top_trans[0]['Bonservis_Bedeli']) / 1000000.0 if top_trans else 0.0
    
    top_mgr = stats_summary.get('managers', [{}])
    mgr_name = f"{top_mgr[0]['Ad']} {top_mgr[0]['Soyad']}" if top_mgr else "Bilinmeyen Hoca"
    mgr_team = top_mgr[0]['Takim_Ad'] if top_mgr else "Bilinmeyen Takım"
    mgr_pct = top_mgr[0]['Galibiyet_Yuzdesi'] if top_mgr else 0.0
    
    ht_list = stats_summary.get('hat_tricks', [])
    ht_text = f"**{ht_list[0]['Ad']} {ht_list[0]['Soyad']}** ({ht_list[0]['Takim_Ad']}) muazzam bir hat-trick ile fileleri tam {ht_list[0]['Gol_Sayisi']} kez havalandırarak şov yaparken," if ht_list else "gol krallığı yarışında kıyasıya bir mücadele yaşanırken,"
    
    rc_list = stats_summary.get('red_cards', [])
    rc_text = f"**{rc_list[0]['Ad']} {rc_list[0]['Soyad']}** ({rc_list[0]['Takim_Ad']}) gördüğü kırmızı kartla takımını sahada adeta ateşe attı." if rc_list else "takımlar bu hafta disiplinli oyunlarıyla dikkat çekti."
    
    agresif = stats_summary.get('agresif_mac')
    agresif_text = f"Haftanın en agresif ve tansiyonu yüksek maçı ise **{agresif['Ev_Sahibi']} - {agresif['Deplasman']}** mücadelesiydi; hakem cebinden toplam {int(agresif['Toplam_Kart'])} kez kart çıkararak oyunu zor kontrol edebildi!" if agresif else "Hafta boyunca hakemler nispeten sakin maçlar yönetti."

    mock_html = f"""
    <div class="space-y-6">
        <div class="bg-gradient-to-r from-indigo-500/20 to-purple-500/20 border border-indigo-500/30 rounded-2xl p-6 text-center">
            <h3 class="text-2xl font-black text-white tracking-tight mb-2">⚽ Haftanın Panoraması</h3>
            <p class="text-indigo-300 text-sm font-medium">Antigravity AI Tarafından Hazırlanan Yapay Zeka Özeti</p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-slate-800/40 border border-slate-700/50 rounded-xl p-5 hover:bg-slate-800/60 transition">
                <div class="flex items-center space-x-3 mb-3">
                    <div class="w-8 h-8 rounded-full bg-emerald-500/20 flex items-center justify-center text-emerald-400">👑</div>
                    <h4 class="text-white font-bold">Liderlik Koltuğu</h4>
                </div>
                <p class="text-sm text-slate-300 leading-relaxed">
                    Ligde kıran kırana geçen haftaların ardından <strong class="text-emerald-400">{lider}</strong>, topladığı <strong class="text-white">{int(lider_puan)} puanla</strong> zirvedeki yerini perçinledi. Rakiplerin analiz duvarlarını paramparça ediyorlar!
                </p>
            </div>

            <div class="bg-slate-800/40 border border-slate-700/50 rounded-xl p-5 hover:bg-slate-800/60 transition">
                <div class="flex items-center space-x-3 mb-3">
                    <div class="w-8 h-8 rounded-full bg-amber-500/20 flex items-center justify-center text-amber-400">🔥</div>
                    <h4 class="text-white font-bold">Sıcak Gelişmeler</h4>
                </div>
                <p class="text-sm text-slate-300 leading-relaxed">
                    Sahada olağanüstü anlar yaşandı! {ht_text} Öte yandan {rc_text}
                </p>
            </div>
        </div>

        <div class="bg-slate-800/40 border border-slate-700/50 rounded-xl p-5">
            <div class="flex items-center space-x-3 mb-4">
                <div class="w-8 h-8 rounded-full bg-rose-500/20 flex items-center justify-center text-rose-400">⚡</div>
                <h4 class="text-white font-bold">Tansiyon ve Transfer</h4>
            </div>
            <ul class="space-y-3">
                <li class="flex items-start space-x-2 text-sm text-slate-300">
                    <span class="text-rose-400 mt-0.5">▪</span>
                    <span>{agresif_text}</span>
                </li>
                <li class="flex items-start space-x-2 text-sm text-slate-300">
                    <span class="text-indigo-400 mt-0.5">▪</span>
                    <span>Finansal cephede <strong class="text-white">{trans_name}</strong> transferinin <strong class="text-indigo-400">{trans_fee:.1f} Milyon EUR</strong> bedeliyle <strong class="text-white">{trans_team}</strong> kadrosuna katılması haftaya damga vurdu.</span>
                </li>
                <li class="flex items-start space-x-2 text-sm text-slate-300">
                    <span class="text-orange-400 mt-0.5">▪</span>
                    <span>Taktik dehaların savaşında <strong class="text-white">{mgr_team}</strong> teknik patronu <strong class="text-orange-400">{mgr_name}</strong>, <strong class="text-white">%{mgr_pct}</strong> galibiyet oranıyla ligin en efektif menajeri konumunda şov yapıyor!</span>
                </li>
            </ul>
        </div>
    </div>
    """
    
    return jsonify({
        "success": True,
        "analysis": mock_html,
        "is_live_ai": False,
        "notice": "Gemini API anahtarı ayarlanmadığı veya hata verdiği için simüle analiz üretildi."
    })

# ---- SCOUTING ENDPOINTS ----

@app.route('/api/scouting/players')
def scouting_players():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        query = """
        SELECT o.Oyuncu_ID, o.Ad, o.Soyad,
               TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS Yas,
               o.Uyruk, o.Mevki, t.Ad AS Takim_Ad, t.Takim_ID,
               COALESCE((SELECT tr.Bonservis_Bedeli FROM Transferler tr
                         WHERE tr.Oyuncu_ID = o.Oyuncu_ID
                         ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC LIMIT 1), 0.00) AS Son_Bonservis,
               SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) AS Toplam_Gol,
               SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) AS Toplam_Asist,
               SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) AS Toplam_Sari_Kart,
               SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) AS Toplam_Kirmizi_Kart,
               (SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) + 
                (SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) * 2)) / 
                NULLIF((SELECT COUNT(*) FROM Maclar m WHERE m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID), 0) AS Kart_Orani
        FROM Oyuncular o 
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        WHERE 1=1"""
        params = []
        mevki = request.args.get('mevki')
        if mevki and mevki != 'Tumu':
            query += " AND o.Mevki=%s"
            params.append(mevki)
        uyruk = request.args.get('uyruk')
        if uyruk:
            query += " AND o.Uyruk LIKE %s"
            params.append(f"%{uyruk}%")
        max_yas = request.args.get('max_yas')
        if max_yas:
            query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) <= %s"
            params.append(int(max_yas))
        min_yas = request.args.get('min_yas')
        if min_yas:
            query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) >= %s"
            params.append(int(min_yas))
        takim_id = request.args.get('takim_id')
        if takim_id and takim_id != 'Tumu':
            query += " AND o.Takim_ID = %s"
            params.append(int(takim_id))
        max_bonservis = request.args.get('max_bonservis')
        if max_bonservis:
            query += """ AND COALESCE((SELECT tr.Bonservis_Bedeli FROM Transferler tr
                         WHERE tr.Oyuncu_ID=o.Oyuncu_ID ORDER BY tr.Tarih DESC LIMIT 1),0) <= %s"""
            params.append(float(max_bonservis))
            
        query += " GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, Yas, o.Uyruk, o.Mevki, t.Ad, t.Takim_ID, Son_Bonservis HAVING 1=1"
        
        min_gol = request.args.get('min_gol')
        if min_gol:
            query += " AND Toplam_Gol >= %s"
            params.append(int(min_gol))
            
        min_asist = request.args.get('min_asist')
        if min_asist:
            query += " AND Toplam_Asist >= %s"
            params.append(int(min_asist))
            
        max_kart_orani = request.args.get('max_kart_orani')
        if max_kart_orani:
            query += " AND Kart_Orani <= %s"
            params.append(float(max_kart_orani))
            
        sort_by = request.args.get('sort_by')
        if sort_by == 'gol':
            query += " ORDER BY Toplam_Gol DESC, Son_Bonservis DESC"
        elif sort_by == 'asist':
            query += " ORDER BY Toplam_Asist DESC, Son_Bonservis DESC"
        elif sort_by == 'yas_asc':
            query += " ORDER BY Yas ASC, Son_Bonservis DESC"
        elif sort_by == 'yas_desc':
            query += " ORDER BY Yas DESC, Son_Bonservis DESC"
        elif sort_by == 'bonservis_asc':
            query += " ORDER BY Son_Bonservis ASC, Toplam_Gol DESC"
        elif sort_by == 'bonservis_desc':
            query += " ORDER BY Son_Bonservis DESC, Toplam_Gol DESC"
        else:
            query += " ORDER BY Son_Bonservis DESC, Toplam_Gol DESC, o.Soyad ASC"
            
        cursor.execute(query, tuple(params))
        return jsonify({"success": True, "players": cursor.fetchall()})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/managers')
def scouting_managers():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
        SELECT td.Direktor_ID, td.Ad, td.Soyad, t.Ad AS Takim_Ad, t.Takim_ID,
               COUNT(m.Mac_ID) AS Toplam_Mac,
               SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor)
                          OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor)
                    THEN 1 ELSE 0 END) AS Galibiyetler,
               SUM(CASE WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 ELSE 0 END) AS Beraberlikler,
               SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor<m.Deplasman_Skor)
                          OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor<m.Ev_Sahibi_Skor)
                    THEN 1 ELSE 0 END) AS Maglubiyetler,
               ROUND((SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor)
                                 OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor)
                           THEN 1 ELSE 0 END) / NULLIF(COUNT(m.Mac_ID),0))*100, 1) AS Galibiyet_Yuzdesi
        FROM Teknik_Direktorler td
        JOIN Takimlar t ON td.Takim_ID=t.Takim_ID
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID=t.Takim_ID OR m.Deplasman_Takim_ID=t.Takim_ID)
        AND m.Ev_Sahibi_Skor IS NOT NULL
        GROUP BY td.Direktor_ID, td.Ad, td.Soyad, t.Ad, t.Takim_ID
        HAVING (Galibiyet_Yuzdesi >= %s OR %s = 0) AND Toplam_Mac >= %s
        ORDER BY Galibiyet_Yuzdesi DESC""",
        (float(request.args.get('min_win_rate', 0)), float(request.args.get('min_win_rate', 0)), int(request.args.get('min_mac', 0))))
        return jsonify({"success": True, "managers": cursor.fetchall()})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/player-detail/<int:player_id>')
def player_detail(player_id):
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # Mükemmel JOIN yapısı: Oyuncu bilgileri ve Mac Olaylari toplamları
        cursor.execute("""
        SELECT o.Oyuncu_ID, o.Ad, o.Soyad, o.Dogum_Tarihi,
               TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS Yas,
               o.Uyruk, o.Mevki, t.Ad AS Takim_Ad,
               SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) AS Gol,
               SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) AS Asist,
               SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) AS Sari_Kart,
               SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) AS Kirmizi_Kart
        FROM Oyuncular o 
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = o.Oyuncu_ID
        WHERE o.Oyuncu_ID=%s
        GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, o.Dogum_Tarihi, Yas, o.Uyruk, o.Mevki, t.Ad
        """, (player_id,))
        player = cursor.fetchone()
        
        if not player:
            return jsonify({"success": False, "error": "Oyuncu bulunamadı."}), 404
            
        stats = {
            'Gol': player['Gol'],
            'Asist': player['Asist'],
            'Sari_Kart': player['Sari_Kart'],
            'Kirmizi_Kart': player['Kirmizi_Kart']
        }
        
        cursor.execute("""
        SELECT tr.Transfer_ID, tr.Tarih, tr.Bonservis_Bedeli, tr.Para_Birimi,
               IFNULL(t_e.Ad, 'Serbest Oyuncu') AS Eski_Takim,
               t_y.Ad AS Yeni_Takim
        FROM Transferler tr
        LEFT JOIN Takimlar t_e ON tr.Eski_Takim_ID=t_e.Takim_ID
        LEFT JOIN Takimlar t_y ON tr.Yeni_Takim_ID=t_y.Takim_ID
        WHERE tr.Oyuncu_ID=%s ORDER BY tr.Tarih ASC""", (player_id,))
        transfers_asc = cursor.fetchall()
        
        total_investment = 0.0
        max_value = 0.0
        current_value = 0.0
        value_trend = "Stabil"
        chart_labels = []
        chart_values = []
        
        for idx, tr in enumerate(transfers_asc):
            bedel = float(tr['Bonservis_Bedeli']) if tr['Bonservis_Bedeli'] else 0.0
            
            total_investment += bedel
            if bedel > max_value:
                max_value = bedel
                
            current_value = bedel
            
            if idx > 0:
                prev_bedel = float(transfers_asc[idx-1]['Bonservis_Bedeli']) if transfers_asc[idx-1]['Bonservis_Bedeli'] else 0.0
                if bedel > prev_bedel:
                    value_trend = "Yukseliste"
                elif bedel < prev_bedel:
                    value_trend = "Dususte"
                else:
                    value_trend = "Stabil"
            
            tarih_str = tr['Tarih'].strftime('%Y-%m-%d') if hasattr(tr['Tarih'], 'strftime') else str(tr['Tarih'])
            chart_labels.append(tarih_str)
            chart_values.append(bedel)
            
        financial_analysis = {
            "total_investment": total_investment,
            "max_value": max_value,
            "current_value": current_value,
            "value_trend": value_trend,
            "chart_data": {
                "labels": chart_labels,
                "values": chart_values
            }
        }
        
        transfers_desc = transfers_asc[::-1]
        
        cursor.execute("""
        SELECT 
            YEAR(m.Tarih_Saat) AS sezon,
            m.Mac_ID AS mac_id, 
            m.Tarih_Saat,
            CASE 
                WHEN m.Ev_Sahibi_Takim_ID = o.Takim_ID THEN t_dep.Ad
                ELSE t_ev.Ad
            END AS rakip,
            CONCAT(m.Ev_Sahibi_Skor, ' - ', m.Deplasman_Skor) AS skor,
            SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) AS gol,
            SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) AS asist,
            SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) AS sari_kart,
            SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) AS kirmizi_kart
        FROM Oyuncular o
        JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = o.Takim_ID OR m.Deplasman_Takim_ID = o.Takim_ID)
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON m.Mac_ID = mo.Mac_ID AND mo.Oyuncu_ID = o.Oyuncu_ID
        WHERE o.Oyuncu_ID = %s AND m.Ev_Sahibi_Skor IS NOT NULL
        GROUP BY sezon, mac_id, m.Tarih_Saat, rakip, skor
        ORDER BY m.Tarih_Saat DESC
        """, (player_id,))
        raw_history = cursor.fetchall()
        
        performance_history = []
        for row in raw_history:
            # Tarih_Saat nesnesini formata cevir (DD.MM.YYYY)
            tarih_str = row['Tarih_Saat'].strftime('%d.%m.%Y') if hasattr(row['Tarih_Saat'], 'strftime') else str(row['Tarih_Saat'])
            performance_history.append({
                "sezon": row['sezon'],
                "mac_id": row['mac_id'],
                "tarih": tarih_str,
                "rakip": row['rakip'],
                "skor": row['skor'],
                "gol": row['gol'],
                "asist": row['asist'],
                "sari_kart": row['sari_kart'],
                "kirmizi_kart": row['kirmizi_kart']
            })
        
        total_team_matches = len(performance_history)
        sari = int(stats['Sari_Kart'] or 0)
        kirmizi = int(stats['Kirmizi_Kart'] or 0)
        kart_orani = (sari + (kirmizi * 2)) / total_team_matches if total_team_matches > 0 else 0.0
        
        if kart_orani > 0.5:
            risk = "Yüksek Risk (Agresif Profil)"
        elif kart_orani >= 0.2:
            risk = "Orta Risk (Kontrollü Agresif)"
        else:
            risk = "Düşük Risk (Güvenli Profil)"
            
        discipline_analysis = {
            "toplam_sari_kart": sari,
            "toplam_kirmizi_kart": kirmizi,
            "mac_basina_kart_orani": round(kart_orani, 2),
            "risk_durumu": risk
        }
        
        return jsonify({
            "success": True, 
            "player": player, 
            "stats": stats, 
            "transfers": transfers_desc, 
            "financial_analysis": financial_analysis,
            "discipline_analysis": discipline_analysis,
            "match_history": performance_history,
            "performance_history": performance_history
        })
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/compare-players')
def compare_players():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({"success": False, "error": "Karşılaştırma için ID listesi boş olamaz."}), 400
        
    try:
        player_ids = [int(i.strip()) for i in ids_param.split(',') if i.strip().isdigit()]
    except ValueError:
        return jsonify({"success": False, "error": "Geçersiz ID formatı."}), 400
        
    if len(player_ids) < 2 or len(player_ids) > 3:
        return jsonify({"success": False, "error": "Karşılaştırma en az 2, en fazla 3 oyuncuyla yapılmalıdır."}), 400

    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        placeholders = ', '.join(['%s'] * len(player_ids))
        
        query = f"""
        SELECT 
            o.Oyuncu_ID AS id,
            o.Ad AS ad,
            o.Soyad AS soyad,
            t.Ad AS takim,
            o.Mevki AS mevki,
            TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS yas,
            o.Uyruk AS uyruk,
            SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) AS toplam_gol,
            SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) AS toplam_asist,
            SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) AS sari_kart,
            SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) AS kirmizi_kart,
            ROUND(SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) / 
                  NULLIF((SELECT COUNT(DISTINCT m2.Mac_ID) 
                          FROM Maclar m2 
                          WHERE (m2.Ev_Sahibi_Takim_ID = o.Takim_ID OR m2.Deplasman_Takim_ID = o.Takim_ID) 
                            AND m2.Ev_Sahibi_Skor IS NOT NULL), 0), 2) AS mac_basina_gol,
            ROUND(SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) / 
                  NULLIF((SELECT COUNT(DISTINCT m2.Mac_ID) 
                          FROM Maclar m2 
                          WHERE (m2.Ev_Sahibi_Takim_ID = o.Takim_ID OR m2.Deplasman_Takim_ID = o.Takim_ID) 
                            AND m2.Ev_Sahibi_Skor IS NOT NULL), 0), 2) AS mac_basina_asist
        FROM Oyuncular o
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        WHERE o.Oyuncu_ID IN ({placeholders})
        GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, t.Ad, o.Mevki, yas, o.Uyruk, o.Takim_ID
        """
        
        cursor.execute(query, tuple(player_ids))
        compared_players = cursor.fetchall()
        
        # Replace None with 0.0 for mac_basina metrics if applicable
        for p in compared_players:
            if p['mac_basina_gol'] is None: p['mac_basina_gol'] = 0.0
            if p['mac_basina_asist'] is None: p['mac_basina_asist'] = 0.0
        
        return jsonify({
            "success": True,
            "compared_players": compared_players
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/team-detail/<int:team_id>')
def team_detail(team_id):
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # A) Takım Künyesi ve Teknik Kadro Sorgusu
        cursor.execute("""
        SELECT t.Takim_ID AS team_id, t.Ad AS ad, t.Kurulus_Yili AS kurulus_yili, t.Sehir AS sehir,
               CONCAT(td.Ad, ' ', td.Soyad) AS teknik_direktor,
               s.Ad AS stadyum_adi, s.Kapasite AS stadyum_kapasite
        FROM Takimlar t
        LEFT JOIN Teknik_Direktorler td ON td.Takim_ID = t.Takim_ID
        LEFT JOIN Stadyumlar s ON s.Stadyum_ID = t.Stadyum_ID
        WHERE t.Takim_ID = %s
        """, (team_id,))
        team_info = cursor.fetchone()
        
        if not team_info:
            return jsonify({"success": False, "error": "Takım bulunamadı."}), 404
            
        # B) Güncel Kadro Listesi Sorgusu
        cursor.execute("""
        SELECT Oyuncu_ID AS oyuncu_id, CONCAT(Ad, ' ', Soyad) AS ad_soyad, Mevki AS mevki,
               TIMESTAMPDIFF(YEAR, Dogum_Tarihi, CURDATE()) AS yas, Uyruk AS uyruk
        FROM Oyuncular 
        WHERE Takim_ID = %s 
        ORDER BY Mevki, Soyad
        """, (team_id,))
        squad = cursor.fetchall()
        
        # C) Sezonluk Takım Performans İstatistikleri Sorgusu
        cursor.execute("""
        SELECT 
            COUNT(m.Mac_ID) AS toplam_mac,
            SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                          (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 ELSE 0 END) AS galibiyet,
            SUM(CASE WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 ELSE 0 END) AS beraberlik,
            SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor < m.Deplasman_Skor) OR 
                          (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor < m.Ev_Sahibi_Skor) THEN 1 ELSE 0 END) AS maglubiyet,
            SUM(CASE WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Ev_Sahibi_Skor ELSE m.Deplasman_Skor END) AS atilan_gol,
            SUM(CASE WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Deplasman_Skor ELSE m.Ev_Sahibi_Skor END) AS yenilen_gol,
            (SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                           (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 ELSE 0 END) * 3) +
            SUM(CASE WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 ELSE 0 END) AS puan
        FROM Takimlar t
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID) AND m.Ev_Sahibi_Skor IS NOT NULL
        WHERE t.Takim_ID = %s
        GROUP BY t.Takim_ID
        """, (team_id,))
        stats = cursor.fetchone()
        
        # Format the missing values to 0 if no matches played
        if stats:
            for key in stats:
                if stats[key] is None:
                    stats[key] = 0
            # Ensure proper casting
            stats = {k: int(v) for k, v in stats.items()}
        else:
            stats = {
                "toplam_mac": 0, "galibiyet": 0, "beraberlik": 0, "maglubiyet": 0,
                "atilan_gol": 0, "yenilen_gol": 0, "puan": 0
            }

        return jsonify({
            "success": True, 
            "team_info": team_info, 
            "stats": stats, 
            "squad": squad
        })
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/search')
def global_search():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({"success": True, "players": [], "managers": [], "teams": []})
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        search_pattern = f"%{q}%"
        
        cursor.execute("""
            SELECT Oyuncu_ID as id, Ad as ad, Soyad as soyad, Mevki as mevki, Uyruk as uyruk, 'player' as type 
            FROM Oyuncular 
            WHERE CONCAT(Ad, ' ', Soyad) LIKE %s LIMIT 5
        """, (search_pattern,))
        players = cursor.fetchall()
        
        cursor.execute("""
            SELECT Direktor_ID as id, Ad as ad, Soyad as soyad, 'manager' as type 
            FROM Teknik_Direktorler 
            WHERE CONCAT(Ad, ' ', Soyad) LIKE %s LIMIT 3
        """, (search_pattern,))
        managers = cursor.fetchall()
        
        cursor.execute("""
            SELECT Takim_ID as id, Ad as ad, 'team' as type 
            FROM Takimlar 
            WHERE Ad LIKE %s LIMIT 3
        """, (search_pattern,))
        teams = cursor.fetchall()
        
        return jsonify({
            "success": True,
            "players": players,
            "managers": managers,
            "teams": teams
        })
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/compare-managers')
def compare_managers():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({"success": False, "error": "ID listesi boş."}), 400
    try:
        m_ids = [int(i.strip()) for i in ids_param.split(',') if i.strip().isdigit()]
    except:
        return jsonify({"success": False, "error": "Geçersiz ID."}), 400
        
    if len(m_ids) < 2 or len(m_ids) > 3:
        return jsonify({"success": False, "error": "2 veya 3 direktör seçilmeli."}), 400
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        placeholders = ', '.join(['%s'] * len(m_ids))
        cursor.execute(f"""
        SELECT td.Direktor_ID AS id, td.Ad AS ad, td.Soyad AS soyad, t.Ad AS takim,
               COUNT(m.Mac_ID) AS toplam_mac,
               SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor)
                          OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor)
                    THEN 1 ELSE 0 END) AS galibiyet,
               SUM(CASE WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 ELSE 0 END) AS beraberlik,
               SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor<m.Deplasman_Skor)
                          OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor<m.Ev_Sahibi_Skor)
                    THEN 1 ELSE 0 END) AS maglubiyet,
               ROUND((SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor)
                                 OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor)
                           THEN 1 ELSE 0 END) / NULLIF(COUNT(m.Mac_ID),0))*100, 1) AS galibiyet_yuzdesi
        FROM Teknik_Direktorler td
        LEFT JOIN Takimlar t ON td.Takim_ID=t.Takim_ID
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID=t.Takim_ID OR m.Deplasman_Takim_ID=t.Takim_ID)
        AND m.Ev_Sahibi_Skor IS NOT NULL
        WHERE td.Direktor_ID IN ({placeholders})
        GROUP BY td.Direktor_ID, td.Ad, td.Soyad, t.Ad
        """, tuple(m_ids))
        
        results = cursor.fetchall()
        for r in results:
            if r['galibiyet_yuzdesi'] is None: r['galibiyet_yuzdesi'] = 0.0
            
        return jsonify({"success": True, "compared_managers": results})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


@app.route('/api/scouting/compare-teams')
def compare_teams():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({"success": False, "error": "ID listesi boş."}), 400
    try:
        t_ids = [int(i.strip()) for i in ids_param.split(',') if i.strip().isdigit()]
    except:
        return jsonify({"success": False, "error": "Geçersiz ID."}), 400
        
    if len(t_ids) < 2 or len(t_ids) > 3:
        return jsonify({"success": False, "error": "2 veya 3 takım seçilmeli."}), 400
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        placeholders = ', '.join(['%s'] * len(t_ids))
        cursor.execute(f"""
        SELECT t.Takim_ID AS id, t.Ad AS ad, t.Kurulus_Yili AS kurulus, t.Sehir AS sehir,
               SUM(CASE WHEN m.Ev_Sahibi_Takim_ID=t.Takim_ID THEN m.Ev_Sahibi_Skor 
                        WHEN m.Deplasman_Takim_ID=t.Takim_ID THEN m.Deplasman_Skor ELSE 0 END) AS atilan_gol,
               SUM(CASE WHEN m.Ev_Sahibi_Takim_ID=t.Takim_ID THEN m.Deplasman_Skor 
                        WHEN m.Deplasman_Takim_ID=t.Takim_ID THEN m.Ev_Sahibi_Skor ELSE 0 END) AS yenilen_gol,
               COUNT(m.Mac_ID) AS oynanan_mac,
               SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor) OR 
                             (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor) THEN 3
                        WHEN m.Ev_Sahibi_Skor=m.Deplasman_Skor THEN 1 ELSE 0 END) AS puan
        FROM Takimlar t
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID=t.Takim_ID OR m.Deplasman_Takim_ID=t.Takim_ID) 
        AND m.Ev_Sahibi_Skor IS NOT NULL
        WHERE t.Takim_ID IN ({placeholders})
        GROUP BY t.Takim_ID, t.Ad, t.Kurulus_Yili, t.Sehir
        """, tuple(t_ids))
        
        results = cursor.fetchall()
        for r in results:
            if r['atilan_gol'] is None: r['atilan_gol'] = 0
            if r['yenilen_gol'] is None: r['yenilen_gol'] = 0
            r['averaj'] = int(r['atilan_gol']) - int(r['yenilen_gol'])
            
        return jsonify({"success": True, "compared_teams": results})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/scouting/leaderboard')
def scouting_leaderboard():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    mevki = request.args.get('mevki', 'FV').upper()
    kriter = request.args.get('kriter', 'Gol')
    
    valid_mevki = ['KL', 'DF', 'OS', 'FV']
    if mevki not in valid_mevki:
        mevki = 'FV'
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        zaman = request.args.get('zaman')
        zaman_condition = ""
        zaman_cs_condition = ""
        if zaman and zaman.isdigit():
            zaman_condition = f" AND m2.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL {int(zaman)} DAY)"
            zaman_cs_condition = f" AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL {int(zaman)} DAY)"
            
        query = f"""
        SELECT o.Oyuncu_ID AS oyuncu_id, o.Ad AS ad, o.Soyad AS soyad, 
               IFNULL(t.Ad, 'Serbest') AS takim, o.Mevki AS mevki, o.Uyruk AS uyruk,
               SUM(CASE WHEN mo.Olay_Tipi = 'Gol'{zaman_condition} THEN 1 ELSE 0 END) AS gol,
               SUM(CASE WHEN mo.Olay_Tipi = 'Asist'{zaman_condition} THEN 1 ELSE 0 END) AS asist,
               (SELECT COUNT(m.Mac_ID) 
                FROM Maclar m 
                WHERE ((m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Deplasman_Skor = 0) 
                   OR (m.Deplasman_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor = 0))
                   {zaman_cs_condition}) AS temiz_saha
        FROM Oyuncular o
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        LEFT JOIN Maclar m2 ON mo.Mac_ID = m2.Mac_ID
        WHERE o.Mevki = %s
        """
        params = [mevki]
        
        takim_id = request.args.get('takim_id')
        if takim_id and takim_id != 'Tumu':
            query += " AND o.Takim_ID = %s"
            params.append(int(takim_id))
            
        uyruk = request.args.get('uyruk')
        if uyruk:
            query += " AND o.Uyruk LIKE %s"
            params.append(f"%{uyruk}%")
            
        min_yas = request.args.get('min_yas')
        if min_yas:
            query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) >= %s"
            params.append(int(min_yas))
            
        max_yas = request.args.get('max_yas')
        if max_yas:
            query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) <= %s"
            params.append(int(max_yas))

        query += " GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, t.Ad, o.Mevki, o.Uyruk, t.Takim_ID"
        
        if kriter == 'Temiz_Saha':
            query += " ORDER BY temiz_saha DESC, gol DESC, asist DESC LIMIT 10"
        elif kriter == 'Asist':
            query += " ORDER BY asist DESC, gol DESC LIMIT 10"
        else: # Gol
            query += " ORDER BY gol DESC, asist DESC LIMIT 10"
            
        cursor.execute(query, tuple(params))
        results = cursor.fetchall()
        
        leaderboard = []
        for idx, row in enumerate(results):
            row['rank'] = idx + 1
            if row['gol'] is None: row['gol'] = 0
            if row['asist'] is None: row['asist'] = 0
            if row['temiz_saha'] is None: row['temiz_saha'] = 0
            
            row['gol'] = int(row['gol'])
            row['asist'] = int(row['asist'])
            row['temiz_saha'] = int(row['temiz_saha'])
            
            leaderboard.append(row)
            
        return jsonify({"success": True, "leaderboard": leaderboard})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/match/detail/<int:match_id>')
def match_detail(match_id):
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # 1. Maç Genel Bilgileri Sorgusu (Sorgu A)
        query_info = """
        SELECT t_ev.Ad AS ev_sahibi, t_dep.Ad AS deplasman,
               m.Ev_Sahibi_Skor AS ev_sahibi_skor, m.Deplasman_Skor AS deplasman_skor,
               m.Tarih_Saat AS tarih, s.Stadyum_ID AS stadyum_id, s.Ad AS stadyum, s.Sehir AS sehir, s.Kapasite AS kapasite
        FROM Maclar m
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        LEFT JOIN Stadyumlar s ON m.Stadyum_ID = s.Stadyum_ID
        WHERE m.Mac_ID = %s
        """
        cursor.execute(query_info, (match_id,))
        match_info = cursor.fetchone()
        
        if not match_info:
            return jsonify({"success": False, "error": "Maç bulunamadı."}), 404
            
        if match_info['tarih']:
            match_info['tarih'] = match_info['tarih'].strftime('%Y-%m-%dT%H:%M:%S') if hasattr(match_info['tarih'], 'strftime') else str(match_info['tarih'])
            
        # 2. Dakika Dakika Maç Olayları Sorgusu (Sorgu B)
        query_events = """
        SELECT mo.Olay_ID AS olay_id, mo.Dakika AS dakika, mo.Olay_Tipi AS olay_tipi,
               o.Ad AS ad, o.Soyad AS soyad, o.Oyuncu_ID AS oyuncu_id
        FROM Mac_Olaylari mo
        JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
        WHERE mo.Mac_ID = %s
        ORDER BY mo.Dakika ASC
        """
        cursor.execute(query_events, (match_id,))
        events_raw = cursor.fetchall()
        
        timeline = []
        for e in events_raw:
            timeline.append({
                "dakika": int(e['dakika']),
                "olay_tipi": e['olay_tipi'],
                "oyuncu": f"{e['ad']} {e['soyad']}",
                "oyuncu_id": int(e['oyuncu_id'])
            })
            
        return jsonify({
            "success": True,
            "match_info": match_info,
            "timeline": timeline
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/match/archive')
def match_archive():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    baslangic = request.args.get('baslangic_tarihi', '').strip()
    bitis = request.args.get('bitis_tarihi', '').strip()
    
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        if baslangic and bitis:
            date_filter = "DATE(m.Tarih_Saat) BETWEEN %s AND %s"
            params = (baslangic, bitis)
        else:
            date_filter = "DATE(m.Tarih_Saat) BETWEEN DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND CURDATE()"
            params = ()
            
        query = f"""
        SELECT m.Mac_ID AS mac_id, m.Tarih_Saat AS tarih_saat, m.Ev_Sahibi_Skor, m.Deplasman_Skor,
               t_ev.Ad AS ev_sahibi, t_dep.Ad AS deplasman, s.Ad AS stadyum
        FROM Maclar m
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        LEFT JOIN Stadyumlar s ON m.Stadyum_ID = s.Stadyum_ID
        WHERE {date_filter}
        ORDER BY m.Tarih_Saat DESC
        """
        
        cursor.execute(query, params)
        raw_matches = cursor.fetchall()
        
        archived_matches = []
        for row in raw_matches:
            if row['Ev_Sahibi_Skor'] is None or row['Deplasman_Skor'] is None:
                skor_gosterim = "Oynanmadı"
            else:
                skor_gosterim = f"{row['Ev_Sahibi_Skor']} - {row['Deplasman_Skor']}"
                
            archived_matches.append({
                "mac_id": row['mac_id'],
                "ev_sahibi": row['ev_sahibi'],
                "deplasman": row['deplasman'],
                "tarih_saat": row['tarih_saat'].strftime('%Y-%m-%dT%H:%M:%S') if hasattr(row['tarih_saat'], 'strftime') else str(row['tarih_saat']),
                "stadyum": row['stadyum'] or "Bilinmiyor",
                "skor_gosterim": skor_gosterim
            })
            
        return jsonify({"success": True, "archived_matches": archived_matches})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/stadium/detail/<int:stadium_id>')
def stadium_detail(stadium_id):
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # 1. Stadyum Künyesi Sorgusu
        cursor.execute("SELECT Stadyum_ID AS stadium_id, Ad AS ad, Sehir AS sehir, Kapasite AS kapasite FROM Stadyumlar WHERE Stadyum_ID = %s", (stadium_id,))
        stadium_info = cursor.fetchone()
        
        if not stadium_info:
            return jsonify({"success": False, "error": "Stadyum bulunamadı."}), 404
            
        # 2. Stadyum Maç Geçmişi Sorgusu
        query_matches = """
        SELECT m.Mac_ID AS mac_id, t_ev.Ad AS ev_sahibi, t_dep.Ad AS deplasman,
               m.Ev_Sahibi_Skor, m.Deplasman_Skor, m.Tarih_Saat AS tarih
        FROM Maclar m
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        WHERE m.Stadyum_ID = %s
        ORDER BY m.Tarih_Saat DESC
        """
        cursor.execute(query_matches, (stadium_id,))
        matches_raw = cursor.fetchall()
        
        played_matches = []
        for m in matches_raw:
            skor_text = f"{m['Ev_Sahibi_Skor']} - {m['Deplasman_Skor']}" if m['Ev_Sahibi_Skor'] is not None else "vs"
            tarih_text = m['tarih'].strftime('%Y-%m-%d') if hasattr(m['tarih'], 'strftime') else str(m['tarih']).split(' ')[0]
            
            played_matches.append({
                "mac_id": m['mac_id'],
                "ev_sahibi": m['ev_sahibi'],
                "deplasman": m['deplasman'],
                "skor": skor_text,
                "tarih": tarih_text
            })
            
        return jsonify({
            "success": True,
            "stadium_info": stadium_info,
            "played_matches": played_matches
        })
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/scouting/players/search')
def scouting_players_search():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"success": True, "search_results": []})
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        query = """
        SELECT o.Oyuncu_ID AS oyuncu_id, o.Ad, o.Soyad, o.Mevki AS mevki, o.Uyruk AS uyruk,
               t.Ad AS takim,
               (SELECT COUNT(m.Mac_ID) FROM Maclar m WHERE m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID) AS toplam_mac,
               SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) AS gol,
               SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) AS asist
        FROM Oyuncular o
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        WHERE o.Ad LIKE %s OR o.Soyad LIKE %s
        GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, o.Mevki, o.Uyruk, t.Ad, t.Takim_ID
        LIMIT 20
        """
        like_q = f"%{q}%"
        cursor.execute(query, (like_q, like_q))
        results = cursor.fetchall()
        
        search_results = []
        for r in results:
            search_results.append({
                "oyuncu_id": r['oyuncu_id'],
                "ad_soyad": f"{r['Ad']} {r['Soyad']}",
                "takim": r['takim'] or "Serbest",
                "mevki": r['mevki'],
                "uyruk": r['uyruk'],
                "toplam_mac": int(r['toplam_mac']) if r['toplam_mac'] else 0,
                "gol": int(r['gol']) if r['gol'] else 0,
                "asist": int(r['asist']) if r['asist'] else 0
            })
            
        return jsonify({"success": True, "search_results": search_results})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

