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
STS_MAX_SPREAD_PCT = 1.0      
STS_MAX_VPIN = 0.65           # [V5.3] 필터 완화 (0.55 -> 0.65)
OBI_LEVELS = 20               # [V5.3] 오더북 깊이 확장 (5 -> 20)

# 후보 선정(Target Selector) 필터 기준
STS_MIN_DOLLAR_VOL = 200000  # 최소 거래대금 $300k (약 4억원)
STS_MAX_PRICE = 50.0         # 최대 가격 $30 (저가주 집중)
STS_MIN_RVOL = 1.5           # (SniperBot 단계) 최소 상대 거래량
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
        # 1. 틱이 너무 적으면(5개 미만) 아예 계산 포기 (정상)
        if len(self.raw_ticks) < 5: return None
        
        df = pd.DataFrame(self.raw_ticks).set_index('t')
        ohlcv = df['p'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
        volume = df['s'].resample('1s').sum()
        tick_count = df['s'].resample('1s').count()
        
        df_res = pd.concat([ohlcv, volume, tick_count], axis=1).iloc[-600:]
        df_res.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
        
        # [중요] 거래 없는 시간은 직전 가격 유지
        df = df_res.ffill().fillna(0)
        
        # 보정 후에도 데이터가 5개 미만이면 리턴
        if len(df) < 5: return None 
        
        try:
            # [수정됨] 여기서부터 들여쓰기가 한 칸 더 들어가야 합니다!
            df['vwap'] = ind.compute_intraday_vwap_series(df, 'close', 'volume')
            df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=600)
            _, df['bb_width_norm'], df['squeeze_flag'] = ind.compute_bb_squeeze(df['close'], window=20, mult=2, norm_window=300)
            df['rv_60'] = ind.compute_rv_60(df['close'])
            df['vol_ratio_60'] = ind.compute_vol_ratio_60(df['volume'])
            df['tick_accel'] = df['tick_speed'].diff().fillna(0)
            
            # NaN을 0으로 채움 (AI 입력 오류 방지)
            df = df.fillna(0)

            last = df.iloc[-1]
            raw_df = pd.DataFrame(list(self.raw_ticks)[-100:]) 
            
            if len(raw_df) < 1: return None 

            signs = [ind.classify_trade_sign(r.p, r.bid, r.ask) for r in raw_df.itertuples()]
            signed_vol = raw_df['s'].values * np.array(signs)
            vpin = ind.compute_vpin(signed_vol)
            
            bids = np.array([q['s'] for q in self.quotes.get('bids', [])[:OBI_LEVELS]])
            asks = np.array([q['s'] for q in self.quotes.get('asks', [])[:OBI_LEVELS]])
            obi = ind.compute_order_book_imbalance(bids, asks)
            
            obi_mom = obi - self.prev_obi
            self.prev_obi = obi
            
            vwap_dist = (last['close'] - last['vwap']) / last['vwap'] * 100 if last['vwap'] > 0 else 0
            
            best_bid = self.raw_ticks[-1]['bid']
            best_ask = self.raw_ticks[-1]['ask']
            # 0 나누기 방지
            if best_bid > 0:
                spread = (best_ask - best_bid) / best_bid * 100 
            else:
                spread = 0

            return {
                'obi': obi, 'obi_mom': obi_mom, 'tick_accel': last['tick_accel'],
                'vpin': vpin, 'vwap_dist': vwap_dist,
                'fibo_pos': last['fibo_pos'], 
                'fibo_dist_382': abs(last['fibo_pos'] - 0.382),
                'fibo_dist_618': abs(last['fibo_pos'] - 0.618),
                'bb_width_norm': last['bb_width_norm'], 'squeeze_flag': last['squeeze_flag'],
                'rv_60': last['rv_60'], 'vol_ratio_60': last['vol_ratio_60'],
                'spread': spread, 'last_price': last['close'], 'tick_speed': last['tick_speed'], 
                'timestamp': raw_df.iloc[-1]['t'], 'vwap': last['vwap']
            }
        except Exception as e:
            # print(f"Metric Calc Error: {e}")
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
                    last_updated = NOW()
                WHERE sts_live_targets.status != 'FIRED'; -- 이미 발사된 건 건드리지 않음
                """
                cursor.execute(query, (t, d['c'], score)) 
            
            conn.commit()
            cursor.close()
        except Exception as e:
            print(f"❌ [DB Save Error] {e}", flush=True)
            if conn: conn.rollback()
        finally:
            if conn: db_pool.putconn(conn)

    # [핵심 수정] 3분 주기: RVOL 및 Liquidity 기반 Top 10 선정
    def get_top_gainers_candidates(self, limit=10):
        scored = []
        now = time.time()
        
        # 1. 전체 스캔
        for t, d in self.snapshots.items():
            if now - d['last_updated'] > 600: continue # 죽은 데이터 제외
            
            # [Filter 1] Price Cap: $50 이하
            if d['c'] > STS_MAX_PRICE: continue
            
            # [Filter 2] Liquidity Floor: 거래대금 필터
            dollar_vol = d['c'] * d['v']
            if dollar_vol < STS_MIN_DOLLAR_VOL: continue

            # [Score Logic] 등락률 + 거래대금 가중치
            change_pct = (d['c'] - d['start_price']) / d['start_price'] * 100
            
            if change_pct < 1.0: continue

            score = change_pct * np.log1p(dollar_vol)
            scored.append((t, score, change_pct, dollar_vol))
        
        # 점수 내림차순 정렬
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Top 10 추출
        top_list = scored[:limit]

        # 로그 출력
        if top_list:
            # 🔥 [핵심] 찾은 놈들을 DB에 저장해라! (그래야 UI에 뜸)
            self.save_candidates_to_db(top_list)
            print(f"🔎 [Scanner] Top Candidate: {top_list[0][0]} (Chg:{top_list[0][2]:.1f}%) -> Saved to DB", flush=True)

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

    def on_data(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        
        # [수정 1] VWAP 안전 확보 (Agg가 없으면 Analyzer나 현재가로 대체)
        if agg_data and agg_data.get('vwap'):
            self.vwap = agg_data.get('vwap')
        
        # [복구 완료] ATR 업데이트 (이게 있어야 TP/SL이 종목에 맞춰짐)
        if agg_data:
            # agg_data가 있을 때만 갱신 (없으면 기존 값 유지)
            current_atr = self.selector.get_atr(self.ticker)
            if current_atr > 0:
                self.atr = current_atr

        m = self.analyzer.get_metrics()

        # [핵심] 데이터 부족으로 지표(m)가 없으면 -> 'WARM_UP' 상태로 DB 업데이트하고 종료
        if not m:
            now = time.time()
            # 2초마다 갱신 (너무 자주 DB 때리지 않게)
            if now - self.last_db_update > 2.0:
                # 점수 0점, 상태 'WARM_UP'으로 저장 -> UI에서 필터링 가능
                dummy_metrics = {'last_price': tick_data['p'], 'obi': 0, 'vpin': 0, 'tick_speed': 0, 'vwap_dist': 0}
                update_dashboard_db(self.ticker, dummy_metrics, 0, "WARM_UP")
                self.last_db_update = now
            return # 여기서 끝냄. (억지로 아래 로직 실행 안 함)
        
        # [수정 2] VWAP 2차 방어 (Agg 데이터가 없을 때)
        if self.vwap == 0 and m and m.get('vwap'):
            self.vwap = m['vwap']
            
        # [수정 3] VWAP 3차 방어 (정 안되면 현재가 사용 - 0 나누기 에러 방지)
        if self.vwap == 0:
            self.vwap = tick_data['p']

        # 데이터 예열 중 처리
        if not m:
            now = time.time()
            if now - self.last_db_update > 2.0:
                dummy_metrics = {'last_price': tick_data['p'], 'obi': 0, 'vpin': 0, 'tick_speed': 0, 'vwap_dist': 0}
                update_dashboard_db(self.ticker, dummy_metrics, 0, "WARM_UP")
                self.last_db_update = now
            return

        is_bad_spread = m['spread'] > STS_MAX_SPREAD_ENTRY 
        is_low_vol = m['vol_ratio_60'] < 1.0 

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
            except Exception as e:
                print(f"⚠️ [AI Fail] {self.ticker}: {e}", flush=True)
                pass

        now = time.time()
        is_hot = (prob * 100) >= 60
        force_update = (self.state != self.last_logged_state)
        display_status = self.state
        
        if self.state == "WATCHING":
            if is_bad_spread: display_status = "BAD_SPREAD"
            elif is_low_vol: display_status = "LOW_VOL"

        if m['vpin'] > STS_MAX_VPIN and self.state == "WATCHING": display_status = "TOXIC_FLOW"

        if force_update or (now - self.last_db_update > (1.0 if is_hot else 2.0)):
            score_to_save = prob * 100
            update_dashboard_db(self.ticker, m, score_to_save, display_status)
            self.last_db_update = now
            self.last_logged_state = self.state

        if self.state != "FIRED":
            if is_bad_spread or is_low_vol or m['vpin'] > STS_MAX_VPIN: return 

        self.logger.log_replay({
            'timestamp': m['timestamp'], 'ticker': self.ticker, 'price': m['last_price'], 
            'vwap': self.vwap, 'atr': self.atr, 'obi': m['obi'], 
            'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 'ai_prob': prob
        })

        if self.state == "WATCHING":
            if self.vwap > 0:
                dist = (m['last_price'] - self.vwap) / self.vwap * 100
            else:
                dist = 0
                
            cond_dist = 0.2 < dist < 2.0
            cond_sqz = m['squeeze_flag'] == 1
            cond_accel = m['tick_accel'] > 0
            cond_vol = m['vol_ratio_60'] >= STS_MIN_RVOL 
            
            if prob > 0.5:
                print(f"🧐 [Watch] {self.ticker} P:{prob:.2f} V:{cond_vol} S:{cond_sqz}", flush=True)

            if cond_dist and (cond_sqz or prob > 0.65) and cond_accel and cond_vol:
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
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=20) as ws:
                    print("✅ [STS V5.3] Pipeline Started with Scheduler", flush=True)
                    
                    await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                    _ = await ws.recv()

                    # 초기 구독: 전체 Agg(A.*) 구독
                    await self.subscribe(ws, ["A.*"])

                    # 태스크 실행
                    asyncio.create_task(self.worker())
                    asyncio.create_task(self.task_global_scan())
                    asyncio.create_task(self.task_focus_manager(ws))

                    # 메인 루프: 데이터 수신
                    await self.producer(ws)

            except (websockets.ConnectionClosed, asyncio.TimeoutError):
                print("⚠️ Reconnecting...", flush=True)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ Critical Error: {e}", flush=True)
                await asyncio.sleep(5)

    # [4] Producer
    async def producer(self, ws):
        async for msg in ws:
            try: self.msg_queue.put_nowait(msg)
            except asyncio.QueueFull: pass 

    # [5] Worker (데이터 연결 로직 수정됨)
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
                    
                    elif ev == 'Q':
                        self.last_quotes[t] = {
                            'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                            'asks': [{'p':item.get('ap'),'s':item.get('as')}]
                        }
                    
                    # Top 3 종목 정밀 타격 로직
                    elif ev == 'T' and t in self.snipers:
                        # [수정 3] item(T) 대신 저장해둔 last_agg(A)를 넘김
                        # 이렇게 해야 VWAP, High, Low 정보를 봇이 계산할 수 있음
                        current_agg = self.last_agg.get(t)
                        
                        self.snipers[t].on_data(
                            item, 
                            self.last_quotes.get(t, {'bids':[],'asks':[]}), 
                            current_agg  # <-- 여기가 T대신 A를 넘기는 핵심 포인트
                        )
            except Exception: pass
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