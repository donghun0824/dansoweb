from flask import Flask, render_template, jsonify, request, send_from_directory, redirect, url_for, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
import secrets 
import json
import os
import requests
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# --- 1. 설정 및 환경 변수 ---
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_for_session')
API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- 2. DB 연결 함수 ---
def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- 3. Flask-Login 설정 ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

# --- 4. Google OAuth 설정 ---
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# --- 5. User 모델 (세션 관리용) ---
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
        print(f"Login session error: {e}")
    finally:
        if conn: conn.close()
    return None


# --- 6. 페이지 라우트 ---

# 겉지 (Landing Page)
@app.route('/')
def landing_page():
    return render_template('landing.html') 

# 로그인 페이지
@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_page'))
    return render_template('login.html')

# 속지 (Dashboard) - 로그인 필수
@app.route('/dashboard') 
@login_required
def dashboard_page():
    return render_template('dashboard.html', user=current_user)

# 1. [페이지] 사용자가 접속하는 화면 (HTML 렌더링)
@app.route('/sts')
@login_required  # 로그인한 사람만 볼 수 있게 하려면 추가
def sts_page():
    # templates/sts.html 파일을 찾아서 보여줌
    return render_template('sts.html')

@app.route('/api/sts/status')
def get_sts_status():
    conn = None
    try:
        conn = get_db_connection() 
        # 딕셔너리 형태로 데이터를 받기 위해 RealDictCursor 사용
        cursor = conn.cursor(cursor_factory=RealDictCursor) 
        
        # [핵심 수정] 
        # 1. FIRED(격발) > AIMING(조준) > 점수 높은 순 정렬
        # 2. 상위 3개만 조회 (LIMIT 3) -> 화면이 깔끔해짐
        query = """
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
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        targets = []
        for r in rows:
            # DB에 점수가 없으면(None) 0으로 처리
            raw_score = r.get('ai_score') or 0
            
            targets.append({
                'ticker': r['ticker'],
                'price': r['price'],
                'ai_prob': raw_score / 100.0, # 100점 만점 -> 0.xx 확률로 변환
                'obi': r['obi'],
                'vpin': r['vpin'],
                'tick_speed': r['tick_speed'],
                'vwap_dist': r['vwap_dist'],
                'status': r['status']
            })
            
        # 2. 최근 신호 로그 (필요 시 활성화, 없으면 빈 리스트)
        try:
            cursor.execute("""
                SELECT time, ticker, price, score 
                FROM signals 
                ORDER BY time DESC LIMIT 5
            """)
            log_rows = cursor.fetchall()
        except:
            log_rows = []

        logs = []
        for l in log_rows:
            logs.append({
                'timestamp': l['time'].strftime('%H:%M:%S'),
                'ticker': l['ticker'],
                'price': l['price'],
                'score': l['score']
            })
            
        cursor.close()
        
        return jsonify({
            'targets': targets,
            'logs': logs
        })
        
    except psycopg2.errors.UndefinedTable:
        # 봇이 아직 한 번도 실행되지 않아 테이블이 없는 경우
        if conn: conn.rollback()
        return jsonify({'targets': [], 'logs': []})
        
    except Exception as e:
        print(f"❌ API Error: {e}")
        return jsonify({'targets': [], 'logs': [], 'error': str(e)})
        
    finally:
        if conn: 
            conn.close()

# --- 7. 인증(Auth) 라우트 ---

# 구글 로그인 시작
@app.route('/auth/google')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    
    nonce = secrets.token_urlsafe(16)
    session['google_auth_nonce'] = nonce
    
    return oauth.google.authorize_redirect(
        redirect_uri,
        access_type='offline',
        prompt='consent',
        nonce=nonce 
    )

# 구글 로그인 콜백
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
            # 신규 가입
            cursor.execute(
                "INSERT INTO users (email, oauth_provider, is_premium) VALUES (%s, 'google', FALSE) RETURNING id", 
                (email,)
            )
            new_user_id = cursor.fetchone()[0]
            conn.commit()
            user = User(id=new_user_id, email=email, is_premium=False)
        else:
            # 기존 유저
            user = User(id=user_data[0], email=user_data[1], is_premium=user_data[2])
        
        cursor.close()
        conn.close()
        
        login_user(user)
        return redirect(url_for('dashboard_page'))
        
    except Exception as e:
        print(f"OAuth Error: {e}")
        return "Google Login Failed. Please try again. (Check server logs for details)", 400

# 로그아웃
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('landing_page'))


# --- 8. 정적 파일 서빙 ---

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

# --- 9. 데이터 API ---

@app.route('/api/dashboard')
@login_required
def get_dashboard_data():
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
        print(f"Error in /api/dashboard: {e}")
        return jsonify({'status': {'last_scan_time': 'Scanner waiting...', 'watching_count': 0, 'watching_tickers': []}, 'signals': [], 'recommendations': []})

@app.route('/api/posts')
@login_required
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

@app.route('/api/posts', methods=['POST'])
@login_required
def create_post():
    conn = None
    try:
        data = request.get_json()
        author = data.get('author', 'Anonymous')
        content = data.get('content')
        if not content: return jsonify({"status": "error", "message": "Content is empty."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO posts (author, content, time) VALUES (%s, %s, %s)", (author, content, datetime.now()))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "OK", "message": "Post created."})
    except Exception as e:
        if conn: conn.close()
        print(f"Error in /api/posts (POST): {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/quote/<string:ticker>')
@login_required
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

@app.route('/api/details/<string:ticker>')
@login_required
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
            f = results.get('financials', {})
            financial_data = {
                "market_cap": f.get('market_capitalization', {}).get('value', 'N/A'),
                "pe_ratio": f.get('price_to_earnings_ratio', 'N/A'),
                "ps_ratio": f.get('price_to_sales_ratio', 'N/A'),
                "dividend_yield": f.get('dividend_yield', {}).get('value', 'N/A')
            }
            details = {
                "ticker": results.get('ticker'), "name": results.get('name'),
                "industry": results.get('sic_description'),
                "description": results.get('description', 'No description available.'),
                "logo_url": logo_url, "financials": financial_data
            }
            return jsonify({"status": "OK", "results": details})
        else:
            return jsonify({"status": "error", "message": "Details not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/chart_data/<string:ticker>')
@login_required
def get_chart_data(ticker):
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        past_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{past_date}/{today}?sort=asc&limit=5000&apiKey={API_KEY}"
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            chart_data = [{"time": bar['t']/1000, "open": bar['o'], "high": bar['h'], "low": bar['l'], "close": bar.get('c', bar['o'])} for bar in data['results']]
            return jsonify({"status": "OK", "results": chart_data})
        else:
            return jsonify({"status": "error", "message": "Chart data not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# 겉지용 시장 개요 (로그인 불필요)
@app.route('/api/market_overview')
def get_market_overview():
    if not API_KEY: return jsonify({"status": "error", "message": "API Key not configured"}), 500
    try:
        url_g = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}"
        res_g = requests.get(url_g); res_g.raise_for_status()
        gainers = res_g.json().get('tickers') or []
        
        url_l = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={API_KEY}"
        res_l = requests.get(url_l); res_l.raise_for_status()
        losers = res_l.json().get('tickers') or []
        
        return jsonify({"status": "OK", "gainers": gainers, "losers": losers})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- (NEW) 알림 점수 기준 설정 API ---
@app.route('/api/set_alert_threshold', methods=['POST'])
def set_alert_threshold():
    data = request.get_json()
    token = data.get('token')
    threshold = data.get('threshold')

    if not token or threshold is None:
        return jsonify({"status": "error", "message": "Missing token or threshold"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # min_score 업데이트
        cursor.execute(
            "UPDATE fcm_tokens SET min_score = %s WHERE token = %s",
            (int(threshold), token)
        )
        
        if cursor.rowcount == 0:
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "Token not found"}), 404

        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({"status": "OK", "message": "Threshold updated"}), 200

    except Exception as e:
        if conn: 
            conn.rollback()
            conn.close()
        print(f"Error setting threshold: {e}")
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
        # min_score 기본값은 DB 레벨에서 처리됨 (DEFAULT 0)
        cursor.execute("INSERT INTO fcm_tokens (token) VALUES (%s) ON CONFLICT (token) DO NOTHING", (token,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "success"}), 201
    except Exception as e:
        if conn: conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 10. DB 초기화 (서버 시작 시 실행) ---
def init_db():
    conn = None
    try:
        if not DATABASE_URL: return
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, price REAL NOT NULL, time TIMESTAMP NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS recommendations (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, price REAL NOT NULL, time TIMESTAMP NOT NULL, probability_score INTEGER)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS posts (id SERIAL PRIMARY KEY, author TEXT NOT NULL, content TEXT NOT NULL, time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS fcm_tokens (id SERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        
        # Users 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                oauth_provider TEXT,
                is_premium BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        
        # 기존 테이블 컬럼 추가 (마이그레이션 대응)
        try:
            cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            if e.pgcode == '42701': pass # duplicate_column 에러 무시
            else: print(f"❌ [DB] ALTER TABLE recommendations error: {e}")

        # (NEW) fcm_tokens 테이블에 min_score 컬럼 추가
        try:
            cursor.execute("ALTER TABLE fcm_tokens ADD COLUMN min_score INTEGER DEFAULT 0")
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            if e.pgcode == '42701': pass # duplicate_column 에러 무시
            else: print(f"❌ [DB] ALTER TABLE fcm_tokens error: {e}")

        cursor.close()
        conn.close()
        print("✅ [DB] Init success.")
    except Exception as e:
        if conn: 
            conn.rollback()
            conn.close()
        print(f"❌ [DB] Init failed: {e}")

# ▼▼▼▼▼ [여기] 아래 코드를 붙여넣으세요 ▼▼▼▼
@app.route('/admin/secret/count')
def check_user_count():
    """관리자용: 실시간 가입자 및 기기 수 확인 페이지"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. 회원가입한 사람 수 (users 테이블)
        try:
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
        except:
            user_count = 0 
            conn.rollback()

        # 2. 알림 켜놓은 기기 수 (fcm_tokens)
        try:
            cursor.execute("SELECT COUNT(*) FROM fcm_tokens")
            device_count = cursor.fetchone()[0]
        except:
            device_count = 0
            conn.rollback()
        
        cursor.close()
        conn.close()
        
        # 실제 활성 사용자 수 (둘 중 큰 값 기준)
        active_users = max(user_count, device_count)
        remaining = 1000 - active_users
        
        # 대시보드 스타일의 HTML 반환
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Danso Launch Status</title>
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

init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)