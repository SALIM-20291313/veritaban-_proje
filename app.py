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
load_dotenv(override=True)

app = Flask(__name__)

ai_analysis_history = []  # AI geçmişini tutacak FIFO listesi (Max 3)

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
        'use_pure': True,
        'deepseek_key': os.getenv('DEEPSEEK_API_KEY', ''),
        'rapid_key': os.getenv('RAPIDAPI_KEY', '')
    }

def get_statement(cursor):
    if not cursor or not cursor.statement:
        return ""
    stmt = cursor.statement
    if isinstance(stmt, (bytes, bytearray)):
        return stmt.decode('utf-8')
    return str(stmt)

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
    deepseek_key = request.form.get('deepseek_key', '')
    rapid_key = request.form.get('rapid_key', '')

    # Write back to .env
    try:
        env_content = f"""DB_HOST={host}
DB_PORT={port}
DB_USER={user}
DB_PASSWORD={password}
DB_NAME={dbname}
DEEPSEEK_API_KEY={deepseek_key}
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
        queries = {}
        
        # 1. Puan Tablosu (Standings) Query
        standings_query = """
        SELECT * FROM (
            SELECT 
                t.Takim_ID,
                t.Ad AS Takim_Ad,
                COUNT(m.Mac_ID) AS Oynanan_Mac,
                COALESCE(SUM(CASE 
                    WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                         (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 1 
                    ELSE 0 
                END), 0) AS Galibiyet,
                COALESCE(SUM(CASE 
                    WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 
                    ELSE 0 
                END), 0) AS Beraberlik,
                COALESCE(SUM(CASE 
                    WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor < m.Deplasman_Skor) OR 
                         (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor < m.Ev_Sahibi_Skor) THEN 1 
                    ELSE 0 
                END), 0) AS Maglubiyet,
                COALESCE(SUM(CASE 
                    WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Ev_Sahibi_Skor 
                    WHEN m.Deplasman_Takim_ID = t.Takim_ID THEN m.Deplasman_Skor 
                    ELSE 0 
                END), 0) AS Atilan_Gol,
                COALESCE(SUM(CASE 
                    WHEN m.Ev_Sahibi_Takim_ID = t.Takim_ID THEN m.Deplasman_Skor 
                    WHEN m.Deplasman_Takim_ID = t.Takim_ID THEN m.Ev_Sahibi_Skor 
                    ELSE 0 
                END), 0) AS Yenilen_Gol,
                COALESCE(SUM(CASE 
                    WHEN (m.Ev_Sahibi_Takim_ID = t.Takim_ID AND m.Ev_Sahibi_Skor > m.Deplasman_Skor) OR 
                         (m.Deplasman_Takim_ID = t.Takim_ID AND m.Deplasman_Skor > m.Ev_Sahibi_Skor) THEN 3 
                    WHEN m.Ev_Sahibi_Skor = m.Deplasman_Skor THEN 1 
                    ELSE 0 
                END), 0) AS Puan
            FROM Takimlar t
            LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID = t.Takim_ID OR m.Deplasman_Takim_ID = t.Takim_ID)
            AND (m.Ev_Sahibi_Skor IS NOT NULL AND m.Deplasman_Skor IS NOT NULL)
            GROUP BY t.Takim_ID, t.Ad
        ) AS sub_standings
        ORDER BY Puan DESC, (Atilan_Gol - Yenilen_Gol) DESC, Atilan_Gol DESC;
        """
        cursor.execute(standings_query)
        queries['standings'] = get_statement(cursor)
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
            if 'recent_matches' not in queries:
                queries['recent_matches'] = get_statement(cursor)
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
        queries['scorers'] = get_statement(cursor)
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
        queries['assists'] = get_statement(cursor)
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
        queries['transfers_chart'] = get_statement(cursor)
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
        queries['recent_transfers'] = get_statement(cursor)
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
        queries['managers'] = get_statement(cursor)
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
        queries['all_matches'] = get_statement(cursor)
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
        queries['highlights_hat_tricks'] = get_statement(cursor)
        hat_tricks = cursor.fetchall()
        
        cursor.execute("""
            SELECT o.Ad, o.Soyad, t.Ad AS Takim_Ad
            FROM Mac_Olaylari mo
            JOIN Oyuncular o ON mo.Oyuncu_ID = o.Oyuncu_ID
            JOIN Maclar m ON mo.Mac_ID = m.Mac_ID
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            WHERE mo.Olay_Tipi = 'Kirmizi_Kart' AND m.Tarih_Saat >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        """)
        queries['highlights_red_cards'] = get_statement(cursor)
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
        queries['highlights_agresif'] = get_statement(cursor)
        agresif_mac = cursor.fetchone()


        # 8. Transfer Financial Summary
        cursor.execute("SELECT COALESCE(SUM(Bonservis_Bedeli), 0) AS Total_Volume FROM Transferler")
        queries['financial_total'] = get_statement(cursor)
        total_volume_row = cursor.fetchone()
        
        cursor.execute("""
            SELECT t.Ad, SUM(tr.Bonservis_Bedeli) AS Spent
            FROM Transferler tr 
            JOIN Takimlar t ON tr.Yeni_Takim_ID = t.Takim_ID 
            GROUP BY t.Takim_ID 
            ORDER BY Spent DESC 
            LIMIT 1
        """)
        queries['financial_top_spender'] = get_statement(cursor)
        top_spender_row = cursor.fetchone()
        
        cursor.execute("SELECT COUNT(*) AS Free_Transfers FROM Transferler WHERE Bonservis_Bedeli = 0")
        queries['financial_free_transfers'] = get_statement(cursor)
        free_transfers_row = cursor.fetchone()
        
        financial_summary = {
            "total_volume": float(total_volume_row['Total_Volume']) if total_volume_row else 0,
            "top_spender_name": top_spender_row['Ad'] if top_spender_row else "-",
            "top_spender_amount": float(top_spender_row['Spent']) if top_spender_row else 0,
            "free_transfers": free_transfers_row['Free_Transfers'] if free_transfers_row else 0
        }
        
        # 9. En Pahalı 5 Transfer
        cursor.execute("""
            SELECT 
                o.Oyuncu_ID, o.Ad, o.Soyad, 
                COALESCE(t_eski.Ad, 'Serbest') AS Eski_Takim, 
                t_yeni.Ad AS Yeni_Takim, 
                tr.Bonservis_Bedeli,
                tr.Para_Birimi
            FROM Transferler tr 
            JOIN Oyuncular o ON tr.Oyuncu_ID = o.Oyuncu_ID 
            LEFT JOIN Takimlar t_eski ON tr.Eski_Takim_ID = t_eski.Takim_ID 
            JOIN Takimlar t_yeni ON tr.Yeni_Takim_ID = t_yeni.Takim_ID 
            ORDER BY tr.Bonservis_Bedeli DESC 
            LIMIT 5
        """)
        queries['top_transfers'] = get_statement(cursor)
        top_transfers = cursor.fetchall()
        
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
            "weekly_highlights": weekly_highlights,
            "financial_summary": financial_summary,
            "top_transfers": top_transfers,
            "queries": queries
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
        cursor.execute("SELECT Oyuncu_ID, Ad, Soyad, Takim_ID, Mevki FROM Oyuncular WHERE Takim_ID IN (%s, %s) AND Takim_ID IS NOT NULL", (home_id, away_id))
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
    global ai_analysis_history
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabanı bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
        # 1. Puan Durumu Query
        cursor.execute('''
            SELECT t.Ad, 
                   SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor) OR 
                                 (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor) THEN 3
                            WHEN m.Ev_Sahibi_Skor=m.Deplasman_Skor THEN 1 ELSE 0 END) as Puan,
                   SUM(CASE WHEN m.Ev_Sahibi_Takim_ID=t.Takim_ID THEN m.Ev_Sahibi_Skor ELSE m.Deplasman_Skor END) - 
                   SUM(CASE WHEN m.Ev_Sahibi_Takim_ID=t.Takim_ID THEN m.Deplasman_Skor ELSE m.Ev_Sahibi_Skor END) as Averaj
            FROM Takimlar t
            LEFT JOIN Maclar m ON (t.Takim_ID = m.Ev_Sahibi_Takim_ID OR t.Takim_ID = m.Deplasman_Takim_ID)
            GROUP BY t.Takim_ID
            ORDER BY Puan DESC, Averaj DESC
            LIMIT 5
        ''')
        queries['ai_standings'] = get_statement(cursor)
        standings = cursor.fetchall()
        
        # 2. Golcüler
        cursor.execute('''
            SELECT o.Ad, o.Soyad, t.Ad as Takim, COUNT(mo.Olay_ID) as Gol
            FROM Oyuncular o
            JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
            WHERE mo.Olay_Tipi = 'Gol'
            GROUP BY o.Oyuncu_ID
            ORDER BY Gol DESC
            LIMIT 3
        ''')
        queries['ai_scorers'] = get_statement(cursor)
        scorers = cursor.fetchall()
        
        # 3. Son 3 Transfer
        cursor.execute('''
            SELECT o.Ad, o.Soyad, t.Ad as Yeni_Takim, tr.Bonservis_Bedeli
            FROM Transferler tr
            JOIN Oyuncular o ON tr.Oyuncu_ID = o.Oyuncu_ID
            JOIN Takimlar t ON tr.Yeni_Takim_ID = t.Takim_ID
            ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC
            LIMIT 3
        ''')
        queries['ai_transfers'] = get_statement(cursor)
        transfers = cursor.fetchall()
        
        # Bağlam (Context) Hazırlığı
        context = "PUAN DURUMU (İlk 5):\\n"
        for i, s in enumerate(standings, 1):
            context += f"{i}. {s['Ad']} - {s['Puan']} Puan (Averaj: {s['Averaj']})\\n"
            
        context += "\nEN GOLCÜ OYUNCULAR:\n"
        for i, g in enumerate(scorers, 1):
            context += f"- {g['Ad']} {g['Soyad']} ({g['Takim']}): {g['Gol']} Gol\n"
            
        context += "\nSON 3 TRANSFER:\n"
        for tr in transfers:
            context += f"- {tr['Ad']} {tr['Soyad']} -> {tr['Yeni_Takim']} ({float(tr['Bonservis_Bedeli']):,.0f} EUR)\n"
            
        # AI configuration using DeepSeek
        api_key = os.getenv('DEEPSEEK_API_KEY')
        
        prompt = f'''Sen sistem veritabanına bağlı canlı bir yapay zeka futbol analistisin. Sana sağlanan güncel lig puan durumunu, golcüleri ve transferleri incele. 
Raporunu şu kurallara göre yaz:
1. Yarı profesyonel scout jargonu (Box-to-box, Inverted Winger, Gegenpressing vb.), yarı esprili ve samimi bir dil kullan.
2. Sadece düz metin yazma; bol bol Emoji, Markdown Tabloları (örn. performans karşılaştırmaları), Kalın/İtalik vurgular ve Alıntı blokları (>) kullanarak göze hitap eden, dergi tarzı görsel bir tasarım oluştur.
3. Verileri ilgi çekici kılmak için metin tabanlı küçük bar grafikleri (örn: 🟩🟩🟩🟩⬛⬛) veya skor tabloları kullan.
4. Taktiksel derinliği ve oyuncu potansiyel analizlerini mutlaka ekle.

Canlı Veritabanı Verileri:
{context}
'''
        html_text = ""
        is_live = False
        gemini_err = None
        
        if api_key:
            try:
                import json
                import urllib.request
                req = urllib.request.Request(
                    'https://api.deepseek.com/chat/completions',
                    data=json.dumps({
                        "model": "deepseek-chat",
                        "messages": [
                            {"role": "system", "content": "Sen usta, esprili ve görsel sunuma (tablolar, ascii grafikler, emojiler) çok önem veren bir futbol analisti & scout şefisin. Yanıtlarını zengin markdown formatında Türkçe olarak ver."},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.85
                    }).encode('utf-8'),
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json'
                    }
                )
                with urllib.request.urlopen(req) as response:
                    res_body = response.read().decode('utf-8')
                    res_json = json.loads(res_body)
                    ai_reply = res_json['choices'][0]['message']['content']
                    html_text = markdown.markdown(ai_reply)
                    is_live = True
            except Exception as e:
                gemini_err = str(e)
                print(f"DeepSeek Error: {e}")
                
        if not is_live:
            # Fallback
            lider = standings[0]['Ad'] if standings else "Bilinmeyen Takım"
            lider_puan = standings[0]['Puan'] if standings else 0
            
            html_text = f"""
            <div class="space-y-6">
                <!-- Lider -->
                <div class="bg-gradient-to-r from-amber-500/10 to-transparent border-l-4 border-amber-500 p-4 rounded-r-xl">
                    <h3 class="text-amber-400 font-bold text-lg mb-1 flex items-center">
                        <span class="text-2xl mr-2">🏆</span> Haftanın Lideri: {lider}
                    </h3>
                    <p class="text-slate-300 text-sm">
                        <strong class="text-white">{lider}</strong>, topladığı <strong class="text-white text-lg">{int(lider_puan)}</strong> puanla zirvede adeta şov yapıyor! Rakiplerin analiz duvarlarını paramparça ettiler. Taktiksel dehaları sahada parlıyor.
                    </p>
                </div>

                <!-- Golcüler -->
                <div class="bg-zinc-900/50 border border-zinc-800 rounded-xl p-4">
                    <h3 class="text-emerald-400 font-bold text-base mb-3 flex items-center">
                        <span class="text-xl mr-2">⚽</span> Gol Krallığı Yarışı
                    </h3>
                    <div class="space-y-2">
            """
            for i, g in enumerate(scorers):
                medal = "🥇" if i == 0 else ("🥈" if i == 1 else "🥉")
                html_text += f"""
                        <div class="flex items-center justify-between p-2 hover:bg-zinc-800/50 rounded-lg transition">
                            <div class="flex items-center space-x-3">
                                <span>{medal}</span>
                                <span class="text-slate-200 font-medium">{g['Ad']} {g['Soyad']}</span>
                            </div>
                            <div class="bg-emerald-500/10 text-emerald-400 font-bold px-3 py-1 rounded-lg text-sm">
                                {g['Gol']} Gol
                            </div>
                        </div>
                """
            html_text += """
                    </div>
                </div>

                <!-- Transferler -->
                <div class="bg-zinc-900/50 border border-zinc-800 rounded-xl p-4">
                    <h3 class="text-indigo-400 font-bold text-base mb-3 flex items-center">
                        <span class="text-xl mr-2">💸</span> Transfer Borsası
                    </h3>
                    <div class="space-y-2">
            """
            for tr in transfers:
                feeStr = "Bedelsiz" if float(tr['Bonservis_Bedeli']) == 0 else f"{float(tr['Bonservis_Bedeli']):,.0f} EUR"
                html_text += f"""
                        <div class="flex flex-col p-2 hover:bg-zinc-800/50 rounded-lg transition border-l-2 border-indigo-500/30 pl-3">
                            <div class="text-slate-200 font-semibold text-sm">{tr['Ad']} {tr['Soyad']}</div>
                            <div class="flex items-center text-xs text-slate-400 mt-1">
                                <span class="bg-zinc-800 px-2 py-0.5 rounded text-slate-300">Yeni Takım: {tr['Yeni_Takim']}</span>
                                <span class="ml-auto font-medium text-amber-300">{feeStr}</span>
                            </div>
                        </div>
                """
            html_text += """
                    </div>
                </div>
            </div>
            """
            
        # Save to history
        import datetime
        now_str = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
        
        record = {
            "date": now_str,
            "content": html_text
        }
        
        ai_analysis_history.insert(0, record)
        if len(ai_analysis_history) > 3:
            ai_analysis_history.pop() # Keep only last 3
            
        return jsonify({
            "success": True,
            "analysis": html_text,
            "history": ai_analysis_history,
            "is_live_ai": is_live,
            "gemini_err": gemini_err,
            "queries": queries
        })
            
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
        queries = {'scout_managers': get_statement(cursor)}
        return jsonify({"success": True, "managers": cursor.fetchall(), "queries": queries})
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
        queries = {}
        
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
        queries['player_stats'] = get_statement(cursor)
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
        queries['player_transfers'] = get_statement(cursor)
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
        queries['player_history'] = get_statement(cursor)
        raw_history = cursor.fetchall()
        
        performance_history = []
        for row in raw_history:
            # Tarih_Saat nesnesini formata cevir (YYYY-MM-DD for JS parsing)
            tarih_str = row['Tarih_Saat'].strftime('%Y-%m-%d') if hasattr(row['Tarih_Saat'], 'strftime') else str(row['Tarih_Saat'])
            performance_history.append({
                "sezon": row['sezon'],
                "mac_id": row['mac_id'],
                "Tarih_Saat": tarih_str,
                "Rakip_Takim": row['rakip'],
                "skor": row['skor'],
                "Mac_Gol": row['gol'],
                "Mac_Asist": row['asist'],
                "Mac_Sari_Kart": row['sari_kart'],
                "Mac_Kirmizi_Kart": row['kirmizi_kart']
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
            "performance_history": performance_history,
            "queries": queries
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
        queries = {}
        
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
        queries['compare_players'] = get_statement(cursor)
        compared_players = cursor.fetchall()
        
        # Replace None with 0.0 for mac_basina metrics if applicable
        for p in compared_players:
            if p['mac_basina_gol'] is None: p['mac_basina_gol'] = 0.0
            if p['mac_basina_asist'] is None: p['mac_basina_asist'] = 0.0
        
        return jsonify({
            "success": True,
            "compared_players": compared_players,
            "queries": queries
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
        queries = {}
        
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
        queries['team_info'] = get_statement(cursor)
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
        queries['team_squad'] = get_statement(cursor)
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
        queries['team_stats'] = get_statement(cursor)
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
            "squad": squad,
            "queries": queries
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
        
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
        search_pattern = f"%{q}%"
        
        cursor.execute("""
            SELECT Oyuncu_ID as id, Ad as ad, Soyad as soyad, Mevki as mevki, Uyruk as uyruk, 'player' as type 
            FROM Oyuncular 
            WHERE CONCAT(Ad, ' ', Soyad) LIKE %s LIMIT 5
        """, (search_pattern,))
        queries['search_players'] = get_statement(cursor)
        players = cursor.fetchall()
        
        cursor.execute("""
            SELECT Direktor_ID as id, Ad as ad, Soyad as soyad, Takim_ID as team_id, 'manager' as type 
            FROM Teknik_Direktorler 
            WHERE CONCAT(Ad, ' ', Soyad) LIKE %s LIMIT 3
        """, (search_pattern,))
        queries['search_managers'] = get_statement(cursor)
        managers = cursor.fetchall()
        
        cursor.execute("""
            SELECT Takim_ID as id, Ad as ad, 'team' as type 
            FROM Takimlar 
            WHERE Ad LIKE %s LIMIT 3
        """, (search_pattern,))
        queries['search_teams'] = get_statement(cursor)
        teams = cursor.fetchall()
        
        return jsonify({
            "success": True,
            "players": players,
            "managers": managers,
            "teams": teams,
            "queries": queries
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
        
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
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
        
        queries['compare_managers'] = get_statement(cursor)
        results = cursor.fetchall()
        for r in results:
            if r['galibiyet_yuzdesi'] is None: r['galibiyet_yuzdesi'] = 0.0
            
        return jsonify({"success": True, "compared_managers": results, "queries": queries})
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
        
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
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
                        WHEN m.Ev_Sahibi_Skor=m.Deplasman_Skor THEN 1 ELSE 0 END) AS puan,
               (SELECT COUNT(*) FROM Oyuncular WHERE Takim_ID=t.Takim_ID) AS oyuncu_sayisi,
               (SELECT AVG(TIMESTAMPDIFF(YEAR, Dogum_Tarihi, CURDATE())) FROM Oyuncular WHERE Takim_ID=t.Takim_ID) AS yas_ortalamasi,
               (SELECT COUNT(*) FROM Mac_Olaylari mo JOIN Oyuncular o ON mo.Oyuncu_ID=o.Oyuncu_ID WHERE o.Takim_ID=t.Takim_ID AND mo.Olay_Tipi='Sari_Kart') AS sari_kart,
               (SELECT COUNT(*) FROM Mac_Olaylari mo JOIN Oyuncular o ON mo.Oyuncu_ID=o.Oyuncu_ID WHERE o.Takim_ID=t.Takim_ID AND mo.Olay_Tipi='Kirmizi_Kart') AS kirmizi_kart
        FROM Takimlar t
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID=t.Takim_ID OR m.Deplasman_Takim_ID=t.Takim_ID) 
        AND m.Ev_Sahibi_Skor IS NOT NULL
        WHERE t.Takim_ID IN ({placeholders})
        GROUP BY t.Takim_ID, t.Ad, t.Kurulus_Yili, t.Sehir
        """, tuple(t_ids))
        
        queries['compare_teams'] = get_statement(cursor)
        results = cursor.fetchall()
        for r in results:
            if r['atilan_gol'] is None: r['atilan_gol'] = 0
            if r['yenilen_gol'] is None: r['yenilen_gol'] = 0
            r['averaj'] = int(r['atilan_gol']) - int(r['yenilen_gol'])
            
        return jsonify({"success": True, "compared_teams": results, "queries": queries})
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
        queries = {}
        
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
        queries['scouting_leaderboard'] = get_statement(cursor)
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
            
        return jsonify({"success": True, "leaderboard": leaderboard, "queries": queries})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/scouting/players')
def scouting_players_advanced():
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
        query = """
        SELECT o.Oyuncu_ID as Oyuncu_ID, o.Ad as Ad, o.Soyad as Soyad, o.Mevki as Mevki, o.Uyruk as Uyruk,
               TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) as Yas,
               t.Ad as Takim_Ad,
               (SELECT Bonservis_Bedeli FROM Transferler tr WHERE tr.Oyuncu_ID = o.Oyuncu_ID AND tr.Para_Birimi='EUR' ORDER BY Tarih DESC LIMIT 1) as Son_Bonservis,
               SUM(CASE WHEN mo.Olay_Tipi = 'Gol' THEN 1 ELSE 0 END) as Gol,
               SUM(CASE WHEN mo.Olay_Tipi = 'Asist' THEN 1 ELSE 0 END) as Asist,
               SUM(CASE WHEN mo.Olay_Tipi = 'Sari_Kart' THEN 1 ELSE 0 END) as Toplam_Sari_Kart,
               SUM(CASE WHEN mo.Olay_Tipi = 'Kirmizi_Kart' THEN 1 ELSE 0 END) as Toplam_Kirmizi_Kart
        FROM Oyuncular o
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        LEFT JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        WHERE 1=1
        """
        params = []
        
        mevki = request.args.get('mevki')
        if mevki and mevki != 'Tumu':
            query += " AND o.Mevki = %s"
            params.append(mevki)
            
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
            
        query += " GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, o.Mevki, o.Uyruk, o.Dogum_Tarihi, t.Ad"
        
        having_clauses = []
        
        min_gol = request.args.get('min_gol')
        if min_gol:
            having_clauses.append("Gol >= %s")
            params.append(int(min_gol))
            
        min_asist = request.args.get('min_asist')
        if min_asist:
            having_clauses.append("Asist >= %s")
            params.append(int(min_asist))
            
        max_kart = request.args.get('max_kart_orani')
        if max_kart:
            having_clauses.append("(Toplam_Sari_Kart + Toplam_Kirmizi_Kart) <= %s")
            params.append(int(max_kart))
            
        max_bonservis = request.args.get('max_bonservis')
        if max_bonservis:
            having_clauses.append("(Son_Bonservis IS NULL OR Son_Bonservis <= %s)")
            params.append(float(max_bonservis))
            
        if having_clauses:
            query += " HAVING " + " AND ".join(having_clauses)
            
        query += " ORDER BY Gol DESC, Asist DESC LIMIT 50"
        
        cursor.execute(query, tuple(params))
        queries['scouting_advanced'] = get_statement(cursor)
        players = cursor.fetchall()
        
        for p in players:
            if p['Gol'] is None: p['Gol'] = 0
            if p['Asist'] is None: p['Asist'] = 0
            if p['Toplam_Sari_Kart'] is None: p['Toplam_Sari_Kart'] = 0
            if p['Toplam_Kirmizi_Kart'] is None: p['Toplam_Kirmizi_Kart'] = 0
            if p['Son_Bonservis'] is None: p['Son_Bonservis'] = 0
            
            p['Gol'] = int(p['Gol'])
            p['Asist'] = int(p['Asist'])
            p['Toplam_Sari_Kart'] = int(p['Toplam_Sari_Kart'])
            p['Toplam_Kirmizi_Kart'] = int(p['Toplam_Kirmizi_Kart'])
            p['Son_Bonservis'] = float(p['Son_Bonservis'])
            
        return jsonify({"success": True, "players": players, "queries": queries})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()


@app.route('/api/match/detail/<int:match_id>')
def match_detail(match_id):
    if not db_connected:
        return jsonify({"success": False, "error": "DB bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        queries = {}
        
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
        queries['match_info'] = get_statement(cursor)
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
        queries['match_events'] = get_statement(cursor)
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
            "timeline": timeline,
            "queries": queries
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
        queries = {}
        
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
        queries['match_archive'] = get_statement(cursor)
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
            
        return jsonify({"success": True, "archived_matches": archived_matches, "queries": queries})
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

# --- ADMIN CRUD ENDPOINTS ---

@app.route('/api/admin/players', methods=['GET'])
def admin_get_players():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT o.Oyuncu_ID, o.Ad, o.Soyad, o.Mevki, o.Uyruk, o.Dogum_Tarihi, t.Ad as Takim_Ad, o.Takim_ID
            FROM Oyuncular o
            LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
            ORDER BY o.Ad ASC, o.Soyad ASC
        """)
        queries = {'crud_players_query': get_statement(cursor)}

        players = cursor.fetchall()
        
        # Format date for json
        for p in players:
            if p['Dogum_Tarihi']:
                p['Dogum_Tarihi'] = p['Dogum_Tarihi'].strftime('%Y-%m-%d')

        return jsonify({"success": True, "players": players, "queries": queries})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/admin/players/add', methods=['POST'])
def admin_add_player():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        soyad = data.get('Soyad')
        mevki = data.get('Mevki')
        uyruk = data.get('Uyruk')
        dogum_tarihi = data.get('Dogum_Tarihi')
        takim_id = data.get('Takim_ID')
        
        if not all([ad, soyad, mevki, uyruk, dogum_tarihi]):
            return jsonify({"success": False, "error": "Eksik bilgi."}), 400

        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("SELECT Oyuncu_ID FROM Oyuncular WHERE Ad = %s AND Soyad = %s AND Dogum_Tarihi = %s", (ad, soyad, dogum_tarihi))
        if cursor.fetchone():
            return jsonify({"success": False, "error": f"{ad} {soyad} isimli oyuncu bu doğum tarihiyle zaten kayıtlı."}), 400
            
        cursor.execute("""
            INSERT INTO Oyuncular (Ad, Soyad, Mevki, Uyruk, Dogum_Tarihi, Takim_ID) 
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (ad, soyad, mevki, uyruk, dogum_tarihi, takim_id if takim_id else None))
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app.route('/api/admin/players/delete/<int:player_id>', methods=['DELETE'])
def admin_delete_player(player_id):
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabani baglantisi yok."}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        # Dependent data in Mac_Olaylari and Transferler will be deleted automatically due to ON DELETE CASCADE
        cursor.execute("DELETE FROM Oyuncular WHERE Oyuncu_ID = %s", (player_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- ADMIN CRUD: TEAMS ---
@app.route('/api/admin/teams', methods=['GET'])
def admin_get_teams():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT t.Takim_ID, t.Ad, t.Kurulus_Yili, t.Sehir, s.Ad as Stadyum_Ad, t.Stadyum_ID
            FROM Takimlar t
            LEFT JOIN Stadyumlar s ON t.Stadyum_ID = s.Stadyum_ID
            ORDER BY t.Ad ASC
        """)
        queries = {'crud_teams_query': get_statement(cursor)}

        return jsonify({"success": True, "teams": cursor.fetchall(), "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/teams/add', methods=['POST'])
def admin_add_team():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        yil = data.get('Kurulus_Yili')
        sehir = data.get('Sehir')
        stadyum_id = data.get('Stadyum_ID')
        if not all([ad, yil, sehir]): return jsonify({"success": False, "error": "Eksik bilgi"}), 400
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("SELECT Takim_ID FROM Takimlar WHERE Ad = %s", (ad,))
        if cursor.fetchone():
            return jsonify({"success": False, "error": f"'{ad}' isimli takım zaten kayıtlı."}), 400
            
        cursor.execute("INSERT INTO Takimlar (Ad, Kurulus_Yili, Sehir, Stadyum_ID) VALUES (%s, %s, %s, %s)",
                       (ad, yil, sehir, stadyum_id if stadyum_id else None))
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/teams/delete/<int:team_id>', methods=['DELETE'])
def admin_delete_team(team_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Takimlar WHERE Takim_ID = %s", (team_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

# --- ADMIN CRUD: MANAGERS ---
@app.route('/api/admin/managers', methods=['GET'])
def admin_get_managers():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT td.Direktor_ID, td.Ad, td.Soyad, t.Ad as Takim_Ad, td.Takim_ID
            FROM Teknik_Direktorler td
            LEFT JOIN Takimlar t ON td.Takim_ID = t.Takim_ID
            ORDER BY td.Ad ASC, td.Soyad ASC
        """)
        queries = {'crud_managers_query': get_statement(cursor)}

        return jsonify({"success": True, "managers": cursor.fetchall(), "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/managers/add', methods=['POST'])
def admin_add_manager():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        soyad = data.get('Soyad')
        takim_id = data.get('Takim_ID')
        if not all([ad, soyad]): return jsonify({"success": False, "error": "Eksik bilgi"}), 400
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check (Ad & Soyad)
        cursor.execute("SELECT Direktor_ID FROM Teknik_Direktorler WHERE Ad = %s AND Soyad = %s", (ad, soyad))
        if cursor.fetchone():
            return jsonify({"success": False, "error": f"{ad} {soyad} isimli teknik direktör zaten kayıtlı."}), 400
            
        # Unique team check (prevent duplicate managers on same team)
        if takim_id:
            cursor.execute("SELECT Direktor_ID FROM Teknik_Direktorler WHERE Takim_ID = %s", (takim_id,))
            if cursor.fetchone():
                return jsonify({"success": False, "error": "Seçilen takımın zaten aktif bir teknik direktörü var."}), 400
                
        cursor.execute("INSERT INTO Teknik_Direktorler (Ad, Soyad, Takim_ID) VALUES (%s, %s, %s)",
                       (ad, soyad, takim_id if takim_id else None))
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/managers/delete/<int:manager_id>', methods=['DELETE'])
def admin_delete_manager(manager_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Teknik_Direktorler WHERE Direktor_ID = %s", (manager_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

# --- ADMIN CRUD: STADIUMS ---
@app.route('/api/admin/stadiums', methods=['GET'])
def admin_get_stadiums():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT Stadyum_ID, Ad, Kapasite, Sehir FROM Stadyumlar ORDER BY Ad ASC")
        queries = {'crud_stadiums_query': get_statement(cursor)}

        return jsonify({"success": True, "stadiums": cursor.fetchall(), "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/stadiums/add', methods=['POST'])
def admin_add_stadium():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        kapasite = data.get('Kapasite')
        sehir = data.get('Sehir')
        if not all([ad, kapasite, sehir]): return jsonify({"success": False, "error": "Eksik bilgi"}), 400
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("SELECT Stadyum_ID FROM Stadyumlar WHERE Ad = %s", (ad,))
        if cursor.fetchone():
            return jsonify({"success": False, "error": f"'{ad}' isimli stadyum zaten kayıtlı."}), 400
            
        cursor.execute("INSERT INTO Stadyumlar (Ad, Kapasite, Sehir) VALUES (%s, %s, %s)", (ad, kapasite, sehir))
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/stadiums/delete/<int:stadium_id>', methods=['DELETE'])
def admin_delete_stadium(stadium_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Stadyumlar WHERE Stadyum_ID = %s", (stadium_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


# --- ADMIN CRUD: MATCHES ---
@app.route('/api/admin/matches', methods=['GET'])
def admin_get_matches():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT 
                M.Mac_ID, 
                M.Ev_Sahibi_Takim_ID,
                M.Deplasman_Takim_ID,
                E.Ad AS Ev_Sahibi, 
                D.Ad AS Deplasman, 
                M.Tarih_Saat, 
                S.Ad AS Stadyum, 
                M.Ev_Sahibi_Skor, 
                M.Deplasman_Skor
            FROM Maclar M
            JOIN Takimlar E ON M.Ev_Sahibi_Takim_ID = E.Takim_ID
            JOIN Takimlar D ON M.Deplasman_Takim_ID = D.Takim_ID
            LEFT JOIN Stadyumlar S ON M.Stadyum_ID = S.Stadyum_ID
            ORDER BY M.Tarih_Saat DESC
            LIMIT 50
        """)
        queries = {'crud_matches_query': get_statement(cursor)}

        
        matches = []
        for row in cursor.fetchall():
            skor = f"{row['Ev_Sahibi_Skor']} - {row['Deplasman_Skor']}" if row['Ev_Sahibi_Skor'] is not None and row['Deplasman_Skor'] is not None else "? - ?"
            matches.append({
                "mac_id": row["Mac_ID"],
                "ev_sahibi_id": row["Ev_Sahibi_Takim_ID"],
                "deplasman_id": row["Deplasman_Takim_ID"],
                "ev_sahibi": row["Ev_Sahibi"],
                "deplasman": row["Deplasman"],
                "tarih_saat": row["Tarih_Saat"],
                "stadyum": row["Stadyum"] or "Bilinmiyor",
                "skor_gosterim": skor,
                "ev_skor": row["Ev_Sahibi_Skor"],
                "dep_skor": row["Deplasman_Skor"]
            })
            
        return jsonify({"success": True, "matches": matches, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/matches/add', methods=['POST'])
def admin_add_match():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ev = data.get('Ev_Sahibi_Takim_ID')
        dep = data.get('Deplasman_Takim_ID')
        tarih = data.get('Tarih_Saat')
        stadyum = data.get('Stadyum_ID')
        ev_skor = data.get('Ev_Sahibi_Skor')
        dep_skor = data.get('Deplasman_Skor')
        
        if ev == dep:
            return jsonify({"success": False, "error": "Ev sahibi ve deplasman takımları aynı olamaz."}), 400
        
        if not all([ev, dep, tarih]):
            return jsonify({"success": False, "error": "Eksik bilgi (Ev Sahibi, Deplasman ve Tarih zorunludur)"}), 400
            
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("""
            SELECT Mac_ID FROM Maclar 
            WHERE Ev_Sahibi_Takim_ID = %s AND Deplasman_Takim_ID = %s AND Tarih_Saat = %s
        """, (ev, dep, tarih))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Bu iki takım arasında bu tarih ve saatte zaten bir maç tanımlı."}), 400
            
        cursor.execute("""
            INSERT INTO Maclar (Ev_Sahibi_Takim_ID, Deplasman_Takim_ID, Tarih_Saat, Stadyum_ID, Ev_Sahibi_Skor, Deplasman_Skor)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (ev, dep, tarih, stadyum if stadyum else None, ev_skor if ev_skor != '' else None, dep_skor if dep_skor != '' else None))
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "match_id": cursor.lastrowid, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/matches/delete/<int:match_id>', methods=['DELETE'])
def admin_delete_match(match_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Maclar WHERE Mac_ID = %s", (match_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


# --- ADMIN CRUD: TRANSFERS ---
@app.route('/api/admin/transfers', methods=['GET'])
def admin_get_transfers():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT 
                TR.Transfer_ID, 
                O.Ad, 
                O.Soyad, 
                E.Ad AS Eski_Takim, 
                Y.Ad AS Yeni_Takim, 
                TR.Tarih, 
                TR.Bonservis_Bedeli, 
                TR.Para_Birimi
            FROM Transferler TR
            JOIN Oyuncular O ON TR.Oyuncu_ID = O.Oyuncu_ID
            LEFT JOIN Takimlar E ON TR.Eski_Takim_ID = E.Takim_ID
            LEFT JOIN Takimlar Y ON TR.Yeni_Takim_ID = Y.Takim_ID
            ORDER BY TR.Tarih DESC, TR.Transfer_ID DESC
            LIMIT 50
        """)
        queries = {'crud_transfers_query': get_statement(cursor)}

        
        transfers = []
        for row in cursor.fetchall():
            transfers.append({
                "Transfer_ID": row["Transfer_ID"],
                "Oyuncu": f"{row['Ad']} {row['Soyad']}",
                "Eski_Takim": row["Eski_Takim"] or "Serbest",
                "Yeni_Takim": row["Yeni_Takim"] or "Serbest",
                "Tarih": row["Tarih"].strftime('%Y-%m-%d') if row["Tarih"] else "",
                "Bedel": float(row["Bonservis_Bedeli"]),
                "Para_Birimi": row["Para_Birimi"]
            })
            
        return jsonify({"success": True, "transfers": transfers, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/transfers/add', methods=['POST'])
def admin_add_transfer():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        oyuncu_id = data.get('Oyuncu_ID')
        eski_takim = data.get('Eski_Takim_ID')
        yeni_takim = data.get('Yeni_Takim_ID')
        tarih = data.get('Tarih')
        bedel = data.get('Bonservis_Bedeli')
        para_birimi = data.get('Para_Birimi', 'EUR')
        
        if not all([oyuncu_id, tarih, bedel, para_birimi]):
            return jsonify({"success": False, "error": "Eksik bilgi"}), 400
            
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("""
            SELECT Transfer_ID FROM Transferler 
            WHERE Oyuncu_ID = %s AND Yeni_Takim_ID = %s AND Tarih = %s
        """, (oyuncu_id, yeni_takim if yeni_takim else None, tarih))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Bu oyuncunun bu tarihte bu yeni takıma zaten bir transfer kaydı mevcut."}), 400
            
        # 1. Insert Transfer
        cursor.execute("""
            INSERT INTO Transferler (Oyuncu_ID, Eski_Takim_ID, Yeni_Takim_ID, Tarih, Bonservis_Bedeli, Para_Birimi)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (oyuncu_id, eski_takim if eski_takim else None, yeni_takim if yeni_takim else None, tarih, bedel, para_birimi))
        
        # 2. Update Player's Team
        cursor.execute("""
            UPDATE Oyuncular SET Takim_ID = %s WHERE Oyuncu_ID = %s
        """, (yeni_takim if yeni_takim else None, oyuncu_id))
        
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: 
        if conn: conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/transfers/delete/<int:transfer_id>', methods=['DELETE'])
def admin_delete_transfer(transfer_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Transferler WHERE Transfer_ID = %s", (transfer_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


# --- ADMIN CRUD: MATCH EVENTS ---
@app.route('/api/admin/match-events', methods=['GET'])
def admin_get_match_events():
    mac_id = request.args.get('mac_id')
    if not mac_id:
        return jsonify({"success": False, "error": "mac_id parametresi gerekli"}), 400
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT 
                MO.Olay_ID, 
                MO.Mac_ID, 
                MO.Oyuncu_ID, 
                O.Ad, 
                O.Soyad, 
                MO.Olay_Tipi, 
                MO.Dakika
            FROM Mac_Olaylari MO
            JOIN Oyuncular O ON MO.Oyuncu_ID = O.Oyuncu_ID
            WHERE MO.Mac_ID = %s
            ORDER BY MO.Dakika ASC
        """, (mac_id,))
        
        events = []
        for row in cursor.fetchall():
            events.append({
                "Olay_ID": row["Olay_ID"],
                "Mac_ID": row["Mac_ID"],
                "Oyuncu_ID": row["Oyuncu_ID"],
                "Oyuncu": f"{row['Ad']} {row['Soyad']}",
                "Olay_Tipi": row["Olay_Tipi"],
                "Dakika": row["Dakika"]
            })
            
        return jsonify({"success": True, "events": events})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/match-events/add', methods=['POST'])
def admin_add_match_event():
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        mac_id = data.get('Mac_ID')
        oyuncu_id = data.get('Oyuncu_ID')
        olay_tipi = data.get('Olay_Tipi')
        dakika = data.get('Dakika')
        
        if not all([mac_id, oyuncu_id, olay_tipi, dakika]):
            return jsonify({"success": False, "error": "Eksik bilgi"}), 400
            
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        
        # Duplicate validation check
        cursor.execute("""
            SELECT Olay_ID FROM Mac_Olaylari 
            WHERE Mac_ID = %s AND Oyuncu_ID = %s AND Olay_Tipi = %s AND Dakika = %s
        """, (mac_id, oyuncu_id, olay_tipi, dakika))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Bu oyuncu için bu dakikada aynı olay tipi zaten kayıtlı."}), 400
            
        cursor.execute("""
            INSERT INTO Mac_Olaylari (Mac_ID, Oyuncu_ID, Olay_Tipi, Dakika)
            VALUES (%s, %s, %s, %s)
        """, (mac_id, oyuncu_id, olay_tipi, dakika))
        
        conn.commit()
        queries = {'crud_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/match-events/delete/<int:event_id>', methods=['DELETE'])
def admin_delete_match_event(event_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Mac_Olaylari WHERE Olay_ID = %s", (event_id,))
        conn.commit()
        queries = {'crud_delete_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


# --- ADMIN CRUD: UPDATE ENDPOINTS ---
@app.route('/api/admin/players/update/<int:player_id>', methods=['PUT'])
def admin_update_player(player_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        soyad = data.get('Soyad')
        mevki = data.get('Mevki')
        uyruk = data.get('Uyruk')
        dogum = data.get('Dogum_Tarihi')
        takim = data.get('Takim_ID')
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE Oyuncular 
            SET Ad=%s, Soyad=%s, Mevki=%s, Uyruk=%s, Dogum_Tarihi=%s, Takim_ID=%s 
            WHERE Oyuncu_ID=%s
        """, (ad, soyad, mevki, uyruk, dogum, takim if takim else None, player_id))
        conn.commit()
        queries = {'crud_update_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/teams/update/<int:team_id>', methods=['PUT'])
def admin_update_team(team_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        yil = data.get('Kurulus_Yili')
        sehir = data.get('Sehir')
        stadyum = data.get('Stadyum_ID')
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE Takimlar 
            SET Ad=%s, Kurulus_Yili=%s, Sehir=%s, Stadyum_ID=%s 
            WHERE Takim_ID=%s
        """, (ad, yil, sehir, stadyum if stadyum else None, team_id))
        conn.commit()
        queries = {'crud_update_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/managers/update/<int:manager_id>', methods=['PUT'])
def admin_update_manager(manager_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        soyad = data.get('Soyad')
        takim = data.get('Takim_ID')
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("UPDATE Teknik_Direktorler SET Ad=%s, Soyad=%s, Takim_ID=%s WHERE Direktor_ID=%s",
                       (ad, soyad, takim if takim else None, manager_id))
        conn.commit()
        queries = {'crud_update_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/stadiums/update/<int:stadium_id>', methods=['PUT'])
def admin_update_stadium(stadium_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ad = data.get('Ad')
        kapasite = data.get('Kapasite')
        sehir = data.get('Sehir')
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("UPDATE Stadyumlar SET Ad=%s, Kapasite=%s, Sehir=%s WHERE Stadyum_ID=%s",
                       (ad, kapasite, sehir, stadium_id))
        conn.commit()
        queries = {'crud_update_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()

@app.route('/api/admin/matches/update/<int:match_id>', methods=['PUT'])
def admin_update_match(match_id):
    if not db_connected: return jsonify({"success": False}), 500
    conn = None
    try:
        data = request.json
        ev = data.get('Ev_Sahibi_Skor')
        dep = data.get('Deplasman_Skor')
        
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor()
        cursor.execute("UPDATE Maclar SET Ev_Sahibi_Skor=%s, Deplasman_Skor=%s WHERE Mac_ID=%s",
                       (ev, dep, match_id))
        conn.commit()
        queries = {'crud_update_action': get_statement(cursor)}
        return jsonify({"success": True, "queries": queries})
    except Error as e: return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

