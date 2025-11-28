import asyncio
import websockets
import json
import os
import time
import numpy as np
import pandas as pd
import csv
import xgboost as xgb
import psycopg2
from psycopg2 import pool
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import partial
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
STS_MAX_SPREAD_PCT = 0.8      # [V5.2] 리스크 필터 (0.8% 초과 시 무시)
STS_MAX_VPIN = 0.55           # [V5.2] 독성 필터
OBI_LEVELS = 5

# AI & Risk Params
MODEL_FILE = "sts_xgboost_model.json"
AI_PROB_THRESHOLD = 0.85      # 격발 기준 확률
ATR_TRAIL_MULT = 1.5          # 익절 트레일링 계수
HARD_STOP_PCT = 0.015         # 하드 스탑 (-1.5%)

# Logging
TRADE_LOG_FILE = "sts_trade_log_v5.csv"
REPLAY_LOG_FILE = "sts_replay_data_v5.csv"

# System Optimization
DB_UPDATE_INTERVAL = 3.0      # DB 업데이트 주기 (초)
GC_INTERVAL = 300             # 가비지 컬렉션 주기 (5분)
GC_TTL = 600                  # 데이터 유효 시간 (10분)

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
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, dsn=DATABASE_URL)
            print("✅ [DB] Connection Pool Initialized")
            
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
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY, 
            ticker TEXT NOT NULL, 
            price REAL NOT NULL, 
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
        cursor.close()
        db_pool.putconn(conn)
    except Exception as e:
        print(f"❌ [DB Init Error] {e}")

def get_db_connection():
    global db_pool
    if db_pool is None: init_db()
    return db_pool.getconn()

def init_firebase():
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR: 
            print("⚠️ Firebase Key Missing")
            return
        
        if not firebase_admin._apps:
            cred = credentials.Certificate(json.loads(FIREBASE_ADMIN_SDK_JSON_STR))
            firebase_admin.initialize_app(cred)
            print("✅ [FCM] Initialized")
    except Exception as e:
        print(f"❌ [FCM Error] {e}")

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

def log_signal_to_db(ticker, price):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO signals (ticker, price, time) VALUES (%s, %s, %s)", 
                       (ticker, price, datetime.now()))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [DB Signal Error] {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# --- FCM Sending Logic ---
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
            db_pool.putconn(conn); return

        noti_title = f"💎 {ticker} 신호 (점수: {probability_score})"
        if entry: 
            noti_body = f"진입: ${entry:.4f} | 익절: ${tp:.4f} | 손절: ${sl:.4f}"
        else: 
            noti_body = f"현재가: ${price:.4f}"

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
                msg = messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=noti_title, body=noti_body),
                    data=data_payload,
                    android=messaging.AndroidConfig(
                        priority='high', 
                        notification=messaging.AndroidNotification(channel_id='high_importance_channel', visibility='public', default_sound=True)
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(aps=messaging.Aps(alert=messaging.ApsAlert(title=noti_title, body=noti_body), sound="default"))
                    )
                )
                messaging.send(msg)
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
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(_send_fcm_sync, ticker, price, probability_score, entry, tp, sl))

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

        return {
            'obi': obi, 'obi_mom': obi_mom, 'tick_accel': last['tick_accel'],
            'vpin': vpin, 'vwap_dist': vwap_dist,
            'fibo_pos': last['fibo_pos'], 'fibo_dist_382': fibo_dist_382, 'fibo_dist_618': fibo_dist_618,
            'bb_width_norm': last['bb_width_norm'], 'squeeze_flag': last['squeeze_flag'],
            'rv_60': last['rv_60'], 'vol_ratio_60': last['vol_ratio_60'],
            'spread': spread, 'last_price': last['close'], 'tick_speed': last['tick_speed'], 'timestamp': raw_df.iloc[-1]['t']
        }

class TargetSelector:
    def __init__(self):
        self.snapshots = {} 
        self.top_targets = []
        self.last_gc_time = time.time()

    def update(self, agg_data):
        t = agg_data['sym']
        if t not in self.snapshots: 
            self.snapshots[t] = {'h': deque(maxlen=60), 'l': deque(maxlen=60), 
                                 'c': deque(maxlen=60), 'v': deque(maxlen=60),
                                 'last_updated': time.time()}
        
        s = self.snapshots[t]
        s['h'].append(agg_data['h'])
        s['l'].append(agg_data['l'])
        s['c'].append(agg_data['c'])
        s['v'].append(agg_data['v'])
        s['last_updated'] = time.time()

    def get_atr(self, ticker):
        if ticker not in self.snapshots: return 0.05
        d = self.snapshots[ticker]
        return ind.compute_atr(list(d['h']), list(d['l']), list(d['c']))

    def rank_targets(self):
        scored = []
        for t, d in self.snapshots.items():
            if len(d['c']) < 10: continue
            rv = ind.compute_realized_vol(np.array(d['c']))
            vol = np.sum(d['v'])
            scored.append((t, rv * 1000 + vol * 0.0001))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in scored[:STS_TARGET_COUNT]]

    def garbage_collect(self):
        """[Risk 3 해결] 메모리 청소"""
        now = time.time()
        if now - self.last_gc_time < GC_INTERVAL: return

        # print("🧹 [GC] Cleaning memory...", flush=True)
        to_remove = [t for t, d in self.snapshots.items() if now - d['last_updated'] > GC_TTL]
        
        for t in to_remove:
            del self.snapshots[t]
            
        # print(f"🧹 [GC] Removed {len(to_remove)} inactive tickers.", flush=True)
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

    def on_data(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        if agg_data:
            self.vwap = agg_data.get('vwap', tick_data['p'])
            self.atr = self.selector.get_atr(self.ticker)

        m = self.analyzer.get_metrics()
        if not m: return

        # [V5.2] Risk Filter (Spread & VPIN)
        if m['spread'] > STS_MAX_SPREAD_PCT or m['vpin'] > STS_MAX_VPIN: return

        # --- AI Inference (상시 수행) ---
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
                # print(f"⚠️ AI Error: {e}")
                pass

       # [Risk 2 해결] DB Throttling (안전장치 강화)
        now = time.time()
        
        # 1. 평소(WATCHING)에는 3초(DB_UPDATE_INTERVAL)마다 저장
        # 2. 급할 때(AIMING/FIRED)는 0.5초마다 저장 (0.1초마다 쏘면 DB 죽음)
        is_urgent = (self.state != "WATCHING")
        time_passed = (now - self.last_db_update > DB_UPDATE_INTERVAL)
        
        if time_passed or (is_urgent and (now - self.last_db_update > 0.5)):
            # prob가 계산 안 된 상태일 수 있으므로 안전 처리
            score_to_save = prob * 100 if 'prob' in locals() else 0
            update_dashboard_db(self.ticker, m, score_to_save, self.state)
            self.last_db_update = now

        # Replay Log
        self.logger.log_replay({
            'timestamp': m['timestamp'], 'ticker': self.ticker, 
            'price': m['last_price'], 'vwap': self.vwap, 'atr': self.atr,
            'obi': m['obi'], 'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 
            'ai_prob': prob
        })

        # --- FSM Logic ---
        if self.state == "WATCHING":
            dist = (m['last_price'] - self.vwap) / self.vwap * 100
            
            # [V5.2] 진입 조건 강화 (VWAP + Squeeze + Accel)
            cond_dist = 0.2 < dist < 2.0
            cond_sqz = m['squeeze_flag'] == 1
            cond_accel = m['tick_accel'] > 0
            
            if cond_dist and (cond_sqz or prob > 0.7) and cond_accel:
                self.state = "AIMING"
                print(f"👀 [조준] {self.ticker} (Prob:{prob:.2f} | Sqz:{cond_sqz})", flush=True)

        elif self.state == "AIMING":
            if m['tick_accel'] < -5 or prob < 0.6:
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
        
        log_signal_to_db(self.ticker, price)
        
        tp_price = price + (self.atr * ATR_TRAIL_MULT)
        asyncio.create_task(send_fcm_notification(
            self.ticker, price, int(prob*100), 
            entry=price, tp=tp_price, sl=self.position['sl']
        ))
        
        self.logger.log_trade({
            'ticker': self.ticker, 'action': 'ENTRY', 'price': price, 'ai_prob': prob,
            'obi': metrics['obi'], 'obi_mom': metrics['obi_momentum'],
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
        self.snipers = {}
        self.last_agg = {}
        self.last_quotes = {}
        self.logger = DataLogger()
        
        self.shared_model = None
        if os.path.exists(MODEL_FILE):
            print(f"🤖 [System] Loading AI Model: {MODEL_FILE}", flush=True)
            try:
                self.shared_model = xgb.Booster()
                self.shared_model.load_model(MODEL_FILE)
            except Exception as e:
                print(f"❌ Model Load Failed: {e}", flush=True)
        else:
            print(f"⚠️ [Warning] Model file not found!", flush=True)

    async def connect(self):
        init_db()
        init_firebase()
        
        if not POLYGON_API_KEY:
            print("❌ Error: No API Key", flush=True)
            return

        # [Risk 1 해결] 재연결 로직
        retry_delay = 1
        while True:
            try:
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=20) as ws:
                    print("✅ [STS V5.2] Final Engine Started", flush=True)
                    retry_delay = 1
                    
                    await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                    await ws.recv()
                    await self.subscribe(ws, ["A.*"])
                    await self.msg_handler(ws)
                    
            except (websockets.ConnectionClosed, asyncio.TimeoutError) as e:
                print(f"⚠️ Connection Lost: {e}. Reconnecting in {retry_delay}s...", flush=True)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            except Exception as e:
                print(f"❌ Critical Error: {e}", flush=True)
                await asyncio.sleep(5)

    async def subscribe(self, ws, params):
        await ws.send(json.dumps({"action": "subscribe", "params": ",".join(params)}))

    async def unsubscribe(self, ws, params):
        await ws.send(json.dumps({"action": "unsubscribe", "params": ",".join(params)}))

    async def manage_snipers(self, ws, new_targets):
        if not new_targets: return
        curr, new = set(self.snipers.keys()), set(new_targets)
        
        to_del = curr - new
        if to_del:
            await self.unsubscribe(ws, [f"T.{t},Q.{t}" for t in to_del])
            for t in to_del: del self.snipers[t]
            
        to_add = new - curr
        if to_add:
            await self.subscribe(ws, [f"T.{t},Q.{t}" for t in to_add])
            for t in to_add: 
                self.snipers[t] = SniperBot(t, self.logger, self.selector, self.shared_model) 

    async def msg_handler(self, ws):
        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                for item in data:
                    ev, t = item.get('ev'), item.get('sym')
                    if ev == 'A': 
                        self.selector.update(item)
                        self.last_agg[t] = item
                    elif ev == 'Q':
                        self.last_quotes[t] = {'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                                               'asks': [{'p':item.get('ap'),'s':item.get('as')}]}
                    elif ev == 'T' and t in self.snipers:
                        self.snipers[t].on_data(item, self.last_quotes.get(t,{'bids':[],'asks':[]}), self.last_agg.get(t))
                
                new = self.selector.rank_targets()
                if new: await self.manage_snipers(ws, new)
                
                # [Risk 3 해결] GC 호출
                self.selector.garbage_collect()
                
            except Exception as e:
                # print(f"Error: {e}")
                break

if __name__ == "__main__":
    try:
        asyncio.run(STSPipeline().connect())
    except KeyboardInterrupt:
        print("🛑 System Halted")