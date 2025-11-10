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

# --- (v12.0) API 키 설정 (보안) ---
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

# --- (v15.3) Vertex AI 설정 (us-central1 복귀) ---
GCP_PROJECT_ID = "gen-lang-client-0379169283"
GCP_REGION = "us-central1"

# --- ✅ Firebase VAPID 키 (FCM 발송용) ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_EMAIL = "mailto:cbvkqtm98@gmail.com"

# --- (v9.5) "5분 안정화 엔진" (합의점) ---
MAX_PRICE = 10
TOP_N = 50
MIN_DATA_REQ = 6

# --- (v9.5) 엔진 1: WAE (5분) ---
WAE_MACD = (2, 3, 4)
WAE_SENSITIVITY = 150
WAE_BB = (5, 1.5)
WAE_ATR = 5
WAE_ATR_MULT = 1.5
WAE_CMF = 5
WAE_RSI_RANGE = (45, 75)
RSI_LENGTH = 5

# --- (v9.5) 엔진 2: 일목 (5분) ---
ICHIMOKU_SHORT = (2, 3, 5)
CLOUD_PROXIMITY = 20.0
CLOUD_THICKNESS = 0.5
OBV_LOOKBACK = 3

# --- (v13.0) DB 경로 설정 (PostgreSQL 연동) ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """PostgreSQL DB 연결을 생성합니다."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

ticker_minute_history = {}
ticker_tick_history = {}

# --- 🔽 [수정됨] Gemini API 호출 함수 🔽 ---
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
    * For high-risk signals, assign a **very low probability_score (e.g., 20-40)**, even if other conditions ("engine_1_pass", "engine_2_pass") are true. A good signal at a bad price is a bad signal.
2.  **Analyze Signal Strength (Secondary):**
    * If the signal is **NOT** high-risk, then evaluate its strength.
    * `engine_1_pass (Explosion)` is a strong momentum indicator.
    * `engine_2_pass (Setup)` is a good trend-following indicator.
    * `volume_ok` and `chikou_ok` provide good confirmation.
3.  **Scoring Guideline:**
    * **50 = Neutral.**
    * **20- (High Risk / Trap):** Signal is overextended (High RSI or Cloud Distance). **Strongly avoid.**
    * **60-75 (Good):** A decent signal with low risk.
    * **80+ (Excellent):** A strong signal (e.g., Engine 1 or 2 passed) AND low risk (Low RSI, close to cloud).
You MUST respond ONLY with the specified JSON schema, without any markdown code blocks.
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
        "contents": [{"role": "user", "parts": [{"text": combined_prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
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
                else:
                     print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: 응답에 'candidates' 없음. {result}")
                return 50

            response_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')

            # --- ✅ [수정] 마크다운 코드 블록 제거 로직 추가 ---
            cleaned_text = response_text.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            cleaned_text = cleaned_text.strip()

            try:
                score_data = json.loads(cleaned_text)
                score = int(score_data.get("probability_score", 50))
                reasoning = score_data.get("reasoning", "No reasoning provided.")
                print(f"-> [Gemini AI] {ticker}: 상승 확률 {score}% (이유: {reasoning})")
                return score
            except json.JSONDecodeError:
                print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: AI 응답이 유효한 JSON이 아님. 응답: {response_text}")
                return 50

    except Exception as e:
        if 'response' not in locals():
            print(f"-> ❌ [Gemini AI] {ticker} 분석 실패: {e}")
        return 50
# --- 🔼 [수정 완료] 🔼 ---


def init_db():
    """PostgreSQL DB와 테이블을 생성합니다."""
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
            if e.pgcode == '42701':
                pass
            else:
                raise

        cursor.close()
        conn.close()
        print(f"✅ [DB] PostgreSQL 테이블 초기화 성공.")
    except Exception as e:
        if conn: conn.close()
        print(f"❌ [DB] PostgreSQL 초기화 실패: {e}")


def send_discord_alert(ticker, price, type="signal", probability_score=50):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL or len(DISCORD_WEBHOOK_URL) < 50:
        print(f"🔔 [알림] {ticker} @ ${price} (디스코드 URL 미설정)")
        return

    if type == "signal":
        content = f"🚀 **WAE 폭발 신호** 🚀\n**{ticker}** @ **${price}**\n**AI 상승 확률: {probability_score}%**"
    else:
        content = f"💡 **정석 셋업 (추천)** 💡\n**{ticker}** @ **${price}**\n**AI 상승 확률: {probability_score}%**"

    data = {"content": content}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=data)
        print(f"🔔 [알림] {ticker} @ ${price} (디스코드 전송 완료)")
    except Exception as e:
        print(f"[알림 오류] {ticker} 디스코드 전송 실패: {e}")

# --- 🔽 [수정됨] FCM 푸시 알림 발송 함수 🔽 ---
def send_fcm_notification(ticker, price, probability_score):
    """DB의 모든 토큰에 FCM 푸시 알림을 발송합니다."""
    if not VAPID_PRIVATE_KEY:
        print("🔔 [FCM] VAPID_PRIVATE_KEY가 설정되지 않아 푸시 알림을 건너뜁니다.")
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM fcm_tokens")
        tokens = cursor.fetchall()
        cursor.close()
        conn.close()

        if not tokens:
            print("🔔 [FCM] DB에 등록된 알림 구독자가 없습니다.")
            return

        message_data = json.dumps({
            "title": f"🚀 AI Signal: {ticker}",
            "body": f"New setup detected @ ${price} (AI Score: {probability_score}%)",
            "icon": "/static/images/danso_logo.png"
        })

        print(f"🔔 [FCM] {len(tokens)}명의 구독자에게 {ticker} 알림 발송 시도...")

        for (token_str,) in tokens:
            try:
                # --- ✅ [수정] 토큰 형식 오류 방어 로직 강화 ---
                if not token_str:
                    continue
                subscription_info = json.loads(token_str)
                # --- ✅ 여기까지 수정 ---

                webpush(
                    subscription_info=subscription_info,
                    data=message_data,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_EMAIL}
                )
            except json.JSONDecodeError:
                # [수정] JSON 파싱 실패 시, 해당 토큰만 건너뛰고 로그 남김
                print(f"❌ [FCM] 토큰 형식 오류 (유효한 JSON이 아님). 건너뜁니다: {token_str[:75]}...")
                continue
            except WebPushException as ex:
                print(f"❌ [FCM] 토큰 전송 실패: {ex}")
                # 참고: 만료된 토큰(410, 404)은 DB에서 삭제하는 로직 추가 권장
            except Exception as e:
                print(f"❌ [FCM] 알 수 없는 오류 발생: {e}")

        print(f"✅ [FCM] {len(tokens)}명에게 알림 발송 완료.")

    except Exception as e:
        if conn: conn.close()
        print(f"❌ [FCM] 푸시 알림 발송 중 DB 오류: {e}")
# --- 🔼 [수정 완료] 🔼 ---


def log_signal(ticker, price, probability_score=50):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
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


def find_active_tickers():
    if not POLYGON_API_KEY:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류: POLYGON_API_KEY가 설정되지 않았습니다.")
        return set()

    print(f"\n[사냥꾼] 1단계: 'Top Gainers' (조건: ${MAX_PRICE} 미만) 스캔 중...")
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
            return tickers_to_watch
    except Exception as e:
        print(f"-> ❌ [사냥꾼] 1단계 스캔 오류 (API 키/한도 확인): {e}")
        return tickers_to_watch


async def handle_msg(msg_list):
    global ticker_minute_history, ticker_tick_history
    m_fast, m_slow, m_sig = WAE_MACD; bb_len, bb_std = WAE_BB
    T, K, S = ICHIMOKU_SHORT

    minute_data = []
    for msg in msg_list:
        ticker = msg.get('sym')
        if not ticker:
            continue

        if msg.get('ev') == 'T':
            if ticker not in ticker_tick_history:
                ticker_tick_history[ticker] = []
            ticker_tick_history[ticker].append([msg.get('t'), msg.get('p'), msg.get('s')])
            if len(ticker_tick_history[ticker]) > 1000:
                ticker_tick_history[ticker] = ticker_tick_history[ticker][-1000:]

        elif msg.get('ev') == 'AM':
            print(f"-> [엔진 v10.0] 1분봉 데이터 수신: {ticker} @ ${msg.get('c')} (Vol: {msg.get('v')})")
            minute_data.append(msg)

    for msg in minute_data:
        ticker = msg.get('sym')

        if ticker not in ticker_minute_history:
            ticker_minute_history[ticker] = pd.DataFrame(columns=['o', 'h', 'l', 'c', 'v', 't'])
            ticker_minute_history[ticker].set_index('t', inplace=True)

        timestamp = pd.to_datetime(msg.get('s'), unit='ms')
        new_row = {'o': msg.get('o'), 'h': msg.get('h'), 'l': msg.get('l'), 'c': msg.get('c'), 'v': msg.get('v')}
        ticker_minute_history[ticker].loc[timestamp] = new_row

        if len(ticker_minute_history[ticker]) > 60:
            ticker_minute_history[ticker] = ticker_minute_history[ticker].iloc[-60:]

        df_raw = ticker_minute_history[ticker].copy()

        if len(df_raw) < MIN_DATA_REQ: continue

        df = df_raw.resample('1min').agg({
            'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum'
        })

        if ticker in ticker_tick_history and len(ticker_tick_history[ticker]) > 0:
            try:
                ticks_df = pd.DataFrame(ticker_tick_history[ticker], columns=['t', 'p', 's'])
                ticks_df['t'] = pd.to_datetime(ticks_df['t'], unit='ms')
                ticks_df.set_index('t', inplace=True)

                df['c'] = df['c'].combine_first(ticks_df['p'].resample('1min').last())
                df['o'] = df['o'].combine_first(ticks_df['p'].resample('1min').first())
                df['h'] = df['h'].combine_first(ticks_df['p'].resample('1min').max())
                df['l'] = df['l'].combine_first(ticks_df['p'].resample('1min').min())
                df['v'] = df['v'].combine_first(ticks_df['s'].resample('1min').sum())

                ticker_tick_history[ticker] = ticker_tick_history[ticker][-100:]

            except Exception as e:
                print(f"-> [v9.0 틱 보간 실패] {ticker}: {e}")

        df.interpolate(method='linear', inplace=True)
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        if len(df) < MIN_DATA_REQ:
            continue

        df.rename(columns={'c': 'close', 'h': 'high', 'l': 'low', 'o': 'open', 'v': 'volume'}, inplace=True)

        df.ta.macd(fast=m_fast, slow=m_slow, signal=m_sig, append=True)
        df.ta.bbands(length=bb_len, std=bb_std, append=True)
        df.ta.atr(length=WAE_ATR, append=True)
        df.ta.cmf(length=WAE_CMF, append=True)
        df.ta.obv(append=True)
        df.ta.rsi(length=RSI_LENGTH, append=True)
        df.ta.ichimoku(tenkan=T, kijun=K, senkou=S, append=True)

        MACD_COL = next((c for c in df.columns if c.startswith('MACD_')), None)
        BB_UP_COL = next((c for c in df.columns if c.startswith('BBU_')), None)
        BB_LOW_COL= next((c for c in df.columns if c.startswith('BBL_')), None)
        ATR_COL = next((c for c in df.columns if c.startswith('ATRr_')), None)
        CMF_COL = next((c for c in df.columns if c.startswith('CMF_')), None)
        RSI_COL = next((c for c in df.columns if c.startswith('RSI_')), None)
        SENKOU_A_COL = next((c for c in df.columns if c.startswith('ISA_')), None)
        SENKOU_B_COL = next((c for c in df.columns if c.startswith('ISB_')), None)
        TENKAN_COL = next((c for c in df.columns if c.startswith('ITS_')), None)
        KIJUN_COL = next((c for c in df.columns if c.startswith('IKS_')), None)
        CHIKOU_COL = next((c for c in df.columns if c.startswith('ICS_')), None)

        if not all([MACD_COL, BB_UP_COL, BB_LOW_COL, ATR_COL, CMF_COL, RSI_COL,
                    SENKOU_A_COL, SENKOU_B_COL, TENKAN_COL, KIJUN_COL, CHIKOU_COL]):
            continue

        df['t1'] = (df[MACD_COL] - df[MACD_COL].shift(1)) * WAE_SENSITIVITY
        df['e1'] = df[BB_UP_COL] - df[BB_LOW_COL]
        df['deadZone'] = df[ATR_COL] * WAE_ATR_MULT

        if len(df) < MIN_DATA_REQ: continue

        last = df.iloc[-1]; prev = df.iloc[-2]

        try:
            cond_wae_momentum = (last['t1'] > last['e1']) and (last['t1'] > last['deadZone'])
            cond_volume = (last[CMF_COL] > 0) and (last['OBV'] > prev['OBV'])
            cond_rsi = (WAE_RSI_RANGE[0] < last[RSI_COL] < WAE_RSI_RANGE[1])

            cloud_a_current = df[SENKOU_A_COL].iloc[-K]; cloud_b_current = df[SENKOU_B_COL].iloc[-K]
            cloud_top = max(cloud_a_current, cloud_b_current);
            is_above_cloud = last['close'] > cloud_top
            tk_cross_bullish = (prev[TENKAN_COL] < prev[KIJUN_COL]) and (last[TENKAN_COL] > last[KIJUN_COL])
            cond_ichimoku_trend = is_above_cloud and tk_cross_bullish

            cloud_thickness = abs(cloud_a_current - cloud_b_current) / last['close'] * 100
            dist_bull = (last['close'] - cloud_top) / last['close'] * 100
            cond_cloud_shape = (cloud_thickness >= CLOUD_THICKNESS) and (0 <= dist_bull <= CLOUD_PROXIMITY)

            chikou = last[CHIKOU_COL]
            price_K_ago = df['close'].iloc[-K]
            cond_chikou = chikou > price_K_ago

            engine_1_pass = (cond_wae_momentum and cond_rsi)
            engine_2_pass = (cond_cloud_shape and cond_volume and cond_rsi)

            if engine_1_pass or engine_2_pass:
                conditions_data = {
                    "engine_1_pass (Explosion)": bool(engine_1_pass),
                    "engine_2_pass (Setup)": bool(engine_2_pass),
                    "wae_momentum": bool(cond_wae_momentum),
                    "rsi_ok": bool(cond_rsi),
                    "volume_ok": bool(cond_volume),
                    "cloud_shape_ok (20%)": bool(cond_cloud_shape),
                    "ichimoku_trend_ok": bool(cond_ichimoku_trend),
                    "chikou_ok": bool(cond_chikou),
                    "rsi_value": float(round(last[RSI_COL], 2)),
                    "cmf_value": float(round(last[CMF_COL], 2)),
                    "cloud_distance_percent": float(round(dist_bull, 2))
                }

                probability_score = await get_gemini_probability(ticker, conditions_data)

                print(f"💡💡💡 [통합 엔진 v5.1] {ticker} @ ${last['close']:.4f} (AI Score: {probability_score}%) 💡💡💡")
                is_new_rec = log_recommendation(ticker, float(last['close']), probability_score)

                if is_new_rec:
                    send_discord_alert(ticker, float(last['close']), "recommendation", probability_score)
                    send_fcm_notification(ticker, float(last['close']), probability_score)

        except Exception as e:
            print(f"-> ❌ [엔진 CRASH] {ticker} 분석 중 치명적 오류: {e}")
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
            print(f"\n[사냥꾼] (v9.7) 7분 주기 시작. '신호 피드' (signals, recommendations) DB를 청소합니다...")
            conn = get_db_connection()
            cursor = conn.cursor()
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
                print(f"[사냥꾼] {len(tickers_to_add)}개 신규 종목 구독 시작: {tickers_to_add}")
                for ticker in tickers_to_add:
                    params_str = f"AM.{ticker},T.{ticker}"
                    sub_payload = json.dumps({"action": "subscribe", "params": params_str})
                    await websocket.send(sub_payload)
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

        status_tickers_list = [{"ticker": ticker, "is_new": ticker in tickers_to_add} for ticker in current_subscriptions]
        status_data = {
            'last_scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'watching_count': len(current_subscriptions),
            'watching_tickers': status_tickers_list
        }
        try:
            status_json_string = json.dumps(status_data)
            conn = get_db_connection()
            cursor = conn.cursor()
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

        print(f"\n[사냥꾼] 7분(420초) 후 다음 스캔을 시작합니다...")
        await asyncio.sleep(420)


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


async def main():
    required_vars = ['POLYGON_API_KEY', 'DATABASE_URL', 'GEMINI_API_KEY', 'GCP_PROJECT_ID']
    for var in required_vars:
        if not globals().get(var) or ("YOUR_PROJECT_ID" in str(globals().get(var))):
            print(f"❌ [메인] 환경 변수 '{var}'가 설정되지 않았습니다. 스캐너를 시작할 수 없습니다.")
            return

    if not VAPID_PRIVATE_KEY:
        print("⚠️ [메인] VAPID_PRIVATE_KEY가 설정되지 않았습니다. FCM 푸시 알림이 비활성화됩니다.")

    print("스캐너 V15.5 (오류 수정 버전)을 시작합니다...")
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


if __name__ == "__main__":
    init_db()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[메인] 사용자에 의해 프로그램이 종료되었습니다.")