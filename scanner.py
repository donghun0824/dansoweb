import asyncio
import websockets
import requests
import os
import pandas as pd
import pandas_ta as ta
import json
from datetime import datetime, timedelta
import psycopg2
from psycopg2 import pool
import time
import httpx
import firebase_admin
from firebase_admin import credentials, messaging
import sys
import pytz
import traceback
import numpy as np
import xgboost as xgb
import joblib
import warnings
from concurrent.futures import ThreadPoolExecutor
# ==============================================================================
# 1. CONFIGURATION & CONSTANTS
# ==============================================================================

# API Keys
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
DATABASE_URL = os.environ.get('DATABASE_URL')

# Vertex AI Config
GCP_PROJECT_ID = "gen-lang-client-0379169283"
GCP_REGION = "us-central1"

# VAPID Config (Legacy)
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_EMAIL = "mailto:cbvkqtm98@gmail.com"

# Tuning Parameters
MAX_PRICE = 20
TOP_N = 1000
MIN_DATA_REQ = 20
HISTORY_WORKERS = 50

WAE_MACD = (2, 3, 4)
WAE_SENSITIVITY = 150
WAE_BB = (5, 1.5)
WAE_ATR = 5
WAE_ATR_MULT = 1.5
WAE_CMF = 5
WAE_RSI_RANGE = (40, 70)
RSI_LENGTH = 5

ICHIMOKU_SHORT = (2, 3, 5)
CLOUD_PROXIMITY = 20.0
CLOUD_THICKNESS = 0.5
OBV_LOOKBACK = 3

# Global State
ticker_minute_history = {}
ticker_tick_history = {}
watched_tickers = set()
ai_cooldowns = {}
ai_request_queue = asyncio.Queue()
db_pool = None

# --- [AI 모델 설정] ---
MODEL_FILE = "sniper_model_advanced.json"
sniper_model = None

def load_model():
    global sniper_model
    if os.path.exists(MODEL_FILE):
        try:
            # XGBoost 모델 불러오기
            sniper_model = xgb.XGBClassifier()
            sniper_model.load_model(MODEL_FILE)
            print(f"✅ [AI] 스나이퍼 모델 장전 완료: {MODEL_FILE}")
        except Exception as e:
            print(f"❌ [AI] 모델 로드 실패: {e}")
    else:
        print(f"⚠️ [AI] 모델 파일 없음 ({MODEL_FILE}). 파일이 업로드 되었는지 확인하세요.")

# 봇 시작 시 모델 즉시 로드
load_model()

# ==============================================================================
# 2. DATABASE & FIREBASE FUNCTIONS
# ==============================================================================

def init_firebase():
    """Firebase Admin SDK를 초기화합니다."""
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR:
            print("❌ [FCM] FIREBASE_ADMIN_SDK_JSON이 설정되지 않아 FCM을 건너뜁니다.")
            return False
        
        sdk_json_dict = json.loads(FIREBASE_ADMIN_SDK_JSON_STR)
        cred = credentials.Certificate(sdk_json_dict)
        
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            
        print(f"✅ [FCM] Firebase Admin SDK 초기화 성공 (Project ID: {sdk_json_dict.get('project_id')})")
        return True
    except Exception as e:
        print(f"❌ [FCM] Firebase Admin SDK 초기화 실패: {e}")
        return False

def init_db():
    """PostgreSQL 커넥션 풀을 생성하고 테이블을 초기화합니다."""
    global db_pool
    if not DATABASE_URL:
        print("❌ [DB] DATABASE_URL이 설정되지 않아 초기화를 건너뜁니다.")
        return

    try:
        if db_pool is None:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
            print("✅ [DB] 커넥션 풀(Turbo) 가동 시작.")

        conn = db_pool.getconn()
        try:
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                min_score INTEGER DEFAULT 0
            )
            """)
            
            try:
                cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            except psycopg2.Error:
                conn.rollback()
            
            try:
                cursor.execute("ALTER TABLE fcm_tokens ADD COLUMN min_score INTEGER DEFAULT 0")
            except psycopg2.Error:
                conn.rollback()
                
            conn.commit()
            cursor.close()
            print(f"✅ [DB] 테이블 초기화 완료.")
            
        except Exception as e:
            print(f"❌ [DB 테이블 생성 오류] {e}")
            if conn: conn.rollback()
        finally:
            if conn: db_pool.putconn(conn)

    except Exception as e:
        print(f"❌ [DB] 커넥션 풀 생성 실패: {e}")

def get_db_connection():
    global db_pool
    if db_pool is None:
        init_db()
    return db_pool.getconn()


def send_discord_alert(ticker, price, type="signal", probability_score=50, reasoning=""):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL or len(DISCORD_WEBHOOK_URL) < 50:
        print(f"🔔 [알림] {ticker} @ ${price} (디스코드 URL 미설정)")
        return
        
    if type == "signal": 
        content = f"🚀 **WAE 폭발 신호** 🚀\n**{ticker}** @ **${price:.4f}**\n**AI 상승 확률: {probability_score}%**"
    else: 
        content = (
            f"💡 **AI Setup (Recommendation)** 💡\n"
            f"**{ticker}** @ **${price:.4f}**\n"
            f"**AI Score: {probability_score}%**"
            f"**AI Comment:** {reasoning}"
        )
        
    data = {"content": content}
    try: 
        requests.post(DISCORD_WEBHOOK_URL, json=data)
        print(f"🔔 [알림] {ticker} @ ${price:.4f} (디스코드 전송 완료)")
    except Exception as e: 
        print(f"[알림 오류] {ticker} 디스코드 전송 실패: {e}")

def send_fcm_notification(ticker, price, probability_score, reasoning=""):
    if not firebase_admin._apps:
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        
        if not subscribers:
            db_pool.putconn(conn)
            return

        data_payload = {
            'title': "Danso AI 신호", 
            'body': f"{ticker}: {reasoning}",
            'ticker': ticker,
            'price': f"{price:.4f}",
            'probability': str(probability_score)
        }
        
        success_count = 0
        failure_count = 0
        skipped_count = 0
        failed_tokens = []

        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            if not token: continue

            if probability_score < user_min_score:
                skipped_count += 1
                continue 

            try:
                message = messaging.Message(
                    token=token,
                    data=data_payload, 
                    webpush=messaging.WebpushConfig(
                        headers={'Urgency': 'high'}
                    )
                )
                messaging.send(message)
                success_count += 1
            except Exception as e:
                failure_count += 1
                if "Requested entity was not found" in str(e) or "registration-token-not-registered" in str(e):
                    failed_tokens.append(token)
        
        if failed_tokens:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            cursor.close()
            print(f"🧹 [FCM] 만료된 토큰 {len(failed_tokens)}개 삭제 완료.")

    except Exception as e:
        print(f"❌ [FCM] 발송 중 오류: {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

def log_signal(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO signals (ticker, price, time) VALUES (%s, %s, %s)", 
                       (ticker, price, datetime.now()))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [DB] 'signals' 저장 실패: {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

def log_recommendation(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
        INSERT INTO recommendations (ticker, price, time, probability_score) 
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker) DO NOTHING
        """, 
                       (ticker, price, datetime.now(), probability_score))
        conn.commit()
        
        is_new_rec = cursor.rowcount > 0
        cursor.close()
        return is_new_rec
        
    except Exception as e:
        print(f"❌ [DB] 'recommendations' 저장 실패: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: db_pool.putconn(conn)

# ==============================================================================
# 3. HELPER FUNCTIONS & DATA FETCHING
# ==============================================================================

def get_current_session():
    try:
        ny_tz = pytz.timezone('US/Eastern')
        now = datetime.now(ny_tz).time()

        time_pre_start = datetime.strptime("04:00", "%H:%M").time()
        time_regular_start = datetime.strptime("09:30", "%H:%M").time()
        time_after_start = datetime.strptime("16:00", "%H:%M").time()
        time_market_close = datetime.strptime("20:00", "%H:%M").time()

        if time_pre_start <= now < time_regular_start:
            return "premarket"
        elif time_regular_start <= now < time_after_start:
            return "regular"
        elif time_after_start <= now < time_market_close:
            return "aftermarket"
        else:
            return "closed"
    except Exception as e:
        print(f"⚠️ [Time Check Error] {e}")
        return "premarket"

def calculate_volume_ratio(df):
    try:
        if len(df) < 6: return 1.0
        current_vol = df['volume'].iloc[-1]
        avg_vol_5 = df['volume'].iloc[-6:-1].mean()
        
        if avg_vol_5 == 0: return 0.0
        
        ratio = current_vol / avg_vol_5
        return round(ratio, 2)
    except Exception as e:
        print(f"⚠️ [Volume Ratio Error] {e}")
        return 1.0

def find_active_tickers():
    if not POLYGON_API_KEY:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류: POLYGON_API_KEY가 설정되지 않았습니다.")
        return set()
        
    print(f"\n🔭 [사냥꾼] 시장 전체 스캔 중... (Top {TOP_N} Gainers / Max ${MAX_PRICE})")
    
    # Polygon Snapshot API (시장 전체 상태 한방에 조회)
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={POLYGON_API_KEY}"

    tickers_to_watch = set()
    try:
        # 타임아웃 10초 설정 (네트워크 지연 시 무한 대기 방지)
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        
        if data.get('status') == 'OK':
            # 수신된 티커 리스트 순회
            for ticker in data.get('tickers', []):
                # 가격 정보 추출 (lastTrade가 없는 경우 999로 처리하여 필터링)
                price = ticker.get('lastTrade', {}).get('p', 999) 
                ticker_symbol = ticker.get('ticker')
                
                # 가격 조건 확인 ($20 이하)
                is_price_ok = price <= MAX_PRICE
                
                if is_price_ok and ticker_symbol:
                    tickers_to_watch.add(ticker_symbol)
                
                # 목표 수량(TOP_N)을 채우면 즉시 중단 (효율성)
                if len(tickers_to_watch) >= TOP_N: 
                    break
            
            print(f"-> ✅ [타겟 확보] 총 {len(tickers_to_watch)}개 종목 조준 완료.")
            
    except Exception as e:
        print(f"-> ❌ [스캔 실패] API 호출 중 오류 발생: {e}")
        # 오류가 나더라도 지금까지 확보한 티커라도 반환하여 봇이 멈추지 않게 함
        return tickers_to_watch
        
    return tickers_to_watch

def fetch_initial_data(ticker):
    if not POLYGON_API_KEY: return
    
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/"
        f"{start_date}/{end_date}?adjusted=true&sort=desc&limit=200&apiKey={POLYGON_API_KEY}"
    )
    
    try:
        print(f"⏳ [초기화 시도] {ticker} 과거 데이터 요청 중...")
        res = requests.get(url, timeout=5)
        data = res.json()
        
        if data.get('status') == 'OK' and data.get('results'):
            results = data['results']
            results.sort(key=lambda x: x['t']) 
            
            df = pd.DataFrame(results)
            df = df[['t', 'o', 'h', 'l', 'c', 'v']]
            df['t'] = pd.to_datetime(df['t'], unit='ms')
            df.set_index('t', inplace=True)
            df = df[['o', 'h', 'l', 'c', 'v']].astype(float)
            
            ticker_minute_history[ticker] = df
            print(f"✅ [초기화] {ticker} 과거 캔들 {len(df)}개 로딩 완료. 즉시 분석 가능.")
        else:
            print(f"⚠️ [데이터 없음] {ticker}: Status={data.get('status')}, Count={data.get('count')}, Msg={data.get('message')}")
    except Exception as e:
        print(f"⚠️ [초기화 실패] {ticker}: {e}")

# ==============================================================================
# 4. CORE CALCULATION ENGINE (NUMPY)
# ==============================================================================

def calculate_f1_indicators(closes, highs, lows, volumes):
    """
    Pandas TA를 대체하는 초고속 NumPy 지표 계산 함수 (V16 OAR 적용)
    """
    # ---------------- Helper Functions ----------------
    def sma(arr, n):
        ret = np.cumsum(arr, dtype=float)
        ret[n:] = ret[n:] - ret[:-n]
        return ret[n - 1:] / n

    def ema(arr, n):
        alpha = 2 / (n + 1)
        res = np.empty_like(arr)
        res[0] = arr[0]
        for i in range(1, len(arr)):
            res[i] = alpha * arr[i] + (1 - alpha) * res[i-1]
        return res

    def rolling_max(arr, n):
        return np.array([arr[i-n+1:i+1].max() for i in range(n-1, len(arr))])

    def rolling_min(arr, n):
        return np.array([arr[i-n+1:i+1].min() for i in range(n-1, len(arr))])

    def rsi_func(arr, n=5):
        delta = np.diff(arr)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        
        avg_gain = np.zeros_like(arr); avg_loss = np.zeros_like(arr)
        avg_gain[n] = np.mean(gain[:n]); avg_loss[n] = np.mean(loss[:n])
        
        for i in range(n+1, len(arr)):
            avg_gain[i] = (avg_gain[i-1] * (n-1) + gain[i-1]) / n
            avg_loss[i] = (avg_loss[i-1] * (n-1) + loss[i-1]) / n
            
        rs = avg_gain / (avg_loss + 1e-10) 
        return 100 - (100 / (1 + rs))

    # ---------------- 1. Standard Indicators ----------------
    # [WAE] MACD (2, 3, 4)
    ema_fast = ema(closes, 2)
    ema_slow = ema(closes, 3)
    macd = ema_fast - ema_slow

    # [WAE] Bollinger Bands (5, 1.5)
    bb5_sma = np.zeros_like(closes)
    w = 5
    bb5_up = np.zeros_like(closes)
    bb5_low = np.zeros_like(closes)
    
    for i in range(w, len(closes)):
        window = closes[i-w+1:i+1]
        mean = np.mean(window)
        std = np.std(window)
        bb5_up[i] = mean + (std * 1.5)
        bb5_low[i] = mean - (std * 1.5)

    # [Squeeze] Bollinger Bands (20, 2.0)
    w20 = 20
    bb20_up = np.zeros_like(closes)
    bb20_low = np.zeros_like(closes)
    for i in range(w20, len(closes)):
        window = closes[i-w20+1:i+1]
        mean = np.mean(window)
        std = np.std(window)
        bb20_up[i] = mean + (std * 2.0)
        bb20_low[i] = mean - (std * 2.0)

    # [WAE] ATR (5)
    prev_close = np.roll(closes, 1); prev_close[0] = closes[0]
    tr1 = highs - lows
    tr2 = np.abs(highs - prev_close)
    tr3 = np.abs(lows - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    
    atr = np.zeros_like(closes)
    atr[5] = np.mean(tr[:5])
    for i in range(6, len(closes)):
        atr[i] = (atr[i-1] * 4 + tr[i]) / 5

    # ---------------- Array Alignment Helper ----------------
    target_len = len(closes)
    def normalize_len(arr):
        diff = target_len - len(arr)
        if diff > 0:
            return np.concatenate([np.full(diff, arr[0]), arr])
        return arr

    # [Ichimoku] (2, 3, 5)
    t_max = normalize_len(rolling_max(highs, 2))
    t_min = normalize_len(rolling_min(lows, 2))
    tenkan = (t_max + t_min) / 2
    
    k_max = normalize_len(rolling_max(highs, 3))
    k_min = normalize_len(rolling_min(lows, 3))
    kijun = (k_max + k_min) / 2
    
    senkou_a = (tenkan + kijun) / 2
    
    s_max = normalize_len(rolling_max(highs, 5))
    s_min = normalize_len(rolling_min(lows, 5))
    senkou_b = (s_max + s_min) / 2
    
    # [RSI] (5)
    rsi = rsi_func(closes, 5)

    # [CMF] (5)
    denom = highs - lows
    denom = np.where(denom == 0, 1e-10, denom)
    mfm = ((closes - lows) - (highs - closes)) / denom
    mfm = np.nan_to_num(mfm) 
    mfv = mfm * volumes
    
    cmf = np.zeros_like(closes)
    for i in range(5, len(closes)):
        sum_mfv = np.sum(mfv[i-4:i+1])
        sum_vol = np.sum(volumes[i-4:i+1])
        if sum_vol != 0:
            cmf[i] = sum_mfv / sum_vol

    # [OBV]
    obv = np.zeros_like(volumes)
    obv[0] = volumes[0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv[i] = obv[i-1] + volumes[i]
        elif closes[i] < closes[i-1]:
            obv[i] = obv[i-1] - volumes[i]
        else:
            obv[i] = obv[i-1]

    # [VWAP]
    tp = (highs + lows + closes) / 3
    vp = tp * volumes
    cum_vp = np.cumsum(vp)
    cum_vol = np.cumsum(volumes)
    vwap = np.divide(cum_vp, cum_vol, out=np.zeros_like(cum_vp), where=cum_vol!=0)

    # ---------------- 2. V16 OAR & Microstructure ----------------
    # 1. RVOL (Relative Volume)
    vol_sma_20 = np.zeros_like(volumes)
    for i in range(20, len(volumes)):
        vol_sma_20[i] = np.mean(volumes[i-20:i])
    rvol = np.divide(volumes, vol_sma_20, out=np.zeros_like(volumes), where=vol_sma_20!=0)

    # 2. Volatility Z-Score
    candle_range = highs - lows
    range_ma_20 = np.zeros_like(candle_range)
    range_std_20 = np.zeros_like(candle_range)
    for i in range(20, len(candle_range)):
        window = candle_range[i-20:i]
        range_ma_20[i] = np.mean(window)
        range_std_20[i] = np.std(window)
    
    volatility_z = np.divide(
        (candle_range - range_ma_20), 
        (range_std_20 + 1e-10)
    )

    # 3. Order Imbalance & Trend Align
    range_span = highs - lows
    clv = np.divide(
        ((closes - lows) - (highs - closes)), 
        (range_span + 1e-10)
    )
    order_imbalance = clv * volumes
    
    ema_60 = ema(closes, 60)
    trend_align = np.where(closes > ema_60, 1, -1)

    # 4. OAR Calculation
    imb_score = np.log1p(np.clip(order_imbalance, 0, None))
    oar_calc = (np.clip(rvol, 0, 5) * imb_score) * (1 / (np.abs(volatility_z) + 0.5))
    
    idx = -1

    return {
        "close": closes[idx],
        "vwap": vwap[idx],
        "volume": volumes[idx],
        "macd_delta": (macd[idx] - macd[idx-1]) * 150, 
        "bb_gap_wae": bb5_up[idx] - bb5_low[idx],      
        "dead_zone": atr[idx] * 1.5,                   
        "rsi": rsi[idx],
        
        # 👇 [V16 필수 데이터] 모델이 요구하는 것들
        "rvol": rvol[idx],
        "volatility_z": volatility_z[idx],
        "order_imbalance": order_imbalance[idx], # 👈 [중요] 이게 빠져서 에러가 났던 겁니다. 추가 완료.
        "oar_calc": oar_calc[idx],
        "oar_prev": oar_calc[idx-1], 
        "trend_align": trend_align[idx],
        
        # 👇 콤마(,) 문제 없이 연결
        "pump_strength": (closes[idx] - closes[idx-5]) / closes[idx-5] * 100 if closes[idx-5] != 0 else 0,
        "cmf": cmf[idx],
        "obv_now": obv[idx],
        "obv_prev": obv[idx-1],
        "cloud_top": max(senkou_a[-3], senkou_b[-3]),
        "senkou_a": senkou_a[-3],
        "senkou_b": senkou_b[-3],
        "bb_up_std": bb20_up[idx],
        "bb_low_std": bb20_low[idx],
        "bb_width_now": (bb20_up[idx] - bb20_low[idx]) / closes[idx],
        "bb_width_avg": np.mean((bb20_up[-20:] - bb20_low[-20:]) / closes[-20:])
    }

# ==============================================================================
# 5. AI WORKER & FUNCTIONS
# ==============================================================================

# 🚀 [Math] XGBoost 기반 초고속 승률 계산 (V16 Advanced Model + KeyError 방지)
def get_ai_score(ticker, ai_data):
    global sniper_model
    
    # 모델이 없으면 기본값 50점 반환
    if sniper_model is None:
        return 50

    try:
        # ⚠️ [중요] 모델 학습 당시의 피처 순서와 100% 일치해야 함
        # 모든 필드에 .get()을 적용하여 데이터가 잠시 누락되어도 봇이 죽지 않게 함
        
        features = pd.DataFrame([{
            # 기존 5개 (여기에도 .get을 꼭 써야 에러가 안 납니다!)
            'vwap_dist': ai_data.get('vwap_distance', 0.0),
            'squeeze': ai_data.get('squeeze_ratio', 1.0),
            'rsi': ai_data.get('rsi_value', 50.0),
            'pump': ai_data.get('pump_strength_5m', 0.0),
            'pullback': ai_data.get('pullback_from_high', 0.0),
            
            # V16 추가 5개
            'rvol': ai_data.get('rvol', 0.0),
            'volatility_z': ai_data.get('volatility_z', 0.0),
            'order_imbalance': ai_data.get('order_imbalance', 0.0),
            'trend_align': ai_data.get('trend_align', 0),
            'session': ai_data.get('session_int', 3)
        }])
        
        # 확률 계산 (0.0 ~ 1.0) -> 점수 변환 (0 ~ 100)
        probs = sniper_model.predict_proba(features)[:, 1]
        score = int(probs[0] * 100)
        
        return score

    except Exception as e:
        # 에러 발생 시 로그만 남기고 50점 반환 (봇 멈춤 방지)
        print(f"❌ [AI Score Error] {ticker}: {e}")
        return 50

# 🧠 [Logic] 제미나이: V16 엘리트 스캘퍼 페르소나 적용
async def get_gemini_reasoning(ticker, ai_data, xgb_score):
    if not GEMINI_API_KEY: return "AI Comment Unavailable"

    # 1. 데이터 추출 (V16 키값이 없을 경우를 대비해 get으로 안전하게 호출)
    session_type = ai_data.get('session_type', 'Unknown')
    session_int = ai_data.get('session_int', 3)
    vwap_dist = ai_data.get('vwap_distance', 0.0)
    oar_delta = ai_data.get('oar_delta', 0.0)
    rvol = ai_data.get('rvol', 0.0)
    rsi = ai_data.get('rsi_value', 50.0)
    pump = ai_data.get('pump_strength_5m', 0.0)
    pullback = ai_data.get('pullback_from_high', 0.0)
    trend_align = ai_data.get('trend_align', 0)
    squeeze = ai_data.get('squeeze_ratio', 1.0)

    # 2. 🆕 V16 프롬프트 적용 (Elite Nasdaq Scalper)
    prompt = f"""
    Tone should be sharp, emotionless, and practical — like a sniper talking to another sniper.
    You are an Elite Nasdaq Momentum scalper AI assisting a real-time trading engine. 
    The system already generated a trade signal using mathematical filters (VWAP Distance, OAR Delta, RVol, RSI, Pump Strength, Session Context, and XGBoost score). 

    Your job is NOT to predict direction again. 
    Your job is to explain the signal and provide execution guidance.

    ----------------------------------------
    📌 DATA INPUT (Read & Use Carefully)
    ----------------------------------------
    Ticker: {ticker}
    Score: {xgb_score}%

    Session: {session_type} (Numeric Code: {session_int})
    VWAP Distance: {vwap_dist}%
    OAR Delta: {oar_delta}
    Relative Volume (RVOL): {rvol}
    RSI: {rsi}
    Pump (5m): {pump}%
    Pullback: {pullback}%
    Trend Align: {trend_align} (1 bullish / -1 bearish)
    Squeeze Ratio: {squeeze}

    ----------------------------------------
    📌 TASKS
    ----------------------------------------

    1. **Explain WHY the setup is strong or weak.**
       - Keep it concise.
       - Reference the key factors ONLY (VWAP behavior, momentum, volume confirmation, OAR flow).
       - No generic analysis.

    2. **Give the trader an execution plan:**
       - Entry confirmation rule (when to execute vs wait)
       - Stop-loss level logic (based on VWAP or structure)
       - Profit target logic (based on recent high or trend continuation)

    3. **Include a risk flag if needed:**
       - Overextension: Pump > 4%
       - Low conviction volume: rvol < 1.5 
       - RSI overheating: RSI > 75
       - Weak VWAP control (< 0 or barely above)
       - Trend misalignment

    ----------------------------------------
    📌 OUTPUT FORMAT (STRICT)
    ----------------------------------------
    - Sentence 1: Summary of why this signal triggered (technical reasoning).
    - Sentence 2: Entry condition and confirmation rule.
    - Sentence 3: Stop-loss and target suggestion.
    - Sentence 4 (only if needed): Risk warning or caution tag.

    Keep the tone short, confident, and Korean. No emojis. No extra text.
    """
    
    api_url = f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/publishers/google/models/gemini-2.5-flash-lite:generateContent"
    payload = { "contents": [{ "role": "user", "parts": [{"text": prompt}] }] }
    headers = { "Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(api_url, json=payload, headers=headers, timeout=5.0)
            if not resp.is_success: return f"Gemini Error ({resp.status_code})"
            
            res_json = resp.json()
            text = res_json.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            return text.strip()
    except:
        return "AI 분석 시간 초과"

async def ai_worker():
    print("👨‍🍳 [Worker] 하이브리드 AI(Math + Logic) 가동 시작!")
    while True:
        task = await ai_request_queue.get()
        try:
            ticker = task['ticker']
            price_now = task['price']
            ai_data = task['ai_data']
            
            # 1단계: 단소의 수학적 확신 (XGBoost) - 0.001초 소요
            score = get_ai_score(ticker, ai_data)

            # --- 🆕 NEW: V16 Decision Logic (RSI + OAR Filter) ---
            is_valid_signal = False
            reasoning_prefix = ""
            
            # 데이터 언패킹
            session_int = ai_data.get('session_int', 3)
            rsi = ai_data.get('rsi_value', 50.0)
            pump = ai_data.get('pump_strength_5m', 0.0)
            oar_delta = ai_data.get('oar_delta', 0.0)
            rvol = ai_data.get('rvol', 0.0)
            vwap_dist = ai_data.get('vwap_distance', 0.0)

            # [Rule 1] Session 0: Legend Mode
            if session_int == 0:
                if (1.5 <= pump <= 5.5) and (0.8 <= oar_delta <= 5.0) and \
                   (rvol >= 1.5) and (score >= 50):
                    is_valid_signal = True
                    reasoning_prefix = "[Morning Rush]"

            # [Rule 2] Session 1: Iron Dome
            elif session_int == 1:
                if (1.0 <= pump <= 2.5) and (oar_delta >= 2.0) and \
                   (rvol >= 5.0) and (score >= 70):
                    is_valid_signal = True
                    reasoning_prefix = "[Iron Dome]"

            # [Rule 3] Session 2: RSI Sniper (오후장 정밀 타격)
            elif session_int == 2:
                # RSI 50~75 필터 & VWAP 타이트하게
                if (50 <= rsi <= 75) and (vwap_dist <= 2.0):
                    if (1.0 <= pump <= 3.5) and (1.0 <= oar_delta <= 5.0) and \
                       (rvol >= 3.0) and (score >= 60):
                        is_valid_signal = True
                        reasoning_prefix = "[Afternoon Sniper]"

            print(f"🏎️ [AI 체크] {ticker} | 점수:{score} | 세션:{session_int} | 유효:{is_valid_signal}")
            
            # 2. 결과 처리 (조건 만족 시에만 알림)
            if is_valid_signal:
                print(f"🚀 [V16 신호] {ticker} | {reasoning_prefix} | 점수: {score}%")
                
                # Gemini 호출 (옵션: 점수가 높거나 확실한 신호일 때만)
                gemini_comment = ""
                if score >= 70: # 코멘트 기준 점수
                    gemini_comment = await get_gemini_reasoning(ticker, ai_data, score)
                
                final_reasoning = f"{reasoning_prefix} {gemini_comment}"
                
                # 알림 발송
                is_new = log_recommendation(ticker, float(price_now), score)
                if is_new:
                    send_discord_alert(ticker, float(price_now), "recommendation", score, final_reasoning)
                    send_fcm_notification(ticker, float(price_now), score, final_reasoning)
            else:
                # 조건 불만족 시 로그만 남김 (디버깅용)
                print(f"💤 [Pass] {ticker} (S:{session_int}/RSI:{rsi:.1f}/Score:{score}) - 조건 미달")

        except Exception as e:
            print(f"❌ [Worker 오류] {e}")
            traceback.print_exc()
        finally:
            ai_request_queue.task_done()

# ==============================================================================
# 6. ANALYSIS LOGIC & PIPELINE
# ==============================================================================

async def run_f1_analysis_and_signal(ticker, df):
    global ai_cooldowns, ai_request_queue
    try:
        closes = df['c'].values
        highs = df['h'].values
        lows = df['l'].values
        volumes = df['v'].values
        opens = df['o'].values

        if len(df) < 52: return

        indicators = calculate_f1_indicators(closes, highs, lows, volumes)
        
        price_now = indicators['close']

        # 🆕 [VWAP] 이격도 계산 (현재가가 VWAP보다 몇 % 위에 있는지)
        # (+)값이면 상승세(지지), (-)값이면 하락세(저항)
        vwap_val = indicators['vwap']
        dist_vwap = ((price_now - vwap_val) / vwap_val) * 100 if vwap_val != 0 else 0.0
        
        if len(closes) >= 6:
            price_5m = closes[-6]
            pump_strength_5m = ((price_now - price_5m) / price_5m) * 100 if price_5m != 0 else 0
        else: pump_strength_5m = 0.0

        day_high = np.max(highs)
        pullback = ((day_high - price_now) / day_high) * 100 if day_high > 0 else 0.0

        day_open = opens[0]
        daily_change = ((price_now - day_open) / day_open) * 100 if day_open > 0 else 0.0

        squeeze_ratio = indicators['bb_width_now'] / indicators['bb_width_avg'] if indicators['bb_width_avg'] > 0 else 1.0

        vol_avg_5 = np.mean(volumes[-6:-1]) if len(volumes) > 6 else 1
        is_volume_dry = indicators['volume'] < (vol_avg_5 * 1.0) 

        cond_wae = (indicators['macd_delta'] > indicators['bb_gap_wae']) and \
                   (indicators['macd_delta'] > indicators['dead_zone'])
        
        rsi_val = indicators['rsi']
        cmf_val = indicators['cmf']
        
        cond_rsi = 40 < rsi_val < 70
        cond_vol = (cmf_val > 0)

        cloud_top = indicators['cloud_top']
        is_above_cloud = price_now > cloud_top
        
        cloud_thick = abs(indicators['senkou_a'] - indicators['senkou_b']) / price_now * 100
        dist_bull = (price_now - cloud_top) / price_now * 100
        cond_cloud_shape = (cloud_thick >= 0.5) and (0 <= dist_bull <= 20.0)

        engine_1 = cond_wae and cond_rsi
        engine_2 = cond_cloud_shape and cond_vol and cond_rsi
        cond_pre = (squeeze_ratio < 1.3) and is_volume_dry and is_above_cloud

        if (engine_1 or engine_2 or cond_pre) and pump_strength_5m > 0.0:
            print(f"✨ [초기 감지] {ticker} | 전략: {'WAE' if engine_1 else 'Squeeze' if cond_pre else 'Cloud'} | RSI:{rsi_val:.0f} | Sqz:{squeeze_ratio:.2f}")

        # ... (앞부분의 engine_1, engine_2 판단 로직 그대로 유지) ...

        if (engine_1 or engine_2 or cond_pre) and cond_rsi:
            
            import time
            current_ts = time.time()
            if ticker in ai_cooldowns:
                last_call = ai_cooldowns[ticker]
                if current_ts - last_call < 60: return 

            session = get_current_session()
            vol_ratio = indicators['volume'] / vol_avg_5 if vol_avg_5 > 0 else 1.0

            if engine_1: strat = "Explosion (WAE)"
            elif cond_pre: strat = "Pre-Breakout (Squeeze)"
            else: strat = "Standard Setup"

            # 🛠️ [FIX] V16 세션 정수 변환 및 변수 추출 (순서 중요!)
            ny_tz = pytz.timezone('US/Eastern')
            now_dt = datetime.now(ny_tz)
            total_min = now_dt.hour * 60 + now_dt.minute
            
            session_int = 3 # Default
            if 570 <= total_min < 630: session_int = 0
            elif 630 <= total_min < 840: session_int = 1
            elif 840 <= total_min < 960: session_int = 2
            
            # 여기서 미리 변수를 꺼내야 에러가 안 납니다.
            oar_current = indicators['oar_calc']
            oar_prev = indicators['oar_prev']
            oar_delta = oar_current - oar_prev
            rvol_val = indicators['rvol']
            trend_val = indicators['trend_align']

            ai_data = {
                "session_type": session,
                "session_int": session_int,  # 정수형 세션 추가
                "strategy_type": strat,
                "vwap_distance": float(round(dist_vwap, 2)),
                "volume_ratio": float(round(vol_ratio, 2)),
                "pump_strength_5m": float(round(pump_strength_5m, 2)),
                "pullback_from_high": float(round(pullback, 2)),
                "daily_change": float(round(daily_change, 2)),
                "squeeze_ratio": float(round(squeeze_ratio, 2)),
                "is_volume_dry": bool(is_volume_dry),
                "engine_1_pass": bool(engine_1),
                "engine_2_pass": bool(engine_2),
                "pre_breakout": bool(cond_pre),
                "rsi_value": float(round(rsi_val, 2)),
                "cmf_value": float(round(cmf_val, 2)),
                "cloud_distance_percent": float(round(dist_bull, 2)),
                # 👇 V16 필수 데이터
                "rvol": float(round(rvol_val, 2)),
                "oar_calc": float(round(oar_current, 2)),
                "oar_delta": float(round(oar_delta, 2)),
                "trend_align": int(trend_val)
            }
            
            task_payload = {
                'ticker': ticker,
                'price': price_now,
                'ai_data': ai_data,
                'strat': strat,
                'squeeze': squeeze_ratio,
                'pump': pump_strength_5m
            }
            ai_cooldowns[ticker] = current_ts
            ai_request_queue.put_nowait(task_payload)
    except Exception as e:
        print(f"❌ [F1 Engine Error] {ticker}: {e}")
        # traceback.print_exc() # 디버깅 시 주석 해제

async def run_initial_analysis():
    print("⏳ [초기 분석] 로드된 과거 데이터를 기반으로 지표 계산 시작...")
    global ticker_minute_history
    
    for ticker, df in ticker_minute_history.items():
        try:
            cols_to_fix = ['o', 'h', 'l', 'c', 'v']
            for col in cols_to_fix:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            df.ffill(inplace=True)
            df.bfill(inplace=True)
            df.fillna(0, inplace=True)
            df = df.astype(float)
        except Exception as e:
             print(f"⚠️ [초기 분석 데이터 세탁 실패] {ticker}: {e}")
             continue

        await run_f1_analysis_and_signal(ticker, df)
        
    print("✅ [초기 분석] 모든 종목의 지표 계산 및 초기 시그널 검토 완료.")

# ==============================================================================
# 7. WEBSOCKET HANDLING & SCANNER
# ==============================================================================

async def handle_msg(msg_data):
    global ticker_minute_history, ticker_tick_history
    
    if isinstance(msg_data, dict): msg_data = [msg_data]
    minute_data = []

    for msg in msg_data:
        ticker = msg.get('sym')
        if not ticker: continue
        if msg.get('ev') == 'T':
            if ticker not in ticker_tick_history: ticker_tick_history[ticker] = []
            ticker_tick_history[ticker].append([msg.get('t'), msg.get('p'), msg.get('s')])
            if len(ticker_tick_history[ticker]) > 2000: ticker_tick_history[ticker].pop(0)
        elif msg.get('ev') == 'AM':
            minute_data.append(msg)

    for msg in minute_data:
        ticker = msg.get('sym')
        
        if ticker not in ticker_minute_history:
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v', 't'])
            ticker_minute_history[ticker].set_index('t', inplace=True)
            
        ts = pd.to_datetime(msg['s'], unit='ms')
        ticker_minute_history[ticker].loc[ts] = [msg['o'], msg['h'], msg['l'], msg['c'], msg['v']]
        
        if len(ticker_minute_history[ticker]) > 1000:
            ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-1000:]
            
        df = ticker_minute_history[ticker].copy()
        
        if len(df) < 52: continue

        try:
            cols_to_fix = ['o', 'h', 'l', 'c', 'v']
            for col in cols_to_fix:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            df.ffill(inplace=True)
            df.bfill(inplace=True)
            df.fillna(0, inplace=True)
            
            df = df.astype(float)

            df = df.resample('1min').agg({
                'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum'
            })
            df.ffill(inplace=True) 
            
            if ticker in ticker_tick_history and len(ticker_tick_history[ticker]) > 0:
                try:
                    ticks_df = pd.DataFrame(ticker_tick_history[ticker], columns=['t', 'p', 's'])
                    ticks_df['t'] = pd.to_datetime(ticks_df['t'], unit='ms')
                    ticks_df.set_index('t', inplace=True)
                    last_tick_price = ticks_df['p'].iloc[-1]
                    df.iloc[-1, df.columns.get_loc('c')] = float(last_tick_price)
                except Exception as e:
                    print(f"⚠️ [Tick Interpolation Error] {ticker}: {e}")

        except Exception as e:
            print(f"⚠️ [데이터 세탁 실패] {ticker}: {e}")
            continue

        try:
            closes = df['c'].values
            highs = df['h'].values
            lows = df['l'].values
            volumes = df['v'].values
            opens = df['o'].values

            indicators = calculate_f1_indicators(closes, highs, lows, volumes)
            
            price_now = indicators['close']
            
            if len(closes) >= 6:
                price_5m = closes[-6]
                pump_strength_5m = ((price_now - price_5m) / price_5m) * 100 if price_5m != 0 else 0
            else: pump_strength_5m = 0.0

            day_high = np.max(highs)
            pullback = ((day_high - price_now) / day_high) * 100 if day_high > 0 else 0.0

            day_open = opens[0]
            daily_change = ((price_now - day_open) / day_open) * 100 if day_open > 0 else 0.0

            squeeze_ratio = indicators['bb_width_now'] / indicators['bb_width_avg'] if indicators['bb_width_avg'] > 0 else 1.0

            vol_avg_5 = np.mean(volumes[-6:-1]) if len(volumes) > 6 else 1
            is_volume_dry = indicators['volume'] < (vol_avg_5 * 1.0)

            cond_wae = (indicators['macd_delta'] > indicators['bb_gap_wae']) and \
                       (indicators['macd_delta'] > indicators['dead_zone'])
            
            rsi_val = indicators['rsi']
            cmf_val = indicators['cmf']
            
            cond_rsi = 40 < rsi_val < 75
            cond_vol = (cmf_val > 0)

            cloud_top = indicators['cloud_top']
            is_above_cloud = price_now > cloud_top
            
            cloud_thick = abs(indicators['senkou_a'] - indicators['senkou_b']) / price_now * 100
            dist_bull = (price_now - cloud_top) / price_now * 100
            cond_cloud_shape = (cloud_thick >= 0.5) and (0 <= dist_bull <= 20.0)

            engine_1 = cond_wae and cond_rsi
            engine_2 = cond_cloud_shape and cond_vol and cond_rsi
            cond_pre = (squeeze_ratio < 1.3) and is_volume_dry and is_above_cloud

            if pump_strength_5m > 2.0:
                 print(f"🔍 [Check] {ticker} (+{pump_strength_5m:.1f}%) | Sqz:{squeeze_ratio:.2f} | WAE:{cond_wae} | RSI:{rsi_val:.0f}")

            if (engine_1 or engine_2 or cond_pre) and cond_rsi:
                
                import time
                current_ts = time.time()
                if ticker in ai_cooldowns:
                    last_call = ai_cooldowns[ticker]
                    if current_ts - last_call < 60: continue 

                session = get_current_session()
                if session == "closed": pass
                
                vol_ratio = indicators['volume'] / vol_avg_5 if vol_avg_5 > 0 else 1.0

                if engine_1: strat = "Explosion (WAE)"
                elif cond_pre: strat = "Pre-Breakout (Squeeze)"
                else: strat = "Standard Setup"

                ai_data = {
                    "session_type": session,
                    "strategy_type": strat,
                    "volume_ratio": float(round(vol_ratio, 2)),
                    "pump_strength_5m": float(round(pump_strength_5m, 2)),
                    "pullback_from_high": float(round(pullback, 2)),
                    "daily_change": float(round(daily_change, 2)),
                    "squeeze_ratio": float(round(squeeze_ratio, 2)),
                    "is_volume_dry": bool(is_volume_dry),
                    "engine_1_pass": bool(engine_1),
                    "engine_2_pass": bool(engine_2),
                    "pre_breakout": bool(cond_pre),
                    "rsi_value": float(round(rsi_val, 2)),
                    "cmf_value": float(round(cmf_val, 2)),
                    "cloud_distance_percent": float(round(dist_bull, 2))
                }
                
                task_payload = {
                    'ticker': ticker,
                    'price': price_now,
                    'ai_data': ai_data,
                    'strat': strat,
                    'squeeze': squeeze_ratio,
                    'pump': pump_strength_5m
                }
                ai_cooldowns[ticker] = current_ts
                ai_request_queue.put_nowait(task_payload)

        except Exception as e:
            print(f"❌ [handle_msg Error] {ticker}: {e}")
            traceback.print_exc()

async def websocket_engine(websocket):
    try:
        async for message in websocket:
            try:
                data_list = json.loads(message)
                await handle_msg(data_list) 
            except Exception as e:
                print(f"-> ❌ [v9.0 수신 엔진 CRASH] 'handle_msg' 호출 실패: {e}")
                
    except websockets.exceptions.ConnectionClosed as e:
        print(f"-> ❌ [엔진 v9.0] 웹소켓 연결 종료: {e.reason}") 
    except Exception as e:
        print(f"-> ❌ [엔진 v9.0] 웹소켓 오류: {e}")

# ==============================================================================
# 7. WEBSOCKET HANDLING & SCANNER (V18.0: Zero Latency)
# ==============================================================================

async def polygon_ws_client():
    uri = "wss://socket.polygon.io/stocks"
    
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print("\n🔌 [WebSocket] Polygon 서버 접속 중...")
                
                # 1. 인증
                await websocket.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                auth_res = await websocket.recv()
                print(f"🔑 [Auth] {auth_res}")

                # 2. DB 청소 (시작 시 1회 수행)
                print("[System] 오래된 데이터 정리 중...")
                conn = None
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM signals WHERE time < NOW() - INTERVAL '24 hours'")
                    cursor.execute("DELETE FROM recommendations WHERE time < NOW() - INTERVAL '24 hours'")
                    conn.commit()
                    cursor.close()
                except Exception as e:
                    print(f"⚠️ [DB Clean Error] {e}")
                    if conn: conn.rollback()
                finally:
                    if conn: db_pool.putconn(conn)

                # 3. 감시 종목 선정 (Top 1000)
                global watched_tickers
                watched_tickers = find_active_tickers()
                
                if not watched_tickers:
                    print("⚠️ [Warning] 감시할 종목이 없습니다. 30초 후 재시도.")
                    await asyncio.sleep(30)
                    continue

                # 4. 🚀 [핵심] 병렬 데이터 로딩 (Parallel Loading)
                # 기존의 순차적 대기(8분 소요)를 제거하고 50개 스레드로 동시 요청 (30초 소요)
                print(f"📚 [History] {len(watched_tickers)}개 종목 과거 데이터 병렬 수집 시작 (Workers: {HISTORY_WORKERS})...")
                
                loop = asyncio.get_event_loop()
                # ThreadPoolExecutor를 사용하여 fetch_initial_data를 병렬로 실행
                await loop.run_in_executor(
                    None, 
                    lambda: list(ThreadPoolExecutor(max_workers=HISTORY_WORKERS).map(fetch_initial_data, list(watched_tickers)))
                )
                print("✅ [History] 데이터 수집 완료. 실시간 분석 엔진 가동.")

                # 5. 초기 데이터 기반 1차 분석 실행 (V17 로직 적용)
                await run_initial_analysis()

                # 6. WebSocket 구독 (Batch Subscribe)
                # 1000개를 한 번에 보내면 메시지가 너무 길 수 있으므로 나눠서 구독
                ticker_list = list(watched_tickers)
                batch_size = 500 
                
                for i in range(0, len(ticker_list), batch_size):
                    batch = ticker_list[i:i+batch_size]
                    params = ",".join([f"AM.{t}" for t in batch] + [f"T.{t}" for t in batch])
                    await websocket.send(json.dumps({"action": "subscribe", "params": params}))
                    print(f"📡 [Subscribe] Batch {i//batch_size + 1}: {len(batch)}개 구독 요청.")

                # 7. AI 워커 태스크 시작
                asyncio.create_task(ai_worker())

                print("🔥 [System] V18.0 Real-time Scanning Started (Delay Removed) 🔥")

                # 8. 무한 루프: 메시지 수신 즉시 처리 (Non-blocking)
                while True:
                    msg = await websocket.recv()
                    data = json.loads(msg)
                    # 메시지를 받자마자 비동기 Task로 던져버림 (대기 시간 0)
                    asyncio.create_task(handle_msg(data))

        except Exception as e:
            print(f"❌ [WebSocket Error] {e} - 5초 후 재연결...")
            await asyncio.sleep(5)
async def manual_keepalive(websocket):
    try:
        while True:
            await websocket.ping()
            print("-> [Keepalive] Ping 전송 (연결 유지)")
            await asyncio.sleep(20)
    except websockets.exceptions.ConnectionClosed:
        print("-> [Keepalive] 연결 종료됨. Ping 중단.")
    except Exception as e:
        print(f"-> ❌ [Keepalive] 핑 전송 중 오류: {e}")

# ==============================================================================
# 8. MAIN ENTRY POINT
# ==============================================================================

async def main():
    if not POLYGON_API_KEY:
        print("❌ [메인] POLYGON_API_KEY가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    if not DATABASE_URL:
        print("❌ [메인] DATABASE_URL이 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    if not GEMINI_API_KEY:
        print("❌ [메인] GEMINI_API_KEY가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    if not GCP_PROJECT_ID or "YOUR_PROJECT_ID" in GCP_PROJECT_ID:
        print("❌ [메인] GCP_PROJECT_ID가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    
    if not FIREBASE_ADMIN_SDK_JSON_STR:
        print("⚠️ [메인] FIREBASE_ADMIN_SDK_JSON이 설정되지 않았습니다. FCM 푸시 알림이 비활성화됩니다.")

    print("스캐너 V16.7 (FCM-Admin SDK)을 시작합니다...") 
    uri = "wss://socket.polygon.io/stocks"
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=None, ping_timeout=300) as websocket:
                print(f"[메인] 웹소켓 {uri} 연결 성공.")
                
                response = await websocket.recv()
                print(f"[메인] 연결 응답: {response}")

                if '"status":"connected"' not in str(response):
                     print("-> ❌ [메인] 비정상 연결 응답. 10초 후 재시도...")
                     await asyncio.sleep(10)
                     continue

                api_key_to_use = POLYGON_API_KEY or ""
                print(f"[메인] API 키 ({api_key_to_use[:4]}...)로 '수동 인증'을 시도합니다...")
                auth_payload = json.dumps({"action": "auth", "params": api_key_to_use})
                await websocket.send(auth_payload)
                
                response = await websocket.recv()
                print(f"[메인] 인증 응답: {response}")
                
                if '"status":"auth_success"' in str(response):
                    print("-> ✅ [메인] '수동 인증' 성공! 4개 로봇(사냥꾼, 엔진, 핑, 워커)을 시작합니다.")
                    
                    watcher_task = websocket_engine(websocket) 
                    keepalive_task = manual_keepalive(websocket)
                    worker_task = asyncio.create_task(ai_worker())
                    
                    await asyncio.gather(
                        watcher_task, 
                        keepalive_task, 
                        worker_task
                    )
                else:
                    print("-> ❌ [메인] '수동 인증' 실패. 10초 후 재시도...")
                    await asyncio.sleep(10)
                    continue  
                    
        except websockets.exceptions.ConnectionClosed as e:
            print(f"-> ❌ [메인] 웹소켓 연결이 예기치 않게 종료되었습니다 ({e.code}). 10초 후 재연결합니다...")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"-> ❌ [메인] 치명적 오류 발생: {e}. 10초 후 재연결합니다...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    init_db() 
    init_firebase() 
    
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        print("--- [TEST MODE] ---")
        print("DB와 Firebase 초기화 완료. 3초 후 테스트 알림을 발송합니다...")
        time.sleep(3) 
        
        send_fcm_notification(
            ticker="TEST", 
            price=123.45, 
            probability_score=99
        )
        
        print("--- [TEST MODE] 테스트 완료. 스크립트를 종료합니다. ---")
    
    else:
        try: 
            print("--- [LIVE MODE] 스캐너를 시작합니다... ---")
            asyncio.run(main()) 
        except KeyboardInterrupt: 
            print("\n[메인] 사용자에 의해 프로그램이 종료되었습니다.")