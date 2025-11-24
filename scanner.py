import asyncio
import websockets 
import requests
import os  # 1. os 임포트
import pandas as pd
import pandas_ta as ta
import json
from datetime import datetime, timedelta
import psycopg2  # 2. sqlite3 대신 psycopg2
from psycopg2 import pool # 👈 이거 추가
import time
import httpx 
import firebase_admin # ✅ 1. firebase-admin 임포트
from firebase_admin import credentials, messaging # ✅ 2. 관련 모듈 임포트
import sys
import pytz
import traceback
import numpy as np

# --- (v12.0) API 키 설정 (보안) ---
# 3. Render 환경 변수에서 API 키를 읽어옵니다.
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

# ✅ 3. Firebase Admin SDK 환경 변수
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')

# --- (v15.3) Vertex AI 설정 (us-central1 복귀) ---
GCP_PROJECT_ID = "gen-lang-client-0379169283" 
# 1. 리전을 'us-central1'로 유지
GCP_REGION = "us-central1" 

# --- ✅ 2. (NEW) Firebase VAPID 키 (FCM 발송용) ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY') # (이제 pywebpush용이라 사용 안 함)
VAPID_EMAIL = "mailto:cbvkqtm98@gmail.com" # (이제 pywebpush용이라 사용 안 함)

# --- (v16.2) 튜닝 되돌리기 (API 한도 문제 해결) ---
MAX_PRICE = 20
TOP_N = 100
MIN_DATA_REQ = 20

# --- (v16.2) 튜닝 되돌리기 ---
WAE_MACD = (2, 3, 4) 
WAE_SENSITIVITY = 150
WAE_BB = (5, 1.5) 
WAE_ATR = 5 
WAE_ATR_MULT = 1.5
WAE_CMF = 5 
WAE_RSI_RANGE = (40, 70) # <-- ✅ 75로 복귀
RSI_LENGTH = 5 

# --- (v16.2) 튜닝 되돌리기 ---
ICHIMOKU_SHORT = (2, 3, 5) 
CLOUD_PROXIMITY = 20.0 # <-- ✅ 20.0으로 복귀
CLOUD_THICKNESS = 0.5
OBV_LOOKBACK = 3 

# --- [F1 엔진] NumPy 고속 연산 함수 모음 ---
def calculate_f1_indicators(closes, highs, lows, volumes):
    """
    Pandas TA를 대체하는 초고속 NumPy 지표 계산 함수
    입력: np.array (closes, highs, lows, volumes)
    출력: 딕셔너리 (지표 값들)
    """
    # 1. 기본 함수 정의 (SMA, EMA, Rolling)
    def sma(arr, n):
        ret = np.cumsum(arr, dtype=float)
        ret[n:] = ret[n:] - ret[:-n]
        return ret[n - 1:] / n

    def ema(arr, n):
        alpha = 2 / (n + 1)
        # Numba 없이 Python Loop로 해도 1000개는 순식간임
        res = np.empty_like(arr)
        res[0] = arr[0]
        for i in range(1, len(arr)):
            res[i] = alpha * arr[i] + (1 - alpha) * res[i-1]
        return res

    def rolling_max(arr, n):
        # 간단한 슬라이딩 윈도우 최대값
        return np.array([arr[i-n+1:i+1].max() for i in range(n-1, len(arr))])

    def rolling_min(arr, n):
        return np.array([arr[i-n+1:i+1].min() for i in range(n-1, len(arr))])

    def rsi_func(arr, n=5):
        delta = np.diff(arr)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        
        # Wilder's Smoothing (Pandas TA 방식)
        avg_gain = np.zeros_like(arr); avg_loss = np.zeros_like(arr)
        avg_gain[n] = np.mean(gain[:n]); avg_loss[n] = np.mean(loss[:n])
        
        for i in range(n+1, len(arr)):
            avg_gain[i] = (avg_gain[i-1] * (n-1) + gain[i-1]) / n
            avg_loss[i] = (avg_loss[i-1] * (n-1) + loss[i-1]) / n
            
        rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss!=0)
        return 100 - (100 / (1 + rs))

    # --- 지표 계산 시작 ---
    
    # [WAE] MACD (2, 3, 4)
    ema_fast = ema(closes, 2)
    ema_slow = ema(closes, 3)
    macd = ema_fast - ema_slow
    # macd_signal = ema(macd, 4) # Signal은 WAE 계산식에 직접 안쓰임 (트렌드 델타만 씀)

    # [WAE] Bollinger Bands (5, 1.5)
    # BB 계산: SMA +/- (Std * 1.5)
    bb5_sma = np.zeros_like(closes)
    # 단순화를 위해 끝부분만 계산 (전체 계산 안하고 효율화 가능하지만 일단 전체)
    # 1000개 배열 루프는 Python에서도 빠름. 
    # 정확한 표준편차 계산을 위해 Pandas Rolling Std와 유사하게 구현
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
    # TR = max(h-l, abs(h-cp), abs(l-cp))
    prev_close = np.roll(closes, 1); prev_close[0] = closes[0]
    tr1 = highs - lows
    tr2 = np.abs(highs - prev_close)
    tr3 = np.abs(lows - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    
    # ATR은 보통 RMA(Running Moving Average) 사용
    atr = np.zeros_like(closes)
    atr[5] = np.mean(tr[:5])
    for i in range(6, len(closes)):
        atr[i] = (atr[i-1] * 4 + tr[i]) / 5

    # [Ichimoku] (2, 3, 5) - 매우 짧은 설정
    # 전환선(Tenkan): (9일 -> 2일) 고가+저가 / 2
    t_max = rolling_max(highs, 2)
    t_min = rolling_min(lows, 2)
    tenkan = (t_max + t_min) / 2
    
    # 기준선(Kijun): (26일 -> 3일)
    k_max = rolling_max(highs, 3)
    k_min = rolling_min(lows, 3)
    kijun = (k_max + k_min) / 2
    
    # 선행스팬 A/B (원래는 미래로 밀어야 하지만 현재 값 비교용으로 계산)
    # 5일 전의 (전환+기준)/2 -> 선행 A
    # 5일 전의 (52일 -> 5일) 고+저/2 -> 선행 B
    # 사용자 코드 로직: cloud_a_current = df[SENKOU_A_COL].iloc[-K] (K=3)
    # 즉 3봉 전의 값을 현재 구름대로 씀.
    
    senkou_a = (tenkan + kijun) / 2
    
    s_max = rolling_max(highs, 5)
    s_min = rolling_min(lows, 5)
    senkou_b = (s_max + s_min) / 2
    
    # [RSI] (5)
    rsi = rsi_func(closes, 5)

    # [CMF] (5)
    # MFM = ((C-L) - (H-C)) / (H-L)
    # MFV = MFM * V
    # CMF = Sum(MFV, 5) / Sum(V, 5)
    mfm = ((closes - lows) - (highs - closes)) / (highs - lows)
    # 0으로 나누기 방지
    mfm = np.nan_to_num(mfm) 
    mfv = mfm * volumes
    
    cmf = np.zeros_like(closes)
    for i in range(5, len(closes)):
        sum_mfv = np.sum(mfv[i-4:i+1])
        sum_vol = np.sum(volumes[i-4:i+1])
        if sum_vol != 0:
            cmf[i] = sum_mfv / sum_vol

    # [OBV]
    # OBV는 누적합
    obv = np.zeros_like(volumes)
    obv[0] = volumes[0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv[i] = obv[i-1] + volumes[i]
        elif closes[i] < closes[i-1]:
            obv[i] = obv[i-1] - volumes[i]
        else:
            obv[i] = obv[i-1]

    # 필요한 마지막 값들만 리턴 (속도 최적화)
    idx = -1
    
    return {
        "close": closes[idx],
        "volume": volumes[idx],
        "macd_delta": (macd[idx] - macd[idx-1]) * 150, # WAE Sensitivity
        "bb_gap_wae": bb5_up[idx] - bb5_low[idx],      # WAE 폭발력
        "dead_zone": atr[idx] * 1.5,                   # WAE ATR Mult
        "rsi": rsi[idx],
        "cmf": cmf[idx],
        "obv_now": obv[idx],
        "obv_prev": obv[idx-1],
        
        # Ichimoku (K=3 이므로 -3 인덱스 사용)
        "cloud_top": max(senkou_a[-3], senkou_b[-3]),
        "senkou_a": senkou_a[-3],
        "senkou_b": senkou_b[-3],
        
        # Squeeze (20, 2.0)
        "bb_up_std": bb20_up[idx],
        "bb_low_std": bb20_low[idx],
        "bb_width_now": (bb20_up[idx] - bb20_low[idx]) / closes[idx],
        
        # Squeeze Avg (과거 20개 평균 폭)
        "bb_width_avg": np.mean((bb20_up[-20:] - bb20_low[-20:]) / closes[-20:])
    }
# --- (v13.0) DB 경로 설정 (PostgreSQL 연동) ---
# 4. Render 환경 변수에서 PostgreSQL DB 연결 주소를 읽어옵니다.
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    # DATABASE_URL이 설정되지 않았는지 확인
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# ✅ 4. Firebase Admin SDK 초기화 함수 (새로 추가)
def init_firebase():
    """Firebase Admin SDK를 초기화합니다."""
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR:
            print("❌ [FCM] FIREBASE_ADMIN_SDK_JSON이 설정되지 않아 FCM을 건너뜁니다.")
            return False
        
        # 환경 변수에서 JSON 문자열을 읽어 딕셔너리로 변환
        sdk_json_dict = json.loads(FIREBASE_ADMIN_SDK_JSON_STR)
        
        cred = credentials.Certificate(sdk_json_dict)
        
        # 이미 초기화되었는지 확인 (Render가 재시작할 때 오류 방지)
        if not firebase_admin._apps:
            # ✅ [수정] .json 파일에 projectId가 이미 있으므로, 딕셔너리 덮어쓰기 제거
            firebase_admin.initialize_app(cred)
            
        print(f"✅ [FCM] Firebase Admin SDK 초기화 성공 (Project ID: {sdk_json_dict.get('project_id')})")
        return True
    except Exception as e:
        print(f"❌ [FCM] Firebase Admin SDK 초기화 실패: {e}")
        return False

ticker_minute_history = {} 
ticker_tick_history = {} 
ai_cooldowns = {}
# ✅ [신규] AI 분석 요청 대기열 (Queue)
ai_request_queue = asyncio.Queue()
# --- (v16.1) Gemini API 호출 함수 (AI 응답 오류 수정) ---
async def get_gemini_probability(ticker, conditions_data):
    if not GEMINI_API_KEY:
        print(f"-> [Gemini AI] {ticker}: GEMINI_API_KEY가 설정되지 않아 AI 분석을 건너뜁니다.")
        return 50 
    if not GCP_PROJECT_ID or "YOUR_PROJECT_ID" in GCP_PROJECT_ID:
        print(f"-> [Gemini AI] {ticker}: GCP_PROJECT_ID가 설정되지 않아 AI 분석을 건너뜁니다.")
        return 50

    system_prompt = """
You are an elite **"Penny Stock Sniper AI"**.
You represent a strict scalper who only pulls the trigger on **PERFECT setups**.
**Your Rule:** It is better to miss a trade than to lose money.
**Score Inflation is Forbidden.** 90+ scores must be RARE and PERFECT.

**INPUT DATA Analysis:**
1. `pullback_from_high`: **The most critical filter.**
   - **> 12%:** BROKEN TREND. (Immediate Fail).
   - **< 5%:** ELITE STRENGTH. (High Tight Flag).
2. `pump_strength_5m`:
   - **> 3.0%:** Chasing. Too risky for a 90+ score.
3. `daily_change`: Indicates momentum.
4. `squeeze_ratio`: < 1.0 indicates stored energy.

---
### STRICT SCORING LOGIC

**🛑 KILL SWITCH (The "FOXX" Filter)**
* **IF** `pullback_from_high` > 12.0%:
   → **MAX SCORE = 40.** (Trend is broken. Do not catch a falling knife).
   → *Reasoning: "Deep pullback (-x%) detected. Chart is broken."*

**🏆 Pattern A: "The King's Setup" (Rare & Perfect)**
* **Conditions (ALL must be met):**
   1. `pullback_from_high` < 5.0% (Holding gains like a rock)
   2. `squeeze_ratio` < 1.0 (Energy is tightly coiled)
   3. `pump_strength_5m` < 3.0% (Not currently spiking/chasing)
   4. `is_volume_dry` is True (Sellers are gone)
* **Verdict:** **SCORE 90~99** (Sniper Entry).

**🥈 Pattern B: "Standard Momentum" (Good but Risky)**
* **Conditions:**
   - `pullback_from_high` is 5% ~ 12% (Normal volatility)
   - `engine_1_pass` (WAE) is True OR `squeeze_ratio` < 1.1
* **Verdict:** **SCORE 75~85** (Good trade, but not perfect).

**🗑️ Pattern C: "The Chase" or "The Dump"**
* **Conditions:**
   - `pump_strength_5m` > 4.0% (You are chasing)
   - OR `pullback_from_high` > 12% (Dump)
* **Verdict:** **SCORE 40~60** (Pass).

---
**Generate JSON Output:**
Respond ONLY with this JSON structure.
{
  "probability_score": <int>,
  "reasoning": "<[Grade] King/Standard/Trash? [Risk] Pullback: -x.x%. [Verdict] Why this specific score?>"
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
    
    # API URL은 'us-central1' 리전 사용
    api_url = (
        f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT_ID}"
        f"/locations/{GCP_REGION}/publishers/google/models/gemini-2.5-flash-lite:generateContent"
    )

    # "system" 프롬프트와 "user" 프롬프트를 하나로 합쳐서 "user" 역할로만 보냅니다.
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
            
            # --- ✅ (v16.1) AI가 Markdown으로 감싸서 응답할 경우 JSON 추출 ---
            if '```json' in response_text:
                print(f"-> [Gemini AI] {ticker}: Markdown 감지됨, JSON 추출 시도...")
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                if start != -1 and end != -1:
                    response_text = response_text[start:end]
            # --- 여기까지 추가 ---
            
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

# --- 전역 변수: 커넥션 풀 저장소 ---
db_pool = None 

# --- (수정) DB 커넥션 가져오기 (풀링 방식) ---
def get_db_connection():
    global db_pool
    # 풀이 없으면 생성 시도
    if db_pool is None:
        init_db()
    # 풀에서 커넥션 하나를 빌려옴
    return db_pool.getconn()

# --- (수정) DB 초기화 및 풀 생성 함수 ---
def init_db():
    """
    PostgreSQL 커넥션 풀을 생성하고 테이블을 초기화합니다.
    (Turbo Mode: 매번 연결하지 않고 재사용)
    """
    global db_pool
    
    # 1. DATABASE_URL 확인
    if not DATABASE_URL:
        print("❌ [DB] DATABASE_URL이 설정되지 않아 초기화를 건너뜁니다.")
        return

    try:
        # 2. 커넥션 풀 생성 (최소 1개 ~ 최대 20개 유지)
        if db_pool is None:
            db_pool = psycopg2.pool.SimpleConnectionPool(
                1, 20, dsn=DATABASE_URL
            )
            print("✅ [DB] 커넥션 풀(Turbo) 가동 시작.")

        # 3. 테이블 생성을 위해 커넥션 하나 빌리기
        conn = db_pool.getconn()
        try:
            cursor = conn.cursor()
            
            # --- 테이블 생성 쿼리 (기존 로직 유지) ---
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
            
            # FCM 토큰 테이블
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS fcm_tokens (
                id SERIAL PRIMARY KEY, 
                token TEXT NOT NULL UNIQUE, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                min_score INTEGER DEFAULT 0
            )
            """)
            
            # --- 컬럼 추가 (호환성 유지) ---
            try:
                cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            except psycopg2.Error:
                conn.rollback() # 이미 존재하면 롤백하고 계속 진행
            
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
            # 🔥 [핵심] 다 쓴 커넥션은 반드시 풀에 반납(putconn)해야 함!
            if conn: db_pool.putconn(conn)

    except Exception as e:
        print(f"❌ [DB] 커넥션 풀 생성 실패: {e}")

# --- (v16.1) 튜닝: 알림/로그 함수 ---
def send_discord_alert(ticker, price, type="signal", probability_score=50):
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
        requests.post(DISCORD_WEBHOOK_URL, json=data)
        print(f"🔔 [알림] {ticker} @ ${price:.4f} (디스코드 전송 완료)")
    except Exception as e: 
        print(f"[알림 오류] {ticker} 디스코드 전송 실패: {e}")

# --- (수정) 알림 발송 함수 (DB 연결 최적화: 빌리고 반납하기) ---
def send_fcm_notification(ticker, price, probability_score):
    """DB의 min_score를 확인하여 조건에 맞는 사용자에게만 알림을 발송합니다."""
    
    if not firebase_admin._apps:
        # print("🔔 [FCM] Firebase Admin SDK 미초기화. 패스.")
        return

    conn = None
    try:
        conn = get_db_connection() # 1. 커넥션 빌리기
        cursor = conn.cursor()
        
        # 구독자 및 설정값 조회
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        
        if not subscribers:
            # 구독자가 없으면 바로 반납하고 종료
            db_pool.putconn(conn)
            return

        # 데이터 페이로드 구성
        data_payload = {
            'title': "Danso AI 신호", 
            'ticker': ticker,
            'price': f"{price:.4f}",
            'probability': str(probability_score)
        }
        
        success_count = 0
        failure_count = 0
        skipped_count = 0
        failed_tokens = []

        # 발송 루프
        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            if not token: continue

            # 점수 필터링
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
                # 토큰 만료/오류 시 삭제 목록에 추가
                if "Requested entity was not found" in str(e) or "registration-token-not-registered" in str(e):
                    failed_tokens.append(token)
        
        # print(f"🔔 [FCM] 성공:{success_count}, 실패:{failure_count}, 패스:{skipped_count}")
        
        # 만료된 토큰 DB에서 삭제
        if failed_tokens:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            cursor.close()
            print(f"🧹 [FCM] 만료된 토큰 {len(failed_tokens)}개 삭제 완료.")

    except Exception as e:
        print(f"❌ [FCM] 발송 중 오류: {e}")
        if conn: conn.rollback() # 오류 시 롤백
    finally:
        # 🔥 [핵심] 다 썼으면 반드시 반납! (close 아님)
        if conn: db_pool.putconn(conn)

# --- (수정) DB 로그 함수 (PostgreSQL 용) ---
def log_signal(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection() # 1. 빌리기
        cursor = conn.cursor()
        # INSERT 실행
        cursor.execute("INSERT INTO signals (ticker, price, time) VALUES (%s, %s, %s)", 
                       (ticker, price, datetime.now()))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [DB] 'signals' 저장 실패: {e}")
        if conn: conn.rollback()
    finally:
        # 🔥 [핵심] 반납하기
        if conn: db_pool.putconn(conn)

def log_recommendation(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection() # 1. 빌리기
        cursor = conn.cursor()
        
        # 중복 방지 INSERT (ON CONFLICT DO NOTHING)
        cursor.execute("""
        INSERT INTO recommendations (ticker, price, time, probability_score) 
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker) DO NOTHING
        """, 
                       (ticker, price, datetime.now(), probability_score))
        conn.commit()
        
        # 이미 존재하면 rowcount는 0, 새로 들어가면 1
        is_new_rec = cursor.rowcount > 0
        cursor.close()
        return is_new_rec
        
    except Exception as e:
        print(f"❌ [DB] 'recommendations' 저장 실패: {e}")
        if conn: conn.rollback()
        return False
    finally:
        # 🔥 [핵심] 반납하기
        if conn: db_pool.putconn(conn)

# --- 1단계 로직: "오늘의 관심 잡주" (v7.2) ---
def find_active_tickers():
    if not POLYGON_API_KEY:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류: POLYGON_API_KEY가 설정되지 않았습니다.")
        return set()
        
    print(f"\n[사냥꾼] 1단계: 'Top Gainers' (조건: ${MAX_PRICE} 미만) 스캔 중...")
    
    # ✅ (수정) URL을 올바른 f-string 형식으로 변경
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={POLYGON_API_KEY}"

    tickers_to_watch = set()
    try:
        response = requests.get(url)
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
        return tickers_to_watch # 예외 발생 시 반환
        
    # ✅ (추가) 성공 시에도 항상 set을 반환
    return tickers_to_watch
# --- ✅ [추가 2] 시간 및 거래량 분석 함수 추가 ---

def get_current_session():
    """
    현재 시간에 따라 세션 타입을 반환합니다. (US/Eastern 기준)
    - premarket: 04:00 ~ 09:30
    - regular: 09:30 ~ 16:00
    - aftermarket: 16:00 ~ 20:00
    """
    try:
        ny_tz = pytz.timezone('US/Eastern')
        now = datetime.now(ny_tz).time()

        # 시간대 설정
        time_pre_start = datetime.strptime("04:00", "%H:%M").time()
        time_regular_start = datetime.strptime("09:30", "%H:%M").time()
        time_after_start = datetime.strptime("16:00", "%H:%M").time()
        time_market_close = datetime.strptime("20:00", "%H:%M").time()

        if time_pre_start <= now < time_regular_start:
            return "premarket"  # [모드 A]
        elif time_regular_start <= now < time_after_start:
            return "regular"    # [모드 B] (엄격 모드)
        elif time_after_start <= now < time_market_close:
            return "aftermarket" # [모드 A]
        else:
            return "closed"      # 장 마감
    except Exception as e:
        print(f"⚠️ [Time Check Error] {e}")
        return "premarket" # 에러 시 기본값

def calculate_volume_ratio(df):
    """
    현재 캔들 거래량 / 직전 5개 캔들 평균 거래량
    """
    try:
        if len(df) < 6: return 1.0
        current_vol = df['volume'].iloc[-1]
        avg_vol_5 = df['volume'].iloc[-6:-1].mean() # 직전 5개 평균
        
        if avg_vol_5 == 0: return 0.0
        
        ratio = current_vol / avg_vol_5
        return round(ratio, 2)
    except:
        return 1.0

    # --- (신규) 과거 데이터 스냅샷 가져오기 ---
def fetch_initial_data(ticker):
    """
    새로운 종목 구독 시, 과거 200분(캔들) 데이터를 즉시 로딩하여
    52분 대기 시간(Cold Start)을 없애고 바로 분석 가능하게 만듦.
    """
    if not POLYGON_API_KEY: return
    
   # [수정] 안전하게 최근 7일(일주일) 범위에서 최신 200개를 가져오도록 설정
    # 주말/공휴일이 껴있어도 데이터가 끊기지 않게 하기 위함
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    # ⚠️ 중요: 반드시 sort=desc여야 '최신 데이터'를 가져옵니다! asc로 하면 옛날 데이터 가져옴.
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
            # Polygon Aggs는 최신순(desc)으로 요청했어도 리스트는 섞일 수 있으니 다시 정렬
            # 보통 오름차순(옛날 -> 최신)으로 DataFrame을 만들어야 함
            results.sort(key=lambda x: x['t']) 
            
            # [수정된 코드]
            df = pd.DataFrame(results)
            
            # 🔥 핵심: 여기서 필요한 6개 컬럼만 딱 골라냅니다! (나머지 버림)
            df = df[['t', 'o', 'h', 'l', 'c', 'v']]
            
            df['t'] = pd.to_datetime(df['t'], unit='ms')
            df.set_index('t', inplace=True)
            
            # 혹시 모르니 순서 확실하게 맞추고 실수형(float)으로 변환
            df = df[['o', 'h', 'l', 'c', 'v']].astype(float)
            
            # 전역 변수에 주입
            ticker_minute_history[ticker] = df
            print(f"✅ [초기화] {ticker} 과거 캔들 {len(df)}개 로딩 완료. 즉시 분석 가능.")
        else:
            # 🔥 여기가 핵심: 왜 실패했는지 로그 출력
            print(f"⚠️ [데이터 없음] {ticker}: Status={data.get('status')}, Count={data.get('count')}, Msg={data.get('message')}")
    except Exception as e:
        print(f"⚠️ [초기화 실패] {ticker}: {e}")
# --- [신규] AI 워커 (웨이터): 큐에서 꺼내서 처리 ---
async def ai_worker():
    print("👨‍🍳 [Worker] AI 처리 전담반 가동 시작!")
    while True:
        # 1. 큐에서 일감 꺼내기 (일 없으면 여기서 대기)
        task = await ai_request_queue.get()
        
        try:
            # 데이터 언패킹
            ticker = task['ticker']
            price_now = task['price']
            ai_data = task['ai_data']
            strat = task['strat']
            squeeze_val = task['squeeze']
            pump_val = task['pump']

            # 2. AI 분석 요청 (오래 걸리는 작업)
            # 여기서 시간이 걸려도 메인 루프(handle_msg)는 멈추지 않음!
            score = await get_gemini_probability(ticker, ai_data)

            # 3. 결과 처리 (로그 & DB & 알림)
            print(f"🏎️ [F1 결과] {ticker} @ ${price_now:.4f} | AI: {score}% | Sqz: {squeeze_val:.2f} | Pump: {pump_val:.1f}%")
            
            is_new = log_recommendation(ticker, float(price_now), score)
            if is_new:
                send_discord_alert(ticker, float(price_now), "recommendation", score)
                send_fcm_notification(ticker, float(price_now), score)
                
        except Exception as e:
            print(f"❌ [Worker 오류] {e}")
        finally:
            # 작업 완료 신호 (큐에게 알려줌)
            ai_request_queue.task_done()
# --- [F1 버전] 고속 데이터 처리 엔진 ---
async def handle_msg(msg_data):
    global ticker_minute_history, ticker_tick_history
    
    if isinstance(msg_data, dict): msg_data = [msg_data]
    minute_data = []

    # 1. 데이터 수집 (기존과 동일)
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
        
        # 데이터 업데이트
        if ticker not in ticker_minute_history:
            # DataFrame 대신 그냥 리스트나 딕셔너리 쓸 수도 있지만, 
            # 일단 1단계에서는 기존 구조 호환을 위해 DataFrame 유지 (연산만 NumPy로)
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v', 't']).set_index('t')
            
        ts = pd.to_datetime(msg['s'], unit='ms')
        ticker_minute_history[ticker].loc[ts] = [msg['o'], msg['h'], msg['l'], msg['c'], msg['v']]
        
        # 버퍼 관리 (1000개)
        if len(ticker_minute_history[ticker]) > 1000:
            ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-1000:]
            
        df = ticker_minute_history[ticker]
        if len(df) < 52: continue # 최소 데이터 체크

        # ---------------------------------------------------
        # 🏎️ [F1 엔진 가동] Pandas TA 제거 -> NumPy 연산
        # ---------------------------------------------------
        try:
            # DataFrame을 NumPy 배열로 변환 (연산 속도 UP)
            closes = df['c'].values.astype(float)
            highs = df['h'].values.astype(float)
            lows = df['l'].values.astype(float)
            volumes = df['v'].values.astype(float)
            opens = df['o'].values.astype(float)

            # 🔥 F1 계산 함수 호출 (여기서 모든 지표가 0.001초 만에 계산됨)
            indicators = calculate_f1_indicators(closes, highs, lows, volumes)
            
            # --- 결과 추출 ---
            price_now = indicators['close']
            
            # 1. [Pump Strength] 5분 급등률
            if len(closes) >= 6:
                price_5m = closes[-6]
                pump_strength_5m = ((price_now - price_5m) / price_5m) * 100
            else: pump_strength_5m = 0.0

            # 2. [Pullback] 고점 대비 눌림폭
            day_high = np.max(highs)
            pullback = ((day_high - price_now) / day_high) * 100 if day_high > 0 else 0.0

            # 3. [Daily Change]
            day_open = opens[0]
            daily_change = ((price_now - day_open) / day_open) * 100 if day_open > 0 else 0.0

            # 4. [Squeeze Ratio]
            squeeze_ratio = indicators['bb_width_now'] / indicators['bb_width_avg'] if indicators['bb_width_avg'] > 0 else 1.0

            # 5. [Vol Dry]
            vol_avg_5 = np.mean(volumes[-6:-1]) if len(volumes) > 6 else 1
            is_volume_dry = indicators['volume'] < (vol_avg_5 * 0.8)

            # --- 트리거 조건 ---
            # WAE 폭발: (MACD Delta > BB Gap) AND (MACD Delta > DeadZone)
            cond_wae = (indicators['macd_delta'] > indicators['bb_gap_wae']) and \
                       (indicators['macd_delta'] > indicators['dead_zone'])
            
            # RSI, CMF
            rsi_val = indicators['rsi']
            cmf_val = indicators['cmf']
            cond_rsi = 40 < rsi_val < 75
            cond_vol = (cmf_val > 0) and (indicators['obv_now'] > indicators['obv_prev'])

            # Ichimoku Cloud
            cloud_top = indicators['cloud_top']
            is_above_cloud = price_now > cloud_top
            
            # Cloud Shape (두께, 거리)
            cloud_thick = abs(indicators['senkou_a'] - indicators['senkou_b']) / price_now * 100
            dist_bull = (price_now - cloud_top) / price_now * 100
            cond_cloud_shape = (cloud_thick >= 0.5) and (0 <= dist_bull <= 20.0)

            # --- 최종 판단 ---
            engine_1 = cond_wae and cond_rsi
            engine_2 = cond_cloud_shape and cond_vol and cond_rsi
            cond_pre = (squeeze_ratio < 1.1) and is_volume_dry and is_above_cloud

            # -------------------------------------------------------
            # 🚀 AI 호출 (쿨타임 + 비용 절감 로직 적용)
            # -------------------------------------------------------
            if (engine_1 or engine_2 or cond_pre) and cond_rsi:
                
                # 1. [쿨타임 체크] 60초 내 재호출 금지
                import time
                current_ts = time.time()
                
                if ticker in ai_cooldowns:
                    last_call = ai_cooldowns[ticker]
                    if current_ts - last_call < 60: # 60초 쿨타임
                        continue # 이번 턴은 넘김

                # 2. 데이터 준비
                session = get_current_session()
                if session == "closed": pass
                
                # (F1 엔진에서 이미 계산된 거래량 평균 사용)
                vol_ratio = indicators['volume'] / vol_avg_5 if vol_avg_5 > 0 else 1.0

                if engine_1: strat = "Explosion (WAE)"
                elif cond_pre: strat = "Pre-Breakout (Squeeze)"
                else: strat = "Standard Setup"

                # 3. AI 데이터 패키징 (ai_data로 통일)
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
                
              # 4. [수정] AI 분석 요청 (직접 호출 X -> 큐에 넣기 O)
                # -------------------------------------------------------
                # 웨이터(Worker)에게 넘겨줄 데이터 포장
                task_payload = {
                    'ticker': ticker,
                    'price': price_now,
                    'ai_data': ai_data,
                    'strat': strat,
                    'squeeze': squeeze_ratio,
                    'pump': pump_strength_5m
                }
                
                # 5. 쿨타임 갱신 (큐에 넣는 순간 이미 처리된 걸로 간주)
                # (이걸 여기서 해야 중복 등록을 막습니다!)
                ai_cooldowns[ticker] = current_ts
                
                # 6. 대기열에 집어넣기 (기다리지 않음! 0.00001초 소요)
                ai_request_queue.put_nowait(task_payload)
                
                # print(f"📨 [Queue] {ticker} 분석 요청 등록 완료") 

        except Exception as e:
            import traceback
            # print(f"F1 엔진 오류 {ticker}: {e}")
            pass  

# --- (v7.2) 수신 엔진 ---
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

# --- (수정) 주기적 스캔 (사냥꾼) - DB 풀링 적용 ---
async def periodic_scanner(websocket):
    current_subscriptions = set() 
    
    while True:
        try:
            # ---------------------------------------------------------
            # 1. DB 청소 (커넥션 빌리고 -> 반납)
            # ---------------------------------------------------------
            print(f"\n[사냥꾼] (V2.1) 3분 주기 시작. DB 청소 중...")
            conn = None
            try:
                conn = get_db_connection() # 1. 풀에서 빌리기
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
                # 🔥 [핵심] 반드시 반납해야 함! (안 하면 DB 멈춤)
                if conn: db_pool.putconn(conn)
            
            # ---------------------------------------------------------
            # 2. 새로운 타겟 찾기 및 구독 관리
            # ---------------------------------------------------------
            new_tickers = find_active_tickers() 
            tickers_to_add = new_tickers - current_subscriptions
            tickers_to_remove = current_subscriptions - new_tickers
            
            # 신규 구독 및 초기 데이터 로딩
            if tickers_to_add:
                print(f"[사냥꾼] 신규 {len(tickers_to_add)}개 구독 및 로딩...")
                for ticker in tickers_to_add:
                    params_str = f"AM.{ticker},T.{ticker}"
                    sub_payload = json.dumps({"action": "subscribe", "params": params_str})
                    await websocket.send(sub_payload)
                    
                    # 🔥 과거 데이터 즉시 로딩 (Cold Start 해결)
                    fetch_initial_data(ticker) 
                    await asyncio.sleep(0.1)
                print("[사냥꾼] 신규 구독 완료.")
                
            # 구독 해지 및 메모리 정리
            if tickers_to_remove:
                for ticker in tickers_to_remove:
                    params_str = f"AM.{ticker},T.{ticker}"
                    unsub_payload = json.dumps({"action": "unsubscribe", "params": params_str})
                    await websocket.send(unsub_payload)
                    
                    # (선택사항) 메모리 관리: 쿨타임 정보 삭제
                    if ticker in ai_cooldowns: 
                        del ai_cooldowns[ticker]
                        
                    await asyncio.sleep(0.1)
                print("[사냥꾼] 구독 해지 완료.")
            
            current_subscriptions = new_tickers
            
            # ---------------------------------------------------------
            # 3. 상태 저장 (커넥션 빌리고 -> 반납)
            # ---------------------------------------------------------
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
                conn = get_db_connection() # 1. 풀에서 빌리기
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
                # 🔥 [핵심] 반드시 반납!
                if conn: db_pool.putconn(conn)
            
        except Exception as e:
            print(f"-> ❌ [사냥꾼 루프 오류] {e}")
            
        # 3분 대기
        print(f"\n[사냥꾼] 3분(180초) 후 다음 스캔을 시작합니다...")
        await asyncio.sleep(180)

# --- (v8.1) "수동 Keepalive" 로봇 ---
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

# --- 메인 실행 함수 (v9.0 - 자동 재연결) ---
async def main():
    if not POLYGON_API_KEY:
        print("❌ [메인] POLYGON_API_KEY가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    if not DATABASE_URL:
        print("❌ [메인] DATABASE_URL이 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    # Vertex AI용 키 확인
    if not GEMINI_API_KEY:
        print("❌ [메인] GEMINI_API_KEY가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    if not GCP_PROJECT_ID or "YOUR_PROJECT_ID" in GCP_PROJECT_ID:
        print("❌ [메인] GCP_PROJECT_ID가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
        return
    
    # ✅ (수정) Firebase Admin SDK 키 확인
    if not FIREBASE_ADMIN_SDK_JSON_STR:
        print("⚠️ [메인] FIREBASE_ADMIN_SDK_JSON이 설정되지 않았습니다. FCM 푸시 알림이 비활성화됩니다.")


    # ✅ (튜닝) 버전 정보 수정
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

                # 12. API 키가 None이 아닌지 확인 (환경 변수 로드 실패 대비)
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
                    
                    # 🔥 [추가] AI 워커(웨이터) 태스크 생성
                    # (이게 있어야 큐에 쌓인 데이터를 처리합니다!)
                    worker_task = asyncio.create_task(ai_worker())
                    
                    # gather에 worker_task도 포함시켜서 같이 실행
                    await asyncio.gather(
                        watcher_task, 
                        scanner_task, 
                        keepalive_task, 
                        worker_task  # 👈 여기 추가됨!
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

# ✅ 5. __name__ == "__main__" 블록 수정
if __name__ == "__main__":
    init_db() 
    init_firebase() # ✅ Firebase 초기화 호출 추가
    
    # ✅ [수정] 'test' 인자가 있는지 확인
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        print("--- [TEST MODE] ---")
        print("DB와 Firebase 초기화 완료. 3초 후 테스트 알림을 발송합니다...")
        time.sleep(3) # (로그 볼 시간)
        
        # 테스트 알림 강제 발송
        send_fcm_notification(
            ticker="TEST", 
            price=123.45, 
            probability_score=99
        )
        
        print("--- [TEST MODE] 테스트 완료. 스크립트를 종료합니다. ---")
    
    else:
        # 'test' 인자가 없으면, (기존) 스캐너를 실행합니다.
        try: 
            print("--- [LIVE MODE] 스캐너를 시작합니다... ---")
            asyncio.run(main()) # ✅ asyncio 오타 수정
        except KeyboardInterrupt: 
            print("\n[메인] 사용자에 의해 프로그램이 종료되었습니다.")