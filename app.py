from flask import Flask, render_template
from flask import Flask, render_template, request, jsonify
import sqlite3


app = Flask(__name__)

# Function to initialize database
def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


@app.route("/")
def home():
    return render_template("index.html")
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()

    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Missing fields"}), 400

    try:
        with sqlite3.connect("database.db") as conn:
            cursor = conn.cursor()

            cursor.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, password)
            )

            conn.commit()

        return jsonify({"message": "User created successfully"}), 200

    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "Database error"}), 500
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()

    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Missing fields"}), 400

    try:
        with sqlite3.connect("database.db") as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT password FROM users WHERE username = ?",
                (username,)
            )

            result = cursor.fetchone()

        # 🧠 Core logic
        if result is None:
            return jsonify({"error": "User not found"}), 400

        stored_password = result[0]

        if stored_password != password:
            return jsonify({"error": "Incorrect password"}), 400

        return jsonify({"message": "Login successful", "redirect": "/dashboard"}), 200

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "Database error"}), 500
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")
if __name__ == "__main__":
    init_db()  # create DB on startup
    app.run(debug=True)