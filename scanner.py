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

def _send_fcm_sync(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """FCM 전송 (Entry/TP/SL 포함 & 즉시 알림 표시) - 갤럭시 최적화"""
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

        # 1. 알림 내용 구성
        noti_title = f"💎 {ticker} 신호 (점수: {probability_score})"
        
        if entry and tp and sl:
            noti_body = f"진입: ${entry:.4f} | 익절: ${tp:.4f} | 손절: ${sl:.4f}"
        else:
            noti_body = f"현재가: ${price:.4f} | AI 점수: {probability_score}점"

        # 2. 데이터 페이로드 (앱 백그라운드 처리용 + 중복 정보)
        data_payload = {
            'type': 'hybrid_signal',
            'ticker': ticker,
            'price': str(price),
            'score': str(probability_score),
            'entry': str(entry) if entry else "",
            'tp': str(tp) if tp else "",
            'sl': str(sl) if sl else "",
            'title': noti_title,  # 데이터에도 제목/내용 넣어줌
            'body': noti_body
        }
        
        send_count = 0
        failed_tokens = []

        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            if not token: continue
            if probability_score < user_min_score: continue 

            try:
                message = messaging.Message(
                    token=token,
                    # 🔥 [핵심] notification 필드 (잠금화면 노출용)
                    notification=messaging.Notification(
                        title=noti_title,
                        body=noti_body
                    ),
                    data=data_payload,
                    
                    # 안드로이드 설정 (중요도 높임 & 내용 공개)
                    android=messaging.AndroidConfig(
                        priority='high',
                        notification=messaging.AndroidNotification(
                            channel_id='high_importance_channel', # 앱 채널 ID와 일치해야 함
                            priority='high',
                            default_sound=True,
                            visibility='public' # 잠금화면에서도 내용 표시 (갤럭시 필수)
                        )
                    ),
                    
                    # iOS 설정
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(
                                alert=messaging.ApsAlert(
                                    title=noti_title,
                                    body=noti_body
                                ),
                                sound="default",
                                content_available=True
                            )
                        )
                    )
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

    except Exception as e:
        print(f"❌ [FCM] 발송 중 오류: {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

async def send_fcm_notification(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """비동기 래퍼: 인자 추가됨"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(_send_fcm_sync, ticker, price, probability_score, entry, tp, sl))

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
    [수정됨] Redis Relay 방식
    1. Fetcher가 가져온 'market_snapshot'을 Redis에서 읽음 (API 호출 X)
    2. 급등주 조건 필터링
    3. STS 봇을 위해 'sts_candidates' 키로 Redis에 저장
    """
    # Redis 연결 확인 (전역 변수 r 사용, 없으면 연결 시도)
    global r  # scanner.py 상단에 r이 정의되어 있다고 가정
    if 'r' not in globals() or r is None:
        import redis
        try:
            r = redis.from_url(os.environ.get('REDIS_URL'))
        except Exception as e:
            print(f"❌ [Scanner] Redis 연결 실패: {e}")
            return set()

    try:
        # 1. Redis에서 전체 시장 데이터 읽기 (0.001초 소요)
        data_str = r.get('market_snapshot')
        if not data_str:
            print("⚠️ [Scanner] Redis에 데이터가 없습니다. (Fetcher가 실행 중인가요?)")
            return set()
            
        tickers_data = json.loads(data_str)
        tickers_to_watch = set()
        
        # 2. 필터링 로직 (기존과 동일)
        for t in tickers_data:
            price = t.get('day', {}).get('c', 0)
            change = t.get('todaysChangePerc', 0)
            ticker = t.get('ticker')
            
            # 조건: $20 미만이고 상승 중인 것 (설정에 맞게 조정)
            if ticker and 0 < price <= MAX_PRICE and change > 0:
                tickers_to_watch.add(ticker)
                
            if len(tickers_to_watch) >= TOP_N: break

        print(f"-> [Scanner] Redis 조회 완료. {len(tickers_to_watch)}개 감시 대상 포착.")
        
        # 3. 🔥 [핵심 추가] 찾은 종목을 STS 봇이 볼 수 있게 Redis에 저장
        if tickers_to_watch:
            # set은 JSON 변환이 안 되므로 list로 변환해서 저장
            r.set('sts_candidates', json.dumps(list(tickers_to_watch)))
            
        return tickers_to_watch

    except Exception as e:
        print(f"❌ [Scanner] 로직 오류: {e}")
        import traceback
        traceback.print_exc()
        return set()

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
    [V18.0 Logic] 가속도, 기울기, 연속성을 포함한 퀀트 지표 계산
    기존의 단순 수치 계산에서 벗어나 변화량(Slope)과 질(Quality)을 측정합니다.
    """
    try:
        # 데이터 전처리
        closes = df['c'].values
        highs = df['h'].values
        lows = df['l'].values
        volumes = df['v'].values.astype(float)
        times = df.index # 인덱스가 datetime이어야 함

        # [NEW] ATR (14) 계산
        # True Range = Max(High-Low, Abs(High-PrevClose), Abs(Low-PrevClose))
        prev_closes = np.roll(closes, 1)
        prev_closes[0] = closes[0] # 첫 번째 값 보정

        tr1 = highs - lows
        tr2 = np.abs(highs - prev_closes)
        tr3 = np.abs(lows - prev_closes)
        
        true_range = np.maximum(tr1, np.maximum(tr2, tr3))
        atr_14 = pd.Series(true_range).rolling(14).mean().values

        # 1. VWAP (Volume Weighted Average Price)
        tp = (highs + lows + closes) / 3
        vp = tp * volumes
        cum_vp = np.cumsum(vp)
        cum_vol = np.cumsum(volumes)
        vwap = np.divide(cum_vp, cum_vol, out=np.zeros_like(cum_vp), where=cum_vol!=0)

        # ✅ [NEW] VWAP 기울기 (최근 3분간 변화량)
        vwap_slope = 0.0
        if len(vwap) >= 4:
            vwap_slope = (vwap[-1] - vwap[-4]) / vwap[-4] * 10000
        
        # 2. Squeeze Ratio (BB Width / BB Avg)
        def get_bb_width(c, n=20):
            sma = pd.Series(c).rolling(n).mean().values
            std = pd.Series(c).rolling(n).std().values
            up = sma + (std * 2.0)
            low = sma - (std * 2.0)
            return np.nan_to_num((up - low) / c)
            
        bb_width = get_bb_width(closes)
        bb_avg = pd.Series(bb_width).rolling(20).mean().values
        squeeze_ratio = np.divide(bb_width, bb_avg, out=np.ones_like(bb_width), where=bb_avg!=0)

        # 3. Pump (상승) 가속도 계산
        # 현재 5분 등락률
        price_now = closes[-1]
        price_5m_ago = closes[-6] if len(closes) > 6 else closes[0]
        current_pump = ((price_now - price_5m_ago) / price_5m_ago) * 100 if price_5m_ago != 0 else 0
        
        # ✅ [NEW] 2분 전 시점의 5분 등락률 (과거의 모멘텀)
        price_2m_ago = closes[-3] if len(closes) > 3 else closes[0]
        price_7m_ago = closes[-8] if len(closes) > 8 else closes[0]
        prev_pump = ((price_2m_ago - price_7m_ago) / price_7m_ago) * 100 if price_7m_ago != 0 else 0
        
        # 가속도 = 현재 모멘텀 - 과거 모멘텀 (양수면 가속, 음수면 감속/설거지)
        pump_acceleration = current_pump - prev_pump

        # 4. RSI (14)
        delta = np.diff(closes, prepend=closes[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).rolling(14).mean().values
        avg_loss = pd.Series(loss).rolling(14).mean().values
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # 5. RVOL (Relative Volume)
        vol_ma20 = pd.Series(volumes).rolling(20).mean().values
        rvol = np.divide(volumes, vol_ma20, out=np.zeros_like(volumes), where=vol_ma20!=0)

        # ✅ [NEW] RVOL 3틱 연속 증가 확인 & 기울기
        rvol_consecutive_up = False
        rvol_slope = 0.0
        if len(rvol) >= 4:
            # 3틱 연속 증가: t-2 < t-1 < t
            rvol_consecutive_up = (rvol[-1] > rvol[-2]) and (rvol[-2] > rvol[-3])
            # 기울기: 현재 - 3분전 (추세 강도)
            rvol_slope = rvol[-1] - rvol[-3]
        
        # 6. Volatility Z-Score & Order Imbalance
        candle_range = highs - lows
        range_ma = pd.Series(candle_range).rolling(20).mean().values
        range_std = pd.Series(candle_range).rolling(20).std().values
        # 0 나누기 방지
        volatility_z = np.divide((candle_range - range_ma), (range_std + 1e-10))
        
        range_span = highs - lows
        clv = ((closes - lows) - (highs - closes)) / (range_span + 1e-10)
        order_imbalance = clv * volumes 
        order_imbalance_ma = pd.Series(order_imbalance).rolling(5).mean().values

        # 7. Trend Alignment (EMA 60)
        ema_60 = pd.Series(closes).ewm(span=60, adjust=False).mean().values
        trend_align = np.where(closes > ema_60, 1, -1)

        # 8. Session Bucket (시간대)
        def get_session_val(t):
            total_min = t.hour * 60 + t.minute
            if 570 <= total_min < 630: return 0  # 09:30 ~ 10:30 (Opening)
            elif 630 <= total_min < 840: return 1 # 10:30 ~ 14:00 (Mid-Day)
            elif 840 <= total_min < 960: return 2 # 14:00 ~ 16:00 (Power Hour)
            else: return 3 # Others
  
        session_bucket = np.array([get_session_val(t) for t in times])

        idx = -1
        return {
            "close": closes[idx],
            "volume": volumes[idx],
            "vwap": vwap[idx],
            "vwap_slope": vwap_slope,                # ✅ 추가됨
            "squeeze_ratio": squeeze_ratio[idx],
            "rsi": rsi[idx],
            "rvol": rvol[idx],
            "rvol_slope": rvol_slope,                # ✅ 추가됨
            "rvol_consecutive": rvol_consecutive_up, # ✅ 추가됨
            "pump": current_pump,
            "pump_accel": pump_acceleration,         # ✅ 추가됨
            "volatility_z": volatility_z[idx],
            "atr": atr_14[idx] if not np.isnan(atr_14[idx]) else 0.01,
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

    # [V20.0 System Prompt]
    system_prompt = """
You are a **Senior Scalping Risk Manager & Market Microstructure Analyst**.
Your primary mission is to evaluate whether the setup can realistically produce a **+3% profit within 10 minutes**
while aggressively avoiding **late chasing and bull traps**.

**[CORE PRINCIPLES]**
"Better to miss a trade than to lose money."
Prioritize **Early Breakouts** and reject **overextended, unstable setups**.

---

**[KEY EVALUATION RULES]**

### 1. Squeeze Energy (`squeeze_ratio`)
- < 0.70 = Super Compression (Pre-Breakout 💎 - IGNORE minor flaws if accel > 0)
- 0.70 ~ 0.90 = Healthy coil
- > 2.0 = Volatility spike / Chaos → REJECT

### 2. Momentum Velocity (`pump`, `pump_accel`)
- accel > 0.2 = Speed rising (ideal entry)
- accel < 0 & pump > 4% = Bull Trap (Momentum dying)
- pump > 7% = Late Chasing (High risk)

### 3. VWAP Structure (`vwap_dist`, `vwap_slope`)
- slope > 0 = Uptrend confirmed
- dist < 1.5% = Perfect pullback zone
- dist > 3% = Extended / Mean reversion risk

### 4. Volume Integrity
- rvol_consecutive = Real accumulation
- order_imbalance > 0 = Aggressive buying
- falling rvol_slope = Liquidity loss → Risk

### 5. Pullback Validation
- 0%~5% ideal
- 5%~10% allowed only if squeeze < 0.75
- >10% = Broken structure

---

**[REJECTION TRIGGERS (Instant Score < 50)]**
- pump > 5% AND accel < 0
- vwap_slope < 0
- squeeze_ratio > 2.0
- pump > 8%

---

**[SCORING TIERS]**
- **90-100 (Diamond Early Breakout):** squeeze<0.85 & accel>0.2 & rvol_consecutive
- **80-89 (Gold Valid Entry):** Strong volume & positive accel, but slightly extended
- **60-79 (Silver Watch):** Good structure but waiting for volume trigger
- **< 60 (Trap):** Avoid at all costs

---

### [RESPONSE FORMAT — STRICT JSON]
Return strictly JSON (no markdown, no text before/after):
{
  "probability_score": <0-100>,
  "risk_level": "<LOW | MEDIUM | HIGH>",
  "entry_evaluation": "<EARLY_BREAKOUT | MID_MOMENTUM | LATE_CHASING | TRAP>",
  "should_enter": "<YES | WAIT | NO>",
  "reasoning": "<Concise analysis: 1. Squeeze status 2. Acceleration check 3. Volume/VWAP verdict>",
  "micro_test": "<REQUIRED (if score 60-85) | OPTIONAL (if score > 85) | NOT_NEEDED (if score < 60)>",
  "tp_sl_comment": "<Brief TP/SL guidance based on volatility>"
}
"""

    # [V20.0 User Prompt with Key Metrics]
    user_prompt = f"""
    Analyze the following signal data for Ticker: {ticker}
    
    [MARKET CONTEXT & KEY METRICS]
    - Current Session: {conditions_data.get('session_type', 'unknown')}
    - Squeeze Ratio: {conditions_data.get('squeeze_ratio', 'N/A')} (Lower is better)
    - Pump Acceleration: {conditions_data.get('pump_accel', 'N/A')} (Positive is good)
    - VWAP Slope: {conditions_data.get('vwap_slope', 'N/A')}
    
    [FULL TECHNICAL DATA]
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
            
            # JSON 파싱 강화 로직
            if '```json' in response_text:
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                if start != -1 and end != -1:
                    response_text = response_text[start:end]
            
            try:
                score_data = json.loads(response_text)
            except json.JSONDecodeError:
                # 괄호 강제 추출 재시도
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                if start != -1 and end != -1:
                    score_data = json.loads(response_text[start:end])
                else:
                    print(f"-> ❌ [Gemini AI] {ticker} JSON 파싱 실패: {response_text}")
                    return 50

            score = int(score_data.get("probability_score", 50))
            reasoning = score_data.get("reasoning", "No reasoning provided.")
            print(f"-> [Gemini AI] {ticker}: 상승 확률 {score}% (이유: {reasoning})")
            return score
            
    except Exception as e:
        # 변수가 정의되지 않은 상태에서의 에러 처리
        if 'response' not in locals(): 
            print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: {e}")
        return 50

async def ai_worker():
    print("👨‍🍳 [Worker] V20.0 Hybrid (Quant+AI+Suitability) & Micro Logic 가동!", flush=True)
    
    while True:
        task = await ai_request_queue.get()
        try:
            ticker = task['ticker']
            initial_price = float(task['price'])
            ai_data = task['ai_data']
            
            # 1. 점수 추출
            quant_score = ai_data.get('technical_score', 0)
            suitability_score = ai_data.get('entry_suitability', 50) # 구조 점수
            
            squeeze_val = ai_data.get('squeeze_ratio', 0)
            pump_val = ai_data.get('pump', 0)
            
            print(f"🤖 [Ask Gemini] {ticker} 분석 요청... (Q:{quant_score} | Suit:{suitability_score})", flush=True)
            
            # 2. AI 분석
            ai_score = await get_gemini_probability(ticker, {
                **ai_data, 
                "squeeze_ratio": squeeze_val,
                "pump": pump_val
            })

            # 3. ⚖️ Hybrid Score V2 (3-Factor Model)
            # Quant(50%) + AI(30%) + Suitability(20%) -> 밸런스 중시
            hybrid_score = round((quant_score * 0.50) + (ai_score * 0.30) + (suitability_score * 0.20), 2)
            print(f"📊 [1차 판정] {ticker} | Hybrid: {hybrid_score} (Q{quant_score}/A{ai_score}/S{suitability_score})", flush=True)

            # 4. 1차 컷라인 (65점)
            if hybrid_score < 65: 
                print(f"📉 [Reject] {ticker} Hybrid 점수 미달 ({hybrid_score} < 65)", flush=True)
                continue

            # ==================================================================
            # 🛑 5. Advanced Micro Test (10s) - Tick Speed & Candle Shape
            # ==================================================================
            print(f"⏳ [Micro Test] {ticker} 10초간 틱 속도 및 캔들 검증...", flush=True)
            
            # 검증 시작 전 틱 카운트 (Tick History 길이를 잼)
            ticks_start_len = len(ticker_tick_history.get(ticker, []))
            await asyncio.sleep(10) 
            
            # 검증 후 데이터 확인
            if ticker not in ticker_tick_history: continue
            ticks_end_len = len(ticker_tick_history[ticker])
            
            # A. 틱 속도 (Tick Speed) 계산: 10초간 발생한 체결 건수
            ticks_count = ticks_end_len - ticks_start_len
            if ticks_count < 0: ticks_count = 10 # 리스트 갱신됐으면 기본값 처리
            
            # B. 가격 변동 확인
            current_price = initial_price # 기본값
            if ticker in ticker_tick_history and ticker_tick_history[ticker]:
                current_price = float(ticker_tick_history[ticker][-1][1])
                
            price_delta = ((current_price - initial_price) / initial_price) * 100
            
            # 🚫 [탈락 조건 1] Failing Candle (윗꼬리 달고 음전)
            if price_delta < -0.2: 
                print(f"❌ [Fail] {ticker} Failing Candle (Δ {price_delta:.2f}%) - 매수세 실종", flush=True)
                continue

            # 🚫 [탈락 조건 2] Low Tick Speed (허매수)
            # 10초 동안 체결이 5건 미만이면 호가만 비어있는 가짜 상승
            if ticks_count < 5:
                print(f"❌ [Fail] {ticker} Tick Speed Low ({ticks_count} ticks) - 거래량 부족", flush=True)
                continue

            # ✅ Soft Update (점수 미세 조정)
            bonus_score = 0
            if price_delta > 0.3: bonus_score += 5
            if ticks_count > 30: bonus_score += 5 # 틱 속도가 빠르면(활발하면) 가산점
            
            final_score = min(100, int(hybrid_score + bonus_score))
            
            if final_score < 65: # 최종 컷라인
                print(f"❌ [Drop] {ticker} 최종 점수 미달 (Final: {final_score})", flush=True)
                continue

            # ==================================================================
            # 6. 최종 기록 및 알림 (Entry/TP/SL 정보 포함)
            # ==================================================================
            entry_target = task.get('entry_price', current_price)
            tp_target = task.get('tp_price', current_price * 1.03)
            sl_target = task.get('sl_price', current_price * 0.99)
            
            is_new = log_recommendation(ticker, float(current_price), final_score)
            
            if is_new:
                await send_discord_alert(ticker, float(current_price), "hybrid_signal", final_score)
                await send_fcm_notification(
                    ticker, float(current_price), final_score, 
                    entry=entry_target, tp=tp_target, sl=sl_target
                )
                
                print(f"🏁 FINAL ENTRY: {ticker} | Hybrid: {final_score} | Δ10s: {price_delta:+.2f}% | Ticks: {ticks_count}", flush=True)
                print(f"   🎯 [Action] 진입: ${entry_target:.4f} | 익절: ${tp_target:.4f} | 손절: ${sl_target:.4f}", flush=True)
                
        except Exception as e:
            print(f"❌ [Worker 오류] {ticker}: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            ai_request_queue.task_done()

# ==============================================================================
# 6. ANALYSIS LOGIC & PIPELINE
# ==============================================================================

def calculate_soft_gate_score(data, session):
    """
    [V18.0 Logic] Momentum Acceleration & Support Validation
    단순 펌핑(Pump)이 아니라 '가속도'와 'VWAP 지지'를 봅니다.
    설거지(고점 추격) 방지에 최적화된 로직입니다.
    """
    score = 0
    reasons = []

    # 0. 💥 Squeeze (에너지 응축) - [NEW] 선취매 핵심 로직
    # squeeze_ratio < 1.0 (밴드 수축), 낮을수록 에너지가 강하게 모인 것
    squeeze = data.get('squeeze_ratio', 1.0)
    vwap_slope = data.get('vwap_slope', 0)
    
    # 극도로 수축됨 (폭발 임박) + VWAP가 살아있음
    if squeeze <= 0.8:
        if vwap_slope >= 0:
            score += 30; reasons.append("Super Squeeze (Ready)")
        else:
            score += 10 # 수축은 좋은데 추세가 없어서 관망
    # 적당히 수축됨 (안전한 진입 구간)
    elif 0.8 < squeeze <= 1.1:
        score += 15
    # 이미 밴드가 찢어짐 (이미 폭발 중이거나 변동성 과다)
    elif squeeze > 2.0:
        score -= 10 # 추격 매수 위험

    # 1. 🌊 RVOL (거래량의 질) - '연속성'과 '기울기' 중심
    # 기존: 단순히 크면 장땡 -> 수정: 3틱 연속 증가하며 기울기가 가파른가?
    rvol = data.get('rvol', 0)
    rvol_slope = data.get('rvol_slope', 0)
    is_consecutive = data.get('rvol_consecutive', False)

    if is_consecutive and rvol_slope > 0.5:
        score += 30; reasons.append("Volume Surge (3-Tick)") # 진짜 수급
    elif rvol >= 3.0 and rvol_slope > 0:
        score += 20; reasons.append("High Vol & Rising")
    elif rvol >= 1.5:
        score += 5
    elif rvol_slope < 0:
        score -= 10 # 거래량 죽는 중 (진입 금지)

    # 2. 🚀 Pump Acceleration (상승 가속도)
    pump = data.get('pump', 0)
    pump_accel = data.get('pump_accel', 0)

    # 🚨 설거지 방지: 이미 많이 올랐는데 힘 빠지면 감점
    if pump > 5.0 and pump_accel < 0:
        score -= 50; reasons.append("Peak Out(High Risk)")
    elif pump > 8.0:
        score -= 20
    # ✅ 선취매 보정: Squeeze가 좋은데 Pump가 막 시작될 때 가산점
    elif pump_accel > 0.2 and squeeze <= 1.1:
        score += 20; reasons.append("Early Breakout") 
    elif pump_accel > 0.5:
        score += 15
    
    # 3. 🎯 VWAP Support (지지 검증)
    # 기존: 대충 근처면 OK -> 수정: 딱 붙어서(1%이내) 지지받고 고개를 들었나(Slope>0)?
    vwap_dist = data.get('vwap_dist', 0)
    vwap_slope = data.get('vwap_slope', 0)
    vwap_dist_abs = abs(vwap_dist) # 위아래 상관없이 거리 절대값

    if vwap_dist_abs <= 1.0 and vwap_slope > 0:
        score += 25; reasons.append("VWAP Perfect Support") # 완벽한 눌림목
    elif vwap_dist_abs <= 2.0 and vwap_slope >= 0:
        score += 10
    elif vwap_dist < -2.0:
        score -= 10 # 역배열 (VWAP 아래)
    elif vwap_dist > 5.0:
        score -= 10 # 이격도 과다 (회귀 본능 위험)

    # 4. 📉 RSI Context (과열 방지)
    rsi = data['rsi']
    if session >= 2: # 오후장
        if 45 <= rsi <= 65:
            score += 15; reasons.append("PM Safe Zone")
        elif rsi > 70:
            score -= 10 # 오후장 과매수는 쥐약
    else: # 오전장
        if 50 <= rsi <= 75:
            score += 10
        elif rsi > 80:
            score -= 5 # 초반이라도 과열은 주의

    # 5. 🔬 Microstructure (보너스 점수)
    if data['volatility_z'] > 2.0:
        score += 5
    if data['order_imbalance'] > 0:
        score += 5

    # 6. ⚖️ Session Penalty
    if session == 1: # 점심시간 (Lunch Lull)
        score -= 20 # 점심시간엔 가짜 돌파가 많으므로 페널티 강화
        
    return score, reasons

async def run_f1_analysis_and_signal(ticker, df):
    global ai_cooldowns, ai_request_queue
    try:
        if len(df) < 60: return 

        # ==================================================================
        # 1. 퀀트 지표 계산 (가장 먼저 해야 함)
        # ==================================================================
        indicators = calculate_quant_indicators(df)
        if indicators is None: return
        
        price_now = indicators['close']
        atr_val = indicators.get('atr', price_now * 0.01) # ATR 없으면 1%로 대체

        # ==================================================================
        # 2. Feature Engineering (핵심 변수 정의)
        # ==================================================================
        # Pump & Pullback
        pump_strength = ((price_now - indicators['prev_close_5']) / indicators['prev_close_5']) * 100
        pullback = ((indicators['recent_high'] - price_now) / indicators['recent_high']) * 100
        
        # VWAP Distance
        vwap_dist = ((price_now - indicators['vwap']) / indicators['vwap']) * 100 if indicators['vwap'] != 0 else 0

        # 데이터 패킷 준비 (점수 계산용)
        score_data = {
            'rvol': indicators['rvol'],
            'rvol_slope': indicators.get('rvol_slope', 0),
            'rvol_consecutive': indicators.get('rvol_consecutive', False),
            'pump': pump_strength,
            'pump_accel': indicators.get('pump_accel', 0),
            'rsi': indicators['rsi'],
            'vwap_dist': vwap_dist,
            'vwap_slope': indicators.get('vwap_slope', 0),
            'squeeze_ratio': indicators.get('squeeze_ratio', 1.0), # 필수
            'volatility_z': indicators['volatility_z'],
            'order_imbalance': indicators['order_imbalance']
        }

        # ==================================================================
        # 3. Soft Gate Scoring & Tier 분류 (이제 계산 가능)
        # ==================================================================
        tech_score, score_reasons = calculate_soft_gate_score(score_data, indicators['session'])
        
        tier = "TRASH"
        if tech_score >= 85: tier = "ELITE"
        elif tech_score >= 60: tier = "VALID" # 60점으로 하향 조정
        
        # ELITE나 VALID 등급만 처리
        if tier in ["ELITE", "VALID"]:
            
            # 쿨다운 체크
            import time
            current_ts = time.time()
            if ticker in ai_cooldowns:
                if current_ts - ai_cooldowns[ticker] < 60: return 

            # ==================================================================
            # 4. Entry / TP / SL 공식 적용 (Tier 확인 후 계산)
            # ==================================================================
            
            # 1) Entry Price
            entry_price = price_now + (atr_val * 0.15)
            
            # 2) Take Profit (TP)
            is_super_setup = (indicators.get('squeeze_ratio', 1.0) < 0.6) and \
                             (indicators.get('rvol_consecutive', False)) and \
                             (indicators.get('pump_accel', 0) > 0.2)
            tp_multiplier = 1.8 if is_super_setup else 1.2
            tp_price = entry_price + (atr_val * tp_multiplier)
            
            # 3) Stop Loss (SL)
            sl_price = entry_price - (atr_val * 0.5)
            
            # 손익비 계산
            reward = tp_price - entry_price
            risk = entry_price - sl_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            # ==================================================================
            # 5. Entry Suitability Score (구조적 적합성 평가)
            # ==================================================================
            # (이제 vwap_dist, pullback 등이 정의되었으므로 에러 안 남)
            entry_suitability = 0
            
            # 1. ATR 적합성
            atr_pct = (atr_val / price_now) * 100
            if 0.5 <= atr_pct <= 2.0: entry_suitability += 40 
            elif atr_pct > 2.0: entry_suitability += 20 
            else: entry_suitability += 10 
            
            # 2. VWAP 구조 점수
            if 0 <= abs(vwap_dist) <= 1.5: entry_suitability += 30 
            elif abs(vwap_dist) < 3.0: entry_suitability += 15
            
            # 3. Pullback 건강도
            if 0 <= pullback <= 5.0: entry_suitability += 30 
            elif pullback > 5.0: entry_suitability += 10 

            # ==================================================================
            # 6. 데이터 전송 및 출력 (모든 변수가 준비됨)
            # ==================================================================
            reason_str = ", ".join(score_reasons)
            print(f"✨ [{tier}] {ticker} | Score: {tech_score} | Suitability: {entry_suitability}")

            # AI에게 보낼 데이터 패키징
            ai_data = {
                "technical_score": int(tech_score),
                "entry_suitability": int(entry_suitability),
                "tier": tier,
                
                # 1. VWAP 관련
                "vwap_dist": float(round(vwap_dist, 2)),
                "vwap_slope": float(round(indicators.get('vwap_slope', 0), 4)),
                
                # 2. Squeeze
                "squeeze_ratio": float(round(indicators['squeeze_ratio'], 2)),
                
                # 3. Pump & Accel
                "pump": float(round(pump_strength, 2)),
                "pump_accel": float(round(indicators.get('pump_accel', 0), 2)),
                "pullback": float(round(pullback, 2)),
                
                # 4. Volume & RVOL
                "rvol": float(round(indicators['rvol'], 2)),
                "rvol_slope": float(round(indicators.get('rvol_slope', 0), 2)),
                "rvol_consecutive": bool(indicators.get('rvol_consecutive', False)),
                
                # 5. 기타 지표
                "rsi": float(round(indicators['rsi'], 2)),
                "volatility_z": float(round(indicators['volatility_z'], 2)),
                "order_imbalance": float(round(indicators['order_imbalance'], 2)),
                "trend_align": int(indicators['trend_align']),
                "session": int(indicators['session']),
                
                # 6. 트레이딩 셋업 정보
                "setup_atr": float(round(atr_val, 4)),
                "target_entry": float(round(entry_price, 4)),
                "target_tp": float(round(tp_price, 4)),
                "target_sl": float(round(sl_price, 4)),
                "rr_ratio": float(rr_ratio)
            }
            
            task_payload = {
                'ticker': ticker,
                'price': price_now,
                'ai_data': ai_data,
                'strat': f"SoftGate {tier}", 
                'squeeze_ratio': ai_data['squeeze_ratio'], 
                'pump': ai_data['pump'],
                
                # Worker에게 전달할 가격 정보
                'entry_price': entry_price,
                'tp_price': tp_price,
                'sl_price': sl_price
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

    # 1. 데이터 분류 (Trade vs Aggregate)
    for msg in msg_data:
        ticker = msg.get('sym')
        if not ticker: continue
        
        # 실시간 체결가(Tick) 업데이트 -> 마지막 종가 보정용
        if msg.get('ev') == 'T':
            if ticker not in ticker_tick_history: ticker_tick_history[ticker] = []
            ticker_tick_history[ticker].append([msg.get('t'), msg.get('p'), msg.get('s')])
            # 메모리 관리: 2000개는 너무 많음 -> 500개로 축소
            if len(ticker_tick_history[ticker]) > 500: ticker_tick_history[ticker].pop(0)
            
        # 분봉 데이터(Aggregate) 수집
        elif msg.get('ev') == 'AM':
            minute_data.append(msg)

    # 2. 분봉 데이터 처리 및 분석 트리거
    for msg in minute_data:
        ticker = msg.get('sym')
        
        # DataFrame 초기화
        if ticker not in ticker_minute_history:
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v'])
        
        # 타임스탬프 변환
        ts = pd.to_datetime(msg['s'], unit='ms')
        
        # 데이터 업데이트 (loc 사용)
        ticker_minute_history[ticker].loc[ts] = [
            float(msg['o']), float(msg['h']), float(msg['l']), float(msg['c']), float(msg['v'])
        ]
        
        # 메모리 관리 (최근 120개만 유지 - 2시간 분량이면 충분)
        if len(ticker_minute_history[ticker]) > 120:
            ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-120:]
            
        df = ticker_minute_history[ticker].copy()
        
        # 데이터가 너무 적으면 계산 불가 (최소 20개로 완화)
        if len(df) < 20: continue

        try:
            # 3. 실시간 가격 보정 (Tick 데이터 활용)
            if ticker in ticker_tick_history and len(ticker_tick_history[ticker]) > 0:
                last_tick_price = float(ticker_tick_history[ticker][-1][1])
                # 현재 캔들의 종가를 최신 틱 가격으로 강제 업데이트 (리페인팅 허용)
                df.iloc[-1, df.columns.get_loc('c')] = last_tick_price
                
                # High/Low 갱신
                if last_tick_price > df.iloc[-1, df.columns.get_loc('h')]:
                    df.iloc[-1, df.columns.get_loc('h')] = last_tick_price
                if last_tick_price < df.iloc[-1, df.columns.get_loc('l')]:
                    df.iloc[-1, df.columns.get_loc('l')] = last_tick_price

            # =========================================================
            # 🔥 [핵심 수정] 복잡한 로직 다 버리고 분석 함수 호출로 통일
            # =========================================================
            await run_f1_analysis_and_signal(ticker, df)

        except Exception as e:
            print(f"⚠️ [Processing Error] {ticker}: {e}")
            import traceback
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