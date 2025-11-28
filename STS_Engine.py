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
STS_MAX_SPREAD_PCT = 0.8      
STS_MAX_VPIN = 0.65           # [V5.3] 필터 완화 (0.55 -> 0.65)
OBI_LEVELS = 20               # [V5.3] 오더북 깊이 확장 (5 -> 20)

# 후보 선정(Target Selector) 필터 기준
STS_MIN_DOLLAR_VOL = 300000  # 최소 거래대금 $300k (약 4억원)
STS_MAX_PRICE = 30.0         # 최대 가격 $30 (저가주 집중)
STS_MIN_RVOL = 2.0           # (SniperBot 단계) 최소 상대 거래량
STS_MAX_SPREAD_ENTRY = 0.7   # (SniperBot 단계) 진입 허용 스프레드

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
THREAD_POOL = ThreadPoolExecutor(max_workers=3) # [V5.3] 알림 전송용 풀

# Global DB Pool
db_pool = None

# ==============================================================================
# 2. DATABASE & FIREBASE SETUP
# ==============================================================================
def init_db():
    """DB 커넥션 풀 및 테이블 초기화"""
    global db_pool
    if not DATABASE_URL: return
    try:
        if db_pool is None:
            # 봇용 연결 1개 (최적화)
            db_pool = psycopg2.pool.SimpleConnectionPool(2, 5, dsn=DATABASE_URL)
            print("✅ [DB] Connection Pool Initialized (Limit: 1)")
            
        conn = db_pool.getconn()
        cursor = conn.cursor()
        
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
        # [V5.3] score 컬럼 추가
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
        
        # 컬럼 추가 마이그레이션 (기존 테이블 대응)
        try:
            cursor.execute("ALTER TABLE signals ADD COLUMN score REAL")
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            
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
        query = """
        INSERT INTO sts_live_targets 
        (ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (ticker) DO UPDATE SET
            price = EXCLUDED.price,
            ai_score = EXCLUDED.ai_score,
            obi = EXCLUDED.obi,
            vpin = EXCLUDED.vpin,
            tick_speed = EXCLUDED.tick_speed,
            vwap_dist = EXCLUDED.vwap_dist,
            status = EXCLUDED.status,
            last_updated = NOW();
        """
        cursor.execute(query, (
            ticker, float(metrics['last_price']), float(score), 
            float(metrics['obi']), float(metrics['vpin']), 
            int(metrics['tick_speed']), float(metrics['vwap_dist']), status
        ))
        conn.commit()
        cursor.close()
    except Exception as e:
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

def log_signal_to_db(ticker, price, score):
    """[V5.3] Score 포함 저장"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO signals (ticker, price, score, time) VALUES (%s, %s, %s, %s)", 
                       (ticker, price, float(score), datetime.now()))
        conn.commit()
        cursor.close()
    except Exception as e:
        # print(f"❌ [DB Signal Error] {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# --- FCM Sending Logic ---
def _send_fcm_sync(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """[V5.3] ThreadPoolExecutor에서 실행될 동기 함수"""
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

        # 알림 내용 구성
        noti_title = f"💎 {ticker} 신호 (점수: {probability_score})"
        if entry and tp and sl:
            noti_body = f"진입: ${entry:.4f} | 익절: ${tp:.4f} | 손절: ${sl:.4f}"
        else:
            noti_body = f"현재가: ${price:.4f} | AI 점수: {probability_score}점"

        data_payload = {
            'type': 'hybrid_signal', 'ticker': ticker, 'price': str(price),
            'score': str(probability_score), 'title': noti_title, 'body': noti_body,
            'entry': str(entry) if entry else "", 'tp': str(tp) if tp else "", 'sl': str(sl) if sl else ""
        }
        
        failed_tokens = []
        for row in subscribers:
            token = row[0]
            user_min_score = row[1] if row[1] is not None else 0 
            if probability_score < user_min_score: continue

            try:
                message = messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=noti_title, body=noti_body),
                    data=data_payload,
                    android=messaging.AndroidConfig(
                        priority='high', 
                        notification=messaging.AndroidNotification(channel_id='high_importance_channel', priority='high', default_sound=True, visibility='public')
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(alert=messaging.ApsAlert(title=noti_title, body=noti_body), sound="default"))
                    )
                )
                messaging.send(message)
            except Exception as e:
                if "Requested entity was not found" in str(e): failed_tokens.append(token)
        
        if failed_tokens:
            c = conn.cursor()
            c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            c.close()

    except Exception:
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

async def send_fcm_notification(ticker, price, probability_score, entry=None, tp=None, sl=None):
    """[V5.3] ThreadPoolExecutor 사용"""
    loop = asyncio.get_running_loop()
    # THREAD_POOL 사용으로 메인 루프 블로킹 방지
    await loop.run_in_executor(THREAD_POOL, partial(_send_fcm_sync, ticker, price, probability_score, entry, tp, sl))


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
        df = self._resample_ohlc()
        if df is None or len(df) < 60: return None 
        
        df['vwap'] = ind.compute_intraday_vwap_series(df, 'close', 'volume')
        df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=600)
        _, df['bb_width_norm'], df['squeeze_flag'] = ind.compute_bb_squeeze(df['close'], window=20, mult=2, norm_window=300)
        df['rv_60'] = ind.compute_rv_60(df['close'])
        df['vol_ratio_60'] = ind.compute_vol_ratio_60(df['volume'])
        df['tick_accel'] = df['tick_speed'].diff().fillna(0)

        last = df.iloc[-1]
        
        raw_df = pd.DataFrame(list(self.raw_ticks)[-100:]) 
        signs = [ind.classify_trade_sign(r.p, r.bid, r.ask) for r in raw_df.itertuples()]
        signed_vol = raw_df['s'].values * np.array(signs)
        vpin = ind.compute_vpin(signed_vol)
        
        # [V5.3] OBI 깊이 20으로 확장
        bids = np.array([q['s'] for q in self.quotes.get('bids', [])[:OBI_LEVELS]])
        asks = np.array([q['s'] for q in self.quotes.get('asks', [])[:OBI_LEVELS]])
        obi = ind.compute_order_book_imbalance(bids, asks)
        
        obi_mom = obi - self.prev_obi
        self.prev_obi = obi
        
        vwap_dist = (last['close'] - last['vwap']) / last['vwap'] * 100 if last['vwap'] > 0 else 0
        fibo_dist_382 = abs(last['fibo_pos'] - 0.382)
        fibo_dist_618 = abs(last['fibo_pos'] - 0.618)
        
        best_bid = self.raw_ticks[-1]['bid']
        best_ask = self.raw_ticks[-1]['ask']
        spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0

        # [V5.3] vwap 값도 리턴 (Replay Log 저장용)
        return {
            'obi': obi, 'obi_mom': obi_mom, 'tick_accel': last['tick_accel'],
            'vpin': vpin, 'vwap_dist': vwap_dist,
            'fibo_pos': last['fibo_pos'], 'fibo_dist_382': fibo_dist_382, 'fibo_dist_618': fibo_dist_618,
            'bb_width_norm': last['bb_width_norm'], 'squeeze_flag': last['squeeze_flag'],
            'rv_60': last['rv_60'], 'vol_ratio_60': last['vol_ratio_60'],
            'spread': spread, 'last_price': last['close'], 'tick_speed': last['tick_speed'], 
            'timestamp': raw_df.iloc[-1]['t'],
            'vwap': last['vwap'] # 추가됨
        }

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

    # [핵심 수정] 3분 주기: RVOL 및 Liquidity 기반 Top 10 선정
    def get_top_gainers_candidates(self, limit=10):
        scored = []
        now = time.time()
        
        # 1. 전체 스캔
        for t, d in self.snapshots.items():
            if now - d['last_updated'] > 600: continue # 죽은 데이터 제외
            
            # [Filter 1] Price Cap: $30 이하 (저유동성/작전주 타겟팅)
            if d['c'] > STS_MAX_PRICE: continue
            
            # [Filter 2] Liquidity Floor: 거래대금 $300k 미만 칼같이 제외 (핵심)
            dollar_vol = d['c'] * d['v']
            if dollar_vol < STS_MIN_DOLLAR_VOL: continue

            # [Score Logic] 등락률 + 거래대금 가중치
            # 단순히 많이 오른 놈(X) -> 돈이 몰리면서 오르는 놈(O)
            change_pct = (d['c'] - d['start_price']) / d['start_price'] * 100
            
            # 등락률이 최소 1%는 되어야 의미 있음
            if change_pct < 1.0: continue

            # 점수 산정: 등락률 * log(거래대금) 
            # -> 거래량이 받쳐주는 상승일수록 높은 점수
            score = change_pct * np.log1p(dollar_vol)
            
            scored.append((t, score, change_pct, dollar_vol))
        
        # 점수 내림차순 정렬
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # 로그 출력 (디버깅용)
        if scored:
            print(f"🔎 [Scanner] Top Candidate: {scored[0][0]} (Chg:{scored[0][2]:.1f}% $Vol:{scored[0][3]/1000:.0f}k)", flush=True)

        return [x[0] for x in scored[:limit]]

    # [수정] 1분 주기: 후보군 중 거래량 가속도(Volume Velocity) Top 3 선정
    def get_best_snipers(self, candidates, limit=3):
        scored = []
        for t in candidates:
            if t not in self.snapshots: continue
            d = self.snapshots[t]
            # 여기서는 단순히 누적 거래량이 아니라 '거래대금'이 가장 큰 놈을 우선시
            # (이미 Top 10에서 필터링 되었으므로, 그 중 대장주를 뽑음)
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

    def on_data(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        if agg_data:
            self.vwap = agg_data.get('vwap', tick_data['p'])
            self.atr = self.selector.get_atr(self.ticker)

        m = self.analyzer.get_metrics()
        
        # [수정 1] 데이터 예열 중(Warm-up)이라도 화면에 띄우기
        if not m:
            now = time.time()
            # 2초마다 DB에 생존 신고 (화면에 'WARM_UP' 표시됨)
            if now - self.last_db_update > 2.0:
                dummy_metrics = {
                    'last_price': tick_data['p'], 'obi': 0, 'vpin': 0, 
                    'tick_speed': 0, 'vwap_dist': 0
                }
                update_dashboard_db(self.ticker, dummy_metrics, 0, "WARM_UP")
                self.last_db_update = now
            return

        # [수정 2] 스프레드/RVOL 필터 (DB 저장 전에 return 하지 않음!)
        # 상태 메시지를 결정하기 위한 플래그
        is_bad_spread = m['spread'] > STS_MAX_SPREAD_ENTRY # 0.7% 이상이면 나쁨
        is_low_vol = m['vol_ratio_60'] < 1.0 # 평소보다 거래량 없으면 나쁨

        # [기존 AI 로직]
        prob = 0.0
        if self.model:
            try:
                input_data = np.array([[
                    m['obi'], m['obi_mom'], m['tick_accel'], m['vpin'], m['vwap_dist'],
                    m['fibo_pos'], m['fibo_dist_382'], m['fibo_dist_618'], 
                    m['bb_width_norm'], m['squeeze_flag'],
                    m['rv_60'], m['vol_ratio_60']
                ]])
                dtest = xgb.DMatrix(input_data)
                raw_prob = self.model.predict(dtest)[0]
                self.prob_history.append(raw_prob)
                prob = sum(self.prob_history) / len(self.prob_history)
            except: pass

        # [수정 3] DB 업데이트를 가장 먼저 수행 (화면 표시 보장)
        now = time.time()
        is_hot = (prob * 100) >= 60
        force_update = (self.state != self.last_logged_state)
        
        # 상태 메시지 결정 (화면에 보여줄 텍스트)
        display_status = self.state
        if self.state == "WATCHING":
            if is_bad_spread: display_status = "BAD_SPREAD"
            elif is_low_vol: display_status = "LOW_VOL"

        # VPIN(독성)이 너무 높으면 필터링 (단, DB엔 기록 남김)
        if m['vpin'] > STS_MAX_VPIN:
             if self.state == "WATCHING": display_status = "TOXIC_FLOW"

        if force_update or (now - self.last_db_update > (1.0 if is_hot else 2.0)):
            score_to_save = prob * 100
            update_dashboard_db(self.ticker, m, score_to_save, display_status)
            self.last_db_update = now
            self.last_logged_state = self.state

        # [수정 4] 실제 진입 로직 차단 (Bad Condition일 경우)
        # 이미 진입한 상태(FIRED)가 아니라면, 조건 나쁠 때 진입 금지
        if self.state != "FIRED":
            if is_bad_spread or is_low_vol or m['vpin'] > STS_MAX_VPIN:
                return 

        # --- FSM (상태 머신) ---
        # [V5.3] Replay Log 저장
        self.logger.log_replay({
            'timestamp': m['timestamp'], 'ticker': self.ticker, 
            'price': m['last_price'], 'vwap': m['vwap'], 'atr': self.atr,
            'obi': m['obi'], 'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 
            'ai_prob': prob
        })

        if self.state == "WATCHING":
            dist = (m['last_price'] - self.vwap) / self.vwap * 100
            cond_dist = 0.2 < dist < 2.0
            cond_sqz = m['squeeze_flag'] == 1
            cond_accel = m['tick_accel'] > 0
            
            # [핵심] RVOL > 2.0 (평소 대비 2배 거래량) 조건 추가
            cond_vol = m['vol_ratio_60'] >= STS_MIN_RVOL 
            
            if cond_dist and (cond_sqz or prob > 0.7) and cond_accel and cond_vol:
                self.state = "AIMING"
                print(f"👀 [조준] {self.ticker} (Prob:{prob:.2f} | RVOL:{m['vol_ratio_60']:.1f})", flush=True)

        elif self.state == "AIMING":
            if m['tick_accel'] < -3 and prob < 0.55:
                self.state = "WATCHING"
                return

            if prob >= AI_PROB_THRESHOLD:
                self.fire(m['last_price'], prob, m)

        elif self.state == "FIRED":
            self.manage_position(m['last_price'])

    def fire(self, price, prob, metrics):
        print(f"🔫 [격발] {self.ticker} AI_Prob:{prob:.4f} Price:${price:.4f}", flush=True)
        self.state = "FIRED"
        self.position = {
            'entry': price, 'high': price,
            'sl': price - (self.atr * 0.5),
            'atr': self.atr
        }
        
        # [V5.3] Score 포함 저장
        log_signal_to_db(self.ticker, price, prob*100)
        
        tp_price = price + (self.atr * ATR_TRAIL_MULT)
        
        # [V5.3] ThreadPool로 알림 전송
        asyncio.create_task(send_fcm_notification(
            self.ticker, price, int(prob*100), 
            entry=price, tp=tp_price, sl=self.position['sl']
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
        self.logger = DataLogger()
        
        # [핵심 변경] 수신과 처리를 분리할 큐 생성
        self.msg_queue = asyncio.Queue(maxsize=100000)
        
        self.shared_model = None
        if os.path.exists(MODEL_FILE):
            print(f"🤖 [System] Loading AI Model: {MODEL_FILE}", flush=True)
            try:
                self.shared_model = xgb.Booster()
                self.shared_model.load_model(MODEL_FILE)
            except Exception as e: print(f"❌ Load Error: {e}")

            # ==========================================================
    # [누락된 함수 추가] STSPipeline 클래스 내부 메서드로 추가하세요
    # ==========================================================
    async def subscribe(self, ws, params):
        """Polygon 웹소켓 구독 요청 전송"""
        try:
            # 리스트로 들어오면 콤마로 합치기, 문자열이면 그대로 사용
            if isinstance(params, list):
                params_str = ",".join(params)
            else:
                params_str = params
                
            req = {"action": "subscribe", "params": params_str}
            await ws.send(json.dumps(req))
            print(f"📡 [Sub] Request sent: {params_str}", flush=True)
        except Exception as e:
            print(f"❌ [Sub Error] {e}", flush=True)

    async def unsubscribe(self, ws, params):
        """Polygon 웹소켓 구독 취소 요청 전송"""
        try:
            if isinstance(params, list):
                params_str = ",".join(params)
            else:
                params_str = params
                
            req = {"action": "unsubscribe", "params": params_str}
            await ws.send(json.dumps(req))
            print(f"🔕 [Unsub] Request sent: {params_str}", flush=True)
        except Exception as e:
            print(f"❌ [Unsub Error] {e}", flush=True)

    async def connect(self):
        init_db()
        init_firebase()
        
        if not POLYGON_API_KEY:
            print("❌ [CRITICAL] POLYGON_API_KEY가 없습니다! 환경변수를 확인하세요.", flush=True)
            # return을 지우고 무한 대기
            while True:
                await asyncio.sleep(60)

        while True:
            try:
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=20) as ws:
                    print("✅ [STS V5.3] Pipeline Started with Scheduler", flush=True)
                    
                    await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                    _ = await ws.recv()

                    # 1. 초기 구독: 전체 Agg(A.*)만 구독하여 Top 10 발굴 시작
                    await self.subscribe(ws, ["A.*"])

                    # 2. 태스크 분리 실행 (Producer는 아래 메인 루프에서 실행)
                    # Consumer (데이터 처리 워커)
                    worker_task = asyncio.create_task(self.worker())
                    # 3분 주기 스캐너 (Top 10)
                    scanner_task = asyncio.create_task(self.task_global_scan())
                    # 1분 주기 매니저 (Top 3 & 구독 관리)
                    manager_task = asyncio.create_task(self.task_focus_manager(ws))

                    # 3. 메인 루프: 데이터 수신 (Producer) - 멈추지 않음
                    await self.producer(ws)

            except (websockets.ConnectionClosed, asyncio.TimeoutError):
                print("⚠️ Reconnecting...", flush=True)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ Critical Error: {e}", flush=True)
                await asyncio.sleep(5)

    # [신규] Producer: 데이터를 큐에 넣기만 함 (논블로킹)
    async def producer(self, ws):
        async for msg in ws:
            try:
                self.msg_queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass # 큐가 꽉 차면 최신 데이터를 위해 드랍

    # [신규] Consumer: 큐에서 꺼내서 파싱 및 처리
    async def worker(self):
        while True:
            msg = await self.msg_queue.get()
            try:
                # [위치 이동] JSON 파싱을 여기서 수행
                data = json.loads(msg)
                
                for item in data:
                    ev, t = item.get('ev'), item.get('sym')
                    
                    if ev == 'A': 
                        self.selector.update(item) # 전체 감시
                    
                    elif ev == 'Q':
                        self.last_quotes[t] = {
                            'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                            'asks': [{'p':item.get('ap'),'s':item.get('as')}]
                        }
                    
                    # Top 3 종목만 정밀 타격 로직(AI) 수행
                    elif ev == 'T' and t in self.snipers:
                        self.snipers[t].on_data(
                            item, 
                            self.last_quotes.get(t, {'bids':[],'asks':[]}), 
                            item 
                        )
            except Exception: pass
            finally:
                self.msg_queue.task_done()

    # [신규] 3분 주기: Top 10 후보군 갱신
    async def task_global_scan(self):
        print("🔭 [Scanner] Started (3 min interval)", flush=True)
        while True:
            try:
                await asyncio.sleep(180) # 3분 대기
                self.candidates = self.selector.get_top_gainers_candidates(limit=10)
                print(f"📋 [Top 10 Candidates] {self.candidates}", flush=True)
                self.selector.garbage_collect()
            except Exception: pass

    async def task_focus_manager(self, ws, candidates=None): # candidates 인자 유연하게 처리
        """[1분 주기] Top 10 중 Top 3 선정 및 구독 변경"""
        print("🎯 [Manager] Started (1 min interval)", flush=True)
        while True:
            try:
                await asyncio.sleep(60) # 1분 대기
                if not self.candidates: continue

                # Top 10 후보군 중에서 Top 3 선정
                target_top3 = self.selector.get_best_snipers(self.candidates, limit=STS_TARGET_COUNT)
                
                current_set = set(self.snipers.keys())
                new_set = set(target_top3)
                
                # 1. 탈락한 종목 -> 구독 해지 (수정됨)
                to_remove = current_set - new_set
                if to_remove:
                    print(f"👋 Detach: {list(to_remove)}", flush=True)
                    # [FIX] T.* 와 Q.*를 명확히 분리하여 리스트 병합
                    unsubscribe_params = [f"T.{t}" for t in to_remove] + [f"Q.{t}" for t in to_remove]
                    await self.unsubscribe(ws, unsubscribe_params)
                    
                    for t in to_remove: 
                        if t in self.snipers: del self.snipers[t]

                # 2. 신규 진입 종목 -> 구독 시작 (수정됨)
                to_add = new_set - current_set
                if to_add:
                    print(f"🚀 Attach: {list(to_add)}", flush=True)
                    # [FIX] T.* 와 Q.*를 명확히 분리하여 리스트 병합
                    subscribe_params = [f"T.{t}" for t in to_add] + [f"Q.{t}" for t in to_add]
                    await self.subscribe(ws, subscribe_params)
                    
                    for t in to_add:
                        self.snipers[t] = SniperBot(t, self.logger, self.selector, self.shared_model)

            except Exception as e:
                print(f"❌ Manager Error: {e}")
                # ==============================================================================
# 5. MAIN EXECUTION (실행 진입점)
# ==============================================================================
if __name__ == "__main__":
    # 윈도우 환경에서 실행 시 asyncio 루프 정책 충돌 방지 (혹시 로컬 테스트할 경우 대비)
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        print("🚀 [System] Initializing STS Sniper Bot...", flush=True)
        
        # 파이프라인 인스턴스 생성
        pipeline = STSPipeline()
        
        # 비동기 루프 시작 (여기서 무한 루프가 돕니다)
        asyncio.run(pipeline.connect())

    except KeyboardInterrupt:
        print("\n🛑 [System] Bot stopped by user.", flush=True)
    except Exception as e:
        print(f"❌ [Fatal Error] Main loop crashed: {e}", flush=True)
        # 치명적 오류 발생 시 5초 대기 후 종료 (로그 확인할 시간 확보)
        time.sleep(5)