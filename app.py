from flask import Flask, render_template, jsonify, request, send_from_directory
import json
import os
import requests
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# --- API 키 설정 (보안) ---
API_KEY = os.environ.get('POLYGON_API_KEY')

# --- DB 경로 설정 (PostgreSQL 연동) ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- 1. 메인 페이지 라우트 ---
@app.route('/')
def dashboard_page():
    return render_template('index.html')

# --- 2. PWA 파일 서빙 라우트 ---
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
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'),
                               'danso_logo.png', mimetype='image/png')

# --- 3. 대시보드 데이터 API (PostgreSQL) ---
@app.route('/api/dashboard')
def get_dashboard_data():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT value FROM status WHERE key = 'status_data' ORDER BY last_updated DESC LIMIT 1")
        status_row = cursor.fetchone()
        if status_row:
            status = json.loads(status_row['value'])
        else:
            status = {'last_scan_time': 'N/A', 'watching_count': 0, 'watching_tickers': []}

        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time FROM signals ORDER BY time DESC LIMIT 50")
        signals = cursor.fetchall()

        cursor.execute("SELECT ticker, price, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time, probability_score FROM recommendations ORDER BY time DESC LIMIT 50")
        recommendations = cursor.fetchall()

        cursor.close()
        conn.close()

        return jsonify({
            'status': status,
            'signals': signals,
            'recommendations': recommendations
        })
    except Exception as e:
        if conn: conn.close()
        print(f"Error in /api/dashboard: {e}")
        return jsonify({
            'status': {'last_scan_time': 'Scanner waiting...', 'watching_count': 0, 'watching_tickers': []},
            'signals': [], 'recommendations': []
        })

# --- 4. 커뮤니티 API (게시글 읽기) (PostgreSQL) ---
@app.route('/api/posts')
def get_posts():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT author, content, TO_CHAR(time, 'YYYY-MM-DD HH24:MI:SS') as time FROM posts ORDER BY time DESC LIMIT 100")
        posts = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({"status": "OK", "posts": posts})
    except Exception as e:
        if conn: conn.close()
        print(f"Error in /api/posts (GET): {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 5. 커뮤니티 API (게시글 쓰기) (PostgreSQL) ---
@app.route('/api/posts', methods=['POST'])
def create_post():
    conn = None
    try:
        data = request.get_json()
        author = data.get('author', 'Anonymous')
        content = data.get('content')

        if not content:
            return jsonify({"status": "error", "message": "Content is empty."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO posts (author, content, time) VALUES (%s, %s, %s)",
                       (author, content, datetime.now()))
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"status": "OK", "message": "Post created."})
    except Exception as e:
        if conn: conn.close()
        print(f"Error in /api/posts (POST): {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 6. 실시간 호가 API ---
@app.route('/api/quote/<string:ticker>')
def get_quote(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500

    url = f"https://api.polygon.io/v3/quotes/{ticker.upper()}?limit=1&apiKey={API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            return jsonify(data['results'][0])
        else:
            return jsonify({"status": "error", "message": "Ticker not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 7. 기업 상세 정보 + 재무제표 API ---
@app.route('/api/details/<string:ticker>')
def get_ticker_details(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500

    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}?apiKey={API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            results = data['results']
            logo_url = results.get('branding', {}).get('logo_url', '')
            if logo_url: logo_url += f"?apiKey={API_KEY}"

            financials_node = results.get('financials', {})
            market_cap = financials_node.get('market_capitalization', {}).get('value', 'N/A')
            dividend_yield = financials_node.get('dividend_yield', {}).get('value', 'N/A')
            pe_ratio = financials_node.get('price_to_earnings_ratio', 'N/A')
            ps_ratio = financials_node.get('price_to_sales_ratio', 'N/A')

            financial_data = {
                "market_cap": market_cap,
                "pe_ratio": pe_ratio,
                "ps_ratio": ps_ratio,
                "dividend_yield": dividend_yield
            }

            details = {
                "ticker": results.get('ticker'), "name": results.get('name'),
                "industry": results.get('sic_description'),
                "description": results.get('description', 'No description available.'),
                "logo_url": logo_url, "financials": financial_data
            }
            return jsonify({"status": "OK", "results": details})
        else:
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
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            chart_data = []
            for bar in data['results']:
                chart_data.append({
                    "time": bar['t'] / 1000, "open": bar['o'],
                    "high": bar['h'], "low": bar['l'], "close": bar.get('c', bar['o'])
                })
            return jsonify({"status": "OK", "results": chart_data})
        else:
            return jsonify({"status": "error", "message": "Chart data not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 9. 시장 개요 API ---
@app.route('/api/market_overview')
def get_market_overview():
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500

    try:
        gainers_data = []
        losers_data = []

        url_g = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}"
        res_g = requests.get(url_g); res_g.raise_for_status()
        gainers_data = res_g.json().get('tickers') or []

        url_l = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={API_KEY}"
        res_l = requests.get(url_l); res_l.raise_for_status()
        losers_data = res_l.json().get('tickers') or []

        return jsonify({"status": "OK", "indices": {}, "gainers": gainers_data, "losers": losers_data})

    except requests.exceptions.HTTPError as http_err:
        return jsonify({"status": "error", "message": f"HTTP error: {http_err}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 10. (NEW) FCM Token Subscription API ---
@app.route('/subscribe', methods=['POST'])
def subscribe():
    """PWA로부터 FCM 토큰을 받아 DB에 저장합니다."""
    data = request.json
    token = data.get('token')

    if not token:
        return jsonify({"status": "error", "message": "No token provided"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO fcm_tokens (token) VALUES (%s) ON CONFLICT (token) DO NOTHING",
            (token,)
        )

        conn.commit()
        cursor.close()
        conn.close()

        print(f"✅ [FCM] New token registered: {token[:20]}...")
        return jsonify({"status": "success", "message": "Token registered"}), 201

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        print(f"❌ [FCM Subscribe Error] DB 저장 실패: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- (v13.0) DB 초기화 함수 (502 오류 최종 수정) ---
def init_db():
    """PostgreSQL DB와 테이블 4개를 생성합니다."""
    conn = None
    try:
        if not DATABASE_URL:
            print("❌ [DB] DATABASE_URL이 설정되지 않아 초기화를 건너뜁니다.")
            return

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS status (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL,
            price REAL NOT NULL,
            time TIMESTAMP NOT NULL
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE,
            price REAL NOT NULL,
            time TIMESTAMP NOT NULL,
            probability_score INTEGER
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id SERIAL PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()

        try:
            cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            conn.commit()
            print("-> [DB] 'recommendations' 테이블에 'probability_score' 컬럼 추가 시도 완료.")
        except psycopg2.Error as e:
            conn.rollback()
            if e.pgcode == '42701': # 'Duplicate Column' 에러
                 pass # 이미 컬럼이 존재하므로 (정상), 조용히 넘어감
            else:
                # 'raise'를 삭제하고 'print'로 변경 (서버 다운 방지)
                print(f"❌ [DB] ALTER TABLE 중 예외 발생 (무시함): {e}")

        cursor.close()
        conn.close()
        print(f"✅ [DB] PostgreSQL 테이블 초기화 성공.")
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        # 여기서도 'raise'를 하면 서버 부팅이 실패하므로 'print'로 변경
        print(f"❌ [DB] PostgreSQL 초기화 실패 (무시함): {e}")

# Gunicorn으로 실행될 때 DB 초기화
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)