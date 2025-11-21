import asyncio
import websockets 
import requests
import os  # 1. os 임포트
import pandas as pd
import pandas_ta as ta
import json
from datetime import datetime, timedelta
import psycopg2  # 2. sqlite3 대신 psycopg2
import time
import httpx 
import firebase_admin # ✅ 1. firebase-admin 임포트
from firebase_admin import credentials, messaging # ✅ 2. 관련 모듈 임포트
import sys
import pytz
import traceback
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

# --- (v13.0) DB 초기화 함수 (PostgreSQL 용) ---
def init_db():
    """PostgreSQL DB와 테이블 4개를 생성합니다."""
    conn = None
    try:
        # 5. DATABASE_URL이 설정되지 않았는지 확인
        if not DATABASE_URL:
            print("❌ [DB] DATABASE_URL이 설정되지 않아 초기화를 건너뜁니다.")
            return
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 6. PostgreSQL에 맞는 테이블 생성 (SERIAL = AUTOINCREMENT, TIMESTAMP)
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
        
        # --- ✅ 3. FCM 토큰 테이블 추가 (scanner.py에도 추가) ---
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id SERIAL PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # --- 여기까지 추가 ---
        
        conn.commit()
        
        try:
            # 7. PostgreSQL용 ALTER TABLE (에러 핸들링으로 처리)
            cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            conn.commit()
            print("-> [DB] 'recommendations' 테이블에 'probability_score' 컬럼 추가 시도 완료.")
        except psycopg2.Error as e:
            conn.rollback() # ✅ (v16.2) 롤백 추가
            if e.pgcode == '42701': # 'Duplicate Column' 에러 코드
                pass # 컬럼이 이미 존재함, 정상
            else:
                # ✅ (v16.2) 502 오류 방지를 위해 raise -> print로 변경
                print(f"❌ [DB] ALTER TABLE 중 예외 발생 (무시함): {e}")
            
        cursor.close()
        conn.close()
        print(f"✅ [DB] PostgreSQL 테이블 초기화 성공.")
    except Exception as e:
        if conn: 
            conn.rollback() # ✅ (v16.2) 롤백 추가
            conn.close()
        # ✅ (v16.2) 502 오류 방지를 위해 raise -> print로 변경
        print(f"❌ [DB] PostgreSQL 초기화 실패 (무시함): {e}")

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

# --- (v16.10 추천) 튜닝: FCM 푸시 알림 발송 함수 (구조화된 data 페이로드 + 점수 필터링) ---
def send_fcm_notification(ticker, price, probability_score):
    """DB의 min_score를 확인하여 조건에 맞는 사용자에게만 알림을 발송합니다."""
    
    if not firebase_admin._apps:
        print("🔔 [FCM] Firebase Admin SDK가 초기화되지 않아 알림을 건너뜁니다.")
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # ✅ [수정 1] 토큰과 함께 'min_score' 설정값도 가져옵니다.
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall() 
        
        cursor.close()
        conn.close()

        if not subscribers:
            print("🔔 [FCM] DB에 등록된 알림 구독자가 없습니다.")
            return

        print(f"🔔 [FCM] 총 {len(subscribers)}명의 구독자 확인. 필터링 및 발송 시작...")
        
        # 1. data 페이로드 구성 (동일)
        data_payload = {
            'title': "Danso AI 신호", 
            'ticker': ticker,
            'price': f"{price:.4f}",
            'probability': str(probability_score)
        }
        
        success_count = 0
        failure_count = 0
        skipped_count = 0 # 필터링된 횟수 카운트
        failed_tokens = []

        # ✅ [수정 2] 토큰과 최소 점수를 하나씩 꺼내서 확인
        for row in subscribers:
            token = row[0]
            # DB 값이 NULL이면 0점으로 처리 (모두 받음)
            user_min_score = row[1] if row[1] is not None else 0 
            
            if not token: continue

            # ✅ [핵심 로직] 신호 점수가 사용자의 설정 점수보다 낮으면 건너뜀
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
                # print(f"❌ [FCM] 토큰 전송 실패: {token[:10]}... (이유: {e})") # 로그 너무 많으면 주석 처리
                failure_count += 1
                if "Requested entity was not found" in str(e) or "registration-token-not-registered" in str(e):
                    failed_tokens.append(token)
        
        print(f"✅ [FCM] 발송 결과: 성공 {success_count}명, 실패 {failure_count}명, (점수 미달 패스: {skipped_count}명)")
        
        # 만료된 토큰 삭제 로직 (동일)
        if failed_tokens:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
                conn.commit()
                cursor.close()
                conn.close()
                print(f"🧹 [FCM] 만료된 토큰 {len(failed_tokens)}개를 DB에서 삭제했습니다.")
            except Exception as e:
                print(f"❌ [FCM] 만료된 토큰 DB 삭제 실패: {e}")

    except Exception as e:
        if conn: conn.close()
        print(f"❌ [FCM] 푸시 알림 발송 중 치명적 오류: {e}")
          
# --- (v13.0) DB 로그 함수 (PostgreSQL 용) ---
def log_signal(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 8. PostgreSQL용 INSERT (%s 사용, ? 대신)
        cursor.execute("INSERT INTO signals (ticker, price, time) VALUES (%s, %s, %s)", 
                       (ticker, price, datetime.now()))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [DB] 'signals' 저장 실패: {e}")

def log_recommendation(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 9. PostgreSQL용 INSERT (ON CONFLICT DO NOTHING = IGNORE)
        cursor.execute("""
        INSERT INTO recommendations (ticker, price, time, probability_score) 
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker) DO NOTHING
        """, 
                       (ticker, price, datetime.now(), probability_score))
        conn.commit()
        is_new_rec = cursor.rowcount > 0
        cursor.close()
        conn.close()
        return is_new_rec
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [DB] 'recommendations' 저장 실패: {e}")
        return False

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
            
            df = pd.DataFrame(results)
            df.rename(columns={'o':'o', 'h':'h', 'l':'l', 'c':'c', 'v':'v', 't':'t'}, inplace=True)
            df['t'] = pd.to_datetime(df['t'], unit='ms')
            df.set_index('t', inplace=True)
            
            # 전역 변수에 주입
            ticker_minute_history[ticker] = df
            print(f"✅ [초기화] {ticker} 과거 캔들 {len(df)}개 로딩 완료. 즉시 분석 가능.")
        else:
            # 🔥 여기가 핵심: 왜 실패했는지 로그 출력
            print(f"⚠️ [데이터 없음] {ticker}: Status={data.get('status')}, Count={data.get('count')}, Msg={data.get('message')}")
    except Exception as e:
        print(f"⚠️ [초기화 실패] {ticker}: {e}")

async def handle_msg(msg_data):
    global ticker_minute_history, ticker_tick_history
    
    # --- 설정값 로드 (외부 변수라 가정) ---
    m_fast, m_slow, m_sig = WAE_MACD
    bb_len, bb_std = WAE_BB
    T, K, S = ICHIMOKU_SHORT
    
    TENKAN_COL = f"ITS_{T}"
    KIJUN_COL = f"IKS_{K}"
    SENKOU_A_COL = f"ISA_{T}"
    SENKOU_B_COL = f"ISB_{K}"
    CHIKOU_COL = f"ICS_{K}"
    
    # ✅ [수정] 입력 데이터 타입 안전성 확보
    if isinstance(msg_data, dict):
        msg_list = [msg_data]
    else:
        msg_list = msg_data

    minute_data = []
    
    # 1. 데이터 수신 및 분류
    for msg in msg_list:
        ticker = msg.get('sym')
        if not ticker: continue
            
        # (1) 실시간 틱 데이터 수집 (보간용)
        if msg.get('ev') == 'T':
            if ticker not in ticker_tick_history:
                ticker_tick_history[ticker] = []
            
            # 필요한 데이터만 경량화해서 저장
            ticker_tick_history[ticker].append([msg.get('t'), msg.get('p'), msg.get('s')])
            
            # 틱 데이터 버퍼 관리
            if len(ticker_tick_history[ticker]) > 1000:
                ticker_tick_history[ticker] = ticker_tick_history[ticker][-1000:]
                
        # (2) 1분봉 데이터 수집
        elif msg.get('ev') == 'AM':
            # 로그는 필요시 주석 해제
            # print(f"-> [엔진 v10.0] 1분봉 수신: {ticker} @ ${msg.get('c')}")
            minute_data.append(msg)

    # 2. 각 종목별 지표 계산 및 분석
    for msg in minute_data:
        ticker = msg.get('sym')
        
        if ticker not in ticker_minute_history:
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v', 't'])
            ticker_minute_history[ticker].set_index('t', inplace=True)
            
        timestamp = pd.to_datetime(msg.get('s'), unit='ms')
        new_row = {'o': msg.get('o'), 'h': msg.get('h'), 'l': msg.get('l'), 'c': msg.get('c'), 'v': msg.get('v')}
        ticker_minute_history[ticker].loc[timestamp] = new_row
        
        # ✅ [중요 수정] 데이터 보관 갯수 60 -> 200개로 증가
        # 일목균형표(52), MACD(26) 등의 선행 계산을 위해 넉넉한 데이터 필요 (NaN 방지)
        if len(ticker_minute_history[ticker]) > 200:
            ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-200:]
        
        df_raw = ticker_minute_history[ticker].copy() 
        
        # 최소 데이터 요구량 체크 (일목균형표 선행스팬B 계산 최소치 고려)
        if len(df_raw) < max(MIN_DATA_REQ, 52): 
            continue

        # 1분봉 리샘플링
        df = df_raw.resample('1min').agg({
            'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum'
        })
        
        # 틱 데이터 기반 보간 (Interpolation)
        if ticker in ticker_tick_history and len(ticker_tick_history[ticker]) > 0:
            try:
                ticks_df = pd.DataFrame(ticker_tick_history[ticker], columns=['t', 'p', 's'])
                ticks_df['t'] = pd.to_datetime(ticks_df['t'], unit='ms')
                ticks_df.set_index('t', inplace=True)
                
                # 현재 생성 중인 최신 봉(Last Row) 업데이트
                df['c'] = df['c'].combine_first(ticks_df['p'].resample('1min').last())
                df['o'] = df['o'].combine_first(ticks_df['p'].resample('1min').first())
                df['h'] = df['h'].combine_first(ticks_df['p'].resample('1min').max())
                df['l'] = df['l'].combine_first(ticks_df['p'].resample('1min').min())
                df['v'] = df['v'].combine_first(ticks_df['s'].resample('1min').sum())
                
                ticker_tick_history[ticker] = ticker_tick_history[ticker][-200:] # 틱 버퍼 정리

            except Exception as e:
                print(f"-> [v9.0 틱 보간 경고] {ticker}: {e}")
                
        # 결측치 처리
        df.interpolate(method='linear', inplace=True)
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        if len(df) < MIN_DATA_REQ: 
            continue 

        df.rename(columns={'c': 'close', 'h': 'high', 'l': 'low', 'o': 'open', 'v': 'volume'}, inplace=True)
        
        # --- 기술적 지표 계산 (pandas_ta) ---
        try:
            df.ta.macd(fast=m_fast, slow=m_slow, signal=m_sig, append=True)
            df.ta.bbands(length=5, std=1.5, append=True)  # WAE용
            df.ta.bbands(length=20, std=2.0, append=True) # ✅ Squeeze 감지용 표준 BB
            df.ta.atr(length=WAE_ATR, append=True)
            df.ta.cmf(length=WAE_CMF, append=True) 
            df.ta.obv(append=True)
            df.ta.rsi(length=RSI_LENGTH, append=True) 
            df.ta.ichimoku(tenkan=T, kijun=K, senkou=S, append=True)
        except Exception as e:
            print(f"-> [지표 계산 오류] {ticker}: {e}")
            continue
        
        # 컬럼 찾기
        MACD_COL = next((c for c in df.columns if c.startswith('MACD_')), None)
        BB_UP_COL = next((c for c in df.columns if c.startswith('BBU_')), None)
        BB_LOW_COL= next((c for c in df.columns if c.startswith('BBL_')), None)
        ATR_COL = next((c for c in df.columns if c.startswith('ATRr_')), None) 
        CMF_COL = next((c for c in df.columns if c.startswith('CMF_')), None)
        RSI_COL = next((c for c in df.columns if c.startswith('RSI_')), None)

        senkou_a_cols = [c for c in df.columns if c.startswith('ISA_') or c.startswith('SENKOU_A_')]
        senkou_b_cols = [c for c in df.columns if c.startswith('ISB_') or c.startswith('SENKOU_B_')]
        tenkan_cols   = [c for c in df.columns if c.startswith('ITS_') or c.startswith('TENKAN_')]
        kijun_cols    = [c for c in df.columns if c.startswith('IKS_') or c.startswith('KIJUN_')]
        chikou_cols   = [c for c in df.columns if c.startswith('ICS_') or c.startswith('CHIKOU_')]

        if not (MACD_COL and BB_UP_COL and BB_LOW_COL and ATR_COL and CMF_COL and
                RSI_COL and senkou_a_cols and senkou_b_cols and tenkan_cols and
                kijun_cols and chikou_cols):
            continue 
        
        SENKOU_A_COL = senkou_a_cols[0]; SENKOU_B_COL = senkou_b_cols[0]
        TENKAN_COL   = tenkan_cols[0];   KIJUN_COL    = kijun_cols[0]
        CHIKOU_COL   = chikou_cols[0]
        
        # WAE 지표 계산
        df['t1'] = (df[MACD_COL] - df[MACD_COL].shift(1)) * WAE_SENSITIVITY
        df['e1'] = df[BB_UP_COL] - df[BB_LOW_COL]
        df['deadZone'] = df[ATR_COL] * WAE_ATR_MULT
            
        last = df.iloc[-1]; prev = df.iloc[-2]

        try:
            # ---------------------------------------------------------
            # ✅ [개선 1] 추세 및 눌림목 분석 지표 (3종 세트)
            # 1. 5분 급등률 (단기 과열 확인)
            # 2. 고점 대비 눌림폭 (추세 이탈 확인 - FOXX 거르기용)
            # 3. 일일 상승률 (모멘텀 확인)
            # ---------------------------------------------------------
            price_now = df['close'].iloc[-1]
            
            # 1. 5분 급등률 (Pump Strength)
            if len(df) >= 6:
                price_5m_ago = df['close'].iloc[-6] 
                pump_strength_5m = ((price_now - price_5m_ago) / price_5m_ago) * 100
            else:
                pump_strength_5m = 0.0

            # 🔥 2. 고점 대비 눌림폭 (Pullback from High) - 핵심!
            # 현재 데이터프레임(최근 200분) 내에서의 최고가 기준
            day_high = df['high'].max()
            if day_high > 0:
                pullback_from_high = ((day_high - price_now) / day_high) * 100
            else:
                pullback_from_high = 0.0

            # 3. 일일 상승률 (Daily Change) - 데이터 시작가 대비
            day_open = df['open'].iloc[0]
            if day_open > 0:
                daily_change = ((price_now - day_open) / day_open) * 100
            else:
                daily_change = 0.0

            # ---------------------------------------------------------
            # ✅ [개선 2] 볼린저 밴드 Squeeze 정교화
            # 단순히 폭이 좁은게 아니라, '평소보다' 좁은지를 비교
            # ---------------------------------------------------------
            bb_upper = df[BB_UP_COL].iloc[-1]
            bb_lower = df[BB_LOW_COL].iloc[-1]
            bb_mid_val = (bb_upper + bb_lower) / 2 if (bb_upper + bb_lower) != 0 else 1
            
            current_width = (bb_upper - bb_lower) / bb_mid_val # 현재 밴드폭 비율

            # 최근 20봉 평균 밴드폭 계산
            bb_width_series = (df[BB_UP_COL] - df[BB_LOW_COL]) / ((df[BB_UP_COL] + df[BB_LOW_COL]) / 2)
            avg_width_20 = bb_width_series.rolling(20).mean().iloc[-1]
            
            # 현재 폭이 평균보다 작으면 '수축(Squeeze)' 상태
            is_squeezed = current_width < avg_width_20

            # ATR(변동성) 축소 확인 (보조 지표)
            atr_now = df[ATR_COL].iloc[-1]
            atr_avg = df[ATR_COL].iloc[-6:-1].mean()
            is_volatility_shrinking = (atr_now < atr_avg) or is_squeezed

            # ---------------------------------------------------------
            # ✅ [개선 3] 거래량 가뭄 (Volume Dry-up)
            # ---------------------------------------------------------
            curr_vol = df['volume'].iloc[-1]
            avg_vol_5 = df['volume'].iloc[-6:-1].mean()
            if avg_vol_5 == 0: avg_vol_5 = 1
            
            is_volume_dry = curr_vol < (avg_vol_5 * 0.7) # 평소의 70% 수준

            # --- 기본 조건 정의 ---
            cond_wae_momentum = (last['t1'] > last['e1']) and (last['t1'] > last['deadZone'])
            cond_volume = (last[CMF_COL] > 0) and (last['OBV'] > prev['OBV'])
            cond_rsi = (WAE_RSI_RANGE[0] < last[RSI_COL] < WAE_RSI_RANGE[1])

            # --- 일목균형표 조건 ---
            # 중요: 현재 캔들과 비교할 구름대는 K(26)개 전의 구름대 값임 (pandas_ta 구조상)
            idx_cloud = -K if len(df) > K else -1
            cloud_a_current = df[SENKOU_A_COL].iloc[idx_cloud]
            cloud_b_current = df[SENKOU_B_COL].iloc[idx_cloud]
            
            cloud_top = max(cloud_a_current, cloud_b_current)
            is_above_cloud = last['close'] > cloud_top
            tk_cross_bullish = (prev[TENKAN_COL] < prev[KIJUN_COL]) and (last[TENKAN_COL] > last[KIJUN_COL])
            cond_ichimoku_trend = is_above_cloud and tk_cross_bullish
            
            # 구름대 두께 및 이격도
            cloud_thickness = abs(cloud_a_current - cloud_b_current) / last['close'] * 100
            dist_bull = (last['close'] - cloud_top) / last['close'] * 100
            cond_cloud_shape = (cloud_thickness >= CLOUD_THICKNESS) and (0 <= dist_bull <= CLOUD_PROXIMITY) 

            # 후행스팬 (26봉 전 주가보다 높아야 함)
            price_K_ago = df['close'].iloc[idx_cloud]
            cond_chikou = last[CHIKOU_COL] > price_K_ago

            # --- 최종 트리거 조합 ---
            
            # A. WAE 폭발 (강력 매수)
            engine_1_pass = (cond_wae_momentum and cond_rsi)
            
            # B. 정석 셋업 (구름대 위 + 거래량 받쳐줌 + 모양 좋음)
            engine_2_pass = (cond_cloud_shape and cond_volume and cond_rsi)
            
            # C. [신규] 발산 전조 (Pre-Breakout)
            # 조건: 수축 상태 + 거래량 말름 + 구름대 위 + (중요) 아직 급등 안함(3% 미만)
            cond_pre_breakout = (is_volatility_shrinking and is_volume_dry and is_above_cloud and pump_strength_5m < 3.0)

            if engine_1_pass or engine_2_pass or cond_pre_breakout:
                
                # 추가 정보 계산
                current_session = get_current_session() # 외부 함수
                if current_session == "closed": pass 

                vol_ratio = calculate_volume_ratio(df) # 외부 함수

                # 전략 타입 결정
                if engine_1_pass: strat_type = "Explosion (WAE)"
                elif cond_pre_breakout: strat_type = "Pre-Breakout (Squeeze)"
                else: strat_type = "Standard Setup"

                conditions_data = {
                    "session_type": current_session,
                    "strategy_type": strat_type,        # 전략 유형 로깅
                    "volume_ratio": vol_ratio,
                    "pump_strength_5m": float(round(pump_strength_5m, 2)),
                    "pullback_from_high": float(round(pullback_from_high, 2)), # 추가됨
                    "daily_change": float(round(daily_change, 2)),             # 추가됨
                    "bb_width_ratio": float(round(current_width / avg_width_20, 2)), # 평균 대비 비율 (1.0 미만이면 수축)
                    "is_volume_dry": bool(is_volume_dry),
                    "engine_1_pass": bool(engine_1_pass),
                    "engine_2_pass": bool(engine_2_pass),
                    "pre_breakout": bool(cond_pre_breakout),
                    "rsi_value": float(round(last[RSI_COL], 2)),
                    "cmf_value": float(round(last[CMF_COL], 2)),
                    "cloud_distance_percent": float(round(dist_bull, 2))
                }
                
                # AI 판단 요청
                probability_score = await get_gemini_probability(ticker, conditions_data)
                
                print(f"💡 [{strat_type}] {ticker} @ ${last['close']:.4f} | AI: {probability_score}% | Pump: {pump_strength_5m:.1f}%")
                
                is_new_rec = log_recommendation(ticker, float(last['close']), probability_score)
                
                if is_new_rec: 
                    send_discord_alert(ticker, float(last['close']), "recommendation", probability_score)
                    send_fcm_notification(ticker, float(last['close']), probability_score)
            
            else:
                pass
                
        except Exception as e:
            # 에러 라인 번호까지 출력하여 디버깅 용이하게 함
            import traceback
            print(f"-> ❌ [엔진 CRASH] {ticker} ({e.__traceback__.tb_lineno} line): {e}") 
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

# --- (v16.2) 튜닝: 3분마다 '사냥꾼' 실행 (API 한도 복귀) ---
async def periodic_scanner(websocket):
    current_subscriptions = set() 
    
    while True:
        try:
            print(f"\n[사냥꾼] (v16.2) 3분 주기 시작. '신호 피드' (signals, recommendations) DB를 청소합니다...")
            conn = get_db_connection()
            cursor = conn.cursor()
            # 10. PostgreSQL은 TRUNCATE가 더 빠름 (DELETE도 작동은 함)
            cursor.execute("TRUNCATE TABLE signals")
            cursor.execute("TRUNCATE TABLE recommendations")
            conn.commit()
            cursor.close()
            conn.close()
            print("-> [사냥꾼] DB 청소 완료.")
        except Exception as e:
            print(f"-> ❌ [사냥꾼] DB 청소 실패: {e}")
            
        new_tickers = find_active_tickers() 
        tickers_to_add = new_tickers - current_subscriptions
        tickers_to_remove = current_subscriptions - new_tickers
        
        try:
            if tickers_to_add:
                print(f"[사냥꾼] {len(tickers_to_add)}개 신규 종목 (1분봉+거래) 1개씩 구독 시작: {tickers_to_add}")
                for ticker in tickers_to_add:
                    params_str = f"AM.{ticker},T.{ticker}"
                    sub_payload = json.dumps({"action": "subscribe", "params": params_str})
                    await websocket.send(sub_payload)
                    # 2. 🔥 [추가] 과거 데이터 즉시 로딩 (52분 대기 시간 삭제)
                    # 이 함수가 실행되면 즉시 ticker_minute_history에 200개 봉이 채워짐
                    fetch_initial_data(ticker)
                    await asyncio.sleep(0.1)
                print("[사냥꾼] 신규 구독 완료.")
                
            if tickers_to_remove:
                print(f"[사냥꾼] {len(tickers_to_remove)}개 식은 종목 구독 해지: {tickers_to_remove}")
                for ticker in tickers_to_remove:
                    params_str = f"AM.{ticker},T.{ticker}"
                    unsub_payload = json.dumps({"action": "unsubscribe", "params": params_str})
                    await websocket.send(unsub_payload)
                    await asyncio.sleep(0.1)
                print("[사냥꾼] 구독 해지 완료.")
                
        except websockets.exceptions.ConnectionClosed:
             print("-> ❌ [사냥꾼] 구독/해지 실패: 웹소켓 연결이 이미 종료되었습니다. (재연결 시도)")
             raise
        except Exception as e:
            print(f"-> ❌ [사냥꾼] 구독/해지 실패: {e}")
            
        current_subscriptions = new_tickers
        
        status_tickers_list = []
        for ticker in current_subscriptions:
            status_tickers_list.append({"ticker": ticker, "is_new": ticker in tickers_to_add})
        status_data = {
            'last_scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'watching_count': len(current_subscriptions),
            'watching_tickers': status_tickers_list
        }
        try:
            status_json_string = json.dumps(status_data)
            conn = get_db_connection()
            cursor = conn.cursor()
            # 11. PostgreSQL용 INSERT (ON CONFLICT DO UPDATE)
            cursor.execute("""
            INSERT INTO status (key, value, last_updated) 
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                last_updated = EXCLUDED.last_updated
            """,
                           ('status_data', status_json_string, datetime.now()))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"❌ [DB] 'status' 저장 실패: {e}")
            
        # ✅ (튜닝 1) 7분(420초) -> 3분(180초)로 변경
        # API 한도 및 서버 부하에 주의해야 합니다.
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
                    print("-> ✅ [메인] '수동 인증' 성공! 3개 로봇(사냥꾼, 엔진, 핑)을 시작합니다.")
                    
                    watcher_task = websocket_engine(websocket) 
                    scanner_task = periodic_scanner(websocket)
                    keepalive_task = manual_keepalive(websocket)
                    
                    await asyncio.gather(watcher_task, scanner_task, keepalive_task)
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