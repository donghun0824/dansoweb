import asyncio
import websockets
import json
import os
import time
import numpy as np
import pandas as pd
import csv
import xgboost as xgb
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import indicators_sts as ind 

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
WS_URI = "wss://socket.polygon.io/stocks"

STS_TARGET_COUNT = 3
STS_MIN_VOLUME_DOLLAR = 1e6
STS_MAX_SPREAD_PCT = 0.5
STS_MAX_VPIN = 0.60 
OBI_LEVELS = 5

MODEL_FILE = "sts_xgboost_model.json"
AI_PROB_THRESHOLD = 0.85  
ATR_TRAIL_MULT = 1.5       
HARD_STOP_PCT = 0.015      
TRADE_LOG_FILE = "sts_trade_log_v5.csv"
REPLAY_LOG_FILE = "sts_replay_data_v5.csv"

# ==============================================================================
# 2. CORE ENGINE
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
    """[V5.1 Upgrade] 실시간 데이터를 1초 OHLC로 변환하여 학습 환경과 동기화"""
    def __init__(self):
        self.raw_ticks = deque(maxlen=3000) # Raw Tick Data
        self.quotes = {'bids': [], 'asks': []}
        
        # 1초봉 데이터 관리용
        self.ohlc_data = pd.DataFrame() 
        self.last_resample_time = 0

    def update_tick(self, tick_data, current_quotes):
        best_bid = current_quotes['bids'][0]['p'] if current_quotes['bids'] else 0
        best_ask = current_quotes['asks'][0]['p'] if current_quotes['asks'] else 0
        
        # 1. Raw Tick 저장
        self.raw_ticks.append({
            't': pd.to_datetime(tick_data['t'], unit='ms'),
            'p': tick_data['p'],
            's': tick_data['s'],
            'bid': best_bid, 
            'ask': best_ask
        })
        self.quotes = current_quotes

    def _resample_ohlc(self):
        """틱 데이터를 1초봉 OHLCV로 변환 (학습 데이터와 동일 포맷)"""
        if len(self.raw_ticks) < 10: return None
        
        df = pd.DataFrame(self.raw_ticks).set_index('t')
        
        # 1초 리샘플링
        ohlcv = df['p'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
        volume = df['s'].resample('1s').sum()
        tick_count = df['s'].resample('1s').count()
        
        # 최신 데이터만 유지 (최근 600초 = 10분)
        df_res = pd.concat([ohlcv, volume, tick_count], axis=1).iloc[-600:]
        df_res.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
        
        # 빈 구간 채우기 (Forward Fill)
        df_res['close'] = df_res['close'].ffill()
        df_res['open'] = df_res['open'].fillna(df_res['close'])
        df_res['high'] = df_res['high'].fillna(df_res['close'])
        df_res['low'] = df_res['low'].fillna(df_res['close'])
        df_res['volume'] = df_res['volume'].fillna(0)
        df_res['tick_speed'] = df_res['tick_speed'].fillna(0)
        
        # Raw Ticks에서 호가 정보 등 추가 매핑 필요시 여기서 처리
        # (여기선 간소화: 현재 호가 정보는 별도로 사용)
        
        return df_res.dropna()

    def get_metrics(self):
        # 1. 1초봉 데이터 생성
        df = self._resample_ohlc()
        if df is None or len(df) < 60: return None # 최소 1분 데이터 필요
        
        # 2. 지표 계산 (indicators_sts 모듈 100% 활용)
        # VWAP
        df['vwap'] = ind.compute_intraday_vwap_series(df, 'close', 'volume')
        
        # Advanced Features
        df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=600)
        
        # Squeeze
        _, df['bb_width_norm'], df['squeeze_flag'] = \
            ind.compute_bb_squeeze(df['close'], window=20, mult=2, norm_window=300)
            
        # Volatility & Ratio
        df['rv_60'] = ind.compute_rv_60(df['close'])
        df['vol_ratio_60'] = ind.compute_vol_ratio_60(df['volume'])
        
        # Tick Accel
        df['tick_accel'] = df['tick_speed'].diff().fillna(0)

        # --- [Current Values for AI Input] ---
        # 가장 최근(마지막) 1초봉 기준 값 추출
        last = df.iloc[-1]
        
        # OBI & VPIN은 Tick 레벨에서 계산 (더 정밀)
        # (df 변환 전 raw_ticks 사용)
        raw_df = pd.DataFrame(list(self.raw_ticks)[-100:]) # 최근 100틱만
        signs = [ind.classify_trade_sign(r.p, r.bid, r.ask) for r in raw_df.itertuples()]
        signed_vol = raw_df['s'].values * np.array(signs)
        vpin = ind.compute_vpin(signed_vol)
        
        bids = np.array([q['s'] for q in self.quotes.get('bids', [])[:OBI_LEVELS]])
        asks = np.array([q['s'] for q in self.quotes.get('asks', [])[:OBI_LEVELS]])
        obi = ind.compute_order_book_imbalance(bids, asks)
        
        # OBI Momentum (Simple Diff)
        # (이전 OBI 저장 필요 -> self.prev_obi)
        if not hasattr(self, 'prev_obi'): self.prev_obi = obi
        obi_mom = obi - self.prev_obi
        self.prev_obi = obi
        
        # VWAP Dist
        vwap_dist = (last['close'] - last['vwap']) / last['vwap'] * 100 if last['vwap'] > 0 else 0
        
        # Fibo Distances (학습과 동일하게 계산)
        fibo_dist_382 = abs(last['fibo_pos'] - 0.382)
        fibo_dist_618 = abs(last['fibo_pos'] - 0.618)
        
        # Spread
        best_bid = self.ticks[-1]['bid'] if hasattr(self, 'ticks') and self.ticks else raw_df.iloc[-1]['bid']
        best_ask = self.ticks[-1]['ask'] if hasattr(self, 'ticks') and self.ticks else raw_df.iloc[-1]['ask']
        spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0

        # [V5.1] 학습 피처 순서와 개수(12개) 완벽 일치
        return {
            'obi': obi, 'obi_mom': obi_mom, 'tick_accel': last['tick_accel'],
            'vpin': vpin, 'vwap_dist': vwap_dist,
            'fibo_pos': last['fibo_pos'], 'fibo_dist_382': fibo_dist_382, 'fibo_dist_618': fibo_dist_618,
            'bb_width_norm': last['bb_width_norm'], 'squeeze_flag': last['squeeze_flag'],
            'rv_60': last['rv_60'], 'vol_ratio_60': last['vol_ratio_60'],
            
            'spread': spread, 'last_price': last['close'], 'timestamp': raw_df.iloc[-1]['t']
        }

class TargetSelector:
    def __init__(self):
        self.snapshots = {} 
        self.top_targets = []

    def update(self, agg_data):
        t = agg_data['sym']
        if t not in self.snapshots: 
            self.snapshots[t] = {'h': deque(maxlen=60), 'l': deque(maxlen=60), 
                                 'c': deque(maxlen=60), 'v': deque(maxlen=60)}
        self.snapshots[t]['h'].append(agg_data['h'])
        self.snapshots[t]['l'].append(agg_data['l'])
        self.snapshots[t]['c'].append(agg_data['c'])
        self.snapshots[t]['v'].append(agg_data['v'])

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

class SniperBot:
    def __init__(self, ticker, logger, selector, shared_model):
        self.ticker = ticker
        self.logger = logger
        self.selector = selector
        self.model = shared_model # [V5.1] 공유 모델 사용
        self.analyzer = MicrostructureAnalyzer()
        self.state = "WATCHING"
        self.vwap = 0
        self.atr = 0.05 
        self.position = {} 

    def on_data(self, tick_data, quote_data, agg_data):
        self.analyzer.update_tick(tick_data, quote_data)
        if agg_data:
            self.vwap = agg_data.get('vwap', tick_data['p'])
            self.atr = self.selector.get_atr(self.ticker)

        m = self.analyzer.get_metrics()
        if not m: return

        # [V5.1] Risk Filter 복구 (Spread & VPIN)
        if m['spread'] > STS_MAX_SPREAD_PCT or m['vpin'] > STS_MAX_VPIN: return

        # --- [STS V5.1 AI Inference] ---
        if self.state == "WATCHING":
            dist = (m['last_price'] - self.vwap) / self.vwap * 100
            # 감시 범위: 0.2 ~ 2.0%
            if 0.2 < dist < 2.0:
                self.state = "AIMING"

        elif self.state == "AIMING":
            prob = 0.0
            
            if self.model:
                # [V5.1] Feature 12개 (학습 순서 엄수)
                # ['obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist',
                #  'fibo_pos', 'fibo_dist_382', 'fibo_dist_618', 'bb_width_norm', 'squeeze_flag', 
                #  'rv_60', 'vol_ratio_60']
                
                input_data = np.array([[
                    m['obi'], m['obi_mom'], m['tick_accel'], m['vpin'], m['vwap_dist'],
                    m['fibo_pos'], m['fibo_dist_382'], m['fibo_dist_618'], # 추가됨
                    m['bb_width_norm'], m['squeeze_flag'],
                    m['rv_60'], m['vol_ratio_60']
                ]])
                
                dtest = xgb.DMatrix(input_data)
                prob = self.model.predict(dtest)[0]
            else:
                prob = 0.0

            # Replay Log
            self.logger.log_replay({
                'timestamp': m['timestamp'], 'ticker': self.ticker, 
                'price': m['last_price'], 'vwap': self.vwap, 'atr': self.atr,
                'obi': m['obi'], 'tick_speed': m['tick_speed'], 'vpin': m['vpin'], 
                'ai_prob': prob
            })

            # 격발
            if prob >= AI_PROB_THRESHOLD:
                self.fire(m['last_price'], prob, m)

        elif self.state == "FIRED":
            self.manage_position(m['last_price'])

    def fire(self, price, prob, metrics):
        print(f"🔫 [격발] {self.ticker} AI_Prob:{prob:.4f} Price:${price:.4f}")
        self.state = "FIRED"
        self.position = {
            'entry': price,
            'high': price,
            'sl': price - (self.atr * 0.5),
            'atr': self.atr
        }
        
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
            print(f"💰 [청산] {self.ticker} Profit: {profit_pct:.2f}%")
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
        
        # [V5.1] 모델 한 번만 로드해서 공유
        self.shared_model = None
        if os.path.exists(MODEL_FILE):
            print(f"🤖 [System] Loading AI Model: {MODEL_FILE}")
            self.shared_model = xgb.Booster()
            self.shared_model.load_model(MODEL_FILE)
        else:
            print(f"⚠️ [Warning] Model file not found!")

    async def connect(self):
        if not POLYGON_API_KEY:
            print("❌ Error: No API Key")
            return

        async with websockets.connect(WS_URI) as ws:
            print("✅ [STS V5.1] AI Execution Engine Started (Sync Features)")
            await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
            await ws.recv()
            await self.subscribe(ws, ["A.*"])
            await self.msg_handler(ws)

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
                # [V5.1] 공유 모델 전달
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
            except Exception as e:
                print(f"Error: {e}")
                break

if __name__ == "__main__":
    try:
        asyncio.run(STSPipeline().connect())
    except KeyboardInterrupt:
        print("🛑 System Halted")