import asyncio
import websockets
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
from functools import partial
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
TOP_N = 100
MIN_DATA_REQ = 20

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
ai_cooldowns = {}
ai_request_queue = asyncio.Queue()
db_pool = None

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

async def send_discord_alert(ticker, price, type="signal", probability_score=50):
    """
    비동기 Discord 알림 전송 (httpx 사용)
    """
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
        )
        
    data = {"content": content}
    try: 
        async with httpx.AsyncClient() as client:
            await client.post(DISCORD_WEBHOOK_URL, json=data)
        print(f"🔔 [알림] {ticker} @ ${price:.4f} (디스코드 전송 완료)")
    except Exception as e: 
        print(f"[알림 오류] {ticker} 디스코드 전송 실패: {e}")

def _send_fcm_sync(ticker, price, probability_score):
    """FCM 전송 실제 로직 (동기) - 비동기 래퍼에서 호출됨"""
    if not firebase_admin._apps: return

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
            'ticker': ticker,
            'price': f"{price:.4f}",
            'probability': str(probability_score)
        }
        
        failed_tokens = []
        send_count = 0

        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            if not token: continue
            if probability_score < user_min_score: continue 

            try:
                message = messaging.Message(
                    token=token, data=data_payload, 
                    webpush=messaging.WebpushConfig(headers={'Urgency': 'high'})
                )
                messaging.send(message)
                send_count += 1
            except Exception as e:
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

async def send_fcm_notification(ticker, price, probability_score):
    """비동기 래퍼: FCM 전송을 별도 스레드에서 실행"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(_send_fcm_sync, ticker, price, probability_score))

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
    except:
        return 1.0

# 기존의 find_active_tickers와 fetch_initial_data를 지우고 아래 코드로 대체하세요.

async def find_active_tickers():
    """
    비동기 방식(httpx)으로 Top Gainers 스캔
    """
    if not POLYGON_API_KEY:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류: POLYGON_API_KEY가 설정되지 않았습니다.")
        return set()
        
    print(f"\n[사냥꾼] 1단계: 'Top Gainers' (조건: ${MAX_PRICE} 미만) 스캔 중...")
    
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={POLYGON_API_KEY}"

    tickers_to_watch = set()
    try:
        # requests 대신 httpx 사용 (비동기)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get('status') == 'OK':
            for ticker in data.get('tickers', []):
                price = ticker.get('lastTrade', {}).get('p', 999) 
                ticker_symbol = ticker.get('ticker')
                is_price_ok = price <= MAX_PRICE
                if is_price_ok and ticker_symbol:
                    tickers_to_watch.add(ticker_symbol)
                if len(tickers_to_watch) >= TOP_N: break
            print(f"-> [사냥꾼] 1단계 스캔 완료. 총 {len(tickers_to_watch)}개 종목 포착.")
            
    except Exception as e:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류 (API 키/한도 확인): {e}")
        return tickers_to_watch
    return tickers_to_watch

async def fetch_initial_data(ticker):
    """
    비동기 방식(httpx)으로 초기 캔들 데이터 로딩
    """
    if not POLYGON_API_KEY: return
    
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/"
        f"{start_date}/{end_date}?adjusted=true&sort=desc&limit=200&apiKey={POLYGON_API_KEY}"
    )
    
    try:
        # print(f"⏳ [초기화 시도] {ticker} 과거 데이터 요청 중...") # 로그 너무 많으면 주석 처리
        
        # requests 대신 httpx 사용 (비동기)
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url)
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
            # print(f"✅ [초기화] {ticker} 과거 캔들 {len(df)}개 로딩 완료.")
        else:
            # 데이터 없으면 조용히 넘어감
            pass
    except Exception as e:
        print(f"⚠️ [초기화 실패] {ticker}: {e}")

# ==============================================================================
# 4. CORE CALCULATION ENGINE (NUMPY)
# ==============================================================================

def calculate_quant_indicators(df):
    """
    [V4.0 백테스트 로직 이식]
    기존 WAE/Cloud 대신 검증된 퀀트 지표(VWAP, RVOL, Volatility_Z, Order_Imbalance 등)를 계산합니다.
    """
    try:
        # 데이터 전처리
        closes = df['c'].values
        highs = df['h'].values
        lows = df['l'].values
        volumes = df['v'].values.astype(float)
        times = df.index # 인덱스가 datetime이어야 함

        # 1. VWAP (Volume Weighted Average Price)
        tp = (highs + lows + closes) / 3
        vp = tp * volumes
        cum_vp = np.cumsum(vp)
        cum_vol = np.cumsum(volumes)
        vwap = np.divide(cum_vp, cum_vol, out=np.zeros_like(cum_vp), where=cum_vol!=0)
        
        # 2. Squeeze Ratio (BB Width / BB Avg)
        def get_bb_width(c, n=20):
            sma = pd.Series(c).rolling(n).mean().values
            std = pd.Series(c).rolling(n).std().values
            up = sma + (std * 2.0)
            low = sma - (std * 2.0)
            # 0 나누기 방지
            return np.nan_to_num((up - low) / c)
            
        bb_width = get_bb_width(closes)
        bb_avg = pd.Series(bb_width).rolling(20).mean().values
        squeeze_ratio = np.divide(bb_width, bb_avg, out=np.ones_like(bb_width), where=bb_avg!=0)

        # 3. RSI (14)
        delta = np.diff(closes, prepend=closes[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).rolling(14).mean().values
        avg_loss = pd.Series(loss).rolling(14).mean().values
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # 4. RVOL (Relative Volume)
        vol_ma20 = pd.Series(volumes).rolling(20).mean().values
        rvol = np.divide(volumes, vol_ma20, out=np.zeros_like(volumes), where=vol_ma20!=0)
        
        # 5. Volatility Z-Score
        candle_range = highs - lows
        range_ma = pd.Series(candle_range).rolling(20).mean().values
        range_std = pd.Series(candle_range).rolling(20).std().values
        volatility_z = (candle_range - range_ma) / (range_std + 1e-10)
        
        # 6. Order Imbalance (매수/매도 압력 불균형)
        range_span = highs - lows
        clv = ((closes - lows) - (highs - closes)) / (range_span + 1e-10)
        order_imbalance = clv * volumes 
        order_imbalance_ma = pd.Series(order_imbalance).rolling(5).mean().values

        # 7. Trend Alignment (EMA 60)
        ema_60 = pd.Series(closes).ewm(span=60, adjust=False).mean().values
        trend_align = np.where(closes > ema_60, 1, -1)

        # 8. Session Bucket (시간대)
        def get_session_val(t):
            # t는 timestamp
            total_min = t.hour * 60 + t.minute
            if 570 <= total_min < 630: return 0  # 09:30 ~ 10:30 (Opening)
            elif 630 <= total_min < 840: return 1 # 10:30 ~ 14:00 (Mid-Day)
            elif 840 <= total_min < 960: return 2 # 14:00 ~ 16:00 (Power Hour)
            else: return 3 # Others
            
        # df.index가 datetime 객체라고 가정
        session_bucket = np.array([get_session_val(t) for t in times])

        # 마지막 시점(Current)의 값들을 딕셔너리로 반환
        idx = -1
        return {
            "close": closes[idx],
            "volume": volumes[idx],
            "vwap": vwap[idx],
            "squeeze_ratio": squeeze_ratio[idx],
            "rsi": rsi[idx],
            "rvol": rvol[idx],
            "volatility_z": volatility_z[idx],
            "order_imbalance": order_imbalance_ma[idx],
            "trend_align": int(trend_align[idx]),
            "session": int(session_bucket[idx]),
            "prev_close_5": closes[idx-5] if len(closes) > 5 else closes[0],
            "recent_high": np.max(highs[-200:]) if len(highs) > 0 else highs[idx]
        }
        
    except Exception as e:
        # print(f"Calc Error: {e}")
        return None
# ==============================================================================
# 5. AI WORKER & FUNCTIONS
# ==============================================================================

async def get_gemini_probability(ticker, conditions_data):
    if not GEMINI_API_KEY:
        print(f"-> [Gemini AI] {ticker}: GEMINI_API_KEY가 설정되지 않아 AI 분석을 건너뜁니다.")
        return 50 
    if not GCP_PROJECT_ID or "YOUR_PROJECT_ID" in GCP_PROJECT_ID:
        print(f"-> [Gemini AI] {ticker}: GCP_PROJECT_ID가 설정되지 않아 AI 분석을 건너뜁니다.")
        return 50

    system_prompt = """
Your goal is to predict the probability of a **+3% profit within 10 minutes**.

**[CRITICAL: Feature Interpretation Guide]**
Analyze the input data based on these trained patterns:

1.  **`rvol` (Relative Volume):** Must be > 1.5. High volume validates the move.
2.  **`volatility_z` (Z-Score):** Values > 2.0 indicate an explosive breakout.
3.  **`order_imbalance`:** Positive values mean aggressive buying. Negative means selling.
4.  **`squeeze`:** Values < 0.10 indicate extreme energy compression (Ready to explode).
5.  **`pump`:** 2% ~ 5% is ideal. > 10% is dangerous (Chasing).
6.  **`pullback`:** Must be < 5.0%. Ideally 0~2% (High Tight Flag).
7.  **`trend_align`:** 1 is Bullish (Price > EMA60). -1 is Bearish.

**[SCORING RULES]**
* **90-99 (Diamond):** Perfect Setup. `rvol` > 3.0, `squeeze` < 0.1, `volatility_z` > 2.0, `order_imbalance` > 0.
* **80-89 (Gold):** Strong Momentum. Good volume and trend alignment.
* **60-79 (Silver):** Decent, but maybe chasing or low volume.
* **0-59 (Pass):** Weak volume, huge pullback (>10%), or negative order imbalance.

Respond ONLY with JSON:
{
  "probability_score": <int>,
  "reasoning": "<Short explanation based on rvol, z-score, and imbalance>"
}
"""
    user_prompt = f"""
    Analyze the following signal data for Ticker: {ticker}
    
    [MARKET CONTEXT]
    - Current Session: {conditions_data.get('session_type', 'unknown')}
    - Volume Ratio: {conditions_data.get('volume_ratio', 0.0)}
    
    [TECHNICAL DATA]
    {json.dumps(conditions_data, indent=2)}
    """
    
    api_url = (
        f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT_ID}"
        f"/locations/{GCP_REGION}/publishers/google/models/gemini-2.5-flash-lite:generateContent"
    )

    combined_prompt = f"{system_prompt}\n\n{user_prompt}"

    payload = {
        "contents": [
            {
                "role": "user", 
                "parts": [{"text": combined_prompt}]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, headers=headers, timeout=10.0)
            
            if not response.is_success:
                print(f"-> ❌ [Gemini AI] {ticker} 요청 실패 (HTTP {response.status_code}): {response.text}")
                response.raise_for_status() 
                
            result = response.json()
            
            if 'candidates' not in result:
                if 'error' in result:
                     print(f"-> ❌ [Gemini AI] {ticker} Vertex AI 오류: {result['error']['message']}")
                     return 50
                print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: 응답에 'candidates' 없음. {result}")
                return 50

            response_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            
            if '```json' in response_text:
                print(f"-> [Gemini AI] {ticker}: Markdown 감지됨, JSON 추출 시도...")
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                if start != -1 and end != -1:
                    response_text = response_text[start:end]
            
            if not response_text.strip().startswith('{'):
                print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: AI가 JSON이 아닌 텍스트로 응답함. {response_text}")
                return 50

            score_data = json.loads(response_text)
            score = int(score_data.get("probability_score", 50))
            reasoning = score_data.get("reasoning", "No reasoning provided.")
            print(f"-> [Gemini AI] {ticker}: 상승 확률 {score}% (이유: {reasoning})")
            return score
    except Exception as e:
        if 'response' not in locals(): 
            print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: {e}")
        return 50

async def ai_worker():
    print("👨‍🍳 [Worker] AI 처리 전담반 가동 시작!")
    while True:
        task = await ai_request_queue.get()
        try:
            ticker = task['ticker']
            price_now = task['price']
            ai_data = task['ai_data']
            strat = task['strat']
            squeeze_val = task['squeeze']
            pump_val = task['pump']

            score = await get_gemini_probability(ticker, ai_data)

            print(f"🏎️ [F1 결과] {ticker} @ ${price_now:.4f} | AI: {score}% | Sqz: {squeeze_val:.2f} | Pump: {pump_val:.1f}%")
            
            is_new = log_recommendation(ticker, float(price_now), score)
            if is_new:
                # [수정됨] await 추가
                await send_discord_alert(ticker, float(price_now), "recommendation", score)
                await send_fcm_notification(ticker, float(price_now), score)
                
        except Exception as e:
            print(f"❌ [Worker 오류] {e}")
        finally:
            ai_request_queue.task_done()

# ==============================================================================
# 6. ANALYSIS LOGIC & PIPELINE
# ==============================================================================

def calculate_soft_gate_score(data, session):
    """
    [V17.0 Soft Gate] Binary Logic -> Weighted Scoring Logic
    각 지표를 점수화하여 종합 점수(Fusion Score)를 산출합니다.
    """
    score = 0
    reasons = []

    # 1. 🌊 RVOL (거래량 에너지) - 가장 중요 (30점 만점)
    if data['rvol'] >= 5.0:
        score += 30; reasons.append("RVOL 폭발")
    elif data['rvol'] >= 3.0:
        score += 20
    elif data['rvol'] >= 1.5:
        score += 10
    else:
        score -= 10 # 거래량 부족 페널티

    # 2. 🚀 Pump Strength (상승 강도) - (25점 만점)
    pump = data['pump']
    if 2.0 <= pump <= 5.0:
        score += 25; reasons.append("Golden Pump")
    elif 1.0 <= pump < 2.0:
        score += 10 # 시동 거는 중
    elif 5.0 < pump <= 8.0:
        score += 15 # 강하지만 추격 위험 있음
    elif pump > 8.0:
        score += 5; reasons.append("Too High(Risk)") # 과열 감점

    # 3. 📉 RSI & Session Context (20점 만점)
    rsi = data['rsi']
    # 오후장(Session 2 이상)은 RSI 필터가 핵심
    if session >= 2:
        if 50 <= rsi <= 75:
            score += 20; reasons.append("PM Safe Zone")
        elif rsi > 75:
            score -= 5 # 오후장 과매수는 위험
        else:
            score += 5
    # 오전장/점심장
    else:
        if 50 <= rsi <= 80:
            score += 20
        elif rsi > 80:
            score += 10 # 초반 슈팅 인정

    # 4. 🎯 VWAP Distance (눌림목) - (15점 만점)
    vwap_dist = data['vwap_dist']
    if 0 <= vwap_dist <= 2.0:
        score += 15; reasons.append("Perfect Pullback")
    elif 2.0 < vwap_dist <= 4.0:
        score += 5
    elif vwap_dist < 0:
        score -= 5 # 역배열 위험

    # 5. 🔬 Microstructure (OAR & Volatility) - (10점 만점)
    if data['volatility_z'] > 2.0:
        score += 5
    if data['order_imbalance'] > 0:
        score += 5

    # 6. ⚖️ Session Weight (세션별 가중치/페널티)
    # 점심장(1)은 가짜가 많으므로 전체 점수에서 10점 깎고 시작 (Iron Dome)
    if session == 1:
        score -= 10
        
    return score, reasons

async def run_f1_analysis_and_signal(ticker, df):
    global ai_cooldowns, ai_request_queue
    try:
        if len(df) < 60: return 

        # 1. 퀀트 지표 계산
        indicators = calculate_quant_indicators(df)
        if indicators is None: return
        
        price_now = indicators['close']
        
        # 2. Feature Engineering
        pump_strength = ((price_now - indicators['prev_close_5']) / indicators['prev_close_5']) * 100
        pullback = ((indicators['recent_high'] - price_now) / indicators['recent_high']) * 100
        vwap_dist = ((price_now - indicators['vwap']) / indicators['vwap']) * 100 if indicators['vwap'] != 0 else 0

        # 데이터 패킷 준비 (점수 계산용)
        score_data = {
            'rvol': indicators['rvol'],
            'pump': pump_strength,
            'rsi': indicators['rsi'],
            'vwap_dist': vwap_dist,
            'volatility_z': indicators['volatility_z'],
            'order_imbalance': indicators['order_imbalance']
        }

        # 3. [V17.0] Soft Gate Scoring (점수 산출)
        tech_score, score_reasons = calculate_soft_gate_score(score_data, indicators['session'])
        
        # 4. Tier 분류
        tier = "TRASH"
        if tech_score >= 85: tier = "ELITE"   # 즉시 진입급
        elif tech_score >= 70: tier = "VALID" # AI 확인 필요
        elif tech_score >= 50: tier = "WATCH" # 관망
        
        # ELITE나 VALID 등급만 AI 프로세스 태움 (API 비용 절약)
        if tier in ["ELITE", "VALID"]:
            
            # 쿨다운 체크
            import time
            current_ts = time.time()
            if ticker in ai_cooldowns:
                if current_ts - ai_cooldowns[ticker] < 60: return 

            reason_str = ", ".join(score_reasons)
            print(f"✨ [{tier}] {ticker} | Score: {tech_score} | {reason_str}")

            # AI에게 보낼 데이터
            ai_data = {
                "technical_score": int(tech_score),
                "tier": tier,
                "vwap_dist": float(round(vwap_dist, 2)),
                "squeeze": float(round(indicators['squeeze_ratio'], 2)),
                "rsi": float(round(indicators['rsi'], 2)),
                "pump": float(round(pump_strength, 2)),
                "pullback": float(round(pullback, 2)),
                "rvol": float(round(indicators['rvol'], 2)),
                "volatility_z": float(round(indicators['volatility_z'], 2)),
                "order_imbalance": float(round(indicators['order_imbalance'], 2)),
                "trend_align": int(indicators['trend_align']),
                "session": int(indicators['session'])
            }
            
            task_payload = {
                'ticker': ticker,
                'price': price_now,
                'ai_data': ai_data,
                'strat': f"SoftGate {tier}", 
                'squeeze': ai_data['squeeze'],
                'pump': ai_data['pump']
            }
            ai_cooldowns[ticker] = current_ts
            ai_request_queue.put_nowait(task_payload)

    except Exception as e:
        # print(f"Error in signal: {e}")
        pass

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
                except: pass

        except Exception as e:
            print(f"⚠️ [데이터 세탁 실패] {ticker}: {e}")
            continue

        try:
            closes = df['c'].values
            highs = df['h'].values
            lows = df['l'].values
            volumes = df['v'].values
            opens = df['o'].values

            indicators = calculate_quant_indicators(closes, highs, lows, volumes)
            
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
            import traceback
            pass

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

async def periodic_scanner(websocket):
    current_subscriptions = set() 
    
    while True:
        try:
            print(f"\n[사냥꾼] (V2.1) 3분 주기 시작. DB 청소 중...")
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("TRUNCATE TABLE signals")
                cursor.execute("TRUNCATE TABLE recommendations")
                conn.commit()
                cursor.close()
                print("-> [사냥꾼] DB 청소 완료.")
            except Exception as e:
                print(f"-> ❌ [사냥꾼] DB 청소 실패: {e}")
                if conn: conn.rollback()
            finally:
                if conn: db_pool.putconn(conn)
            
            # [수정] await 추가됨
            new_tickers = await find_active_tickers() 
            
            tickers_to_add = new_tickers - current_subscriptions
            tickers_to_remove = current_subscriptions - new_tickers
            
            if tickers_to_add:
                print(f"[사냥꾼] 신규 {len(tickers_to_add)}개 구독 및 로딩...")
                for ticker in tickers_to_add:
                    params_str = f"AM.{ticker},T.{ticker}"
                    sub_payload = json.dumps({"action": "subscribe", "params": params_str})
                    await websocket.send(sub_payload)
                    
                    # [수정] await 추가됨
                    await fetch_initial_data(ticker) 
                    
                    # 웹소켓 부하 방지를 위해 짧은 대기
                    await asyncio.sleep(0.05) 
                    
                print("[사냥꾼] 신규 구독 완료.")
                
                await run_initial_analysis()
                
                print("[사냥꾼] 신규 구독 및 초기 분석 완료.")
                
            if tickers_to_remove:
                for ticker in tickers_to_remove:
                    params_str = f"AM.{ticker},T.{ticker}"
                    unsub_payload = json.dumps({"action": "unsubscribe", "params": params_str})
                    await websocket.send(unsub_payload)
                    
                    if ticker in ai_cooldowns: 
                        del ai_cooldowns[ticker]
                        
                    await asyncio.sleep(0.1)
                print("[사냥꾼] 구독 해지 완료.")
            
            current_subscriptions = new_tickers
            
            status_tickers_list = []
            for ticker in current_subscriptions:
                status_tickers_list.append({"ticker": ticker, "is_new": ticker in tickers_to_add})
                
            status_data = {
                'last_scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'watching_count': len(current_subscriptions),
                'watching_tickers': status_tickers_list
            }
            
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                INSERT INTO status (key, value, last_updated) 
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    last_updated = EXCLUDED.last_updated
                """, ('status_data', json.dumps(status_data), datetime.now()))
                conn.commit()
                cursor.close()
            except Exception as e:
                print(f"❌ [DB] 'status' 저장 실패: {e}")
                if conn: conn.rollback()
            finally:
                if conn: db_pool.putconn(conn)
            
        except Exception as e:
            print(f"-> ❌ [사냥꾼 루프 오류] {e}")
            
        print(f"\n[사냥꾼] 3분(180초) 후 다음 스캔을 시작합니다...")
        await asyncio.sleep(180)

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
                    scanner_task = periodic_scanner(websocket)
                    keepalive_task = manual_keepalive(websocket)
                    worker_task = asyncio.create_task(ai_worker())
                    
                    await asyncio.gather(
                        watcher_task, 
                        scanner_task, 
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