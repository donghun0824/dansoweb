import asyncio
import websockets 
import requests
import os
import pandas as pd
import pandas_ta as ta
import json
from datetime import datetime
import psycopg2
import time
import httpx 
from pywebpush import webpush, WebPushException

# --- API 키 설정 ---
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

# --- Vertex AI 설정 ---
GCP_PROJECT_ID = "gen-lang-client-0379169283" 
GCP_REGION = "us-central1" 

# --- Firebase VAPID 키 ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_EMAIL = "mailto:cbvkqtm98@gmail.com"

# --- 엔진 설정 (안정화 버전) ---
MAX_PRICE = 10
TOP_N = 50
MIN_DATA_REQ = 6

# --- 엔진 1: WAE ---
WAE_MACD = (2, 3, 4) 
WAE_SENSITIVITY = 150
WAE_BB = (5, 1.5) 
WAE_ATR = 5 
WAE_ATR_MULT = 1.5
WAE_CMF = 5 
WAE_RSI_RANGE = (45, 75) # 75로 복귀
RSI_LENGTH = 5 

# --- 엔진 2: 일목 ---
ICHIMOKU_SHORT = (2, 3, 5) 
CLOUD_PROXIMITY = 20.0 # 20.0으로 복귀
CLOUD_THICKNESS = 0.5
OBV_LOOKBACK = 3 

# --- PostgreSQL DB 설정 ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

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
You are a specialized quantitative analyst AI for high-speed scalping.
Your task is to evaluate the provided JSON data for a 'buy' signal and return a "probability_score" (0-100) for a short-term price increase (5-30 min).
**Your primary rule is to aggressively penalize overextended signals.**
Many signals fail because they trigger when the price is already too high (overbought).
1.  **Analyze Risk (Most Important):**
    * Look at "rsi_value" and "cloud_distance_percent".
    * If "rsi_value" is high (e.g., > 70) OR "cloud_distance_percent" is large (e.g., > 15%), the signal is **high-risk**.
    * For high-risk signals, assign a **very low probability_score (e.g., 20-40)**, even if other conditions are true. A good signal at a bad price is a bad signal.
2.  **Analyze Signal Strength (Secondary):**
    * If the signal is **NOT** high-risk, then evaluate its strength.
    * `engine_1_pass (Explosion)` is a strong momentum indicator.
    * `engine_2_pass (Setup)` is a good trend-following indicator.
    * `volume_ok` and `chikou_ok` provide good confirmation.
3.  **Scoring Guideline:**
    * **50 = Neutral.**
    * **20- (High Risk / Trap):** Signal is overextended (High RSI or Cloud Distance). **Strongly avoid.**
    * **60-75 (Good):** A decent signal with low risk.
    * **80+ (Excellent):** A strong signal AND low risk (Low RSI, close to cloud).
You MUST respond ONLY with the specified JSON schema.
"""
    user_prompt = f"""
    Analyze the following signal data for Ticker: {ticker}
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
                return 50
                
            result = response.json()
            
            if 'candidates' not in result:
                print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: 응답에 'candidates' 없음. {result}")
                return 50

            response_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            
            # --- ✅ (v16.1) AI가 Markdown으로 감싸서 응답할 경우 JSON 추출 ---
            if '```json' in response_text:
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
        print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: {e}")
        return 50

# --- DB 초기화 ---
def init_db():
    conn = None
    try:
        if not DATABASE_URL:
            print("❌ [DB] DATABASE_URL이 설정되지 않아 초기화를 건너뜁니다.")
            return
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, price REAL NOT NULL, time TIMESTAMP NOT NULL)")
        cursor.execute("CREATE TABLE IF NOT EXISTS recommendations (id SERIAL PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, price REAL NOT NULL, time TIMESTAMP NOT NULL, probability_score INTEGER)")
        cursor.execute("CREATE TABLE IF NOT EXISTS posts (id SERIAL PRIMARY KEY, author TEXT NOT NULL, content TEXT NOT NULL, time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("CREATE TABLE IF NOT EXISTS fcm_tokens (id SERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()
        
        try:
            cursor.execute("ALTER TABLE recommendations ADD COLUMN probability_score INTEGER")
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            if e.pgcode != '42701':
                print(f"❌ [DB] ALTER TABLE 중 예외 발생 (무시함): {e}")
            
        cursor.close()
        conn.close()
        print(f"✅ [DB] PostgreSQL 테이블 초기화 성공.")
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [DB] PostgreSQL 초기화 실패 (무시함): {e}")

# --- 알림 함수 ---
def send_discord_alert(ticker, price, type="recommendation", probability_score=50):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL:
        return
    content = f"💡 **AI Setup (Recommendation)** 💡\n**{ticker}** @ **${price:.4f}**\n**AI Score: {probability_score}%**"
    try: 
        requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
        print(f"🔔 [알림] {ticker} @ ${price:.4f} (디스코드 전송 완료)")
    except Exception as e: 
        print(f"[알림 오류] {ticker} 디스코드 전송 실패: {e}")

# --- FCM 푸시 알림 (토큰 오류 수정) ---
def send_fcm_notification(ticker, price, probability_score):
    if not VAPID_PRIVATE_KEY: return
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM fcm_tokens")
        tokens = cursor.fetchall()
        cursor.close()
        conn.close()

        if not tokens: return
        
        message_data = json.dumps({
            "title": f"🚀 AI Signal: {ticker} @ ${price:.4f}",
            "body": f"New setup detected (AI Score: {probability_score}%)",
            "icon": "/static/images/danso_logo.png"
        })

        print(f"🔔 [FCM] {len(tokens)}명의 구독자에게 {ticker} 알림 발송 시도...")
        success_count, fail_count = 0, 0

        for (token_str,) in tokens:
            try:
                # --- ✅ (v16.1) 비어있는 토큰 방어 코드 ---
                if not token_str:
                    print("❌ [FCM] DB에서 비어있는 토큰 발견 (무시함).")
                    fail_count += 1
                    continue
                # --- 여기까지 추가 ---

                subscription_info = json.loads(token_str) 
                webpush(subscription_info=subscription_info, data=message_data, vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_EMAIL})
                success_count += 1
            except Exception as e:
                print(f"❌ [FCM] 알 수 없는 토큰 오류 (토큰 형식 확인 필요): {e}")
                fail_count += 1
        
        print(f"✅ [FCM] {success_count}명에게 발송 완료, {fail_count}명 실패.")
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [FCM] 푸시 알림 발송 중 DB 오류: {e}")

# --- DB 로그 ---
def log_recommendation(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO recommendations (ticker, price, time, probability_score) VALUES (%s, %s, %s, %s) ON CONFLICT (ticker) DO NOTHING", (ticker, price, datetime.now(), probability_score))
        conn.commit()
        is_new_rec = cursor.rowcount > 0
        cursor.close()
        conn.close()
        return is_new_rec
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [DB] 'recommendations' 저장 실패: {e}")
        return False

# --- 1단계: 티커 스캔 ---
def find_active_tickers():
    if not POLYGON_API_KEY:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류: POLYGON_API_KEY가 설정되지 않았습니다.")
        return set()
        
    print(f"\n[사냥꾼] 1단계: 'Top Gainers' (조건: ${MAX_PRICE} 미만) 스캔 중...")
    url = f"[https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey=](https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey=){POLYGON_API_KEY}"
    tickers_to_watch = set()
    try:
        response = requests.get(url)
        response.raise_for_status() 
        data = response.json()
        if data.get('status') == 'OK':
            for ticker in data.get('tickers', []):
                price = ticker.get('lastTrade', {}).get('p', 999) 
                if price <= MAX_PRICE and (ticker_symbol := ticker.get('ticker')):
                    tickers_to_watch.add(ticker_symbol)
                if len(tickers_to_watch) >= TOP_N: break
            print(f"-> [사냥꾼] 1단계 스캔 완료. 총 {len(tickers_to_watch)}개 종목 포착.")
            
    except Exception as e:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류 (API 키/한도 확인): {e}")
    return tickers_to_watch

# --- 2단계: 데이터 분석 엔진 ---
async def handle_msg(msg_list):
    global ticker_minute_history
    m_fast, m_slow, m_sig = WAE_MACD; bb_len, bb_std = WAE_BB
    T, K, S = ICHIMOKU_SHORT
    
    for msg in msg_list:
        if not (ticker := msg.get('sym')) or msg.get('ev') != 'AM':
            continue

        print(f"-> [엔진 v10.0] 1분봉 데이터 수신: {ticker} @ ${msg.get('c')} (Vol: {msg.get('v')})")
        if ticker not in ticker_minute_history:
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v', 't']).set_index('t')
            
        timestamp = pd.to_datetime(msg.get('s'), unit='ms')
        new_row = {'o': msg.get('o'), 'h': msg.get('h'), 'l': msg.get('l'), 'c': msg.get('c'), 'v': msg.get('v')}
        ticker_minute_history[ticker].loc[timestamp] = new_row
        ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-60:]
        
        df_raw = ticker_minute_history[ticker].copy() 
        if len(df_raw) < MIN_DATA_REQ: continue

        df = df_raw.rename(columns={'c': 'close', 'h': 'high', 'l': 'low', 'o': 'open', 'v': 'volume'})
        
        df.ta.macd(fast=m_fast, slow=m_slow, signal=m_sig, append=True)
        df.ta.bbands(length=bb_len, std=bb_std, append=True)
        df.ta.atr(length=WAE_ATR, append=True)
        df.ta.cmf(length=WAE_CMF, append=True) 
        df.ta.obv(append=True)
        df.ta.rsi(length=RSI_LENGTH, append=True) 
        df.ta.ichimoku(tenkan=T, kijun=K, senkou=S, append=True)
        
        MACD_COL, BB_UP_COL, BB_LOW_COL, ATR_COL, CMF_COL, RSI_COL, SENKOU_A_COL, SENKOU_B_COL, CHIKOU_COL = (
            next((c for c in df.columns if c.startswith(prefix)), None)
            for prefix in ['MACD_', 'BBU_', 'BBL_', 'ATRr_', 'CMF_', 'RSI_', 'ISA_', 'ISB_', 'ICS_']
        )
        if not all([MACD_COL, BB_UP_COL, BB_LOW_COL, ATR_COL, CMF_COL, RSI_COL, SENKOU_A_COL, SENKOU_B_COL, CHIKOU_COL]): continue
        
        df['t1'] = (df[MACD_COL] - df[MACD_COL].shift(1)) * WAE_SENSITIVITY
        df['e1'] = df[BB_UP_COL] - df[BB_LOW_COL]
        df['deadZone'] = df[ATR_COL] * WAE_ATR_MULT
        
        if len(df) < MIN_DATA_REQ: continue 
        last, prev = df.iloc[-1], df.iloc[-2]

        try:
            cond_wae_momentum = (last['t1'] > last['e1']) and (last['t1'] > last['deadZone'])
            cond_volume = (last[CMF_COL] > 0) and (last['OBV'] > prev['OBV'])
            cond_rsi = (WAE_RSI_RANGE[0] < last[RSI_COL] < WAE_RSI_RANGE[1])

            cloud_a_current = df[SENKOU_A_COL].iloc[-K]; cloud_b_current = df[SENKOU_B_COL].iloc[-K]
            cloud_top = max(cloud_a_current, cloud_b_current)
            dist_bull = (last['close'] - cloud_top) / last['close'] * 100
            cloud_thickness = abs(cloud_a_current - cloud_b_current) / last['close'] * 100
            cond_cloud_shape = (cloud_thickness >= CLOUD_THICKNESS) and (0 <= dist_bull <= CLOUD_PROXIMITY)

            engine_1_pass = (cond_wae_momentum and cond_rsi)
            engine_2_pass = (cond_cloud_shape and cond_volume and cond_rsi)
            
            if engine_1_pass or engine_2_pass:
                conditions_data = {
                    "engine_1_pass (Explosion)": bool(engine_1_pass), "engine_2_pass (Setup)": bool(engine_2_pass),
                    "rsi_ok": bool(cond_rsi), "volume_ok": bool(cond_volume),
                    "cloud_shape_ok (20%)": bool(cond_cloud_shape), "rsi_value": float(round(last[RSI_COL], 2)),
                    "cmf_value": float(round(last[CMF_COL], 2)), "cloud_distance_percent": float(round(dist_bull, 2))
                }
                probability_score = await get_gemini_probability(ticker, conditions_data)
                
                if log_recommendation(ticker, float(last['close']), probability_score): 
                    print(f"💡💡💡 [통합 엔진] {ticker} @ ${last['close']:.4f} (AI Score: {probability_score}%) 💡💡💡")
                    send_discord_alert(ticker, float(last['close']), "recommendation", probability_score)
                    send_fcm_notification(ticker, float(last['close']), probability_score)
        except Exception as e:
            print(f"-> ❌ [엔진 CRASH] {ticker} 분석 중 오류: {e}") 

# --- 주기적 스캐너 (7분 주기) ---
async def periodic_scanner(websocket):
    current_subscriptions = set() 
    while True:
        try:
            print(f"\n[사냥꾼] (v16.2) 7분 주기 시작. DB 청소...")
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("TRUNCATE TABLE signals, recommendations")
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"-> ❌ [사냥꾼] DB 청소 실패: {e}")
            
        new_tickers = find_active_tickers() 
        tickers_to_add = new_tickers - current_subscriptions
        tickers_to_remove = current_subscriptions - new_tickers
        
        try:
            if tickers_to_add:
                params_str = ",".join([f"AM.{t}" for t in tickers_to_add])
                await websocket.send(json.dumps({"action": "subscribe", "params": params_str}))
            if tickers_to_remove:
                params_str = ",".join([f"AM.{t}" for t in tickers_to_remove])
                await websocket.send(json.dumps({"action": "unsubscribe", "params": params_str}))
        except Exception as e:
            print(f"-> ❌ [사냥꾼] 구독/해지 실패: {e}")
            
        current_subscriptions = new_tickers
        
        status_data = {'last_scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'watching_count': len(current_subscriptions), 'watching_tickers': list(current_subscriptions)}
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO status (key, value, last_updated) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, last_updated = EXCLUDED.last_updated", ('status_data', json.dumps(status_data), datetime.now()))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"❌ [DB] 'status' 저장 실패: {e}")
            
        print(f"\n[사냥꾼] 7분(420초) 후 다음 스캔을 시작합니다...")
        await asyncio.sleep(420) 

# --- 메인 실행 함수 ---
async def main():
    required_vars = ['POLYGON_API_KEY', 'GEMINI_API_KEY', 'DATABASE_URL', 'GCP_PROJECT_ID']
    if any(not os.environ.get(var) for var in required_vars):
        print(f"❌ [메인] 필수 환경 변수 누락. 스캐너를 시작할 수 없습니다.")
        return

    print("스캐너 V16.2 (7-min scan, bugfixes)을 시작합니다...") 
    uri = "wss://socket.polygon.io/stocks"
    
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print(f"[메인] 웹소켓 {uri} 연결 성공.")
                
                await websocket.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                auth_response = await websocket.recv()
                print(f"[메인] 인증 응답: {auth_response}")
                
                if '"status":"auth_success"' in str(auth_response):
                    print("-> ✅ [메인] 인증 성공! 스캐너와 엔진을 시작합니다.")
                    scanner_task = asyncio.create_task(periodic_scanner(websocket))
                    engine_task = asyncio.create_task(engine_loop(websocket))
                    await asyncio.gather(scanner_task, engine_task)
                else:
                    print("-> ❌ [메인] 인증 실패. 10초 후 재시도...")
                    await asyncio.sleep(10)
        except Exception as e:
            print(f"-> ❌ [메인] 치명적 오류: {e}. 10초 후 재연결합니다...")
            await asyncio.sleep(10)

async def engine_loop(websocket):
    async for message in websocket:
        try:
            await handle_msg(json.loads(message))
        except Exception as e:
            print(f"-> ❌ [엔진 루프] 메시지 처리 실패: {e}")

if __name__ == "__main__":
    init_db() 
    asyncio.run(main())