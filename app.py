import os
import json
import secrets
import requests
from datetime import datetime, timedelta
import logging
from concurrent.futures import ThreadPoolExecutor

# Frameworks
from flask import Flask, render_template, jsonify, request, send_from_directory, redirect, url_for, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from flask_socketio import SocketIO, emit
# Database & Cache
import psycopg2
from psycopg2.extras import RealDictCursor
import redis

# --- 1. CONFIGURATION & SETUP ---

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_for_session')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DansoBackend")

# WebSocket Setup (Async Mode)
# Using 'gevent' or 'eventlet' is recommended for production
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading') 

# Redis Client Setup (Safe Fallback)
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1)
    redis_client.ping()
    logger.info("✅ [Cache] Redis connected.")
except Exception as e:
    logger.warning(f"⚠️ [Cache] Redis connection failed: {e}. Running without cache.")
    redis_client = None

# Thread Pool for DB Concurrency
executor = ThreadPoolExecutor(max_workers=4)

# --- 2. DATABASE HELPER FUNCTIONS ---

def get_db_connection():
    """Creates a fresh PostgreSQL connection."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    """Initialize database tables on startup."""
    conn = None
    try:
        if not DATABASE_URL: return
        conn = get_db_connection()
        cursor = conn.cursor()

        # Create Tables
        cursor.execute("""CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, price REAL NOT NULL, time TIMESTAMP NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS recommendations (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, price REAL NOT NULL, time TIMESTAMP NOT NULL, probability_score INTEGER)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS posts (id SERIAL PRIMARY KEY, author TEXT NOT NULL, content TEXT NOT NULL, time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS fcm_tokens (id SERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                oauth_provider TEXT,
                is_premium BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # STS Targets Table (Expected from Pipeline)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sts_live_targets (
                ticker TEXT PRIMARY KEY,
                price REAL,
                ai_score REAL,
                obi REAL,
                vpin REAL,
                tick_speed INTEGER,
                vwap_dist REAL,
                status TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        
        # Schema Migrations (Safe)
        migrations = [
            "ALTER TABLE recommendations ADD COLUMN probability_score INTEGER",
            "ALTER TABLE fcm_tokens ADD COLUMN min_score INTEGER DEFAULT 0"
        ]
        for query in migrations:
            try:
                cursor.execute(query)
                conn.commit()
            except psycopg2.Error as e:
                conn.rollback()
                if e.pgcode != '42701': # Ignore 'duplicate column' errors
                    logger.error(f"Migration Error: {e}")

        cursor.close()
        conn.close()
        logger.info("✅ [DB] Init success.")
    except Exception as e:
        if conn: conn.close()
        logger.error(f"❌ [DB] Init failed: {e}")

# --- 3. AUTHENTICATION & LOGIN CONFIG ---

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

class User(UserMixin):
    def __init__(self, id, email, is_premium=False):
        self.id = str(id)
        self.email = email
        self.is_premium = is_premium

@login_manager.user_loader
def load_user(user_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, is_premium FROM users WHERE id = %s", (user_id,))
        user_data = cursor.fetchone()
        if user_data:
            return User(id=user_data[0], email=user_data[1], is_premium=user_data[2])
    except Exception as e:
        logger.error(f"Login session error: {e}")
    finally:
        if conn: conn.close()
    return None

# --- 4. WEBSOCKET HANDLERS ---

@socketio.on('connect', namespace='/ws/dashboard')
def handle_connect():
    logger.info("[WS] Client connected to dashboard stream")
    # Optionally push immediate data upon connection
    # emit('dashboard_update', {'msg': 'Welcome'}, namespace='/ws/dashboard')

def broadcast_dashboard_update(payload):
    """Helper to broadcast data to all connected dashboard clients."""
    try:
        socketio.emit('dashboard_update', payload, namespace='/ws/dashboard')
    except Exception as e:
        logger.error(f"WebSocket Broadcast Error: {e}")

# --- 5. CORE API: UNIFIED DASHBOARD (Optimized) ---

def fetch_unified_data_from_db():
    """
    Performs synchronous DB queries to fetch all dashboard data.
    Designed to be run in a thread executor.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 1. SCANNER: Status
        cursor.execute("SELECT value FROM status WHERE key = 'status_data' ORDER BY last_updated DESC LIMIT 1")
        status_row = cursor.fetchone()
        scanner_status = json.loads(status_row['value']) if status_row else {
            'last_scan_time': 'Initializing...', 'watching_count': 0, 'watching_tickers': []
        }

        # 2. SCANNER: Signals (Explosions)
        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'HH24:MI:SS') as time FROM signals ORDER BY time DESC LIMIT 50")
        signals = cursor.fetchall()

        # 3. SCANNER: Recommendations (Setups)
        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'HH24:MI:SS') as time, probability_score FROM recommendations ORDER BY time DESC LIMIT 50")
        recommendations = cursor.fetchall()

        # 4. STS: Top 3 Active Targets
        # Sorting Logic: FIRED > AIMING > High Score
        cursor.execute("""
            SELECT ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status 
            FROM sts_live_targets 
            ORDER BY 
                CASE 
                    WHEN status = 'FIRED' THEN 1 
                    WHEN status = 'AIMING' THEN 2 
                    ELSE 3 
                END ASC,
                ai_score DESC
            LIMIT 3
        """)
        raw_targets = cursor.fetchall()
        
        # Normalize STS data for frontend
        sts_targets = []
        for r in raw_targets:
            raw_score = r.get('ai_score') or 0
            sts_targets.append({
                'ticker': r['ticker'],
                'price': r['price'],
                'ai_prob': raw_score / 100.0,
                'ai_score_raw': raw_score,
                'obi': r['obi'],
                'vpin': r['vpin'],
                'tick_speed': r['tick_speed'],
                'vwap_dist': r['vwap_dist'],
                'status': r['status']
            })

        cursor.close()

        # Reuse recent signals as STS logs if specific log table doesn't exist
        sts_logs = signals[:5] 

        return {
            "scanner": {
                "status": scanner_status,
                "signals": signals,
                "recommendations": recommendations
            },
            "sts": {
                "targets": sts_targets,
                "recent_logs": sts_logs
            },
            "meta": {
                "server_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "db_status": "connected"
            }
        }

    except Exception as e:
        logger.error(f"DB Query Error in fetch_unified_data: {e}")
        return None # Signal failure
    finally:
        if conn: conn.close()


@app.route('/api/dashboard/unified')
@login_required
def get_unified_dashboard():
    """
    Unified endpoint for Dashboard.
    Strategy:
    1. Check Redis Cache (TTL 2s)
    2. If Miss: Query DB (via ThreadPool) -> Update Cache -> Broadcast WS
    3. If Redis down: Query DB directly
    """
    CACHE_KEY = "unified_dashboard_v1"
    TTL = 2
    
    # A. Try Cache First
    if redis_client:
        try:
            cached_data = redis_client.get(CACHE_KEY)
            if cached_data:
                response = json.loads(cached_data)
                response['meta']['cache'] = 'redis'
                return jsonify(response)
        except Exception as e:
            logger.warning(f"Redis Read Error: {e}")

    # B. Cache Miss or Redis Down -> Query Database
    future = executor.submit(fetch_unified_data_from_db)
    db_data = future.result()

    if not db_data:
        return jsonify({"error": "Failed to fetch data from DB"}), 500

    # C. Update Cache & Broadcast
    db_data['meta']['cache'] = 'db'
    
    if redis_client:
        try:
            redis_client.setex(CACHE_KEY, TTL, json.dumps(db_data))
            broadcast_dashboard_update(db_data)
        except Exception as e:
            logger.warning(f"Redis Write Error: {e}")

    return jsonify(db_data)

# --- 6. PAGE ROUTES ---

@app.route('/')
def landing_page():
    return render_template('landing.html') 

@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

@app.route('/dashboard') 
@login_required
def dashboard_page():
    return render_template('dashboard.html', user=current_user)

@app.route('/sts')
@login_required
def sts_page():
    return render_template('sts.html')

# --- 7. AUTH ROUTES ---

@app.route('/auth/google')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    nonce = secrets.token_urlsafe(16)
    session['google_auth_nonce'] = nonce
    return oauth.google.authorize_redirect(redirect_uri, access_type='offline', prompt='consent', nonce=nonce)

@app.route('/auth/google/callback')
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
        nonce = session.pop('google_auth_nonce', None) 
        user_info = oauth.google.parse_id_token(token, nonce=nonce)
        email = user_info['email']

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, email, is_premium FROM users WHERE email = %s", (email,))
        user_data = cursor.fetchone()
        
        if not user_data:
            cursor.execute("INSERT INTO users (email, oauth_provider, is_premium) VALUES (%s, 'google', FALSE) RETURNING id", (email,))
            new_user_id = cursor.fetchone()[0]
            conn.commit()
            user = User(id=new_user_id, email=email, is_premium=False)
        else:
            user = User(id=user_data[0], email=user_data[1], is_premium=user_data[2])
        
        cursor.close()
        conn.close()
        login_user(user)
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        logger.error(f"OAuth Error: {e}")
        return "Google Login Failed. Please try again.", 400

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('landing_page'))

# --- 8. STATIC FILES ---

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/firebase-messaging-sw.js')
def serve_firebase_sw_root():
    return send_from_directory('.', 'firebase-messaging-sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('.', 'manifest.json', mimetype='application/manifest+json')

@app.route('/favicon.ico')
def serve_favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'), 'danso_logo.png', mimetype='image/png')

# --- 9. EXISTING API ENDPOINTS (Preserved) ---

@app.route('/api/dashboard')
@login_required
def get_dashboard_data():
    # Legacy endpoint preserved for backward compatibility
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT value FROM status WHERE key = 'status_data' ORDER BY last_updated DESC LIMIT 1")
        status_row = cursor.fetchone()
        status = json.loads(status_row['value']) if status_row else {'last_scan_time': 'N/A', 'watching_count': 0, 'watching_tickers': []}
        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time FROM signals ORDER BY time DESC LIMIT 50")
        signals = cursor.fetchall()
        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time, probability_score FROM recommendations ORDER BY time DESC LIMIT 50")
        recommendations = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'status': status, 'signals': signals, 'recommendations': recommendations})
    except Exception as e:
        if conn: conn.close()
        return jsonify({'status': {}, 'signals': [], 'recommendations': []})

@app.route('/api/sts/status')
def get_sts_status():
    # Legacy endpoint preserved
    conn = None
    try:
        conn = get_db_connection() 
        cursor = conn.cursor(cursor_factory=RealDictCursor) 
        cursor.execute("""
            SELECT ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status 
            FROM sts_live_targets 
            ORDER BY 
                CASE WHEN status = 'FIRED' THEN 1 WHEN status = 'AIMING' THEN 2 ELSE 3 END ASC,
                ai_score DESC
            LIMIT 3
        """)
        rows = cursor.fetchall()
        
        targets = []
        for r in rows:
            raw_score = r.get('ai_score') or 0
            targets.append({
                'ticker': r['ticker'],
                'price': r['price'],
                'ai_prob': raw_score / 100.0,
                'obi': r['obi'],
                'vpin': r['vpin'],
                'tick_speed': r['tick_speed'],
                'vwap_dist': r['vwap_dist'],
                'status': r['status']
            })
        cursor.close()
        conn.close()
        return jsonify({'targets': targets, 'logs': []})
    except Exception as e:
        if conn: conn.close()
        return jsonify({'targets': [], 'logs': [], 'error': str(e)})

@app.route('/api/posts', methods=['GET', 'POST'])
@login_required
def handle_posts():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        if request.method == 'GET':
            cursor.execute("SELECT author, content, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time FROM posts ORDER BY time DESC LIMIT 100")
            posts = cursor.fetchall()
            cursor.close()
            conn.close()
            return jsonify({"status": "OK", "posts": posts})
            
        elif request.method == 'POST':
            data = request.get_json()
            author = data.get('author', 'Anonymous')
            content = data.get('content')
            if not content: return jsonify({"status": "error", "message": "Content empty"}), 400
            cursor.execute("INSERT INTO posts (author, content, time) VALUES (%s, %s, %s)", (author, content, datetime.now()))
            conn.commit()
            cursor.close()
            conn.close()
            return jsonify({"status": "OK", "message": "Post created."})
            
    except Exception as e:
        if conn: conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/quote/<string:ticker>')
@login_required
def get_quote(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key missing"}), 500
    url = f"https://api.polygon.io/v3/quotes/{ticker.upper()}?limit=1&apiKey={API_KEY}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            return jsonify(data['results'][0])
        return jsonify({"status": "error", "message": "Ticker not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/details/<string:ticker>')
@login_required
def get_ticker_details(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key missing"}), 500
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}?apiKey={API_KEY}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            r = data['results']
            f = r.get('financials', {})
            # Safe extraction logic
            details = {
                "ticker": r.get('ticker'), "name": r.get('name'),
                "industry": r.get('sic_description'),
                "description": r.get('description', 'No description available.'),
                "logo_url": r.get('branding', {}).get('logo_url', '') + f"?apiKey={API_KEY}",
                "financials": {
                    "market_cap": f.get('market_capitalization', {}).get('value', 'N/A'),
                    "pe_ratio": f.get('price_to_earnings_ratio', 'N/A'),
                    "ps_ratio": f.get('price_to_sales_ratio', 'N/A'),
                    "dividend_yield": f.get('dividend_yield', {}).get('value', 'N/A')
                }
            }
            return jsonify({"status": "OK", "results": details})
        return jsonify({"status": "error", "message": "Details not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/chart_data/<string:ticker>')
@login_required
def get_chart_data(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key missing"}), 500
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        past_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{past_date}/{today}?sort=asc&limit=5000&apiKey={API_KEY}"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            chart_data = [{"time": bar['t']/1000, "open": bar['o'], "high": bar['h'], "low": bar['l'], "close": bar.get('c', bar['o'])} for bar in data['results']]
            return jsonify({"status": "OK", "results": chart_data})
        return jsonify({"status": "error", "message": "Chart data not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/market_overview')
def get_market_overview():
    if not API_KEY: return jsonify({"status": "error", "message": "API Key missing"}), 500
    try:
        # Using ThreadPool to fetch both concurrently would be better, but keeping simple here
        url_g = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}"
        res_g = requests.get(url_g, timeout=5); 
        gainers = res_g.json().get('tickers') or []
        
        url_l = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={API_KEY}"
        res_l = requests.get(url_l, timeout=5); 
        losers = res_l.json().get('tickers') or []
        
        return jsonify({"status": "OK", "gainers": gainers, "losers": losers})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/set_alert_threshold', methods=['POST'])
def set_alert_threshold():
    data = request.get_json()
    token = data.get('token')
    threshold = data.get('threshold')
    if not token or threshold is None: return jsonify({"status": "error", "message": "Missing params"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE fcm_tokens SET min_score = %s WHERE token = %s", (int(threshold), token))
        if cursor.rowcount == 0:
            cursor.close(); conn.close()
            return jsonify({"status": "error", "message": "Token not found"}), 404
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status": "OK", "message": "Threshold updated"}), 200
    except Exception as e:
        if conn: conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.json
    token = data.get('token')
    if not token: return jsonify({"status": "error", "message": "No token"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO fcm_tokens (token) VALUES (%s) ON CONFLICT (token) DO NOTHING", (token,))
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status": "success"}), 201
    except Exception as e:
        if conn: conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/secret/count')
def check_user_count():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
        except:
            user_count = 0 
            conn.rollback()

        try:
            cursor.execute("SELECT COUNT(*) FROM fcm_tokens")
            device_count = cursor.fetchone()[0]
        except:
            device_count = 0
            conn.rollback()
        
        cursor.close()
        conn.close()
        
        active_users = max(user_count, device_count)
        remaining = 1000 - active_users
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Danso Launch Status</title>
            <meta http-equiv="refresh" content="10">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ background-color: #05070a; color: #e0e0e0; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
                .container {{ text-align: center; border: 2px solid #00ff9d; padding: 40px; border-radius: 20px; box-shadow: 0 0 30px rgba(0, 255, 157, 0.2); background: #0a0f14; }}
                h1 {{ color: #00ff9d; margin-bottom: 30px; font-size: 24px; text-transform: uppercase; letter-spacing: 2px; }}
                .stat-box {{ margin: 20px 0; padding: 20px; background: rgba(255,255,255,0.05); border-radius: 10px; }}
                .number {{ font-size: 3em; font-weight: bold; color: #fff; display: block; margin-top: 10px; }}
                .label {{ color: #888; font-size: 0.9em; text-transform: uppercase; }}
                .remaining {{ color: #ff4d4d; font-weight: bold; margin-top: 30px; font-size: 1.2em; }}
                hr {{ border-color: #333; opacity: 0.3; margin: 30px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🚀 Launch Status</h1>
                <div class="stat-box">
                    <span class="label">Total Signed Up</span>
                    <span class="number">{user_count}</span>
                </div>
                <div class="stat-box">
                    <span class="label">Active Devices (App)</span>
                    <span class="number" style="color: #00e0ff;">{device_count}</span>
                </div>
                <hr>
                <div class="remaining">
                    🔥 Spots Left: {remaining} / 1,000
                </div>
            </div>
        </body>
        </html>
        """
    except Exception as e:
        return f"Error: {e}"

# Initialize DB on Startup
init_db()

if __name__ == '__main__':
    # Use socketio.run instead of app.run for WebSocket support
    socketio.run(app, debug=True, port=5000)