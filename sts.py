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
from concurrent.futures import ThreadPoolExecutor
import firebase_admin
from firebase_admin import credentials, messaging
import traceback
import redis # Redis 추가
# 커스텀 지표 모듈 (같은 폴더에 있어야 함)
import indicators_sts as ind 

# ==============================================================================
# 1. 설정 및 상수
# ==============================================================================
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
REDIS_URL = os.environ.get('REDIS_URL') # Redis URL
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
WS_URI = "wss://socket.polygon.io/stocks"

# 전략 설정
STS_TARGET_COUNT = 3  # 스캐너가 10개 줘도, 그 중 3개만 집중 타격
STS_MAX_VPIN = 0.65
OBI_LEVELS = 20
STS_MIN_RVOL = 1.5
STS_MAX_SPREAD_ENTRY = 0.9

# AI 및 파일 설정
MODEL_FILE = "sts_xgboost_model.json"
AI_PROB_THRESHOLD = 0.85      
ATR_TRAIL_MULT = 1.5          
TRADE_LOG_FILE = "sts_trade_log_v5.csv"
REPLAY_LOG_FILE = "sts_replay_data_v5.csv"

# 시스템 설정
THREAD_POOL = ThreadPoolExecutor(max_workers=3)
db_pool = None
redis_client = None # Redis 클라이언트

# ==============================================================================
# 2. 초기화 함수들 (DB, Firebase, Redis)
# ==============================================================================
def init_redis():
    """Redis 연결"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        print("✅ [STS] Redis Connected.")
    except Exception as e:
        print(f"❌ [STS] Redis Fail: {e}")

def init_db():
    global db_pool
    if not DATABASE_URL: return
    try:
        if db_pool is None:
            db_pool = psycopg2.pool.SimpleConnectionPool(2, 5, dsn=DATABASE_URL)
        # 테이블 생성 로직은 fetcher/scanner가 했다고 가정하고 생략하거나 유지 가능
        print("✅ [STS] DB Connected.")
    except Exception as e:
        print(f"❌ [STS Init Error] {e}")

def get_db_connection():
    global db_pool
    if db_pool is None: init_db()
    return db_pool.getconn()

def init_firebase():
    """Firebase 초기화 (기존 로직 유지)"""
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR: return
        if firebase_admin._apps: return
        json_str = FIREBASE_ADMIN_SDK_JSON_STR.strip()
        if json_str.startswith("'") and json_str.endswith("'"): json_str = json_str[1:-1]
        try:
            cred_dict = json.loads(json_str)
        except:
            cred_dict = json.loads(json_str.replace('\\n', '\n'))
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("✅ [STS] Firebase Init Success")
    except Exception as e:
        print(f"❌ [STS FCM Error] {e}")

# (DB 저장 및 FCM 전송 함수들은 기존 코드 그대로 사용 - 지면 관계상 생략하지만 필수 포함)
# update_dashboard_db, log_signal_to_db, send_fcm_notification 등...
# 기존 코드의 함수들을 여기에 그대로 두셔야 합니다.

def update_dashboard_db(ticker, metrics, score, status):
    """대시보드 DB 업데이트"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
        INSERT INTO sts_live_targets 
        (ticker, price, ai_score, obi, vpin, tick_speed, vwap_dist, status, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (ticker) DO UPDATE SET
            price = EXCLUDED.price, ai_score = EXCLUDED.ai_score,
            obi = EXCLUDED.obi, vpin = EXCLUDED.vpin,
            tick_speed = EXCLUDED.tick_speed, vwap_dist = EXCLUDED.vwap_dist,
            status = EXCLUDED.status, last_updated = NOW();
        """
        cursor.execute(query, (
            ticker, float(metrics['last_price']), float(score), 
            float(metrics['obi']), float(metrics['vpin']), 
            int(metrics['tick_speed']), float(metrics['vwap_dist']), status
        ))
        conn.commit()
        cursor.close()
    except Exception:
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

def log_signal_to_db(ticker, price, score):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO signals (ticker, price, score, time) VALUES (%s, %s, %s, %s)", 
                       (ticker, price, float(score), datetime.now()))
        conn.commit()
        cursor.close()
    except Exception:
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

def _send_fcm_sync(ticker, price, probability_score, entry=None, tp=None, sl=None):
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

        noti_title = f"💎 {ticker} SIGNAL (SCORE {probability_score})"
        noti_body = f"현재가: ${price:.4f} | AI 점수: {probability_score}점"
        
        # ... (기존 FCM 로직 유지) ...
        # (생략: 위쪽 코드와 동일)
    except Exception:
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

async def send_fcm_notification(ticker, price, probability_score, entry=None, tp=None, sl=None):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(THREAD_POOL, partial(_send_fcm_sync, ticker, price, probability_score, entry, tp, sl))


# ==============================================================================
# 3. 분석 클래스 (기존 로직 유지)
# ==============================================================================
class DataLogger:
    # (기존 DataLogger 코드 그대로 유지)
    def __init__(self):
        self.trade_file = TRADE_LOG_FILE
        self.replay_file = REPLAY_LOG_FILE
        # ... 파일 초기화 로직 ...
    def log_trade(self, data): pass # (내용 유지)
    def log_replay(self, data): pass # (내용 유지)

class MicrostructureAnalyzer:
    # (기존 MicrostructureAnalyzer 코드 그대로 유지 - 중요 로직)
    def __init__(self):
        self.raw_ticks = deque(maxlen=3000)
        self.quotes = {'bids': [], 'asks': []}
        self.prev_tick_speed = 0
        self.prev_obi = 0
    
    def inject_history(self, aggs): pass # (내용 유지)
    def update_tick(self, tick_data, current_quotes): pass # (내용 유지)
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
            # 🔥 [긴급 수정] 주석 해제하고 에러를 출력하게 변경!
            import traceback
            print(f"❌ [Metric Calc Error] {self.ticker if hasattr(self, 'ticker') else 'Unknown'}: {e}", flush=True)
            traceback.print_exc() # 에러가 난 줄번호까지 추적
            return None
        

# ==============================================================================
# 4. 봇 클래스 (SniperBot) - TargetSelector 제거됨!
# ==============================================================================
class SniperBot:
    # (기존 SniperBot 코드 99% 유지)
    def __init__(self, ticker, logger, shared_model):
        self.ticker = ticker
        self.logger = logger
        # self.selector 제거됨 (필요없음)
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
        
        if agg_data and agg_data.get('vwap'): self.vwap = agg_data.get('vwap')
        # ATR은 기본값 0.05 혹은 자체 계산 (Selector 의존성 제거)
        
        m = self.analyzer.get_metrics()
        if not m: return # Warmup

        # ... (AI 예측, Fire 로직 등 기존 SniperBot 코드 그대로 복사 붙여넣기) ...
        # (중요: selector.get_atr() 호출하는 부분이 있다면 그냥 self.atr = m['last_price'] * 0.01 등으로 대체)

    async def warmup(self):
        # (기존 Warmup 코드 유지)
        pass
    
    def fire(self, price, prob, metrics):
        # (기존 Fire 코드 유지)
        pass
        
    def manage_position(self, curr_price):
        # (기존 Manage Position 코드 유지)
        pass

# ==============================================================================
# 5. STS 파이프라인 (핵심 변경: Redis에서 후보군 받기)
# ==============================================================================
class STSPipeline:
    def __init__(self):
        # Selector 삭제! (스캐너가 대신 함)
        self.snipers = {}       # 현재 활성 봇 (최대 3개)
        self.candidates = []    # Redis에서 받아온 후보군
        self.logger = DataLogger() # 껍데기만 씀 (파일저장용)
        self.msg_queue = asyncio.Queue(maxsize=10000)
        
        # 모델 로딩
        self.shared_model = None
        if os.path.exists(MODEL_FILE):
            try:
                self.shared_model = xgb.Booster()
                self.shared_model.load_model(MODEL_FILE)
                print(f"🤖 [STS] Model Loaded: {MODEL_FILE}")
            except Exception as e: print(f"❌ Load Error: {e}")

    async def subscribe(self, ws, params):
        if not params: return
        req = {"action": "subscribe", "params": ",".join(params)}
        await ws.send(json.dumps(req))
        print(f"📡 [STS] Subscribe: {params}", flush=True)

    async def unsubscribe(self, ws, params):
        if not params: return
        req = {"action": "unsubscribe", "params": ",".join(params)}
        await ws.send(json.dumps(req))
        print(f"🔕 [STS] Unsubscribe: {params}", flush=True)

    async def connect(self):
        init_db(); init_firebase(); init_redis()
        
        if not POLYGON_API_KEY:
            print("❌ API KEY Missing")
            return

        while True:
            try:
                async with websockets.connect(WS_URI) as ws:
                    print("✅ [STS] WebSocket Connected", flush=True)
                    await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                    _ = await ws.recv()

                    # 태스크 실행
                    asyncio.create_task(self.worker())
                    asyncio.create_task(self.task_redis_sync()) # 스캐너랑 통신
                    asyncio.create_task(self.task_focus_manager(ws))

                    # 메인 루프
                    async for msg in ws:
                        self.msg_queue.put_nowait(msg)

            except Exception as e:
                print(f"⚠️ Reconnecting... {e}")
                await asyncio.sleep(2)

    async def worker(self):
        """웹소켓 데이터 처리 (T, Q 이벤트만 처리)"""
        while True:
            msg = await self.msg_queue.get()
            try:
                data = json.loads(msg)
                for item in data:
                    ev, t = item.get('ev'), item.get('sym')
                    
                    if t in self.snipers:
                        if ev == 'T': # Trade
                            # T 데이터로 봇 구동
                            self.snipers[t].on_data(item, {}, {}) 
                        elif ev == 'Q': # Quote
                            # 호가창 업데이트만 (계산은 T 왔을 때 함)
                            self.snipers[t].analyzer.quotes = {
                                'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                                'asks': [{'p':item.get('ap'),'s':item.get('as')}]
                            }
                        elif ev == 'A': # Agg
                            # VWAP 등 보조 정보 업데이트
                            self.snipers[t].vwap = item.get('vw', 0)

            except Exception:
                pass
            finally:
                self.msg_queue.task_done()

    async def task_redis_sync(self):
        """[핵심] Redis에서 스캐너가 찾은 후보군 읽어오기"""
        print("🔭 [STS] Redis Sync Started")
        while True:
            try:
                if redis_client:
                    data = redis_client.get('sts_candidates')
                    if data:
                        self.candidates = json.loads(data)
                        # print(f"📋 Candidates: {self.candidates}")
                await asyncio.sleep(2) # 2초마다 갱신
            except Exception as e:
                print(f"⚠️ Redis Sync Error: {e}")
                await asyncio.sleep(5)

    async def task_focus_manager(self, ws):
        """후보군 중 상위 3개만 골라서 웹소켓 구독"""
        print("🎯 [STS] Focus Manager Started")
        while True:
            try:
                await asyncio.sleep(5)
                if not self.candidates: continue

                # 스캐너가 준 순서대로 상위 3개 (이미 정렬되어 있다고 가정)
                target_top3 = self.candidates[:STS_TARGET_COUNT]
                
                current_set = set(self.snipers.keys())
                new_set = set(target_top3)
                
                # 필요 없어진 놈 구독 취소
                to_remove = current_set - new_set
                if to_remove:
                    unsubscribe_params = [f"T.{t}" for t in to_remove] + [f"Q.{t}" for t in to_remove] + [f"A.{t}" for t in to_remove]
                    await self.unsubscribe(ws, unsubscribe_params)
                    for t in to_remove: del self.snipers[t]

                # 새로운 놈 구독 시작
                to_add = new_set - current_set
                if to_add:
                    subscribe_params = [f"T.{t}" for t in to_add] + [f"Q.{t}" for t in to_add] + [f"A.{t}" for t in to_add]
                    await self.subscribe(ws, subscribe_params)
                    
                    for t in to_add:
                        new_bot = SniperBot(t, self.logger, self.shared_model)
                        self.snipers[t] = new_bot
                        asyncio.create_task(new_bot.warmup())

            except Exception as e:
                print(f"❌ Manager Error: {e}")
                await asyncio.sleep(5)

# ==============================================================================
# 6. 실행 진입점
# ==============================================================================
if __name__ == "__main__":
    pipeline = STSPipeline()
    try:
        asyncio.run(pipeline.connect())
    except KeyboardInterrupt:
        print("Stopped.")