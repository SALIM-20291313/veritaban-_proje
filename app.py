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
        
        # 2. Gol Kralligi Query
        scorers_query = """
        SELECT 
            o.Oyuncu_ID,
            o.Ad,
            o.Soyad,
            t.Ad AS Takim_Ad,
            COUNT(mo.Olay_ID) AS Gol_Sayisi
        FROM Oyuncular o
        JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        WHERE mo.Olay_Tipi = 'Gol'
        GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, t.Ad
        ORDER BY Gol_Sayisi DESC
        LIMIT 10;
        """
        cursor.execute(scorers_query)
        scorers = cursor.fetchall()
        
        # 3. Asist Kralligi Query
        assists_query = """
        SELECT 
            o.Oyuncu_ID,
            o.Ad,
            o.Soyad,
            t.Ad AS Takim_Ad,
            COUNT(mo.Olay_ID) AS Asist_Sayisi
        FROM Oyuncular o
        JOIN Mac_Olaylari mo ON o.Oyuncu_ID = mo.Oyuncu_ID
        JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        WHERE mo.Olay_Tipi = 'Asist'
        GROUP BY o.Oyuncu_ID, o.Ad, o.Soyad, t.Ad
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
            o.Ad AS Oyuncu_Ad,
            o.Soyad AS Oyuncu_Soyad,
            t_eski.Ad AS Eski_Takim,
            t_yeni.Ad AS Yeni_Takim,
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
        recent_transfers = cursor.fetchall()

        # 6. Teknik Direktorler Basari Tablosu
        managers_query = """
        SELECT 
            td.Direktor_ID,
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
        GROUP BY td.Direktor_ID, td.Ad, td.Soyad, t.Ad
        ORDER BY Galibiyet_Yuzdesi DESC, Toplam_Mac DESC;
        """
        cursor.execute(managers_query)
        managers = cursor.fetchall()

        # 7. Tum Maclar (Son 15 Mac)
        all_matches_query = """
        SELECT 
            m.Mac_ID,
            t_ev.Ad AS Ev_Sahibi,
            t_dep.Ad AS Deplasman,
            m.Tarih_Saat,
            m.Ev_Sahibi_Skor,
            m.Deplasman_Skor,
            s.Ad AS Stadyum_Ad
        FROM Maclar m
        JOIN Takimlar t_ev ON m.Ev_Sahibi_Takim_ID = t_ev.Takim_ID
        JOIN Takimlar t_dep ON m.Deplasman_Takim_ID = t_dep.Takim_ID
        LEFT JOIN Stadyumlar s ON m.Stadyum_ID = s.Stadyum_ID
        ORDER BY m.Tarih_Saat DESC, m.Mac_ID DESC
        LIMIT 15;
        """
        cursor.execute(all_matches_query)
        matches = cursor.fetchall()

        cursor.close()
        return jsonify({
            "success": True,
            "standings": standings,
            "scorers": scorers,
            "assists": assists,
            "transfers_chart": transfers_chart,
            "recent_transfers": recent_transfers,
            "managers": managers,
            "matches": matches
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

    # 2. Call Gemini API
    api_key = os.getenv('GEMINI_API_KEY', '').strip()
    
    prompt = f"""
    Sen profesyonel bir Türk futbol analistisin. Sana verilen asagidaki ilişkisel veritabanı istatistiklerini incele ve bu haftanin lig durumunu, taktiksel basarisini, finansal harcamalarini ve teknik direktörlerin performansini yorumlayan, taraftarlarin ve kulüp yönetimlerinin ilgisini çekecek 3 paragraflık Türkçe, akıcı, samimi ve son derece analitik bir rapor hazirla. 
    
    Raporuna mutlaka ilgi çekici bir baslik ekle. Raporun icerisinde puan durumuna, gol kralligindaki isimlere, en pahali transfere ve en basarili teknik direktöre spesifik atiflarda bulun.
    
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

    mock_markdown = f"""### ⚽ Antigravity AI Analist Haftalık Raporu

**Liderlik Koltuğu Sallantıda mı? Yoksa Dominasyon mu Başlıyor?**

Ligde kıran kırana geçen haftaların ardından **{lider}**, topladığı **{int(lider_puan)} puanla** zirvedeki yerini sağlamlaştırıyor. Sahada gösterdikleri taktiksel olgunluk ve kompakt oyun anlayışı, rakiplerinin analiz duvarlarına çarparak dağılmasına neden oluyor. Özellikle hücum hattındaki yaratıcı aksiyonlar ve geçiş hücumları, takımın bu sezonki şampiyonluk iddiasının ne kadar güçlü olduğunun canlı kanıtı niteliğinde.

Gol krallığı yarışında ise adeta bir solo resital izliyoruz. **{scorer_name}**, attığı **{scorer_goals} golle** takımını sırtlamaya devam ediyor. Ceza sahasındaki bitiriciliği ve doğru konumlanma becerisi onu durdurulamaz bir silah haline getirdi. Finansal cephede ise **{trans_name}** transferinin **{trans_fee:.1f} Milyon EUR** bonservis bedeliyle **{trans_team}** kadrosuna katılması ligin dengelerini sarstı. Yapılan bu devasa yatırımın sahaya yansıyan performansı, bonservis maliyetinin hakkını fazlasıyla verdiğini gösteriyor.

Kulübelerin arkasındaki akıl hocalarına baktığımızda ise taktik dehaların savaşına şahit oluyoruz. **{mgr_team}** teknik patronu **{mgr_name}**, **%{mgr_pct} galibiyet oranıyla** ligin en efektif menajeri konumunda. Oyuna yaptığı hamleler, devre arası taktik değişiklikler ve basın toplantılarındaki karizmatik duruşuyla ligin kalitesini bambaşka bir seviyeye taşıyor. Bu performans, kulüp yönetimlerinin istikrar konusundaki kararlarının ne kadar haklı olduğunu tesciller nitelikte.
"""
    html_text = markdown.markdown(mock_markdown)
    return jsonify({
        "success": True,
        "analysis": html_text,
        "is_live_ai": False,
        "notice": "Gemini API anahtarı ayarlanmadığı veya hata verdiği için simüle analiz üretildi."
    })

@app.route('/api/scouting/search')
def scouting_search():
    if not db_connected:
        return jsonify({"success": False, "error": "Veritabanı bağlantısı yok."}), 500
        
    conn = None
    try:
        conn = get_mysql_connection(use_db=True)
        cursor = conn.cursor(dictionary=True)
        
        # Base query to get player details, calculated age, and latest transfer value (bonservis)
        query = """
        SELECT 
            o.Oyuncu_ID,
            o.Ad,
            o.Soyad,
            TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS Yas,
            o.Uyruk,
            o.Mevki,
            t.Ad AS Takim_Ad,
            COALESCE(
                (SELECT tr.Bonservis_Bedeli 
                 FROM Transferler tr 
                 WHERE tr.Oyuncu_ID = o.Oyuncu_ID 
                 ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC 
                 LIMIT 1), 0.00
            ) AS Son_Bonservis
        FROM Oyuncular o
        LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID
        WHERE 1=1
        """
        params = []
        
        # Get query parameters
        mevki = request.args.get('mevki')
        if mevki and mevki != 'Tumu':
            query += " AND o.Mevki = %s"
            params.append(mevki)
            
        uyruk = request.args.get('uyruk')
        if uyruk:
            query += " AND o.Uyruk LIKE %s"
            params.append(f"%{uyruk}%")
            
        max_yas = request.args.get('max_yas')
        if max_yas:
            try:
                query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) <= %s"
                params.append(int(max_yas))
            except ValueError:
                pass
                
        max_bonservis = request.args.get('max_bonservis')
        if max_bonservis:
            try:
                query += """ AND COALESCE(
                    (SELECT tr.Bonservis_Bedeli 
                     FROM Transferler tr 
                     WHERE tr.Oyuncu_ID = o.Oyuncu_ID 
                     ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC 
                     LIMIT 1), 0.00
                ) <= %s"""
                params.append(float(max_bonservis))
            except ValueError:
                pass
                
        query += " ORDER BY Son_Bonservis DESC, o.Ad ASC, o.Soyad ASC"
        
        cursor.execute(query, tuple(params))
        players = cursor.fetchall()
        cursor.close()
        
        return jsonify({
            "success": True,
            "players": players
        })
        
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

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
               o.Uyruk, o.Mevki, t.Ad AS Takim_Ad,
               COALESCE((SELECT tr.Bonservis_Bedeli FROM Transferler tr
                         WHERE tr.Oyuncu_ID = o.Oyuncu_ID
                         ORDER BY tr.Tarih DESC, tr.Transfer_ID DESC LIMIT 1), 0.00) AS Son_Bonservis
        FROM Oyuncular o LEFT JOIN Takimlar t ON o.Takim_ID = t.Takim_ID WHERE 1=1"""
        params = []
        mevki = request.args.get('mevki')
        if mevki and mevki != 'Tumu':
            query += " AND o.Mevki=%s"; params.append(mevki)
        uyruk = request.args.get('uyruk')
        if uyruk:
            query += " AND o.Uyruk LIKE %s"; params.append(f"%{uyruk}%")
        max_yas = request.args.get('max_yas')
        if max_yas:
            query += " AND TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) <= %s"; params.append(int(max_yas))
        max_bonservis = request.args.get('max_bonservis')
        if max_bonservis:
            query += """ AND COALESCE((SELECT tr.Bonservis_Bedeli FROM Transferler tr
                         WHERE tr.Oyuncu_ID=o.Oyuncu_ID ORDER BY tr.Tarih DESC LIMIT 1),0) <= %s"""
            params.append(float(max_bonservis))
        query += " ORDER BY Son_Bonservis DESC, o.Soyad ASC"
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
               ROUND((SUM(CASE WHEN (m.Ev_Sahibi_Takim_ID=t.Takim_ID AND m.Ev_Sahibi_Skor>m.Deplasman_Skor)
                                 OR (m.Deplasman_Takim_ID=t.Takim_ID AND m.Deplasman_Skor>m.Ev_Sahibi_Skor)
                           THEN 1 ELSE 0 END) / NULLIF(COUNT(m.Mac_ID),0))*100, 1) AS Galibiyet_Yuzdesi
        FROM Teknik_Direktorler td
        JOIN Takimlar t ON td.Takim_ID=t.Takim_ID
        LEFT JOIN Maclar m ON (m.Ev_Sahibi_Takim_ID=t.Takim_ID OR m.Deplasman_Takim_ID=t.Takim_ID)
        AND m.Ev_Sahibi_Skor IS NOT NULL
        GROUP BY td.Direktor_ID, td.Ad, td.Soyad, t.Ad, t.Takim_ID
        HAVING Galibiyet_Yuzdesi >= %s OR %s = 0
        ORDER BY Galibiyet_Yuzdesi DESC""",
        (float(request.args.get('min_win_rate', 0)), float(request.args.get('min_win_rate', 0))))
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
        cursor.execute("""
        SELECT o.Oyuncu_ID, o.Ad, o.Soyad, o.Dogum_Tarihi,
               TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS Yas,
               o.Uyruk, o.Mevki, t.Ad AS Takim_Ad
        FROM Oyuncular o LEFT JOIN Takimlar t ON o.Takim_ID=t.Takim_ID
        WHERE o.Oyuncu_ID=%s""", (player_id,))
        player = cursor.fetchone()
        if not player:
            return jsonify({"success": False, "error": "Oyuncu bulunamadı."}), 404
        cursor.execute("""
        SELECT Olay_Tipi, COUNT(*) AS Sayi FROM Mac_Olaylari
        WHERE Oyuncu_ID=%s GROUP BY Olay_Tipi""", (player_id,))
        stats = {r['Olay_Tipi']: r['Sayi'] for r in cursor.fetchall()}
        cursor.execute("""
        SELECT tr.Tarih, t_e.Ad AS Eski_Takim, t_y.Ad AS Yeni_Takim,
               tr.Bonservis_Bedeli, tr.Para_Birimi
        FROM Transferler tr
        LEFT JOIN Takimlar t_e ON tr.Eski_Takim_ID=t_e.Takim_ID
        LEFT JOIN Takimlar t_y ON tr.Yeni_Takim_ID=t_y.Takim_ID
        WHERE tr.Oyuncu_ID=%s ORDER BY tr.Tarih DESC""", (player_id,))
        transfers = cursor.fetchall()
        return jsonify({"success": True, "player": player, "stats": stats, "transfers": transfers})
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
        cursor.execute("""
        SELECT t.Takim_ID, t.Ad, t.Kurulus_Yili, t.Sehir,
               s.Ad AS Stadyum_Ad, s.Kapasite,
               td.Ad AS Hoca_Ad, td.Soyad AS Hoca_Soyad
        FROM Takimlar t
        LEFT JOIN Stadyumlar s ON t.Stadyum_ID=s.Stadyum_ID
        LEFT JOIN Teknik_Direktorler td ON td.Takim_ID=t.Takim_ID
        WHERE t.Takim_ID=%s""", (team_id,))
        team = cursor.fetchone()
        if not team:
            return jsonify({"success": False, "error": "Takım bulunamadı."}), 404
        cursor.execute("""
        SELECT o.Oyuncu_ID, o.Ad, o.Soyad, o.Mevki, o.Uyruk,
               TIMESTAMPDIFF(YEAR, o.Dogum_Tarihi, CURDATE()) AS Yas
        FROM Oyuncular o WHERE o.Takim_ID=%s ORDER BY o.Mevki, o.Soyad""", (team_id,))
        squad = cursor.fetchall()
        return jsonify({"success": True, "team": team, "squad": squad})
    except Error as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn and conn.is_connected(): conn.close()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

