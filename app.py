from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
import os
import requests
import json
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

# ALWAYS use the same DB file (fixes “multiple database.db” issue)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

ACH_CACHE_SECONDS = 6 * 60 * 60  # 6 hours


# ==============================
# AUTO-INJECT USERNAME INTO TEMPLATES
# ==============================
@app.context_processor
def inject_user():
    return {"username": session.get("user")}
@app.context_processor
def inject_admin():
    is_admin = False
    if "user_id" in session:
        with db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_admin FROM users WHERE id = %s", (session["user_id"],))
            result = cursor.fetchone()
            if result and result[0] == 1:
                is_admin = True
    return {"is_admin": is_admin}

# ==============================
# DB HELPERS
# ==============================
def db_connect():
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        url = urlparse(database_url)
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        return conn
    else:
        return sqlite3.connect(DB_PATH)

def column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    cols = cursor.fetchall()
    return any(c[1] == column_name for c in cols)


def get_connected_platform_id(user_id, platform):
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT external_user_id
            FROM user_platforms
            WHERE user_id = ? AND platform = ?
        """, (user_id, platform))
        row = cursor.fetchone()
        return row[0] if row else None


def get_ach_cache(user_id: int, appid: str):
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT payload, cached_at
            FROM steam_achievement_cache
            WHERE user_id = ? AND appid = ?
        """, (user_id, appid))
        row = cursor.fetchone()
        if not row:
            return None, None
        return row[0], int(row[1])


def set_ach_cache(user_id: int, appid: str, payload_obj: dict, cached_at: int):
    payload_text = json.dumps(payload_obj)
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO steam_achievement_cache (user_id, appid, payload, cached_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, appid)
            DO UPDATE SET payload = excluded.payload,
                          cached_at = excluded.cached_at
        """, (user_id, appid, payload_text, cached_at))
        conn.commit()


def normalize_achievement_payload(payload: dict, game_name: str):
    """
    Guarantees top-level fields exist even if an older cached payload only had {"achievements":[...]}.
    """
    if not isinstance(payload, dict):
        payload = {}

    if "achievements" not in payload or not isinstance(payload.get("achievements"), list):
        payload["achievements"] = []

    if "game_name" not in payload:
        payload["game_name"] = game_name

    # Compute done/total/completion if missing
    if "done" not in payload or "total" not in payload or "completion" not in payload:
        total = len(payload["achievements"])
        done = sum(1 for a in payload["achievements"] if a.get("achieved") == 1)
        payload["done"] = done
        payload["total"] = total
        payload["completion"] = round((done / total) * 100, 2) if total else 0

    return payload


# ==============================
# STEAM HELPERS
# ==============================
def steam_library_poster_url(appid: int) -> str:
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"


def fetch_steam_owned_games(api_key: str, steam_id: str):
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1
    }

    r = requests.get(url, params=params, timeout=25)
    if r.status_code != 200:
        return None, f"Steam API error: HTTP {r.status_code}"

    data = r.json()
    resp = data.get("response", {})
    games = resp.get("games")

    if games is None:
        return None, "No games returned. Check SteamID64 and ensure profile/game details are public."

    return games, None


def fetch_steam_store_details(appid: str):
    url = "https://store.steampowered.com/api/appdetails"
    params = {"appids": appid, "l": "english"}

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            return None

        j = r.json()
        node = j.get(str(appid))
        if not node or not node.get("success"):
            return None

        return node.get("data")
    except Exception:
        return None


def fetch_steam_player_achievements(api_key: str, steam_id: str, appid: str):
    """
    Returns (done, total) or (None, None) if not available/private/no achievements.
    """
    url = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    params = {"key": api_key, "steamid": steam_id, "appid": appid, "l": "english"}

    try:
        r = requests.get(url, params=params, timeout=25)
        if r.status_code != 200:
            return None, None

        j = r.json()
        pa = j.get("playerstats")
        if not pa or pa.get("success") is False:
            return None, None

        achievements = pa.get("achievements")
        if not achievements:
            return None, None

        total = len(achievements)
        done = sum(1 for a in achievements if a.get("achieved") == 1)
        return done, total
    except Exception:
        return None, None


def fetch_steam_player_achievements_list(api_key: str, steam_id: str, appid: str):
    """
    Returns list of player achievements with apiname/achieved/unlocktime or None.
    """
    url = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    params = {"key": api_key, "steamid": steam_id, "appid": appid, "l": "english"}

    try:
        r = requests.get(url, params=params, timeout=25)
        if r.status_code != 200:
            return None

        j = r.json()
        pa = j.get("playerstats")
        if not pa or pa.get("success") is False:
            return None

        return pa.get("achievements") or None
    except Exception:
        return None


def fetch_steam_schema_for_game(api_key: str, appid: str):
    """
    Returns the achievements schema (icons, display names, descriptions) or None.
    """
    url = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
    params = {"key": api_key, "appid": appid}

    try:
        r = requests.get(url, params=params, timeout=25)
        if r.status_code != 200:
            return None

        j = r.json()
        game = j.get("game")
        if not game:
            return None

        stats = game.get("availableGameStats") or {}
        ach = stats.get("achievements") or []
        return ach
    except Exception:
        return None


# ==============================
# DATABASE INITIALIZATION
# ==============================
def init_db():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY ,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,

        name TEXT NOT NULL,
        platform TEXT NOT NULL,
        developer TEXT,
        genre TEXT,

        playtime REAL DEFAULT 0,
        achievements_done INTEGER DEFAULT 0,
        achievements_total INTEGER DEFAULT 0,

        completion_percentage REAL DEFAULT 0,
        estimated_hours REAL DEFAULT 0,

        cover_url TEXT,
        notes TEXT,
        rating INTEGER DEFAULT 0,
        date_added TEXT,
        last_played TEXT,

        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)
    if not column_exists(cursor, "games", "rating"):
        cursor.execute("ALTER TABLE games ADD COLUMN rating INTEGER DEFAULT 0")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_platforms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        external_user_id TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(user_id, platform),
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)
    if not column_exists(cursor, "games", "notes"):
        cursor.execute("ALTER TABLE games ADD COLUMN notes TEXT")
    if not column_exists(cursor, "games", "external_game_id"):
        cursor.execute("ALTER TABLE games ADD COLUMN external_game_id TEXT")

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_games_user_platform_external
        ON games(user_id, platform, external_game_id)
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS steam_achievement_cache (
        user_id INTEGER NOT NULL,
        appid TEXT NOT NULL,
        payload TEXT NOT NULL,
        cached_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, appid),
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    conn.commit()
    conn.close()
init_db()

# ==============================
# DEBUG endpoint (to prove which DB + file Flask uses)
# ==============================
@app.route("/api/debug")
def api_debug():
    cache_count = 0
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM steam_achievement_cache")
            cache_count = cur.fetchone()[0]
    except Exception:
        pass

    return jsonify({
        "app_file": __file__,
        "cwd": os.getcwd(),
        "db_path": DB_PATH,
        "steam_key_set": bool(os.environ.get("STEAM_API_KEY")),
        "cache_rows": cache_count
    })


# ==============================
# HOME
# ==============================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return redirect(url_for("library") + "?success=Game deleted successfully")


# ==============================
# SIGNUP
# ==============================
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Missing fields"}), 400

    try:
        hashed_password = generate_password_hash(password)

        with db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, hashed_password)
            )
            cursor.execute("SELECT COUNT(*) FROM users")
            count = cursor.fetchone()[0]
            if count == 1:
                cursor.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (username,))
            conn.commit()

        return jsonify({"message": "User created successfully"}), 200

    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "Database error"}), 500


# ==============================
# LOGIN
# ==============================
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Missing fields"}), 400

    try:
        with db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, password FROM users WHERE username = ?", (username,))
            result = cursor.fetchone()

        if result is None:
            return jsonify({"error": "User not found"}), 400

        user_id, stored_password = result

        if not check_password_hash(stored_password, password):
            return jsonify({"error": "Incorrect password"}), 400

        session["user"] = username
        session["user_id"] = user_id

        return jsonify({"message": "Login successful", "redirect": "/library"}), 200

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "Database error"}), 500


# ==============================
# LIBRARY
# ==============================
@app.route("/library")
def library():
    if "user_id" not in session:
        return redirect(url_for("home"))

    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, playtime, completion_percentage, cover_url, platform, rating
            FROM games
            WHERE user_id = ?
            ORDER BY name COLLATE NOCASE ASC
        """, (session["user_id"],))
        games = cursor.fetchall()

    return render_template("library.html", games=games)


# ==============================
# GAME DETAIL
# ==============================
@app.route("/game/<int:game_id>")
def game_detail(game_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
                SELECT id, user_id, name, platform, developer, genre,
                       playtime, achievements_done, achievements_total,
                       completion_percentage, estimated_hours,
                       cover_url, notes, rating, date_added, last_played
                FROM games
                WHERE id = ? AND user_id = ?
        """, (game_id, session["user_id"]))              
        game = cursor.fetchone()              

    if not game:
        return redirect(url_for("library"))

    return render_template("game_detail.html", game=game)


# ==============================
# API: Steam achievements for a DB game_id (CACHED)
# ==============================
@app.route("/api/steam/achievements/<int:game_id>")
def api_steam_achievements(game_id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    refresh = request.args.get("refresh") == "1"

    # Validate game belongs to user + is Steam
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT platform, external_game_id, name
            FROM games
            WHERE id = ? AND user_id = ?
        """, (game_id, session["user_id"]))
        row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Game not found"}), 404

    platform, external_game_id, game_name = row
    if platform != "Steam" or not external_game_id:
        return jsonify({"error": "Achievements available only for Steam imported games."}), 400

    api_key = os.environ.get("STEAM_API_KEY")
    steam_id = get_connected_platform_id(session["user_id"], "Steam")

    if not api_key:
        return jsonify({"error": "STEAM_API_KEY missing in environment."}), 400
    if not steam_id:
        return jsonify({"error": "Steam is not connected for this account."}), 400

    now_ts = int(time.time())

    def build_base_payload(ach_list):
        total = len(ach_list)
        done = sum(1 for a in ach_list if a.get("achieved") == 1)
        completion = round((done / total) * 100, 2) if total else 0
        # IMPORTANT: ordered keys (so you SEE cached at the top of JSON)
        return {
            "game_name": game_name,
            "done": done,
            "total": total,
            "completion": completion,
            "achievements": ach_list
        }

    # -------------------------
    # CACHE HIT (and normalize)
    # -------------------------
    if not refresh:
        payload_text, cached_at = get_ach_cache(session["user_id"], external_game_id)
        if payload_text and cached_at and (now_ts - cached_at < ACH_CACHE_SECONDS):
            try:
                cached_payload = json.loads(payload_text)
            except Exception:
                cached_payload = {}

            ach_list = cached_payload.get("achievements") if isinstance(cached_payload, dict) else None
            if not isinstance(ach_list, list):
                ach_list = []

            base = build_base_payload(ach_list)

            # re-save normalized payload so the cache stops being "old format"
            set_ach_cache(session["user_id"], external_game_id, base, cached_at)

            response = {
                "cached": True,
                "cached_at": cached_at,
                **base
            }
            return jsonify(response), 200

    # -------------------------
    # FRESH FETCH
    # -------------------------
    schema = fetch_steam_schema_for_game(api_key, external_game_id)
    player_list = fetch_steam_player_achievements_list(api_key, steam_id, external_game_id)

    if not schema or not player_list:
        return jsonify({"error": "Achievements not available (private profile, no achievements, or Steam blocked)."}), 400

    player_map = {a.get("apiname"): a for a in player_list if a.get("apiname")}
    merged = []

    for a in schema:
        api_name = a.get("name")
        p = player_map.get(api_name, {})
        achieved = 1 if p.get("achieved") == 1 else 0

        merged.append({
            "api_name": api_name,
            "title": a.get("displayName") or api_name,
            "description": a.get("description") or "",
            "hidden": int(a.get("hidden") or 0),
            "icon": a.get("icon"),
            "icongray": a.get("icongray"),
            "achieved": achieved,
            "unlocktime": int(p.get("unlocktime") or 0)
        })

    # sort: unlocked first, then alpha
    merged.sort(key=lambda x: (0 if x["achieved"] else 1, (x["title"] or "").lower()))

    base = build_base_payload(merged)

    # Save cache (normalized)
    set_ach_cache(session["user_id"], external_game_id, base, now_ts)

    # Update the game completion in DB so library updates too
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE games
                SET achievements_done = ?, achievements_total = ?, completion_percentage = ?
                WHERE id = ? AND user_id = ?
            """, (base["done"], base["total"], base["completion"], game_id, session["user_id"]))
            conn.commit()
    except Exception as e:
        print("WARN: could not update game completion:", e)

    response = {
        "cached": False,
        "cached_at": now_ts,
        **base
    }
    return jsonify(response), 200
# ==============================
# ADD GAME (manual)
# ==============================
@app.route("/add-game", methods=["GET", "POST"])
def add_game():
    if "user_id" not in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        name = request.form.get("name")
        platform = request.form.get("platform")
        developer = request.form.get("developer")
        genre = request.form.get("genre")

        playtime = float(request.form.get("playtime") or 0)
        achievements_done = int(request.form.get("achievements_done") or 0)
        achievements_total = int(request.form.get("achievements_total") or 0)
        estimated_hours = float(request.form.get("estimated_hours") or 0)
        cover_url = request.form.get("cover_url")

        if not name or not platform:
            return redirect(url_for("add_game"))

        completion_percentage = 0
        if achievements_total > 0:
            completion_percentage = round((achievements_done / achievements_total) * 100, 2)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO games (
                    user_id, name, platform, developer, genre,
                    playtime, achievements_done, achievements_total,
                    completion_percentage, estimated_hours,
                    cover_url, date_added, last_played, external_game_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session["user_id"], name, platform, developer, genre,
                playtime, achievements_done, achievements_total,
                completion_percentage, estimated_hours,
                cover_url, now, now, None
            ))
            conn.commit()

        return redirect(url_for("library"))

    return render_template("add_game.html")


# ==============================
# STATS
# ==============================
@app.route("/stats")
def stats():
    if "user_id" not in session:
        return redirect(url_for("home"))

    user_id = session["user_id"]

    with db_connect() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM games WHERE user_id = ?", (user_id,))
        total_games = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COALESCE(SUM(playtime), 0) FROM games WHERE user_id = ?", (user_id,))
        total_playtime = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT COALESCE(SUM(achievements_done), 0), COALESCE(SUM(achievements_total), 0)
            FROM games
            WHERE user_id = ?
        """, (user_id,))
        achievements_done_sum, achievements_total_sum = cursor.fetchone()

        cursor.execute("""
            SELECT COALESCE(AVG(completion_percentage), 0)
            FROM games
            WHERE user_id = ?
        """, (user_id,))
        avg_completion = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT id, name, playtime, completion_percentage, cover_url, platform, rating
            FROM games
            WHERE user_id = ?
            ORDER BY playtime DESC
            LIMIT 1
        """, (user_id,))
        most_played = cursor.fetchone()

        cursor.execute("""
            SELECT id, name, date_added
            FROM games
            WHERE user_id = ?
            ORDER BY date_added DESC
            LIMIT 1
        """, (user_id,))
        recently_added = cursor.fetchone()

        cursor.execute("""
            SELECT id, name, last_played
            FROM games
            WHERE user_id = ?
            ORDER BY last_played DESC
            LIMIT 1
        """, (user_id,))
        recently_played = cursor.fetchone()

        cursor.execute("""
            SELECT platform, COUNT(*)
            FROM games
            WHERE user_id = ?
            GROUP BY platform
            ORDER BY COUNT(*) DESC
        """, (user_id,))
        platforms = cursor.fetchall()

    achievement_pct = 0
    if achievements_total_sum and achievements_total_sum > 0:
        achievement_pct = round((achievements_done_sum / achievements_total_sum) * 100, 2)

    return render_template(
        "stats.html",
        total_games=total_games,
        total_playtime=total_playtime,
        achievements_done_sum=achievements_done_sum,
        achievements_total_sum=achievements_total_sum,
        achievement_pct=achievement_pct,
        avg_completion=round(avg_completion, 2),
        most_played=most_played,
        recently_added=recently_added,
        recently_played=recently_played,
        platforms=platforms
    )


# ==============================
# CONNECT STEAM
# ==============================
@app.route("/connect/steam", methods=["GET", "POST"])
def connect_steam():
    if "user_id" not in session:
        return redirect(url_for("home"))

    msg = None
    saved_steamid = get_connected_platform_id(session["user_id"], "Steam")

    if request.method == "POST":
        steam_id = (request.form.get("steam_id") or "").strip()

        if not (steam_id.isdigit() and len(steam_id) >= 16):
            msg = "Please enter a valid SteamID64 (numbers only)."
            return render_template("connect_steam.html", msg=msg, saved_steamid=saved_steamid)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            with db_connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO user_platforms (user_id, platform, external_user_id, created_at, updated_at)
                    VALUES (?, 'Steam', ?, ?, ?)
                    ON CONFLICT(user_id, platform)
                    DO UPDATE SET external_user_id = excluded.external_user_id,
                                  updated_at = excluded.updated_at
                """, (session["user_id"], steam_id, now, now))
                conn.commit()

            msg = "Steam connected successfully."
            saved_steamid = steam_id

        except Exception as e:
            print("ERROR:", e)
            msg = "Database error while saving SteamID."

    return render_template("connect_steam.html", msg=msg, saved_steamid=saved_steamid)


# ==============================
# IMPORT STEAM (keep your existing template/route)
# ==============================
@app.route("/import/steam", methods=["GET", "POST"])
@app.route("/import/steam", methods=["GET", "POST"])
def import_steam():
    if "user_id" not in session:
        return redirect(url_for("home"))

    api_key = os.environ.get("STEAM_API_KEY")
    steam_id = get_connected_platform_id(session["user_id"], "Steam")

    error = None
    info = None
    found_count = None
    preview_names = []

    if request.method == "POST":
        action = request.form.get("action")

        if not api_key:
            error = "STEAM_API_KEY not set in environment."
        elif not steam_id:
            error = "Steam is not connected for this account."
        else:
            games, err = fetch_steam_owned_games(api_key, steam_id)

            if err:
                error = err
            else:
                found_count = len(games)

                if action == "preview":
                    preview_names = [g.get("name") for g in games[:10]]
                    info = "Preview fetched successfully."

                elif action == "import":
                    with db_connect() as conn:
                        cursor = conn.cursor()

                        for g in games:
                            name = g.get("name")
                            appid = str(g.get("appid"))
                            playtime = round(g.get("playtime_forever", 0) / 60, 2)

                            cover_url = steam_library_poster_url(appid)

                            cursor.execute("""
                                INSERT INTO games (
                                    user_id, name, platform,
                                    playtime, cover_url,
                                    date_added, last_played,
                                    external_game_id
                                )
                                VALUES (?, ?, 'Steam', ?, ?, NOW(), NOW(), ?)
                                ON CONFLICT(user_id, platform, external_game_id)
                                DO UPDATE SET
                                    playtime = excluded.playtime,
                                    cover_url = excluded.cover_url
                            """, (
                                session["user_id"],
                                name,
                                playtime,
                                cover_url,
                                appid
                            ))

                        conn.commit()

                    info = f"Imported {len(games)} games successfully."

    return render_template(
        "import_steam.html",
        api_key_set=bool(api_key),
        steam_id=steam_id,
        error=error,
        info=info,
        found_count=found_count,
        preview_names=preview_names
    )
# EDIT GAME
# ==============================
@app.route("/edit-game/<int:game_id>", methods=["GET", "POST"])
def edit_game(game_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    user_id = session["user_id"]

    with db_connect() as conn:
        cursor = conn.cursor()

        # Check if game exists and belongs to user
        cursor.execute("""
            SELECT * FROM games
            WHERE id = ? AND user_id = ?
        """, (game_id, user_id))
        game = cursor.fetchone()

        if not game:
            return redirect(url_for("library"))

        if request.method == "POST":
            playtime = float(request.form.get("playtime") or 0)
            estimated_hours = float(request.form.get("estimated_hours") or 0)
            achievements_done = int(request.form.get("achievements_done") or 0)
            achievements_total = int(request.form.get("achievements_total") or 0)

            completion = 0
            if achievements_total > 0:
                completion = round((achievements_done / achievements_total) * 100, 2)

            cursor.execute("""
                UPDATE games
                SET playtime = ?,
                    estimated_hours = ?,
                    achievements_done = ?,
                    achievements_total = ?,
                    completion_percentage = ?
                WHERE id = ? AND user_id = ?
            """, (
                playtime,
                estimated_hours,
                achievements_done,
                achievements_total,
                completion,
                game_id,
                user_id
            ))

            conn.commit()
            return redirect(url_for("game_detail", game_id=game_id) + "?success=Game updated successfully")
    return render_template("edit_game.html", game=game)
# ==============================
# DELETE GAME
# ==============================
@app.route("/delete-game/<int:game_id>", methods=["POST"])
def delete_game(game_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    user_id = session["user_id"]

    with db_connect() as conn:
        cursor = conn.cursor()

        # Make sure game belongs to user
        cursor.execute("""
            DELETE FROM games
            WHERE id = ? AND user_id = ?
        """, (game_id, user_id))

        conn.commit()

    return redirect(url_for("library"))
@app.route("/update-notes/<int:game_id>", methods=["POST"])
def update_notes(game_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    notes = request.form.get("notes")

    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE games
            SET notes = ?
            WHERE id = ? AND user_id = ?
        """, (notes, game_id, session["user_id"]))
        conn.commit()

    return redirect(url_for("game_detail", game_id=game_id))
@app.route("/update-rating/<int:game_id>", methods=["POST"])
def update_rating(game_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    rating = int(request.form.get("rating", 0))

    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE games
            SET rating = ?
            WHERE id = ? AND user_id = ?
        """, (rating, game_id, session["user_id"]))
        conn.commit()

    return redirect(url_for("game_detail", game_id=game_id))
# ==============================
# ACHIEVEMENT DASHBOARD
# ==============================
@app.route("/achievements")
def achievements_dashboard():
    if "user_id" not in session:
        return redirect(url_for("home"))

    user_id = session["user_id"]

    with db_connect() as conn:
        cursor = conn.cursor()

        # Total achievements summary
        cursor.execute("""
            SELECT 
                COALESCE(SUM(achievements_done),0),
                COALESCE(SUM(achievements_total),0)
            FROM games
            WHERE user_id = ?
        """, (user_id,))
        total_done, total_total = cursor.fetchone()

        # Top 5 most completed games
        cursor.execute("""
            SELECT name, completion_percentage
            FROM games
            WHERE user_id = ?
            ORDER BY completion_percentage DESC
            LIMIT 5
        """, (user_id,))
        top_completed = cursor.fetchall()

        # Bottom 5 least completed games
        cursor.execute("""
            SELECT name, completion_percentage
            FROM games
            WHERE user_id = ?
            ORDER BY completion_percentage ASC
            LIMIT 5
        """, (user_id,))
        least_completed = cursor.fetchall()

    overall_percent = 0
    if total_total > 0:
        overall_percent = round((total_done / total_total) * 100, 2)

    return render_template(
        "achievements_dashboard.html",
        total_done=total_done,
        total_total=total_total,
        overall_percent=overall_percent,
        top_completed=top_completed,
        least_completed=least_completed
    )
@app.route("/admin")
def admin_dashboard():
    if "user_id" not in session:
        return redirect(url_for("home"))

    with db_connect() as conn:
        cursor = conn.cursor()

        # Check if current user is admin
        cursor.execute("SELECT is_admin FROM users WHERE id = ?", (session["user_id"],))
        result = cursor.fetchone()

        if not result or result[0] != 1:
            return redirect(url_for("library"))

        # Total users
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # Total games
        cursor.execute("SELECT COUNT(*) FROM games")
        total_games = cursor.fetchone()[0]

        # Total Steam-connected users
        cursor.execute("SELECT COUNT(*) FROM user_platforms WHERE platform = 'Steam'")
        steam_connected = cursor.fetchone()[0]

        # Get all users
        cursor.execute("SELECT id, username, is_admin FROM users")
        users = cursor.fetchall()

        return render_template(
            "admin_dashboard.html",
            users=users,
            total_users=total_users,
            total_games=total_games,
            steam_connected=steam_connected
        )
@app.route("/delete-user/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if "user_id" not in session:
        return redirect(url_for("home"))

    with db_connect() as conn:
        cursor = conn.cursor()

        # Verify admin
        cursor.execute("SELECT is_admin FROM users WHERE id = ?", (session["user_id"],))
        result = cursor.fetchone()

        if not result or result[0] != 1:
            return redirect(url_for("library"))

        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

    return redirect(url_for("admin_dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)