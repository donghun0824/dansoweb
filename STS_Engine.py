import copy 
import asyncio
import websockets
import json
import os
import time
import numpy as np
import pandas as pd
import csv
import httpx
import xgboost as xgb
import psycopg2
from psycopg2 import pool
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from concurrent.futures import ThreadPoolExecutor # [V5.3] 추가
import firebase_admin
from firebase_admin import credentials, messaging
import traceback
import pytz
# 커스텀 지표 모듈 임포트
import indicators_sts as ind 

# ==============================================================================
# 1. CONFIGURATION & CONSTANTS
# ==============================================================================
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
WS_URI = "wss://socket.polygon.io/stocks"

# 전략 설정
STS_TARGET_COUNT = 3
STS_MIN_VOLUME_DOLLAR = 1e6
STS_MAX_SPREAD_PCT = 1.0      
STS_MAX_VPIN = 0.80         # [V5.3] 필터 완화 (0.55 -> 0.65)
OBI_LEVELS = 20               # [V5.3] 오더북 깊이 확장 (5 -> 20)

# 후보 선정(Target Selector) 필터 기준
STS_MIN_DOLLAR_VOL = 200000  # 최소 거래대금 $300k (약 4억원)
STS_MAX_PRICE = 50.0         # 최대 가격 $30 (저가주 집중)
STS_MIN_RVOL = 3.0           # (SniperBot 단계) 최소 상대 거래량
STS_MAX_SPREAD_ENTRY = 0.9   # (SniperBot 단계) 진입 허용 스프레드

# AI & Risk Params
MODEL_FILE = "sts_xgboost_model.json"
AI_PROB_THRESHOLD = 0.85      
ATR_TRAIL_MULT = 1.5        
HARD_STOP_PCT = 0.015         

# Logging
TRADE_LOG_FILE = "sts_trade_log_v5.csv"
REPLAY_LOG_FILE = "sts_replay_data_v5.csv"

# System Optimization
DB_UPDATE_INTERVAL = 3.0      # 3초
GC_INTERVAL = 300             
GC_TTL = 600                  

# [변경] 기존 단일 풀(max=3)을 폐기하고 용도별로 분리
# DB 작업용 (빠르고 빈번함) -> 10개 레인
DB_WORKER_POOL = ThreadPoolExecutor(max_workers=10) 
# 알림 발송용 (느리고 가끔 발생) -> 5개 레인
NOTI_WORKER_POOL = ThreadPoolExecutor(max_workers=5)

# Global DB Pool
db_pool = None

# ==============================================================================
# 2. DATABASE & FIREBASE SETUP
# ==============================================================================
def init_db():
    """DB 커넥션 풀 및 테이블 초기화 (안전한 컬럼 추가 로직 적용)"""
    global db_pool
    if not DATABASE_URL: return
    try:
        if db_pool is None:
            # 봇용 연결 1개 (최적화)
            db_pool = psycopg2.pool.SimpleConnectionPool(5, 20, dsn=DATABASE_URL)
            print("✅ [DB] Connection Pool Initialized (Limit: 20)")
            
        conn = db_pool.getconn()
        cursor = conn.cursor()
        
        # ---------------------------------------------------------
        # 1. 테이블 생성 (기존 코드 유지)
        # ---------------------------------------------------------
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sts_live_targets (
            ticker TEXT PRIMARY KEY,
            price REAL,
            ai_score REAL,
            obi REAL,
            vpin REAL,
            tick_speed INTEGER,
            vwap_dist REAL,
            status TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY, 
            ticker TEXT NOT NULL, 
            price REAL NOT NULL, 
            score REAL, 
            time TIMESTAMP NOT NULL
        );
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id SERIAL PRIMARY KEY, 
            token TEXT NOT NULL UNIQUE, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            min_score INTEGER DEFAULT 0
        );
        """)
        conn.commit()

        # ---------------------------------------------------------
        # 2. 컬럼 마이그레이션 (기존 코드 유지)
        # ---------------------------------------------------------
        try:
            cursor.execute("ALTER TABLE signals ADD COLUMN score REAL")
            conn.commit()
        except psycopg2.Error:
            conn.rollback()

        # ---------------------------------------------------------
        # 3. [수정됨] sts_live_targets 테이블 확장 (리스트 & 반복문 적용)
        # 기존: 하나라도 실패하면 전체 취소됨
        # 수정: 하나씩 시도하여 실패한 것(이미 있는 것)만 건너뜀
        # ---------------------------------------------------------
        
        # 추가할 컬럼 목록 정의 (obi_mom부터 day_change까지 포함)
        target_columns = [
            "obi_mom REAL DEFAULT 0",
            "tick_accel REAL DEFAULT 0",
            "vwap_slope REAL DEFAULT 0",
            "squeeze_ratio REAL DEFAULT 0",
            "rvol REAL DEFAULT 0",
            "atr REAL DEFAULT 0",
            "pump_accel REAL DEFAULT 0",
            "spread REAL DEFAULT 0",
            "day_change REAL DEFAULT 0"  # 기존 맨 아래 있던 day_change도 포함
        ]

        print("🔄 [DB] Checking and adding columns...")
        
        for col_def in target_columns:
            try:
                # 구문 실행: ALTER TABLE ... ADD COLUMN ...
                cursor.execute(f"ALTER TABLE sts_live_targets ADD COLUMN {col_def}")
                conn.commit()
                # 컬럼명만 추출해서 로그 출력 (예: "rvol REAL..." -> "rvol")
                col_name = col_def.split()[0]
                print(f"🆕 [DB] Added column: {col_name}")
            except psycopg2.Error:
                # 이미 컬럼이 존재하면 에러가 나므로, 그 건만 롤백하고 다음으로 넘어감
                conn.rollback()
        
        print("✅ [DB] Table Schema Verified & Updated.")
            
        cursor.close()
        db_pool.putconn(conn)
        
    except Exception as e:
        print(f"❌ [DB Init Error] {e}")

def get_db_connection():
    global db_pool
    if db_pool is None: init_db()
    return db_pool.getconn()

def init_firebase():
    """Firebase Admin SDK 초기화 (JSON 파싱 에러 방지 강화판)"""
    try:
        # 1. 환경변수 확인
        if not FIREBASE_ADMIN_SDK_JSON_STR:
            print("⚠️ [FCM Warning] FIREBASE_ADMIN_SDK_JSON 환경변수가 비어있습니다. 푸시 알림을 건너뜁니다.", flush=True)
            return

        # 2. 이미 초기화되었는지 확인
        if firebase_admin._apps:
            return

        # 3. JSON 문자열 다듬기 (이게 핵심!)
        # 실수로 들어간 줄바꿈이나, 이스케이프된 줄바꿈(\n)을 모두 실제 줄바꿈으로 통일하거나 제거
        json_str = FIREBASE_ADMIN_SDK_JSON_STR.strip()
        
        # 따옴표 문제나 줄바꿈 문자가 꼬였을 때를 대비한 전처리
        if json_str.startswith("'") and json_str.endswith("'"):
            json_str = json_str[1:-1] # 앞뒤 불필요한 따옴표 제거
        
        try:
            cred_dict = json.loads(json_str)
        except json.JSONDecodeError:
            # 실패하면 혹시 모르니 줄바꿈 문자를 수동으로 교체해서 재시도
            print("⚠️ [FCM] 1차 JSON 파싱 실패. 줄바꿈 문자 보정 후 재시도...", flush=True)
            fixed_str = json_str.replace('\\n', '\n') # 문자열 "\n"을 실제 엔터로 변경
            cred_dict = json.loads(fixed_str)

        # 4. 초기화
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print(f"✅ [FCM] Firebase 초기화 성공 (Project: {cred_dict.get('project_id', 'Unknown')})", flush=True)

    except json.JSONDecodeError as je:
        print(f"❌ [FCM Critical] JSON 형식이 깨져있습니다. 환경변수를 다시 복사하세요.", flush=True)
        print(f"   에러 위치: {je}", flush=True)
        # 보안상 전체 키를 찍진 말고 앞부분만 확인
        print(f"   입력된 값(앞 20자): {FIREBASE_ADMIN_SDK_JSON_STR[:20]}...", flush=True)
    except Exception as e:
        print(f"❌ [FCM Error] 초기화 중 알 수 없는 오류: {e}", flush=True)

def update_dashboard_db(ticker, metrics, score, status):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # [수정] 모든 지표를 저장하도록 쿼리 확장
        query = """
        INSERT INTO sts_live_targets 
        (ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status, 
         obi_mom, tick_accel, vwap_slope, squeeze_ratio, rvol, atr, pump_accel, spread, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 
                %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (ticker) DO UPDATE SET
            price = EXCLUDED.price,
            ai_score = EXCLUDED.ai_score,
            obi = EXCLUDED.obi,
            vpin = EXCLUDED.vpin,
            tick_speed = EXCLUDED.tick_speed,
            vwap_dist = EXCLUDED.vwap_dist,
            status = EXCLUDED.status,
            
            obi_mom = EXCLUDED.obi_mom,
            tick_accel = EXCLUDED.tick_accel,
            vwap_slope = EXCLUDED.vwap_slope,
            squeeze_ratio = EXCLUDED.squeeze_ratio,
            rvol = EXCLUDED.rvol,
            atr = EXCLUDED.atr,
            pump_accel = EXCLUDED.pump_accel,
            spread = EXCLUDED.spread,
            
            last_updated = NOW();
        """
        
        # metrics 딕셔너리에서 안전하게 값 추출 (없으면 0)
        cursor.execute(query, (
            ticker, 
            float(metrics.get('last_price', 0)), 
            float(score), 
            float(metrics.get('obi', 0)), 
            float(metrics.get('vpin', 0)), 
            int(metrics.get('tick_speed', 0)), 
            float(metrics.get('vwap_dist', 0)), 
            status,
            # [추가된 데이터 매핑]
            float(metrics.get('obi_mom', 0)),
            float(metrics.get('tick_accel', 0)),
            float(metrics.get('vwap_slope', 0)),
            float(metrics.get('squeeze_ratio', 0)),
            float(metrics.get('rvol', 0)),
            float(metrics.get('atr', 0)),
            float(metrics.get('pump_accel', 0)),
            float(metrics.get('spread', 0))
        ))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ DB Update Error: {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# [수정] 상세 매매 전략을 DB에 기록
def log_signal_to_db(ticker, price, score, entry=0, tp=0, sl=0, strategy=""):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 컬럼이 늘어난 버전에 맞춰 Insert
        query = """
            INSERT INTO signals (ticker, price, score, entry, tp, sl, strategy, time) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(query, (
            ticker, float(price), float(score), 
            float(entry), float(tp), float(sl), 
            strategy, datetime.now()
        ))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [DB Signal Error] {e}", flush=True)
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# [수정된 알림 전송 함수] 로그 기능 강화 (기존 로직 유지)
def _send_fcm_sync(ticker, price, probability_score, entry=None, tp=None, sl=None):
    # 1. Firebase 초기화 체크
    if not firebase_admin._apps:
        print(f"⚠️ [FCM] Firebase not initialized. Skipping alert for {ticker}.", flush=True)
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        
        # 구독자가 없으면 로그 남기고 종료
        if not subscribers:
            print(f"⚠️ [FCM] No subscribers found. Skipping alert for {ticker}.", flush=True)
            db_pool.putconn(conn)
            return

        # 2. 알림 내용 구성 (기존 디자인 유지)
        if probability_score >= 90: icon = "💎 ELITE"
        elif probability_score >= 80: icon = "🔥 HOT"
        else: icon = "✅ VALID"

        noti_title = f"{icon} {ticker} 포착! (점수: {probability_score})"
        
        if entry and tp and sl:
            risk = entry - sl
            reward = tp - entry
            rr = reward / risk if risk > 0 else 0
            noti_body = (
                f"Entry: ${entry:.3f}\n"
                f"🎯 TP: ${tp:.3f} | 🛡️ SL: ${sl:.3f}\n"
                f"⚖️ 손익비 1:{rr:.1f}"
            )
        else:
            noti_body = f"현재가: ${price:.4f} | AI 확신도: {probability_score}%"

        data_payload = {
            'type': 'signal', 'ticker': ticker, 
            'price': str(price), 'score': str(probability_score), 
            'title': noti_title, 'body': noti_body
        }
        
        # 3. [로그 추가] 전송 시작 알림 (몇 명에게 보내는지 확인)
        print(f"🔔 [FCM] Sending alert for {ticker} to {len(subscribers)} devices...", flush=True)

        success_count = 0
        failed_tokens = []
        
        # 4. 전송 루프
        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            # 사용자 설정 점수 미달 시 스킵 (로그는 너무 많아질 수 있으니 생략)
            if probability_score < user_min_score: continue

            try:
                message = messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=noti_title, body=noti_body),
                    data=data_payload,
                    android=messaging.AndroidConfig(
                        priority='high', 
                        notification=messaging.AndroidNotification(
                            channel_id='high_importance_channel', 
                            priority='high', 
                            default_sound=True, 
                            visibility='public'
                        )
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(alert=messaging.ApsAlert(title=noti_title, body=noti_body), sound="default"))
                    )
                )
                messaging.send(message)
                success_count += 1
            except Exception as e:
                # [로그 추가] 전송 실패 시 구체적 에러 출력
                print(f"❌ [FCM Fail] Token: {token[:10]}... Error: {e}", flush=True)
                
                # 토큰 만료 에러 등은 삭제 대상에 추가
                if "Requested entity was not found" in str(e) or "registration-token-not-registered" in str(e): 
                    failed_tokens.append(token)
        
        # 5. [로그 추가] 최종 결과 리포트
        if success_count > 0:
            print(f"✅ [FCM] Successfully sent to {success_count} devices.", flush=True)
        else:
            # 보낼 대상이 있었는데 성공이 0이면 문제 상황
            print(f"⚠️ [FCM] Zero success. Check tokens, network, or user min_score filters.", flush=True)

        # 만료된 토큰 DB 삭제 처리
        if failed_tokens:
            c = conn.cursor()
            c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            c.close()
            print(f"🗑️ [FCM] Cleaned up {len(failed_tokens)} invalid tokens.", flush=True)

    except Exception as e:
        print(f"❌ [FCM Critical Error] {e}", flush=True)
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

async def send_fcm_notification(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """[V9.2] 알림 전용 쓰레드 풀 사용"""
    loop = asyncio.get_running_loop()
    
    # [수정] NOTI_WORKER_POOL 사용
    await loop.run_in_executor(
        NOTI_WORKER_POOL, 
        partial(_send_fcm_sync, ticker, price, probability_score, entry, tp, sl)
    )

# ==============================================================================
# 3. CORE CLASSES (Analyzer, Selector, Bot)
# ==============================================================================

class DataLogger:
    def __init__(self):
        self.trade_file = TRADE_LOG_FILE
        self.replay_file = REPLAY_LOG_FILE
        self._init_files()

    def _init_files(self):
        if not os.path.exists(self.trade_file):
            with open(self.trade_file, 'w', newline='') as f:
                csv.writer(f).writerow([
                    'timestamp', 'ticker', 'action', 'price', 'ai_prob', 
                    'obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist', 'profit_pct'
                ])
        if not os.path.exists(self.replay_file):
            with open(self.replay_file, 'w', newline='') as f:
                # [V5.3] vwap, atr 필드 추가
                csv.writer(f).writerow([
                    'timestamp', 'ticker', 'price', 'vwap', 'atr',
                    'obi', 'tick_speed', 'vpin', 'ai_prob'
                ])

    def log_trade(self, data):
        with open(self.trade_file, 'a', newline='') as f:
            csv.writer(f).writerow([
                datetime.now().strftime('%H:%M:%S.%f')[:-3],
                data['ticker'], data['action'], data['price'], 
                f"{data.get('ai_prob', 0):.4f}",
                f"{data.get('obi', 0):.2f}", f"{data.get('obi_mom', 0):.2f}",
                f"{data.get('tick_accel', 0):.1f}", f"{data.get('vpin', 0):.2f}",
                f"{data.get('vwap_dist', 0):.2f}", f"{data.get('profit', 0):.2f}%"
            ])

    def log_replay(self, data):
        with open(self.replay_file, 'a', newline='') as f:
            # [V5.3] vwap, atr 저장
            csv.writer(f).writerow([
                data['timestamp'], data['ticker'], data['price'], 
                f"{data.get('vwap', 0):.4f}", f"{data.get('atr', 0):.4f}",
                f"{data.get('obi', 0):.2f}", data.get('tick_speed', 0),
                f"{data.get('vpin', 0):.2f}", f"{data.get('ai_prob', 0):.4f}"
            ])

class MicrostructureAnalyzer:
    def __init__(self):
        self.raw_ticks = deque(maxlen=3000) 
        self.quotes = {'bids': [], 'asks': []}
        self.prev_tick_speed = 0
        self.prev_obi = 0

    def inject_history(self, aggs):
        """Polygon 1초봉 데이터를 있는 그대로 주입 (가상 변환 X)"""
        if not aggs: return
        
        # 시간순 정렬
        aggs.sort(key=lambda x: x['t'])
        
        for bar in aggs:
            ts = pd.to_datetime(bar['t'], unit='ms')
            
            # 1초봉(Agg) 하나를 하나의 '틱'처럼 그대로 사용
            # 이렇게 하면 VWAP, 볼린저 밴드 계산 시 왜곡 없이 정확함
            self.raw_ticks.append({
                't': ts,
                'p': bar['c'],       # 종가(Close)를 기준 가격으로 사용
                's': bar.get('v', 0), # 거래량(Volume)
                'bid': bar['c'] - 0.01, 
                'ask': bar['c'] + 0.01
            })
            
        print(f"📥 [Analyzer] History Loaded: {len(aggs)} seconds of data ready.", flush=True)

    def update_tick(self, tick_data, current_quotes):
        best_bid = current_quotes['bids'][0]['p'] if current_quotes['bids'] else 0
        best_ask = current_quotes['asks'][0]['p'] if current_quotes['asks'] else 0
        
        self.raw_ticks.append({
            't': pd.to_datetime(tick_data['t'], unit='ms'),
            'p': tick_data['p'], 's': tick_data['s'], 'bid': best_bid, 'ask': best_ask
        })
        self.quotes = current_quotes

    def _resample_ohlc(self):
        if len(self.raw_ticks) < 10: return None
        df = pd.DataFrame(self.raw_ticks).set_index('t')
        ohlcv = df['p'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
        volume = df['s'].resample('1s').sum()
        tick_count = df['s'].resample('1s').count()
        
        df_res = pd.concat([ohlcv, volume, tick_count], axis=1).iloc[-600:]
        df_res.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
        df_res = df_res.ffill().fillna(0)
        return df_res.dropna()
    def get_metrics(self):
        # 1. 데이터 검증 (최소 5개 틱 필요)
        if len(self.raw_ticks) < 5: return None
        
        # 2. DataFrame 생성 (여기서 df가 처음 만들어짐)
        df = pd.DataFrame(self.raw_ticks).set_index('t')
        
        # 1초봉 리샘플링
        ohlcv = df['p'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
        volume = df['s'].resample('1s').sum()
        tick_count = df['s'].resample('1s').count()
        
        # 데이터 합치기
        df_res = pd.concat([ohlcv, volume, tick_count], axis=1).iloc[-600:]
        df_res.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
        
        # 결측치 채우기 (ffill -> fillna)
        df = df_res.ffill().fillna(0)
        
        # 다시 한번 검증
        if len(df) < 5: return None 
        
        try:
            # --- [Phase 5] 윈도우 사이즈 설정 ---
            WIN_MAIN = 60      # 1분
            WIN_SQZ = 30       # 30초
            WIN_SLOPE = 5      # 5초

            # --- [지표 계산 시작] ---
            
            # 1. VWAP 계산
            v = df['volume'].values
            p = df['close'].values
            df['vwap'] = (p * v).cumsum() / (v.cumsum() + 1e-9)
            df['vwap'] = df['vwap'].ffill() 
            
            # 2. VWAP 기울기
            df['vwap_slope'] = (df['vwap'].diff(WIN_SLOPE) / (df['vwap'].shift(WIN_SLOPE) + 1e-9)) * 10000
            
            # 3. RVOL (상대 거래량)
            df['vol_ma'] = df['volume'].rolling(WIN_MAIN).mean()
            df['rvol'] = df['volume'] / (df['vol_ma'] + 1e-9)
            
            # 4. Squeeze (볼린저 밴드)
            rolling_mean = df['close'].rolling(WIN_SQZ).mean()
            rolling_std = df['close'].rolling(WIN_SQZ).std()
            df['bb_width'] = (rolling_std * 4) / df['close']
            df['squeeze_ratio'] = df['bb_width'] / (df['bb_width'].rolling(WIN_SQZ).mean() + 1e-9)
            
            # 5. Pump Accel (가속도)
            df['pump_5m'] = df['close'].pct_change(300)
            df['pump_accel'] = df['pump_5m'].diff(60)
            
            # 6. Tick Accel (틱 속도 변화량)
            df['tick_accel'] = df['tick_speed'].diff().fillna(0)

            # 7. ATR (변동성)
            prev_close = df['close'].shift(1)
            tr1 = df['high'] - df['low']
            tr2 = (df['high'] - prev_close).abs()
            tr3 = (df['low'] - prev_close).abs()
            df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            df['atr'] = df['tr'].rolling(WIN_MAIN).mean()
            
            # --- [Phase 8: AI용 추가 지표 복구] ---
            
            # 8. RV_60 (실현 변동성)
            log_ret = np.log(df['close'] / df['close'].shift(1))
            df['rv_60'] = log_ret.rolling(60).std() * np.sqrt(60) * 100

            # 9. Fibo Pos (위치값)
            rolling_high = df['high'].rolling(600).max()
            rolling_low = df['low'].rolling(600).min()
            rng = rolling_high - rolling_low
            df['fibo_pos'] = (df['close'] - rolling_low) / (rng + 1e-9)
            
            # NaN 제거 및 마지막 값 추출
            df = df.fillna(0)
            last = df.iloc[-1]

            # --- [OBI & VPIN 계산] ---
            bids = np.array([q['s'] for q in self.quotes.get('bids', [])[:OBI_LEVELS]])
            asks = np.array([q['s'] for q in self.quotes.get('asks', [])[:OBI_LEVELS]])
            bid_vol = np.sum(bids) if len(bids) > 0 else 0
            ask_vol = np.sum(asks) if len(asks) > 0 else 0
            obi = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0
            
            obi_mom = obi - self.prev_obi
            self.prev_obi = obi
            
            # VPIN (100틱 샘플링)
            raw_df = pd.DataFrame(list(self.raw_ticks)[-100:])
            if not raw_df.empty:
                buy_vol = raw_df[raw_df['p'] >= raw_df['ask']]['s'].sum()
                sell_vol = raw_df[raw_df['p'] <= raw_df['bid']]['s'].sum()
                total_vol = buy_vol + sell_vol
                vpin = abs(buy_vol - sell_vol) / total_vol if total_vol > 0 else 0
            else:
                vpin = 0

            # VWAP 거리 & 스프레드
            vwap_dist = (last['close'] - last['vwap']) / last['vwap'] * 100 if last['vwap'] > 0 else 0
            
            best_bid = self.raw_ticks[-1]['bid']
            best_ask = self.raw_ticks[-1]['ask']
            spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0

            # --- [최종 리턴] ---
            return {
                'obi': obi, 
                'obi_mom': obi_mom, 
                'tick_accel': last['tick_accel'],
                'vpin': vpin, 
                'vwap_dist': vwap_dist,
                'vwap_slope': last['vwap_slope'],
                'rvol': last['rvol'],
                'squeeze_ratio': last['squeeze_ratio'],
                'pump_accel': last['pump_accel'],
                'atr': last['atr'] if last['atr'] > 0 else last['close'] * 0.005,
                'spread': spread, 
                'last_price': last['close'], 
                'tick_speed': last['tick_speed'], 
                'timestamp': raw_df.iloc[-1]['t'] if not raw_df.empty else pd.Timestamp.now(), 
                'vwap': last['vwap'],
                
                # AI용 추가 지표
                'rv_60': last['rv_60'],
                'fibo_pos': last['fibo_pos'],
                'bb_width_norm': last['squeeze_ratio']
            }
            
        except Exception as e:
            import traceback
            # print(f"❌ [Metric Calc Error] {e}", flush=True) # 너무 시끄러우면 주석 처리
            return None
    
class TargetSelector:
    def __init__(self):
        self.snapshots = {} 
        self.last_gc_time = time.time()
        # [NEW] 시장 평균 거래량 추적용 (RVOL 대용)
        self.market_vol_tracker = defaultdict(float)

    def update(self, agg_data):
        t = agg_data['sym']
        # 데이터 수신
        if t not in self.snapshots: 
            self.snapshots[t] = {
                'o': agg_data['o'], 'h': agg_data['h'], 'l': agg_data['l'], 
                'c': agg_data['c'], 'v': 0, 'vwap': agg_data.get('vw', agg_data['c']),
                'start_price': agg_data['o'], 
                'last_updated': time.time()
            }
        
        d = self.snapshots[t]
        d['c'] = agg_data['c']
        d['h'] = max(d['h'], agg_data['h'])
        d['l'] = min(d['l'], agg_data['l'])
        d['v'] += agg_data['v'] # 누적 거래량
        d['vwap'] = agg_data.get('vw', d['c']) # VWAP 업데이트
        d['last_updated'] = time.time()

    def get_atr(self, ticker):
        if ticker in self.snapshots:
            d = self.snapshots[ticker]
            return (d['h'] - d['l']) * 0.1 
        return 0.05

    # 🔥 [추가된 기능] DB 저장 메소드 (이게 없어서 UI가 안 떴던 것임)
    def save_candidates_to_db(self, candidates):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 현재 감지된 Top 10을 DB에 갱신
            for t, score, change, vol in candidates:
                d = self.snapshots.get(t)
                if not d: continue
                
                # status를 'SCANNING'으로 저장하여 UI가 후보군임을 알게 함
                query = """
                INSERT INTO sts_live_targets 
                (ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status, last_updated)
                VALUES (%s, %s, %s, 0, 0, 0, 0, 'SCANNING', NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    price = EXCLUDED.price,
                    ai_score = EXCLUDED.ai_score,
                    day_change = EXCLUDED.day_change, -- [중요] 등락률 갱신
                    last_updated = NOW()
                WHERE sts_live_targets.status != 'FIRED'; -- 이미 발사된 건 건드리지 않음
                """
                cursor.execute(query, (t, float(d['c']), float(score))) 
            
            conn.commit()
            cursor.close()
        except Exception as e:
            print(f"❌ [DB Save Error] {e}", flush=True)
            if conn: conn.rollback()
        finally:
            if conn: db_pool.putconn(conn)

    # [핵심 수정] 3분 주기: Scanner가 쓰레기 종목을 DB에 넣지 않도록 수정
    def get_top_gainers_candidates(self, limit=10):
        scored = []
        now = time.time()
        
        # 1. 전체 스캔
        for t, d in self.snapshots.items():
            # 죽은 데이터(1분 이상 갱신 없는 놈) 가차 없이 제외
            if now - d['last_updated'] > 60: continue 
            
            # [Filter 1] Price Cap: $50 이하
            if d['c'] > STS_MAX_PRICE: continue
            
            # [Filter 2] Liquidity Floor: 거래대금 필터 (빡세게 수정)
            # 기존 STS_MIN_DOLLAR_VOL 변수 대신 30,000달러(약 4천만원)로 고정
            dollar_vol = d['c'] * d['v']
            if dollar_vol < 30000: continue 

            # [Score Logic] 등락률 확인
            change_pct = (d['c'] - d['start_price']) / d['start_price'] * 100
            if change_pct < 1.0: continue # 1%도 안 오른 놈은 취급 안 함

            # 점수 산정
            score = change_pct * np.log1p(dollar_vol)
            scored.append((t, score, change_pct, dollar_vol))
        
        # 점수 내림차순 정렬
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Top 10 추출
        top_list = scored[:limit]

        # 🔥 [핵심 수정] 여기서 DB 저장을 하지 않습니다!
        # self.save_candidates_to_db(top_list)  <-- 이 줄을 삭제했습니다.
        # 이유: 여기서 저장하면 데이터(Tick)가 없는 놈도 화면에 떠서 0.00으로 도배됨.
        
        if top_list:
            print(f"🔎 [Scanner] Candidates Found: {len(top_list)} items (DB Save Skipped)", flush=True)

        return [x[0] for x in top_list]

    # [수정] 1분 주기: 후보군 중 거래량 가속도(Volume Velocity) Top 3 선정
    def get_best_snipers(self, candidates, limit=3):
        scored = []
        for t in candidates:
            if t not in self.snapshots: continue
            d = self.snapshots[t]
            dollar_vol = d['c'] * d['v']
            scored.append((t, dollar_vol))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in scored[:limit]]

    def garbage_collect(self):
        now = time.time()
        if now - self.last_gc_time < GC_INTERVAL: return
        to_remove = [t for t, d in self.snapshots.items() if now - d['last_updated'] > GC_TTL]
        for t in to_remove: del self.snapshots[t]
        self.last_gc_time = now

class SniperBot:
    def __init__(self, ticker, logger, selector, shared_model):
        self.ticker = ticker
        self.logger = logger
        self.selector = selector
        self.model = shared_model 
        self.analyzer = MicrostructureAnalyzer()
        self.state = "WATCHING"
        self.vwap = 0
        self.atr = 0.05 
        self.position = {} 
        self.prob_history = deque(maxlen=5)
        self.last_db_update = 0
        self.last_logged_state = "WATCHING"
        # [추가] Phase 2-2: 마이크로 테스트용 타이머
        self.aiming_start_time = 0
        self.aiming_start_price = 0

        # [추가] Phase 2: SoftGate 스코어링 (전략적 판단 로직)
    def calculate_soft_gate(self, m):
        score = 0
        reasons = []
        
        # [Phase 9] 뉴욕 시간(US/Eastern) 기준 개장 초반 체크 (서버 위치 무관)
        try:
            ny_tz = pytz.timezone('US/Eastern')
            ny_now = datetime.now(ny_tz).time()
            # 09:30 ~ 10:00 사이를 개장 초반(Volatility Zone)으로 정의
            is_market_open = (9 <= ny_now.hour < 10) and (ny_now.minute >= 30 or ny_now.hour > 9)
        except:
            # 시간대 라이브러리 에러 시 보수적으로 False 처리
            is_market_open = False
        
        # 1. 💥 Squeeze (에너지 응축)
        # [수정] 분석관 제안: < 0.8(30점), < 1.0(20점) 으로 단계화
        if m['squeeze_ratio'] <= 0.8:
            score += 30; reasons.append("Super Squeeze")
        elif m['squeeze_ratio'] <= 1.0:
            score += 20; reasons.append("Squeeze Ready")
        elif m['squeeze_ratio'] > 2.0:
            score -= 20; reasons.append("Over Extended")

        # 2. 🌊 RVOL (거래량의 질) - [수정] 개장 초반 완화 로직
        rvol_threshold = 2.0 if is_market_open else 3.0
        
        if m['rvol'] > rvol_threshold:
            score += 20; reasons.append("Volume Spike")
        elif m['rvol'] < 1.0:
            score -= 10; reasons.append("Low Volume")

        # 3. 🎯 VWAP Support (지지력)
        if 0 < m['vwap_dist'] < 3.0 and m['vwap_slope'] > 0:
            score += 25; reasons.append("Healthy Trend")
        elif m['vwap_dist'] < -1.0:
            score -= 10; reasons.append("Below VWAP")

        # 4. 🚀 Acceleration (가속도)
        if m['pump_accel'] > 0:
            score += 15
        elif m['pump_accel'] < 0:
            score -= 15

        return score, reasons

    def update_dashboard_db(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        
        if agg_data and agg_data.get('vwap'): self.vwap = agg_data.get('vwap')
        if self.vwap == 0: self.vwap = tick_data['p']

        m = self.analyzer.get_metrics()
        
        # ==========================================================
        # 🔥 [여기부터 수정] 입구컷 필터 적용 (쓰레기 데이터 차단)
        # ==========================================================
        
        # 1. 데이터가 없거나(None), 거래가 아예 없는(tick_speed=0) 시체는 즉시 리턴
        # -> 이러면 Warm-up 중이거나 거래량 없는 종목은 화면(DB)에 절대 안 뜸
        if not m or m['tick_speed'] == 0:
            return 

        # 2. VPIN(독성)이 0.8 넘는 설거지 종목도 즉시 리턴
        # -> 화면에 띄워봤자 어차피 안 살 거니까 리소스 낭비 방지
        if m['vpin'] > 0.8:
            return
        
        # ATR 정밀 업데이트
        if m.get('atr') and m['atr'] > 0:
            self.atr = m['atr']
        else:
            self.atr = max(self.selector.get_atr(self.ticker), tick_data['p'] * 0.01)

        # 기본 필터
        is_bad_spread = m['spread'] > STS_MAX_SPREAD_ENTRY 
        is_low_vol = m['rvol'] < 1.0 

        # AI 예측 (예외처리 포함)
        prob = 0.0
        if self.model:
            try:
                features = [
                    m['obi'], 
                    m['obi_mom'], 
                    m['tick_accel'], # 이제 0 아님
                    m['vpin'], 
                    m['vwap_dist'],
                    m['fibo_pos'],   # 이제 0 아님 (계산됨)
                    abs(m['fibo_pos'] - 0.382), # fibo_dist_382 (즉석 계산)
                    m['bb_width_norm'],         # squeeze_ratio와 동일값
                    1 if m['squeeze_ratio'] < 0.7 else 0, # squeeze_flag
                    m['rv_60'],      # 이제 0 아님 (계산됨)
                    m['rvol']        # vol_ratio_60 대체
                ]
                features = [0 if (np.isnan(x) or np.isinf(x)) else x for x in features]
                dtest = xgb.DMatrix(np.array([features]), feature_names=[
                    'obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist',
                    'fibo_pos', 'fibo_dist_382', 'bb_width_norm', 'squeeze_flag', 'rv_60', 'vol_ratio_60'
                ])
                raw_prob = self.model.predict(dtest)[0]
                self.prob_history.append(raw_prob)
                prob = sum(self.prob_history) / len(self.prob_history)
            except Exception: pass

       # --- [VRAX 방지 및 정밀 타격 로직 적용] -----------------------

        # 1. [Critical] Ghost Signal Filter (유령 신호 즉시 차단)
        # 틱 속도가 2 미만이면 분석 가치가 없으므로 즉시 0점 처리하고 리턴
        if m['tick_speed'] < 2:
            self.state = "WATCHING"
            # 대시보드 0점 갱신 (상태: DEAD_ZONE)
            asyncio.get_running_loop().run_in_executor(
                DB_WORKER_POOL, 
                partial(update_dashboard_db, self.ticker, copy.deepcopy(m), 0, "DEAD_ZONE")
            )
            return

        # 2. [Advanced] VPIN Confidence Factor (신뢰도 계수 적용)
        # 거래가 활발할수록(Tick Speed >= 5) VPIN을 100% 신뢰, 그 미만이면 신뢰도 깎음
        vpin_confidence = min(1.0, m['tick_speed'] / 5.0)
        
        # 3. 정량 점수 계산 (Confidence 반영)
        quant_score, reasons = self.calculate_soft_gate(m)
        quant_score *= vpin_confidence # 거래량 적으면 정량 점수도 낮춤

        ai_score = prob * 100
        final_score = 0
        
        # 4. [Core] Event-Driven Warm-up (데이터 개수 기반)
        # 3분치 데이터를 가져왔어도 실제 틱이 50개 미만이면 "데이터 부족"으로 판단
        data_count = len(self.analyzer.raw_ticks)
        
        if data_count < 50:
            final_score = 0 # 원칙적으로 0점
            
            # 예외: RVOL이 5배 이상 폭발하는 극초반 펌프는 AI 점수 절반 인정
            if m['rvol'] > 5.0:
                final_score = ai_score * 0.5
            else:
                if "Insufficient Data" not in reasons: reasons.append("Insufficient Data")
                self.state = "WARM_UP"
        else:
            # 데이터 충분 시: 정상적인 하이브리드 점수 산출
            final_score = (ai_score * 0.6) + (quant_score * 0.4)
            
        # ----------------------------------------------------------- 

        # [SniperBot.update_dashboard_db 내부]
        now = time.time()
        if (self.state != self.last_logged_state) or (now - self.last_db_update > 1.5):
            try:
                # [FIX] m(metrics) 딕셔너리를 deepcopy하여 스레드 충돌 방지
                metrics_copy = copy.deepcopy(m) 
                
                asyncio.get_running_loop().run_in_executor(
                    DB_WORKER_POOL, 
                    partial(update_dashboard_db, self.ticker, metrics_copy, final_score, self.state)
                )
            except Exception as e:
                print(f"⚠️ [DB Async Error] {e}")
            
            self.last_db_update = now
            self.last_logged_state = self.state

        # [수정 후 코드] 점수는 높은데 필터에 걸린 경우, 이유를 로그로 출력
        if self.state != "FIRED":
            # 1. VPIN(독성) 필터
            if m['vpin'] > STS_MAX_VPIN:
                # 점수가 80점 이상인데 안 샀다면 이유를 출력 (로그 스팸 방지 위해 고득점만 표시)
                if final_score >= 80:
                    print(f"🛡️ [FILTER] {self.ticker} Score:{final_score:.0f} but VPIN:{m['vpin']:.2f} (Too Toxic) -> Skipped", flush=True)
                return

            # 2. Spread(호가 공백) 필터
            if is_bad_spread:
                if final_score >= 80:
                    print(f"🛡️ [FILTER] {self.ticker} Score:{final_score:.0f} but Spread:{m['spread']:.2f}% (Too Wide) -> Skipped", flush=True)
                return

            # 3. RVOL(거래량) 필터
            if is_low_vol:
                # 거래량 부족은 흔하므로 로그 생략하거나 필요하면 추가
                return

        self.logger.log_replay({
            'timestamp': m['timestamp'], 'ticker': self.ticker, 'price': m['last_price'], 
            'vwap': self.vwap, 'atr': self.atr, 'obi': m['obi'], 
            'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 'ai_prob': prob
        })

        # ==================================================================
        # [Phase 6] Fast-Track 안전장치 및 진입 로직
        # ==================================================================
        
        if self.state == "WATCHING":
            if final_score >= 65 and m['tick_accel'] > 0:
                self.state = "AIMING"

        elif self.state == "AIMING":
            # 1. [수정된 Fast-Track] "거래량 폭발 + 안전장치" 
            # - RVOL > 5.0 (기존)
            # - 점수 80 이상 (기존)
            # - [NEW] 현재가가 VWAP보다 1% 이상 위 (확실한 상승 추세)
            # - [NEW] 스프레드가 0.5% 미만 (호가 공백 없음)
            is_safe_pump = (m['last_price'] > m['vwap'] * 1.01) and (m['spread'] < 0.5)
            
            if m['rvol'] > 5.0 and final_score >= 80 and is_safe_pump:
                print(f"⚡ [FAST-TRACK] {self.ticker} RVOL:{m['rvol']:.1f} / SafePump:OK -> 즉시 진입!")
                self.fire(m['last_price'], prob, m)
                return

            # 2. 마이크로 테스트
            if self.aiming_start_time == 0:
                self.aiming_start_time = time.time()
                self.aiming_start_price = m['last_price']
                return 

            # 3. 검증: 가격 밀리면 탈락
            price_change = (m['last_price'] - self.aiming_start_price) / self.aiming_start_price * 100
            if price_change < -0.2:
                self.state = "WATCHING"
                self.aiming_start_time = 0
                return

            # 4. 0.5초 대기 후 진입
            elapsed = time.time() - self.aiming_start_time
            if elapsed >= 0.5:
                if final_score >= 80: 
                    self.fire(m['last_price'], prob, m)
                else:
                    self.state = "WATCHING"
                    self.aiming_start_time = 0

        elif self.state == "FIRED":
            self.manage_position(m['last_price'])
    
    async def warmup(self):
        """최근 3분간의 1초 봉 데이터를 가져와서 분석기를 예열함"""
        print(f"🔥 [Warmup] Fetching history for {self.ticker}...", flush=True)
        try:
            # 현재 시간 기준 3분 전부터 조회
            to_ts = int(time.time() * 1000)
            from_ts = to_ts - (180 * 1000) 
            
            url = f"https://api.polygon.io/v2/aggs/ticker/{self.ticker}/range/1/second/{from_ts}/{to_ts}"
            params = {
                "adjusted": "true",
                "sort": "asc",
                "limit": 500,
                "apiKey": POLYGON_API_KEY
            }
            
            # [수정] 여기서부터 들여쓰기가 try 안쪽으로 들어와야 합니다.
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if 'results' in data and data['results']:
                        # 분석기에 주입
                        self.analyzer.inject_history(data['results'])
                        print(f"✅ [Warmup] {self.ticker} Ready! ({len(data['results'])} bars loaded)", flush=True)
                    else:
                        print(f"⚠️ [Warmup] No history data for {self.ticker}", flush=True)
                else:
                    print(f"❌ [Warmup] API Error: {resp.status_code}", flush=True)
                    
        except Exception as e:
            print(f"❌ [Warmup] Failed: {e}", flush=True)

    # [수정] 동적 TP/SL 계산 로직 적용
    def fire(self, price, prob, metrics):
        print(f"🔫 [FIRE] {self.ticker} AI_Prob:{prob:.4f} Price:${price:.4f}", flush=True)
        self.state = "FIRED"
        
        # [Phase 2-2] 상황별 목표가 보정 (Dynamic Targeting)
        # Squeeze가 0.6 미만(초압축)이고 가속도가 붙었으면 '대박'을 노림 -> 익절폭 2.0배
        is_super_setup = (metrics.get('squeeze_ratio', 1.0) < 0.7) and \
                         (metrics.get('pump_accel', 0) > 0.3)
        
        tp_mult = 2.5 if is_super_setup else ATR_TRAIL_MULT
        sl_mult = 0.5                             # 손절은 0.5배 (타이트하게)
        
        # 진입가/익절가/손절가 계산
        tp_price = price + (self.atr * tp_mult)
        sl_price = price - (self.atr * sl_mult)

        self.position = {
            'entry': price, 'high': price,
            'sl': sl_price,
            'atr': self.atr
        }
        
        # [수정] DB 저장을 DB 전용 쓰레드 풀로 처리
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                DB_WORKER_POOL, 
                partial(log_signal_to_db, 
                        self.ticker, price, prob*100, 
                        entry=price, tp=tp_price, sl=sl_price, strategy="SoftGate")
            )
        except Exception as e:
            print(f"⚠️ [DB Async Error] {e}")
        
        # 알림 전송 (이미 비동기 태스크)
        asyncio.create_task(send_fcm_notification(
            self.ticker, price, int(prob*100), 
            entry=price, tp=tp_price, sl=sl_price
        ))
        
        self.logger.log_trade({
            'ticker': self.ticker, 'action': 'ENTRY', 'price': price, 'ai_prob': prob,
            'obi': metrics['obi'], 'obi_mom': metrics['obi_mom'],
            'tick_accel': metrics['tick_accel'], 'vpin': metrics['vpin'], 
            'vwap_dist': metrics['vwap_dist'], 'profit': 0
        })

    def manage_position(self, curr_price):
        pos = self.position
        if curr_price > pos['high']: pos['high'] = curr_price
            
        exit_price = pos['high'] - (pos['atr'] * ATR_TRAIL_MULT)
        profit_pct = (curr_price - pos['entry']) / pos['entry'] * 100

        if curr_price < max(exit_price, pos['sl']):
            print(f"💰 [청산] {self.ticker} Profit: {profit_pct:.2f}%", flush=True)
            self.state = "WATCHING"
            self.position = {}
            self.logger.log_trade({
                'ticker': self.ticker, 'action': 'EXIT', 'price': curr_price,
                'ai_prob': 0, 'obi': 0, 'obi_mom': 0, 'tick_accel': 0, 'vpin': 0,
                'vwap_dist': 0, 'profit': profit_pct
            })

# ==============================================================================
# 4. PIPELINE MANAGER
# ==============================================================================
class STSPipeline:
    def __init__(self):
        self.selector = TargetSelector()
        self.snipers = {}       # 현재 활성 Top 3 봇
        self.candidates = []    # Top 10 후보군 리스트
        self.last_quotes = {}
        
        # [수정 1] ★핵심★: 마지막 Agg(A) 데이터를 저장할 공간 초기화
        # (이게 없으면 T 이벤트가 들어올 때 VWAP 계산을 못함)
        self.last_agg = {}      
        
        self.logger = DataLogger()
        
        # 수신과 처리를 분리할 큐 생성
        self.msg_queue = asyncio.Queue(maxsize=100000)
        
        self.shared_model = None
        if os.path.exists(MODEL_FILE):
            print(f"🤖 [System] Loading AI Model: {MODEL_FILE}", flush=True)
            try:
                self.shared_model = xgb.Booster()
                self.shared_model.load_model(MODEL_FILE)
            except Exception as e: print(f"❌ Load Error: {e}")

    # [1] 구독 요청 함수
    async def subscribe(self, ws, params):
        try:
            if isinstance(params, list): params_str = ",".join(params)
            else: params_str = params
            req = {"action": "subscribe", "params": params_str}
            await ws.send(json.dumps(req))
            print(f"📡 [Sub] Request sent: {params_str}", flush=True)
        except Exception as e: print(f"❌ [Sub Error] {e}", flush=True)

    # [2] 구독 취소 함수
    async def unsubscribe(self, ws, params):
        try:
            if isinstance(params, list): params_str = ",".join(params)
            else: params_str = params
            req = {"action": "unsubscribe", "params": params_str}
            await ws.send(json.dumps(req))
            print(f"🔕 [Unsub] Request sent: {params_str}", flush=True)
        except Exception as e: print(f"❌ [Unsub Error] {e}", flush=True)

    # [3] 메인 연결 함수
    async def connect(self):
        init_db()
        init_firebase()

        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM fcm_tokens")
            count = cur.fetchone()[0]
            print(f"📱 [System] Registered FCM Tokens: {count} devices", flush=True)
            if count == 0:
                print("⚠️ [Warning] No devices registered! Notifications will not be sent.", flush=True)
            cur.close()
            db_pool.putconn(conn)
        except Exception as e:
            print(f"⚠️ [System] Token check failed: {e}", flush=True)
        
        if not POLYGON_API_KEY:
            print("❌ [CRITICAL] POLYGON_API_KEY가 없습니다!", flush=True)
            while True: await asyncio.sleep(60)

        while True:
            try:
                # [변경점] ping_interval 인자를 제거했습니다. (기본값 사용)
                # 대신 뒤에서 manual_keepalive가 강제로 핑을 쏴줄 겁니다.
                    # [수정됨] 고성능 데이터 수신을 위한 웹소켓 설정
                async with websockets.connect(
                    WS_URI,
                    ping_interval=None,   # 1. 자동 Ping 비활성화 (가장 중요!)
                    ping_timeout=180,     # 2. 서버가 침묵해도 기다리는 시간 늘림
                    max_queue=None,       # 3. 수신 버퍼 크기 제한 해제
                    close_timeout=10      # 4. 종료 시 대기 시간
                ) as ws:                  
                    print("✅ [STS V5.3] Pipeline Started with Heartbeat", flush=True)
                    
                    await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                    _ = await ws.recv()

                    # [추가] 심폐소생술 태스크 시작 (이 줄은 꼭 유지하세요!)
                    asyncio.create_task(self.manual_keepalive(ws))

                    # 초기 구독: 전체 Agg(A.*) 구독
                    await self.subscribe(ws, ["A.*"])

                    # 태스크 실행
                    asyncio.create_task(self.worker())
                    asyncio.create_task(self.task_global_scan())
                    asyncio.create_task(self.task_focus_manager(ws))

                    # 메인 루프: 데이터 수신 (Producer 호출)
                    await self.producer(ws)

            except (websockets.ConnectionClosed, asyncio.TimeoutError):
                print("⚠️ Reconnecting...", flush=True)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ Critical Error: {e}", flush=True)
                await asyncio.sleep(5)

    # [추가] 연결 유지용 심폐소생술 (20초 주기)
    async def manual_keepalive(self, ws):
        print("💓 [Heartbeat] 심폐소생술 가동 시작", flush=True)
        try:
            while True:
                await ws.ping()
                await asyncio.sleep(20)
        except Exception:
            pass # 연결 끊기면 조용히 종료            

    # [수정] 큐가 꽉 차면 오래된 데이터를 버리는 로직 적용
    async def producer(self, ws):
        async for msg in ws:
            try:
                self.msg_queue.put_nowait(msg)
            except asyncio.QueueFull:
                # [핵심] 큐가 꽉 찼을 때: 가장 오래된 것 하나 빼고(get) -> 새 것 넣기(put)
                try:
                    self.msg_queue.get_nowait()
                    self.msg_queue.put_nowait(msg)
                except:
                    pass

   # [5] Worker (데이터 연결 로직 수정됨 - 1초봉 강제 구동 추가)
    async def worker(self):
        while True:
            msg = await self.msg_queue.get()
            try:
                data = json.loads(msg)
                for item in data:
                    ev, t = item.get('ev'), item.get('sym')
                    
                    if ev == 'A': 
                        self.selector.update(item)
                        # [수정 2] 실시간 Agg 데이터를 딕셔너리에 저장해둠 (캐싱)
                        self.last_agg[t] = item
                        
                        # 🔥 [긴급 수정] T(체결) 데이터가 안 들어올 때를 대비해
                        # A(1초봉) 데이터가 들어오면 강제로 봇을 구동시킵니다.
                        if t in self.snipers:
                            # A 데이터를 T 데이터인 척 위장해서 봇에게 먹입니다.
                            pseudo_tick = {
                                'p': item['c'],      # 현재가 = 종가
                                's': item['v'],      # 거래량
                                't': item['e']       # 시간
                            }
                            # 봇에게 강제 주입 -> 이러면 Pulse 로그가 무조건 찍힙니다!
                            self.snipers[t].update_dashboard_db(
                                pseudo_tick, 
                                self.last_quotes.get(t, {'bids':[],'asks':[]}), 
                                item
                            )
                    
                    elif ev == 'Q':
                        self.last_quotes[t] = {
                            'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                            'asks': [{'p':item.get('ap'),'s':item.get('as')}]
                        }
                    
                    # Top 3 종목 정밀 타격 로직 (원래 로직 유지)
                    elif ev == 'T' and t in self.snipers:
                        current_agg = self.last_agg.get(t)
                        self.snipers[t].update_dashboard_db(
                            item, 
                            self.last_quotes.get(t, {'bids':[],'asks':[]}), 
                            current_agg 
                        )
            except Exception as e:
                # 🔥 [긴급 수정] 에러 무시하지 말고 출력!
                import traceback
                print(f"❌ [Worker Critical Error] {e}", flush=True)
                traceback.print_exc()
            finally:
                self.msg_queue.task_done()

    # [6] Scanner (20초 주기)
    async def task_global_scan(self):
        print("🔭 [Scanner] Started (Fast Mode: 20s)", flush=True)
        while True:
            try:
                # 봇 켜자마자 바로 한번 스캔
                self.candidates = self.selector.get_top_gainers_candidates(limit=10)
                if self.candidates:
                    print(f"📋 [Top 10 Candidates] {self.candidates}", flush=True)
                
                self.selector.garbage_collect()
                await asyncio.sleep(20) # 20초 대기
            except Exception as e:
                print(f"⚠️ Scanner Warning: {e}", flush=True)
                await asyncio.sleep(5)

    # [7] Manager (5초 주기 & Warmup 적용)
    async def task_focus_manager(self, ws, candidates=None):
        print("🎯 [Manager] Started (Fast Mode: 5s)", flush=True)
        while True:
            try:
                await asyncio.sleep(5)
                if not self.candidates: continue

                target_top3 = self.selector.get_best_snipers(self.candidates, limit=STS_TARGET_COUNT)
                
                current_set = set(self.snipers.keys())
                new_set = set(target_top3)
                
                # Detach
                to_remove = current_set - new_set
                if to_remove:
                    print(f"👋 Detach: {list(to_remove)}", flush=True)
                    unsubscribe_params = [f"T.{t}" for t in to_remove] + [f"Q.{t}" for t in to_remove]
                    await self.unsubscribe(ws, unsubscribe_params)
                    for t in to_remove: 
                        if t in self.snipers: del self.snipers[t]

                # Attach
                to_add = new_set - current_set
                if to_add:
                    print(f"🚀 Attach: {list(to_add)}", flush=True)
                    subscribe_params = [f"T.{t}" for t in to_add] + [f"Q.{t}" for t in to_add]
                    await self.subscribe(ws, subscribe_params)
                    
                    for t in to_add:
                        # 봇 생성
                        new_bot = SniperBot(t, self.logger, self.selector, self.shared_model)
                        
                        # [수정 후] 백그라운드 태스크로 실행 (멈추지 않고 바로 다음으로 넘어감)
                        self.snipers[t] = new_bot # 봇 먼저 등록
                        asyncio.create_task(new_bot.warmup()) # 웜업은 알아서 하라고 던져둠
                        
                        # 준비 완료된 봇 등록
                        self.snipers[t] = new_bot

            except Exception as e:
                print(f"❌ Manager Error: {e}", flush=True)
                await asyncio.sleep(5)
                # ==============================================================================
# ==============================================================================
# 5. MAIN EXECUTION (실행 진입점)
# ==============================================================================

# 🔥 [추가] 봇 부팅 및 테스트를 위한 메인 함수
async def main_startup():
    # 1. 필수 서비스 먼저 초기화 (알림을 보내기 위해 필요)
    init_db()
    init_firebase()
    
    print("🚀 [System] Initializing STS Sniper Bot...", flush=True)
    pipeline = STSPipeline()

    # 2. 🔥 [테스트 알림 발송] 봇 켜질 때 '살아있다'고 신고
    print("🔔 [System] Sending Startup Test Notification...", flush=True)
    try:
        # 가짜 종목(TEST-BOT)으로 99점짜리 알림을 쏴봅니다.
        await send_fcm_notification("TEST-BOT", 123.45, 99, entry=123.45, tp=130.00, sl=120.00)
        print("✅ [System] Test Notification Sent! (Check your phone)", flush=True)
    except Exception as e:
        print(f"❌ [System] Test Notification Failed: {e}", flush=True)

    # 3. 진짜 봇 파이프라인 가동 (무한 루프)
    await pipeline.connect()

if __name__ == "__main__":
    # 윈도우 환경 충돌 방지
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        # 기존: asyncio.run(pipeline.connect()) 
        # 수정: 위에서 만든 main_startup 실행
        asyncio.run(main_startup())

    except KeyboardInterrupt:
        print("\n🛑 [System] Bot stopped by user.", flush=True)
    except Exception as e:
        print(f"❌ [Fatal Error] Main loop crashed: {e}", flush=True)
        time.sleep(5)