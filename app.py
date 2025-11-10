from flask import Flask, render_template, jsonify, request, send_from_directory
import json
import os
import requests
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# --- API 키 및 DB 설정 ---
API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- 1. 메인 페이지 및 PWA 파일 라우트 ---
@app.route('/')
def dashboard_page():
    return render_template('index.html')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/firebase-messaging-sw.js')
def serve_firebase_sw_root():
    return send_from_directory('.', 'firebase-messaging-sw.js', mimetype='application/javascript')

@app.route('/static/firebase-messaging-sw.js')
def serve_firebase_sw_static():
    return send_from_directory('static', 'firebase-messaging-sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('.', 'manifest.json', mimetype='application/manifest+json')

@app.route('/favicon.ico')
def serve_favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'),
                               'danso_logo.png', mimetype='image/png')

# --- 🔽 [수정됨] 대시보드 데이터 API 🔽 ---
@app.route('/api/dashboard')
def get_dashboard_data():
    conn = None
    cursor = None  # <-- 변수를 try 밖에서 초기화
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

        return jsonify({'status': status, 'signals': signals, 'recommendations': recommendations})
    except Exception as e:
        print(f"Error in /api/dashboard: {e}")
        return jsonify({'status': {'last_scan_time': 'Scanner waiting...', 'watching_count': 0, 'watching_tickers': []}, 'signals': [], 'recommendations': []})
    finally:
        # --- 🔽 [수정됨] 🔽 ---
        # cursor와 conn이 모두 성공적으로 생성되었을 때만 닫도록 수정
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        # --- 🔼 [수정됨] 🔼 ---

# --- 4 & 5. 커뮤니티 API (게시글 읽기/쓰기) ---
@app.route('/api/posts', methods=['GET', 'POST'])
def handle_posts():
    conn = None
    cursor = None # <-- 변수를 try 밖에서 초기화
    try:
        conn = get_db_connection()
        if request.method == 'GET':
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT author, content, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time FROM posts ORDER BY time DESC LIMIT 100")
            posts = cursor.fetchall()
            return jsonify({"status": "OK", "posts": posts})
        
        elif request.method == 'POST':
            data = request.get_json()
            author = data.get('author', 'Anonymous')
            content = data.get('content')
            if not content:
                return jsonify({"status": "error", "message": "Content is empty."}), 400
            
            cursor = conn.cursor()
            cursor.execute("INSERT INTO posts (author, content, time) VALUES (%s, %s, %s)",
                           (author, content, datetime.now()))
            conn.commit()
            return jsonify({"status": "OK", "message": "Post created."})
    except Exception as e:
        print(f"Error in /api/posts: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # --- 🔽 [수정됨] 🔽 ---
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        # --- 🔼 [수정됨] 🔼 ---


# --- 6. 실시간 호가 API ---
@app.route('/api/quote/<string:ticker>')
def get_quote(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    url = f"https://api.polygon.io/v3/quotes/{ticker.upper()}?limit=1&apiKey={API_KEY}"
    try:
        response = requests.get(url)
        response.raise_for_status() # HTTP 오류가 나면 예외 발생
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            return jsonify(data['results'][0])
        return jsonify({"status": "error", "message": "Ticker not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 7. 기업 상세 정보 API ---
@app.route('/api/details/<string:ticker>')
def get_ticker_details(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}?apiKey={API_KEY}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            results = data['results']
            logo_url = results.get('branding', {}).get('logo_url', '')
            if logo_url: logo_url += f"?apiKey={API_KEY}"
            
            financials = results.get('financials', {})
            details = {
                "ticker": results.get('ticker'), "name": results.get('name'),
                "industry": results.get('sic_description'),
                "description": results.get('description', 'No description available.'),
                "logo_url": logo_url,
                "financials": {
                    "market_cap": financials.get('market_capitalization', {}).get('value'),
                    "pe_ratio": financials.get('price_to_earnings_ratio'),
                    "ps_ratio": financials.get('price_to_sales_ratio'),
                    "dividend_yield": financials.get('dividend_yield', {}).get('value')
                }
            }
            return jsonify({"status": "OK", "results": details})
        return jsonify({"status": "error", "message": "Ticker details not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 8. 1분봉 차트 데이터 API ---
@app.route('/api/chart_data/<string:ticker>')
def get_chart_data(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        past_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{past_date}/{today}?sort=asc&limit=5000&apiKey={API_KEY}"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            chart_data = [{
                "time": bar['t'] / 1000, "open": bar['o'],
                "high": bar['h'], "low": bar['l'], "close": bar.get('c', bar['o'])
            } for bar in data['results']]
            return jsonify({"status": "OK", "results": chart_data})
        return jsonify({"status": "error", "message": "Chart data not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 9. 시장 개요 API ---
@app.route('/api/market_overview')
def get_market_overview():
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    try:
        gainers_url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}"
        losers_url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={API_KEY}"
        gainers = requests.get(gainers_url).json().get('tickers', [])
        losers = requests.get(losers_url).json().get('tickers', [])
        return jsonify({"status": "OK", "gainers": gainers, "losers": losers})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 10. 푸시 구독 API ---
@app.route('/subscribe', methods=['POST'])
def subscribe():
    """PWA 푸시 구독 정보(객체 전체)를 받아서 DB에 저장합니다."""
    subscription_data = request.json
    if not subscription_data or 'endpoint' not in subscription_data:
        return jsonify({'status': 'error', 'message': 'Invalid subscription data provided'}), 400
        
    token_string_to_store = json.dumps(subscription_data)
    conn = None
    cursor = None # <-- 변수를 try 밖에서 초기화
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO fcm_tokens (token) VALUES (%s) ON CONFLICT (token) DO NOTHING",
            (token_string_to_store,)
        )
        conn.commit()
        print(f"✅ [FCM] 새 구독자 저장 성공: {subscription_data.get('endpoint')[:50]}...")
        return jsonify({'status': 'OK', 'message': 'Subscription saved successfully'})
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ [FCM] 구독자 저장 실패: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        # --- 🔽 [수정됨] 🔽 ---
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        # --- 🔼 [수정됨] 🔼 ---

# --- DB 초기화 ---
def init_db():
    """PostgreSQL DB와 테이블을 생성합니다."""
    conn = None
    cursor = None # <-- 변수를 try 밖에서 초기화
    try:
        if not DATABASE_URL:
            print("❌ [DB] DATABASE_URL이 설정되지 않았습니다.")
            return
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 테이블 생성 쿼리들
        cursor.execute("CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, price REAL NOT NULL, time TIMESTAMP NOT NULL)")
        cursor.execute("CREATE TABLE IF NOT EXISTS recommendations (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, price REAL NOT NULL, time TIMESTAMP NOT NULL, probability_score INTEGER)")
        cursor.execute("CREATE TABLE IF NOT EXISTS posts (id SERIAL PRIMARY KEY, author TEXT NOT NULL, content TEXT NOT NULL, time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS fcm_tokens (id SERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()
        
        # 컬럼 추가 (에러 핸들링 포함)
        try:
            cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            conn.commit()
        except psycopg2.Error as e:
            if e.pgcode == '42701': # 'Duplicate Column'
                conn.rollback()
            else:
                conn.rollback()
                raise 
        
        print(f"✅ [DB] PostgreSQL 테이블 초기화 성공.")
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ [DB] PostgreSQL 초기화 실패: {e}")
    finally:
        # --- 🔽 [수정됨] 🔽 ---
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        # --- 🔼 [수정됨] 🔼 ---

# Gunicorn으로 실행될 때 DB 초기화
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)