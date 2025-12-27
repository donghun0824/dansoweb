import copy 
import asyncio
import websockets
import json
import os
import time
import redis.asyncio as redis
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
import sys
sys.setrecursionlimit(1000)

# ==============================================================================
# 1. CONFIGURATION & CONSTANTS (Refactored)
# ==============================================================================
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
WS_URI = "wss://socket.polygon.io/stocks"

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
r = redis.from_url(REDIS_URL)

# ==============================================================================
# 1. CONFIGURATION & CONSTANTS (Modified for Small-Cap Scalping)
# ==============================================================================

# [A] 스캐너 설정 (Target Selector) - 종목 발굴 기준
STS_SCAN_MIN_DOLLAR_VOL = 100_000   # [변경] 50만불 -> 10만불 (초기 포착 강화)
STS_SCAN_MIN_PRICE = 0.5            
STS_SCAN_MAX_PRICE = 20.0           # [변경] 30불 -> 20불 (무거운 주식 배제)
STS_SCAN_MIN_CHANGE = 2.0           
STS_TARGET_COUNT = 3                # 최종 활성 스나이퍼 수 (Active)
STS_STAGING_COUNT = 10              # [신규] 관찰 대상 수 (Staging)

# [B] 스나이퍼 봇 설정 (SniperBot) - 진입 필터 (Relaxed for Volatility)
STS_BOT_MAX_SPREAD_RATIO = 0.5      # [신규] ATR 대비 스프레드 비율 (절대값 대신 상대값 사용)
STS_BOT_MIN_TICK_SPEED = 2          
STS_BOT_MIN_LIQUIDITY_1M = 50_000   # [변경] 20만불 -> 5만불 (스몰캡 생존 라인)
STS_BOT_SAFE_LIQUIDITY_1M = 200_000 # [변경] 50만불 -> 20만불
STS_BOT_MIN_BOOK_RATIO = 0.02       # [변경] 5% -> 2% (스몰캡은 호가가 얇음)

# [C] 전략별 세부 임계값
STS_VPIN_LIMIT_REBOUND = 0.9        
STS_VPIN_LIMIT_MOMENTUM = 2.0       
STS_RVOL_MIN_REBOUND = 1.5          # [상향] 확실한 거래량 필요
STS_RVOL_MIN_MOMENTUM = 2.5         # [상향] 폭발적인 거래량 필요

# [D] 시스템 설정
OBI_LEVELS = 20               # 오더북 깊이
MODEL_FILE = "sts_xgboost_model.json"
AI_PROB_THRESHOLD = 0.85      
ATR_TRAIL_MULT = 1.5        
HARD_STOP_PCT = 0.015         

# Logging
TRADE_LOG_FILE = "sts_trade_log_v5.csv"
REPLAY_LOG_FILE = "sts_replay_data_v5.csv"

# System Optimization
DB_UPDATE_INTERVAL = 3.0
GC_INTERVAL = 60             
GC_TTL = 300                  

DB_WORKER_POOL = ThreadPoolExecutor(max_workers=10) 
NOTI_WORKER_POOL = ThreadPoolExecutor(max_workers=5)
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
            "day_change REAL DEFAULT 0",  # 기존 맨 아래 있던 day_change도 포함
            "dollar_vol REAL DEFAULT 0",
            "rsi REAL DEFAULT 50",
            "stoch_k REAL DEFAULT 50",
            "fibo_pos REAL DEFAULT 0.5",
            "obi_rev INTEGER DEFAULT 0",
            "regime_p REAL DEFAULT 0.5",
            "ofi REAL DEFAULT 0",          
            "weighted_obi REAL DEFAULT 0",
            "dollar_vol_1m REAL DEFAULT 0", 
            "top5_book_usd REAL DEFAULT 0"
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
        
        # [수정] 쿼리에 rsi, stoch_k, fibo_pos, obi_rev 컬럼 추가
        query = """
        INSERT INTO sts_live_targets 
        (ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status, 
         obi_mom, tick_accel, vwap_slope, squeeze_ratio, rvol, atr, pump_accel, spread,ofi, weighted_obi, 
         rsi, stoch_k, fibo_pos, obi_rev, regime_p,dollar_vol_1m, top5_book_usd,last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 
                %s, %s, %s, %s, %s, %s, %s, %s, 
                %s, %s, %s, %s,%s,%s, %s,%s, %s, NOW())
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
            
            rsi = EXCLUDED.rsi,
            stoch_k = EXCLUDED.stoch_k,
            fibo_pos = EXCLUDED.fibo_pos,
            obi_rev = EXCLUDED.obi_rev,
            regime_p = EXCLUDED.regime_p,
            ofi = EXCLUDED.ofi,                   -- 🔥 [추가 3] 업데이트 구문 추가
            weighted_obi = EXCLUDED.weighted_obi, -- 🔥 [추가 4] 업데이트 구문 추가
            dollar_vol_1m = EXCLUDED.dollar_vol_1m, -- 🔥 [추가 3] 업데이트 구문
            top5_book_usd = EXCLUDED.top5_book_usd, -- 🔥 [추가 4] 업데이트 구문
            last_updated = NOW();
        """
        
        cursor.execute(query, (
            ticker, 
            float(metrics.get('last_price', 0)), 
            float(score), 
            float(metrics.get('obi', 0)), 
            float(metrics.get('vpin', 0)), 
            int(metrics.get('tick_speed', 0)), 
            float(metrics.get('vwap_dist', 0)), 
            status,
            # [기존 매핑]
            float(metrics.get('obi_mom', 0)),
            float(metrics.get('tick_accel', 0)),
            float(metrics.get('vwap_slope', 0)),
            float(metrics.get('squeeze_ratio', 0)),
            float(metrics.get('rvol', 0)),
            float(metrics.get('atr', 0)),
            float(metrics.get('pump_accel', 0)),
            float(metrics.get('spread', 0)),
            
            # 🔥 [NEW] 신규 지표 매핑 추가 (순서 중요!)
            float(metrics.get('rsi', 50)),
            float(metrics.get('stoch_k', 50)),
            float(metrics.get('fibo_pos', 0.5)),
            int(metrics.get('obi_reversal_flag', 0)),
            float(metrics.get('regime_p', 0.5)),
            float(metrics.get('ofi', 0)),
            float(metrics.get('weighted_obi', 0)),
            float(metrics.get('dollar_vol_1m', 0)),
            float(metrics.get('top5_book_usd', 0))
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

# [STS_Engine.py 내부]

def _send_fcm_sync(ticker, price, probability_score, entry=None, tp=None, sl=None):
    # 1. Firebase 초기화 체크
    if not firebase_admin._apps:
        # print(...) # 로그 생략
        return

    # 🟢 [헬퍼] 안전한 타입 변환
    def sanitize(val):
        try:
            if hasattr(val, 'item'): return val.item()
            return val
        except: return 0

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

        # 🟢 [데이터 정제]
        price = sanitize(price)
        score_val = sanitize(probability_score)
        entry = sanitize(entry)
        tp = sanitize(tp)

        # 알림 내용 구성
        if entry and tp:
            noti_title = f"BUY {ticker} ({score_val})"
            noti_body = f"Entry: ${float(entry):.3f}\nTP: ${float(tp):.3f}"
        else:
            noti_title = f"SCAN {ticker}"
            noti_body = f"Current: ${float(price):.4f}"

        # 🟢 [수정 핵심] Data Payload는 모두 문자열이어야 함 (안전하게 str로 감싸기)
        data_payload = {
            'type': 'signal', 
            'ticker': str(ticker), 
            'price': str(price), 
            'score': str(score_val),
            'click_action': 'FLUTTER_NOTIFICATION_CLICK' # 앱 연동을 위해 권장
        }
        
        print(f"🔔 [FCM] Sending: {noti_title}...", flush=True)

        success_count = 0
        failed_tokens = []
        
        # 🟢 [수정 핵심] 메시지 객체 단순화 (Config 제거 테스트)
        # 만약 이래도 에러가 나면 android=, apns= 옵션을 아예 빼고 보내보세요.
        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            
            if score_val < user_min_score: continue

            try:
                message = messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=noti_title, body=noti_body),
                    data=data_payload
                    # ⚠️ [중요] 아래 설정들이 재귀 에러의 주범인 경우가 많습니다.
                    # 에러가 지속되면 아래 주석 처리된 부분을 삭제하고 기본 알림만 보내세요.
                    ,
                    android=messaging.AndroidConfig(
                        priority='high',
                        notification=messaging.AndroidNotification(sound='default')
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound='default')
                        )
                    )
                )
                messaging.send(message)
                success_count += 1
            except Exception as e:
                # 에러 로그가 너무 길어지지 않게 짧게 출력
                print(f"❌ [FCM Fail] Token Error: {str(e)[:50]}...", flush=True)
                if "registration-token-not-registered" in str(e): 
                    failed_tokens.append(token)
        
        # (이하 토큰 정리 로직 동일)
        if failed_tokens:
            c = conn.cursor()
            c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            c.close()

    except Exception as e:
        print(f"❌ [FCM Critical] {e}", flush=True)
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# [STS_Engine.py]

async def send_fcm_notification(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """
    [역할 분리] 엔진은 직접 보내지 않고 Redis 'fcm_queue'에 작업 지시서(JSON)만 넣습니다.
    """
    try:
        # 1. 보낼 데이터 포장 (무조건 문자열로 변환하여 안전하게)
        payload = {
            'ticker': str(ticker),
            'price': str(price),
            'score': str(int(probability_score)),
            'entry': str(entry) if entry else "",
            'tp': str(tp) if tp else "",
            'timestamp': time.time()
        }

        # 2. Redis 큐에 직렬화해서 밀어넣기 (0.001초 소요)
        # r은 redis.asyncio 객체 (이미 코드 상단에 선언되어 있음)
        await r.lpush('fcm_queue', json.dumps(payload))
        
        # 로그는 한 줄만 심플하게
        # print(f"🔔 [Engine] Queued signal for {ticker}", flush=True)

    except Exception as e:
        print(f"❌ [Engine] Failed to queue notification: {e}", flush=True)

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

# [V7.1] MicrostructureAnalyzer (유동성 지표 추가: 1분 거래대금, 호가 총액)
class MicrostructureAnalyzer:
    def __init__(self):
        self.raw_ticks = deque(maxlen=3000) 
        self.quotes = {'bids': [], 'asks': []}
        
        # OFI 계산용 상태 변수
        self.prev_best_bid_p = 0
        self.prev_best_ask_p = 0
        self.prev_best_bid_s = 0
        self.prev_best_ask_s = 0
        self.prev_obi = 0

    def inject_history(self, aggs):
        if not aggs: return
        aggs.sort(key=lambda x: x['t'])
        for bar in aggs:
            ts = pd.to_datetime(bar['t'], unit='ms')
            self.raw_ticks.append({
                't': ts, 'p': bar['c'], 's': bar.get('v', 0),
                'bid': bar['c'] - 0.01, 'ask': bar['c'] + 0.01
            })
        print(f"📥 [Analyzer] History Loaded: {len(aggs)} bars.", flush=True)

    def update_tick(self, tick_data, current_quotes):
        best_bid = current_quotes['bids'][0]['p'] if current_quotes.get('bids') else 0
        best_ask = current_quotes['asks'][0]['p'] if current_quotes.get('asks') else 0
        
        self.raw_ticks.append({
            't': pd.to_datetime(tick_data.get('t', time.time()*1000), unit='ms'), 
            'p': tick_data.get('p', 0), 's': tick_data.get('s', 0),  
            'bid': best_bid, 'ask': best_ask
        })
        self.quotes = current_quotes

    def _calculate_ofi(self, best_bid_p, best_bid_s, best_ask_p, best_ask_s):
        if self.prev_best_bid_p == 0: return 0
        
        e_n_bid = 0
        if best_bid_p > self.prev_best_bid_p: e_n_bid = best_bid_s
        elif best_bid_p == self.prev_best_bid_p: e_n_bid = best_bid_s - self.prev_best_bid_s
        else: e_n_bid = -self.prev_best_bid_s

        e_n_ask = 0
        if best_ask_p > self.prev_best_ask_p: e_n_ask = -self.prev_best_ask_s
        elif best_ask_p == self.prev_best_ask_p: e_n_ask = best_ask_s - self.prev_best_ask_s
        else: e_n_ask = best_ask_s

        return e_n_bid - e_n_ask

    def get_metrics(self):
        if len(self.raw_ticks) < 50: return None
        
        # 변수 안전 초기화
        vpin = 0; obi = 0; ofi = 0; weighted_obi = 0; obi_mom = 0
        ofi_accel = 0.0 # 🔥 [초기화] OFI 가속도 변수 추가
        
        try:
            # 1. 기본 OHLCV 데이터 생성 (리샘플링)
            df_raw = pd.DataFrame(self.raw_ticks).set_index('t') # 원본 보존용
            df = df_raw.copy() # 리샘플링용 복사본
            
            ohlcv = df['p'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
            volume = df['s'].resample('1s').sum()
            tick_count = df['s'].resample('1s').count()

            # 🔥 [추가] 스프레드 평균 계산 (Raw Tick에서 bid/ask 차이를 계산 후 평균)
            # 호가 스프레드가 급격히 좁아지는지(수렴) 확인하기 위함
            if 'bid' in df.columns and 'ask' in df.columns:
                df['raw_spread'] = (df['ask'] - df['bid']) / df['bid'] * 100
                spread_series = df['raw_spread'].resample('1s').mean()
            else:
                spread_series = pd.Series(0, index=ohlcv.index)
            
            df_res = pd.concat([ohlcv, volume, tick_count, spread_series], axis=1).iloc[-600:]
            df_res.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed','spread_avg']
            df = df_res.ffill().fillna(0) # 여기서 df가 1초봉 데이터로 바뀜
            
            if len(df) < 20: return None

            WIN_MAIN = 60
            v = df['volume'].values
            p = df['close'].values

            # 🔥 [핵심 추가 1] Zero-Latency용 10초 이동평균 & 직전 고점 계산
            # 1) 10초 평균 Tick Speed & Spread (평소 상태 측정)
            tick_speed_10s_avg = df['tick_speed'].rolling(10).mean().iloc[-1]
            spread_10s_avg = df['spread_avg'].rolling(10).mean().iloc[-1]
            
            # 2) 직전 1분봉 고점 (Breakout 확인용)
            current_time = df.index[-1]
            # 현재 1분봉이 아닌, '직전' 1분봉의 고점을 구함
            last_minute_start = (current_time - pd.Timedelta(minutes=1)).floor('1min')
            last_minute_end = current_time.floor('1min')
            
            mask = (df.index >= last_minute_start) & (df.index < last_minute_end)
            if mask.any():
                prev_1m_high = df.loc[mask, 'high'].max()
            else:
                prev_1m_high = df['high'].max() # 데이터 없으면 전체 고점 사용
            
            # 🔥 [추가] 유동성 지표 (Liquidity Metrics)
            df['dollar_vol'] = df['close'] * df['volume']
            dollar_vol_1m = df['dollar_vol'].iloc[-60:].sum()

            # 기본 지표들 (VWAP, RVOL 등)
            df['vwap'] = (p * v).cumsum() / (v.cumsum() + 1e-9)
            df['vwap'] = df['vwap'].ffill() 
            df['vwap_slope'] = (df['vwap'].diff(5) / (df['vwap'].shift(5) + 1e-9)) * 10000
            
            df['vol_ma'] = df['volume'].rolling(WIN_MAIN).mean()
            df['rvol'] = df['volume'] / (df['vol_ma'] + 1e-9)
            df['rv_60'] = df['close'].pct_change().rolling(60).std()
            
            df['realized_vol_20s'] = df['close'].pct_change().rolling(20).std()
            df['realized_vol_120s'] = df['close'].pct_change().rolling(120).std()
            df['vol_ratio'] = df['realized_vol_20s'] / (df['realized_vol_120s'] + 1e-9)

            change = df['close'].diff(20).abs()
            path = df['close'].diff().abs().rolling(20).sum()
            df['efficiency_ratio'] = change / (path + 1e-9)
            df['hurst'] = 0.5 + (df['efficiency_ratio'] * 0.5)

            df['squeeze_ratio'] = ind.compute_bb_bandwidth(df['close'], window=20)
            df['pump_5m'] = df['close'].pct_change(300)
            df['pump_accel'] = df['pump_5m'].diff(60)
            df['tick_accel'] = df['tick_speed'].diff().fillna(0)

            prev_close = df['close'].shift(1)
            tr = pd.concat([df['high']-df['low'], (df['high']-prev_close).abs(), (df['low']-prev_close).abs()], axis=1).max(axis=1)
            df['atr'] = tr.rolling(WIN_MAIN).mean()
            
            df['rsi'] = ind.compute_rsi_series(df['close'], period=14)
            df['stoch_k'] = ind.compute_stochastic_series(df['high'], df['low'], df['close'])
            df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=300)

            df = df.fillna(0)
            last = df.iloc[-1]

            # ------------------------------------------------------------------
            # 🔥 [추가] 2.1 OFI 가속도 계산 (원본 df_raw 사용)
            # ------------------------------------------------------------------
            # 최근 30초 vs 직전 30초의 순매수 체결량(OFI) 비교
            now = df_raw.index[-1]
            t_30s = now - pd.Timedelta(seconds=30)
            t_60s = now - pd.Timedelta(seconds=60)
            
            # 시간대별 슬라이싱
            slice_curr = df_raw[df_raw.index >= t_30s]
            slice_prev = df_raw[(df_raw.index >= t_60s) & (df_raw.index < t_30s)]
            
            # 간이 OFI 계산: (체결가 >= 매도호가 ? 매수체결) - (체결가 <= 매수호가 ? 매도체결)
            # raw_ticks에는 'bid', 'ask'가 기록되어 있다고 가정
            def calc_simple_ofi(slice_df):
                if slice_df.empty: return 0
                buy_vol = slice_df[slice_df['p'] >= slice_df['ask']]['s'].sum()
                sell_vol = slice_df[slice_df['p'] <= slice_df['bid']]['s'].sum()
                return buy_vol - sell_vol

            curr_ofi_sum = calc_simple_ofi(slice_curr)
            prev_ofi_sum = calc_simple_ofi(slice_prev)
            
            # 가속도 산출 (이전 30초 대비 현재 30초가 얼마나 폭발했는가)
            if prev_ofi_sum > 0:
                ofi_accel = curr_ofi_sum / prev_ofi_sum
            elif prev_ofi_sum <= 0 and curr_ofi_sum > 0:
                ofi_accel = 10.0 # 음수나 0에서 양수 폭발은 아주 강력한 신호로 간주
            else:
                ofi_accel = 0.0

            # ------------------------------------------------------------------

            # --- 호가 분석 (Orderbook Analysis) ---
            bids_list = self.quotes.get('bids', [])
            asks_list = self.quotes.get('asks', [])

            # 상위 5호가 잔량 총액 ($)
            top5_book_usd = 0
            for q in bids_list[:5]: top5_book_usd += (q['p'] * q['s'])
            for q in asks_list[:5]: top5_book_usd += (q['p'] * q['s'])

            # OFI (Standard)
            curr_bid_p = bids_list[0]['p'] if bids_list else 0
            curr_bid_s = bids_list[0]['s'] if bids_list else 0
            curr_ask_p = asks_list[0]['p'] if asks_list else 0
            curr_ask_s = asks_list[0]['s'] if asks_list else 0
            ofi = self._calculate_ofi(curr_bid_p, curr_bid_s, curr_ask_p, curr_ask_s)
            
            self.prev_best_bid_p = curr_bid_p; self.prev_best_bid_s = curr_bid_s
            self.prev_best_ask_p = curr_ask_p; self.prev_best_ask_s = curr_ask_s

            # Weighted OBI
            w_bid_sum = 0; w_ask_sum = 0
            limit_level = min(len(bids_list), len(asks_list), OBI_LEVELS)
            for i in range(limit_level):
                weight = np.exp(-0.5 * i) 
                w_bid_sum += bids_list[i]['s'] * weight
                w_ask_sum += asks_list[i]['s'] * weight
            weighted_obi = (w_bid_sum - w_ask_sum) / (w_bid_sum + w_ask_sum + 1e-9)

            # Simple OBI
            bids_arr = np.array([q['s'] for q in bids_list[:OBI_LEVELS]])
            asks_arr = np.array([q['s'] for q in asks_list[:OBI_LEVELS]])
            bid_vol = np.sum(bids_arr) if len(bids_arr) > 0 else 0
            ask_vol = np.sum(asks_arr) if len(asks_arr) > 0 else 0
            obi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
            
            obi_mom = obi - self.prev_obi
            prev_obi_val = obi - obi_mom
            obi_reversal_flag = 1 if (obi > 0 and prev_obi_val < 0) else 0
            self.prev_obi = obi 
            
            # VPIN
            raw_df = pd.DataFrame(list(self.raw_ticks)[-100:])
            if not raw_df.empty and 'ask' in raw_df.columns:
                buy_vol = raw_df[raw_df['p'] >= raw_df['ask']]['s'].sum()
                sell_vol = raw_df[raw_df['p'] <= raw_df['bid']]['s'].sum()
                total = buy_vol + sell_vol
                vpin = abs(buy_vol - sell_vol) / total if total > 0 else 0

            vwap_dist = (last['close'] - last['vwap']) / last['vwap'] * 100 if last['vwap'] > 0 else 0
            best_bid = self.raw_ticks[-1]['bid']
            best_ask = self.raw_ticks[-1]['ask']
            spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0

            return {
                'obi': obi, 'weighted_obi': weighted_obi, 'ofi': ofi,
                'obi_mom': obi_mom, 'tick_accel': last['tick_accel'], 'vpin': vpin, 
                'ofi_accel': ofi_accel, # 🔥 [NEW] 반환값에 추가

                # 🔥 [NEW] Zero-Latency용 신규 지표들
                'tick_speed_avg_10s': tick_speed_10s_avg,
                'spread_avg_10s': spread_10s_avg,
                'prev_1m_high': prev_1m_high,
                
                'vwap_dist': vwap_dist, 'vwap_slope': last['vwap_slope'], 'rvol': last['rvol'],
                'squeeze_ratio': last['squeeze_ratio'], 'pump_accel': last['pump_accel'],
                'atr': last['atr'] if last['atr'] > 0 else last['close'] * 0.005,
                'spread': spread, 'last_price': last['close'], 'tick_speed': last['tick_speed'], 
                'timestamp': raw_df.iloc[-1]['t'] if not raw_df.empty else pd.Timestamp.now(), 
                'vwap': last['vwap'], 'rv_60': last['rv_60'], 'fibo_pos': last['fibo_pos'],
                'bb_width_norm': last['squeeze_ratio'], 'rsi': last['rsi'], 'stoch_k': last['stoch_k'],
                'obi_reversal_flag': obi_reversal_flag, 
                'vol_ratio': last['vol_ratio'], 'hurst': last['hurst'],
                
                # SniperBot에게 넘겨줄 유동성 지표
                'dollar_vol_1m': dollar_vol_1m,
                'top5_book_usd': top5_book_usd
            }

        except Exception as e:
            print(f"❌ [Metrics Error] {e}")
            traceback.print_exc()
            return None      

# [STS_Engine.py] TargetSelector 클래스 (Hybrid Mode 적용 - 최종 수정본)

class TargetSelector:
    def __init__(self, api_key=None):
        self.snapshots = {} 
        self.static_stats = {}  # 정적 데이터(전일 거래량 등) 저장소
        self.last_gc_time = time.time()
        self.api_key = api_key 
        
        # 🔥 [핵심] 봇 시작 시 데이터 로딩 및 초기 스냅샷
        if self.api_key:
            self.load_static_data()       # 전일 거래량 (RVOL용)
            self.refresh_market_snapshot() # 🔥 [변경] API 폴링 함수 호출
        else:
            print("⚠️ [Selector] API Key missing. Cold Start protection disabled.", flush=True)

    def load_static_data(self):
        """
        [배치 작업] 최근 거래일의 '전 종목' 데이터를 한 번에 가져와서 메모리에 박아둡니다.
        목적: 실시간 RVOL 계산을 위한 '분모(평균 거래량 대용)' 확보
        """
        print("💾 [System] Loading Static Reference Data (Yesterday's Vol)...", flush=True)
        try:
            # 1. 최근 평일(Business Day) 계산
            import datetime
            today = datetime.datetime.now()
            offset = 1
            if today.weekday() == 0: offset = 3 # 월요일이면 금요일 데이터
            elif today.weekday() == 6: offset = 2 # 일요일이면 금요일 데이터
            
            target_date = (today - datetime.timedelta(days=offset)).strftime('%Y-%m-%d')
            
            # Grouped Daily Bars (해당 날짜의 전 종목 데이터)
            url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{target_date}?adjusted=true&apiKey={self.api_key}"
            
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    count = 0
                    if 'results' in data:
                        for item in data['results']:
                            t = item['T']
                            v = item.get('v', 0)
                            # 거래량이 너무 적으면 노이즈가 심하므로 최소값 보정
                            if v > 0:
                                self.static_stats[t] = {
                                    'prev_vol': max(v, 100000) # 최소 10만주로 보정
                                }
                                count += 1
                        print(f"✅ [System] Static Data Loaded! ({count} tickers from {target_date})", flush=True)
                    else:
                        print(f"⚠️ [System] Static Data Empty (Date: {target_date})", flush=True)
                else:
                    print(f"⚠️ [System] Static Data Load Failed: {resp.status_code}", flush=True)

        except Exception as e:
            print(f"❌ [Error] Reference Data Load Exception: {e}", flush=True)

    # 🔥 [핵심 변경] 함수 이름 변경 & 타임아웃 단축 (5초)
    def refresh_market_snapshot(self):
        """
        [Hybrid Mode] 유료 플랜의 강력함을 이용해 API를 직접 호출하여 데이터 갱신
        웹소켓이 끊겨도 이 함수가 돌면 봇은 죽지 않습니다.
        """
        # print("🌍 [Selector] API Snapshot Polling...", flush=True) # 로그 너무 많으면 주석 처리
        try:
            # 유료 플랜이므로 타임아웃 짧게(5초) 잡고 빠르게 치고 빠짐
            url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey={self.api_key}"
            
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url)
            
            if resp.status_code == 200:
                data = resp.json()
                if 'tickers' in data:
                    for item in data['tickers']:
                        t = item['ticker']
                        day = item.get('day', {})
                        min_bar = item.get('min', {}) 
                        
                        # 거래량 없으면 무시
                        if not day.get('v') or not day.get('o'): continue
                        
                        # 현재가 계산
                        curr_price = item.get('lastTrade', {}).get('p', min_bar.get('c', day.get('c')))
                        if not curr_price: continue

                        # 딕셔너리가 없으면 생성, 있으면 갱신 (덮어쓰기)
                        self.snapshots[t] = {
                            'o': day['o'],      
                            'h': day.get('h', curr_price),
                            'l': day.get('l', curr_price),
                            'c': curr_price,
                            'v': day['v'],           
                            'vwap': day.get('vw', curr_price),
                            'start_price': day['o'], 
                            'last_updated': time.time() # 🔥 시간 갱신 (생존 신고)
                        }
            else:
                print(f"⚠️ Snapshot Poll Failed: {resp.status_code}", flush=True)
        except Exception as e:
            print(f"❌ Snapshot Poll Error: {e}", flush=True)

    def update(self, agg_data):
        t = agg_data['sym']
        if t not in self.snapshots: 
            self.snapshots[t] = {
                'o': agg_data['o'], 'h': agg_data['h'], 'l': agg_data['l'], 
                'c': agg_data['c'], 'v': 0, 
                'vwap': agg_data.get('vw', agg_data['c']),
                'start_price': agg_data['o'], 
                'last_updated': time.time()
            }
        
        d = self.snapshots[t]
        d['c'] = agg_data['c']
        d['h'] = max(d['h'], agg_data['h'])
        d['l'] = min(d['l'], agg_data['l'])
        d['v'] += agg_data['v']
        d['vwap'] = agg_data.get('vw', d['c'])
        d['last_updated'] = time.time()

    def get_atr(self, ticker):
        if ticker in self.snapshots:
            d = self.snapshots[ticker]
            range_vol = d['h'] - d['l']
            return max(range_vol * 0.1, d['c'] * 0.005)
        return 0.05

    def save_candidates_to_db(self, candidates):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            valid_list = []
            for item in candidates:
                if isinstance(item, (list, tuple)) and len(item) >= 4:
                    valid_list.append(item)

            if not valid_list: return

            for t, score, change, vol, *rest in valid_list:
                d = self.snapshots.get(t)
                if not d: continue
                
                # DB 저장 시 rvol 값도 계산해서 넣음
                ref = self.static_stats.get(t, {'prev_vol': 1000000})
                rvol_est = d['v'] / ref['prev_vol']
                
                query = """
                INSERT INTO sts_live_targets 
                (ticker, price, ai_score, day_change, dollar_vol, rvol, status, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, 'SCANNING', NOW()) 
                ON CONFLICT (ticker) DO UPDATE SET
                    price = EXCLUDED.price, day_change = EXCLUDED.day_change,
                    dollar_vol = EXCLUDED.dollar_vol, ai_score = EXCLUDED.ai_score,
                    rvol = EXCLUDED.rvol,
                    last_updated = NOW()
                WHERE sts_live_targets.status = 'SCANNING'; 
                """
                cursor.execute(query, (t, float(d['c']), float(score), float(change), float(vol), float(rvol_est))) 
            
            conn.commit()
            cursor.close()
        except Exception as e:
            if conn: conn.rollback()
            print(f"⚠️ [Scanner DB Error] {e}")
        finally:
            if conn: db_pool.putconn(conn)

    def get_top_gainers_candidates(self, limit=10):
        scored = []
        now = time.time()
        
        # 🔥 [핵심 수정] 딕셔너리를 list()로 감싸서 안전한 리스트 복사본을 만듭니다.
        # 기존: for t, d in self.snapshots.items():  <-- 에러 발생 지점
        snapshot_items = list(self.snapshots.items()) 

        for t, d in snapshot_items:
            if now - d['last_updated'] > 60: continue 
            
            # ---------------------------------------------------------
            # 1. [Gatekeeper] 기본 입장 자격 심사
            # ---------------------------------------------------------
            if d['c'] < STS_SCAN_MIN_PRICE or d['c'] > STS_SCAN_MAX_PRICE: continue
            
            dollar_vol = d['c'] * d['v']
            # STS_SCAN_MIN_DOLLAR_VOL 상수가 정의되어 있는지 확인하세요 (없으면 100_000 등 직접 입력)
            if dollar_vol < STS_SCAN_MIN_DOLLAR_VOL: continue 

            change_pct = (d['c'] - d['start_price']) / d['start_price'] * 100
            if change_pct < STS_SCAN_MIN_CHANGE: continue 

            # Fake Pump 방지
            required_vol = STS_SCAN_MIN_DOLLAR_VOL * (1 + (change_pct * 0.1))
            if dollar_vol < required_vol: continue

            # ---------------------------------------------------------
            # 🚀 [Hybrid Scoring] Momentum x Liquidity Cap x RVOL
            # ---------------------------------------------------------
            
            # [A] Momentum Score (70% 비중) - 변동성 우선
            momentum_score = min(change_pct * 2.0, 100)

            # [B] Liquidity Factor (30% 비중) - Cap 적용 ($100M)
            liquidity_raw = min(dollar_vol, 100_000_000) / 100_000_000 * 100
            
            # [C] RVOL Factor (가산점)
            ref_data = self.static_stats.get(t, {'prev_vol': 1000000})
            rvol = d['v'] / ref_data['prev_vol']
            
            rvol_bonus = min(max(0, rvol - 1.0) * 10, 20)
            
            # 최종 점수 계산
            score = (momentum_score * 0.7) + (liquidity_raw * 0.3) + rvol_bonus
            
            if d['c'] < 10.0: score += 5
            
            score = min(score, 99) 
            
            scored.append((t, score, change_pct, dollar_vol))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        top_list = scored[:limit]

        if top_list: self.save_candidates_to_db(top_list)
        return [x[0] for x in top_list]

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
        """
        메모리와 DB에서 오래된 데이터를 주기적으로 삭제합니다.
        """
        now = time.time()
        if now - self.last_gc_time < GC_INTERVAL: return
        
        # ---------------------------------------------------------
        # 1. [메모리 청소] - 🔥 [핵심 수정]
        # ---------------------------------------------------------
        # 원본 딕셔너리를 직접 돌리지 않고 list()로 복사본을 떠서 돌립니다.
        # 이렇게 해야 루프 도중 데이터가 들어와도 에러가 안 납니다.
        snapshot_items = list(self.snapshots.items())
        
        to_remove = [t for t, d in snapshot_items if now - d['last_updated'] > GC_TTL]
        
        for t in to_remove: 
            # del 대신 pop을 사용하여, 혹시 다른 스레드가 이미 지웠어도 에러(KeyError)가 안 나게 합니다.
            self.snapshots.pop(t, None)
            
        # ---------------------------------------------------------
        # 2. [DB 청소] (이 부분은 기존 유지)
        # ---------------------------------------------------------
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            query = """
                DELETE FROM sts_live_targets 
                WHERE status = 'SCANNING' 
                AND last_updated < NOW() - INTERVAL '1 minute';
            """
            cursor.execute(query)
            conn.commit()
            if cursor.rowcount > 0:
                print(f"🧹 [GC] Cleaned up {cursor.rowcount} stale targets from DB.", flush=True)
            cursor.close()
        except Exception as e:
            print(f"⚠️ [GC Error] DB Cleanup failed: {e}", flush=True)
            if conn: conn.rollback()
        finally:
            if conn: db_pool.putconn(conn)
            
        self.last_gc_time = now

# [V7.1] SniperBot (Hard Kill Filter, Strict Fast-Track, Emergency Exit 적용)
class SniperBot:
    def __init__(self, ticker, logger, selector, model_bytes):
        self.ticker = ticker
        self.logger = logger
        self.selector = selector
        
        self.CONFIG = {
            'weights': {
                'speed': 0.25, 'vwap': 0.20, 'vol': 0.20, 
                'hurst': 0.15, 'rvol': 0.15, 'sqz': 0.05
            },
            'thresh': {
                'fast_track': 80,      
                'entry': 60,           
                'confirm_window': 1.0, 
                'max_slip': -0.1       
            }
        }

        self.model = None
        if model_bytes:
            try:
                self.model = xgb.Booster()
                self.model.load_model(model_bytes)
            except Exception as e:
                print(f"⚠️ {ticker}: Model Load Error - {e}")

        self.analyzer = MicrostructureAnalyzer()
        
        self.state = "WATCHING"
        self.vwap = 0.0
        self.atr = 0.05
        self.position = {}
        self.prob_history = deque(maxlen=5)
        self.regime_p = 0.5  
        
        self.last_db_update = 0
        self.last_logged_state = "WATCHING"
        self.last_ready_alert = 0
        
        self.aiming_start_time = 0.0
        self.aiming_start_price = 0.0

        self.created_at = time.time()

    def _calc_rebound_score(self, m):
        score = 0; reasons = []
        rsi = m.get('rsi', 50)
        if rsi < 30: score += 40; reasons.append(f"Oversold")
        elif rsi < 40: score += 20
        if m.get('squeeze_ratio', 1.5) <= 1.0: score += 30; reasons.append("Squeeze")
        if m.get('vwap_dist', 0) < -1.0: score += 20; reasons.append("Cheap")
        if m.get('vwap_dist', 0) < -0.5 and m.get('rvol', 0) > 1.5 and m.get('tick_accel', 0) > 0:
            score += 10; reasons.append("DipRev")
        return max(score, 0), reasons

    def _calc_momentum_score(self, m):
        score = 0; reasons = []
        rsi = m.get('rsi', 50)
        if 50 <= rsi <= 80: score += 30; reasons.append("MomZone")
        
        if m.get('squeeze_ratio', 1.0) > 2.0:
            if m.get('tick_accel', 0) > 0 and m.get('rvol', 0) > 2.0: score += 40; reasons.append("Breakout")
            else: score -= 10
            
        if m.get('last_price', 0) > m.get('vwap', 0): score += 20; reasons.append("TrendUp")
        if m.get('rvol', 0) > 3.0: score += 30; reasons.append("VolSpike")
        elif m.get('rvol', 0) > 2.0: score += 15
        return max(score, 0), reasons

    def _calculate_regime_p(self, m):
        def clamp(x): return max(0.0, min(1.0, x))
        def sigmoid(x): return 1 / (1 + np.exp(-x))
        
        try:
            w = self.CONFIG['weights']
            p_speed = clamp((m.get('tick_speed', 0) - 2) / 6.0)
            p_vwap = sigmoid(m.get('vwap_dist', 0))
            p_vol = clamp((m.get('vol_ratio', 1.0) - 0.8) / 0.7)
            p_hurst = clamp((m.get('hurst', 0.5) - 0.45) / 0.20)
            p_rvol = clamp((m.get('rvol', 1.0) - 1.5) / 3.0)
            p_squeeze = clamp((m.get('squeeze_ratio', 1.0) - 1.0) / 1.5)

            p_new = (
                w['speed'] * p_speed + w['vwap'] * p_vwap + 
                w['vol'] * p_vol + w['hurst'] * p_hurst + 
                w['rvol'] * p_rvol + w['sqz'] * p_squeeze
            )
            self.regime_p = (0.7 * self.regime_p) + (0.3 * p_new)
            return clamp(self.regime_p)
        except Exception:
            return 0.5

# 기존 _calculate_regime_p 함수가 끝나는 곳 다음에 붙여넣으세요.

    def is_ready(self):
        """[New] 데이터가 충분히 쌓였는지 검증 (Warmup Gate)"""
        # 1. 시간 경과 확인 (봇 생성 후 최소 5초 경과)
        if (time.time() - self.created_at) < 5.0:
            return False

        # 2. 데이터 개수 확인 (최소 300틱 이상)
        if len(self.analyzer.raw_ticks) < 300:
            return False
            
        # 3. 필수 지표 계산 여부
        if self.atr == 0 or self.vwap == 0:
            return False
            
        return True

    def calculate_ers(self, m):
        """[New] Execution Readiness Score - 발사 준비 점수"""
        score = 0
        
        # 1. OFI & 수급 (40점) - 매수 압력 확인
        if m.get('ofi', 0) > 0: score += 20
        if m.get('ofi_accel', 0) > 0: score += 20 
        
        # 2. 속도 (30점) - 평소(10s avg)보다 2배 빠른가?
        avg_speed = m.get('tick_speed_avg_10s', 1)
        curr_speed = m.get('tick_speed', 0)
        
        # 0으로 나누기 방지
        if avg_speed == 0: avg_speed = 1 
        
        if curr_speed > (avg_speed * 2.0): score += 30
        elif curr_speed > (avg_speed * 1.5): score += 15
        
        # 3. 응축 (Spread Squeeze) (20점)
        # 스프레드가 ATR 대비 30% 이내로 좁혀졌는가?
        atr_val = m.get('atr', 1)
        if atr_val == 0: atr_val = 1
        
        rel_spread = m.get('spread', 0) / (atr_val * 100 / m['last_price'] + 1e-9)
        if rel_spread < 0.3: score += 20 
        
        # 4. 건전성 (VPIN) (10점)
        if m.get('vpin', 1.0) < 0.8: score += 10
        
        return min(score, 100)

    # [교체] 기존 _check_filters 삭제 후 이 코드로 대체
    def _check_filters(self, m, strategy, ers_score):
        # 0. Warmup 확인
        if not self.is_ready():
            return False, "Warming Up"

        # 1. 유동성 확인 (스몰캡 기준 완화: $50,000)
        liq_1m = m.get('dollar_vol_1m', 0)
        if liq_1m < STS_BOT_MIN_LIQUIDITY_1M:
            return False, f"Low Liq ${int(liq_1m)}"

        # 2. 호가 잔량 확인 ($10k 이상이면 통과)
        book_usd = m.get('top5_book_usd', 0)
        if book_usd < 10_000: 
            return False, f"Thin Book ${int(book_usd)}"

        # 3. 독성 확인 (VPIN) - 1.0 초과시 차단
        if m.get('vpin', 0) > 1.0: 
            return False, "Toxic Flow"

        return True, "PASS"

    # [교체] 기존 update_dashboard_db 삭제 후 이 코드로 대체
    def update_dashboard_db(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        
        if agg_data and agg_data.get('vwap'): self.vwap = agg_data.get('vwap')
        if self.vwap == 0 and tick_data.get('p'): self.vwap = tick_data['p']

        # 1. Warmup Gate (준비 안됐으면 계산 중단 및 리턴)
        if not self.is_ready():
            # 상태를 WARM_UP으로 찍어서 DB에 알림 (모니터링용)
            if self.state != "WARM_UP":
                 self.state = "WARM_UP"
            return 

        m = self.analyzer.get_metrics()
        if not m or m.get('tick_speed', 0) == 0: return 
        
        if m.get('atr') and m['atr'] > 0: self.atr = m['atr']
        else: self.atr = max(self.selector.get_atr(self.ticker), m['last_price'] * 0.01)

        # 2. AI Score (옵션 - 기존 로직 유지)
        ai_prob = 0.0
        if self.model:
            try:
                features = [
                    m.get('obi', 0), m.get('obi_mom', 0), m.get('tick_accel', 0), m.get('vpin', 0), 
                    m.get('vwap_dist', 0), m.get('fibo_pos', 0.5), abs(m.get('fibo_pos', 0.5) - 0.382), 
                    m.get('bb_width_norm', 0), 1 if m.get('squeeze_ratio', 1) < 0.7 else 0, 
                    m.get('rv_60', 0), m.get('rvol', 0)
                ]
                features = [0 if (np.isnan(x) or np.isinf(x)) else x for x in features]
                dtest = xgb.DMatrix(np.array([features]), feature_names=['obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist','fibo_pos', 'fibo_dist_382', 'bb_width_norm', 'squeeze_flag', 'rv_60', 'vol_ratio_60'])
                ai_prob = self.model.predict(dtest)[0]
                self.prob_history.append(ai_prob)
                ai_prob = sum(self.prob_history) / len(self.prob_history)
            except: pass

        # 3. Score Calculation (ERS 중심)
        ers = self.calculate_ers(m)
        p = self._calculate_regime_p(m)
        
        strategy = "MOMENTUM" # 기본 전략
        
        # 필터 체크 (ERS 점수 전달)
        is_pass, reason = self._check_filters(m, strategy, ers)
        
        # 화면 표시용 점수는 ERS 사용
        display_score = ers if is_pass else 0

        # 4. DB Update (상태 변경 시 혹은 1.5초 주기)
        now = time.time()
        if (self.state != self.last_logged_state) or (now - self.last_db_update > 1.5):
            metrics_copy = copy.deepcopy(m)
            metrics_copy['regime_p'] = p
            asyncio.get_running_loop().run_in_executor(
                DB_WORKER_POOL, 
                partial(update_dashboard_db, self.ticker, metrics_copy, display_score, self.state)
            )
            self.last_db_update = now
            self.last_logged_state = self.state
            
        self.logger.log_replay({
            'timestamp': m['timestamp'], 'ticker': self.ticker, 'price': m['last_price'], 
            'vwap': self.vwap, 'atr': self.atr, 'obi': m['obi'], 
            'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 'ai_prob': ai_prob, 'regime_p': p,
            'ofi': m.get('ofi', 0), 'weighted_obi': m.get('weighted_obi', 0)
        })

        # 5. Zero-Latency Execution Logic
        if self.state == "WATCHING" and is_pass:
            # [Fast Track] ERS 80점 이상이면 즉시 사격
            if ers >= 80:
                 print(f"⚡ [FAST] {self.ticker} High ERS Trigger ({ers})", flush=True)
                 self.fire(m['last_price'], ai_prob, m, strategy="ZERO_LATENCY")
                 return
            
            # [Aiming Mode] ERS 60점 이상이면 조준 시작
            elif ers >= 60:
                 self.state = "AIMING"
                 self.aiming_start_time = time.time()
                 self.aiming_start_price = m['last_price']
                 print(f"👀 [AIM] {self.ticker} Start Aiming (ERS:{ers})...", flush=True)

        elif self.state == "AIMING":
            # 조준 중 가격이 튀거나 ERS가 급상승하면 발사
            price_change_pct = (m['last_price'] - self.aiming_start_price) / self.aiming_start_price * 100
            
            # 조건 1: 가격 상승 + ERS 유지
            if price_change_pct > 0.2 and ers >= 60:
                 self.fire(m['last_price'], ai_prob, m, strategy="MOMENTUM")
                 return
            
            # 조건 2: ERS 폭발
            if ers >= 80:
                 self.fire(m['last_price'], ai_prob, m, strategy="ZERO_LATENCY")
                 return

            # 타임아웃 (1초)
            if (time.time() - self.aiming_start_time) > 1.0 or price_change_pct < -0.1:
                self.state = "WATCHING"
                self.aiming_start_time = 0
            
        elif self.state == "FIRED":
            self.manage_position(m, m['last_price'])
    
    async def warmup(self):
        print(f"🔥 [Warmup] Fetching history for {self.ticker}...", flush=True)
        try:
            to_ts = int(time.time() * 1000)
            from_ts = to_ts - (180 * 1000) 
            url = f"https://api.polygon.io/v2/aggs/ticker/{self.ticker}/range/1/second/{from_ts}/{to_ts}"
            params = {"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": POLYGON_API_KEY}
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if 'results' in data: self.analyzer.inject_history(data['results'])
                    print(f"✅ [Warmup] {self.ticker} Ready!", flush=True)
        except Exception as e: 
            print(f"❌ [Warmup] Failed: {e}", flush=True)

    def fire(self, price, prob, metrics, strategy="MOMENTUM"):
        print(f"🔫 [FIRE] {self.ticker} ({strategy}) AI:{prob:.2f}", flush=True)
        self.state = "FIRED"
        
        tp_mult = 3.0 if strategy == "MOMENTUM" else 1.0
        sl_mult = 1.5 if strategy == "MOMENTUM" else 0.8
        volatility = max(self.atr, price * 0.005)
        tp_price = price + (volatility * tp_mult)
        sl_price = price - (volatility * sl_mult)

        self.position = {
            'entry': price, 'high': price, 'sl': sl_price, 'tp': tp_price,
            'atr': self.atr, 'strategy': strategy
        }
        
        asyncio.get_running_loop().run_in_executor(
            DB_WORKER_POOL, 
            partial(log_signal_to_db, self.ticker, price, prob*100, 
                    entry=price, tp=tp_price, sl=sl_price, strategy=strategy)
        )
        
        asyncio.create_task(send_fcm_notification(
            self.ticker, price, int(prob*100), entry=price, tp=tp_price, sl=sl_price
        ))
        
        self.logger.log_trade({
            'ticker': self.ticker, 'action': 'ENTRY', 'price': price, 'ai_prob': prob,
            'obi': metrics.get('obi', 0), 'obi_mom': metrics.get('obi_mom', 0),
            'tick_accel': metrics.get('tick_accel', 0), 'vpin': metrics.get('vpin', 0), 
            'vwap_dist': metrics.get('vwap_dist', 0), 'profit': 0
        })

    def manage_position(self, metrics, curr_price):
        pos = self.position
        if not pos: return 

        # 🔥 [V7.1 Emergency Exit] 시장 미시구조 악화 시 긴급 탈출
        # 1. VPIN(주문 독성)이 너무 높으면 -> 세력 이탈 가능성 -> 즉시 매도
        if metrics.get('vpin', 0) > 1.2:
            print(f"🚨 [EMERGENCY] {self.ticker} High VPIN ({metrics['vpin']:.2f})", flush=True)
            self._close_position(curr_price, "VPIN Alert")
            return

        # 2. OFI(주문 흐름)가 음수이고 가속도가 꺾이면 -> 힘 빠짐 -> Scale Out (전량 매도)
        if metrics.get('ofi', 0) < 0 and metrics.get('tick_accel', 0) < -1:
            if curr_price > pos['entry']:
                print(f"📉 [WEAK] {self.ticker} OFI Negative - Securing Profit", flush=True)
                self._close_position(curr_price, "Flow Weakness")
                return

        # 기존 Trailing Stop
        if curr_price > pos['high']: pos['high'] = curr_price
        
        trail = 2.0 if pos.get('strategy') == "MOMENTUM" else ATR_TRAIL_MULT
        exit_price = pos['high'] - (pos['atr'] * trail)
        
        is_tp = (curr_price >= pos['tp']) and (pos.get('strategy') == "REBOUND")
        is_sl = (curr_price < max(exit_price, pos['sl']))
        
        if is_tp or is_sl:
            self._close_position(curr_price, "TP/SL Hit")

    def _close_position(self, curr_price, reason):
        profit = (curr_price - self.position['entry']) / self.position['entry'] * 100
        print(f"💰 [EXIT] {self.ticker} ({reason}) Profit: {profit:.2f}%", flush=True)
        self.state = "WATCHING"
        self.position = {}
        self.logger.log_trade({
            'ticker': self.ticker, 'action': 'EXIT', 'price': curr_price, 'profit': profit,
            'ai_prob': 0, 'obi': 0, 'obi_mom': 0, 'tick_accel': 0, 'vpin': 0, 'vwap_dist': 0
        })
#=============================================================================
# 4. PIPELINE MANAGER
# ==============================================================================
class STSPipeline:
    def __init__(self):
        self.snipers = {}       
        self.candidates = []    
        self.last_quotes = {}
        self.selector = TargetSelector(api_key=POLYGON_API_KEY)
        # [수정 1] 마지막 Agg(A) 데이터를 저장할 공간 초기화
        self.last_agg = {}      
        
        self.logger = DataLogger()
        
        # 수신과 처리를 분리할 큐 생성
        self.msg_queue = asyncio.Queue(maxsize=100000)
        
        # 🟢 [수정됨] shared_model 삭제 -> model_bytes 추가
        # 이유: 모델 객체를 공유하면 충돌이 나므로, 바이트(RAM) 데이터로 들고 있다가 복제해서 씁니다.
        self.model_bytes = None 
        
        if os.path.exists(MODEL_FILE):
            print(f"🤖 [System] Loading AI Model to RAM: {MODEL_FILE}", flush=True)
            try:
                # 1. 모델을 임시 로드해서
                temp_booster = xgb.Booster()
                temp_booster.load_model(MODEL_FILE)
                
                # 2. 바이트(Bytearray) 형태로 메모리에 덤프를 뜹니다.
                self.model_bytes = temp_booster.save_raw("json") 
                
                print(f"✅ Model Loaded! Size: {len(self.model_bytes)} bytes", flush=True)
            except Exception as e: 
                print(f"❌ Load Error: {e}")

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

    async def task_global_scan(self):
        print("🔭 [Scanner] Started (Fast Mode: 20s)", flush=True)
        loop = asyncio.get_running_loop()

        while True:
            try:
                # [핵심 수정] DB 작업이 포함된 함수를 별도 스레드(DB_WORKER_POOL)로 격리
                # 이렇게 해야 메인 루프가 차단(Block)되지 않습니다.
                self.candidates = await loop.run_in_executor(
                    DB_WORKER_POOL, 
                    partial(self.selector.get_top_gainers_candidates, limit=10)
                )

                if self.candidates:
                    print(f"📋 [Top 10 Candidates] {self.candidates}", flush=True)
                
                self.selector.garbage_collect()
                await asyncio.sleep(20) 
            except Exception as e:
                print(f"⚠️ Scanner Warning: {e}", flush=True)
                # 에러 발생 시 상세 내용 출력 (디버깅용)
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)

    # [교체] 기존 task_focus_manager 삭제 후 이 코드로 대체
    async def task_focus_manager(self, ws, candidates=None):
        print("🎯 [Manager] Started (Staging Mode: 10 Candidates)", flush=True)
        
        while True:
            try:
                # 1. 반응 속도 향상 (5초 -> 2초)
                await asyncio.sleep(2.0)
                
                if not self.candidates: continue

                # -------------------------------------------------------------
                # [Phase 1] Staging Area 관리 (Top 10 구독 유지)
                # -------------------------------------------------------------
                # 스캐너가 가져온 후보군 중 상위 10개를 무조건 구독합니다.
                # (STS_TARGET_COUNT=3 대신 10개를 봅니다)
                staging_targets = self.candidates[:10]
                
                current_set = set(self.snipers.keys())
                new_set = set(staging_targets)
                
                # A. Detach (Top 10에서 밀려나면 구독 해지 - 방어 로직 적용됨)
                to_remove = current_set - new_set
                if to_remove:
                    real_remove_list = []
                    for t in to_remove:
                        if t in self.snipers:
                            bot = self.snipers[t]
                            
                            # 🛡️ [방어 로직 1] 중요한 상태(조준, 진입, 포지션 보유)면 삭제 금지
                            if bot.state in ['AIMING', 'FIRED'] or bot.position:
                                # 로그가 너무 많으면 아래 줄 주석 처리
                                # print(f"🛡️ [Protect] Keeping {t} (State: {bot.state})", flush=True)
                                continue
                            
                            # 🛡️ [방어 로직 2] 웜업 중이라면 최소 3분(180초)은 기회를 줌
                            if bot.state == 'WARM_UP' and (time.time() - bot.created_at < 180):
                                 continue

                            # 위 조건에 해당하지 않으면(그냥 멍때리는 중이면) 삭제
                            del self.snipers[t]
                            real_remove_list.append(t)
                    
                    # 실제로 삭제된 종목만 웹소켓 구독 취소
                    if real_remove_list:
                        unsubscribe_params = [f"T.{t}" for t in real_remove_list] + [f"Q.{t}" for t in real_remove_list]
                        await self.unsubscribe(ws, unsubscribe_params)
                        
                # B. Attach (Top 10에 새로 진입하면 구독 + 봇 생성 + 웜업 시작)
                to_add = new_set - current_set
                if to_add:
                    print(f"🚀 Staging (Warmup Start): {list(to_add)}", flush=True)
                    subscribe_params = [f"T.{t}" for t in to_add] + [f"Q.{t}" for t in to_add]
                    await self.subscribe(ws, subscribe_params)
                    
                    for t in to_add:
                        # 봇 생성
                        new_bot = SniperBot(t, self.logger, self.selector, self.model_bytes)
                        self.snipers[t] = new_bot 
                        # 생성 즉시 비동기로 과거 데이터 로딩(Warmup) 시작
                        asyncio.create_task(new_bot.warmup())

                # -------------------------------------------------------------
                # [Phase 2] Active Sniper 선정 (Top 3 승격 심사)
                # -------------------------------------------------------------
                # Staging된 10개 봇 중에서 '데이터가 준비되고(Ready)', 'ERS 점수가 높은' 상위 3개를 뽑습니다.
                ready_bots = []
                for ticker, bot in self.snipers.items():
                    # 웜업이 덜 된 봇은 평가에서 제외
                    if bot.is_ready():
                        # 현재 시점의 ERS(실행 점수) 계산
                        m = bot.analyzer.get_metrics()
                        if m:
                            score = bot.calculate_ers(m)
                            ready_bots.append((ticker, score))
                
                # ERS 점수 높은 순 정렬
                ready_bots.sort(key=lambda x: x[1], reverse=True)
                
                # Top 3 선정 (Active List)
                active_tickers = [x[0] for x in ready_bots[:3]]
                
                # (선택 사항) 현재 Active Target이 누구인지 가끔 로그 출력
                # if active_tickers:
                #     print(f"🔥 Active Snipers: {active_tickers}", flush=True)

                # 봇들에게 "너는 지금 주전 선수(Active)야"라고 알려줌 (옵션)
                for ticker, bot in self.snipers.items():
                    # 봇 객체에 is_active_target 속성을 동적으로 할당/갱신
                    bot.is_active_target = (ticker in active_tickers)

            except Exception as e:
                print(f"❌ Manager Error: {e}", flush=True)
                import traceback
                traceback.print_exc()
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
        asyncio.run(main_startup())

    except KeyboardInterrupt:
        print("\n🛑 [System] Bot stopped by user.", flush=True)
    except Exception as e:
        print(f"❌ [Fatal Error] Main loop crashed: {e}", flush=True)
        time.sleep(5)