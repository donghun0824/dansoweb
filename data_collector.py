import pandas as pd
import numpy as np
import requests
import time
import os
import random
from datetime import datetime, timedelta
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor

# =========================================================
# ⚙️ 설정 (V4.0 Triple-Barrier & Curation)
# =========================================================
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
TARGET_PROFIT = 3.0   # 익절 +3% (Win)
STOP_LOSS = -2.0      # 손절 -2% (Loss)
TIME_HORIZON = 10     # 10분 제한
MAX_PRICE = 20.0      
MIN_PRICE = 0.5       
DAYS_BACK = 365       
OUTPUT_FILE = "training_data_v4.csv" # 👈 파일명 V4 확인

# =========================================================
# 🏎️ F1 엔진 V3.0 (Quant Edition - 로직 유지)
# =========================================================
def calculate_indicators(df):
    try:
        closes = df['c'].values
        highs = df['h'].values
        lows = df['l'].values
        volumes = df['v'].values.astype(float)
        times = pd.to_datetime(df['t'], unit='ms')

        # 1. 기본 지표
        tp = (highs + lows + closes) / 3
        vp = tp * volumes
        cum_vp = np.cumsum(vp)
        cum_vol = np.cumsum(volumes)
        vwap = np.divide(cum_vp, cum_vol, out=np.zeros_like(cum_vp), where=cum_vol!=0)
        
        def get_bb_width(c, n=20):
            sma = pd.Series(c).rolling(n).mean().values
            std = pd.Series(c).rolling(n).std().values
            up = sma + (std * 2.0); low = sma - (std * 2.0)
            return np.nan_to_num((up - low) / c)
            
        bb_width = get_bb_width(closes)
        bb_avg = pd.Series(bb_width).rolling(20).mean().values
        squeeze_ratio = np.divide(bb_width, bb_avg, out=np.ones_like(bb_width), where=bb_avg!=0)

        delta = np.diff(closes, prepend=closes[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).rolling(14).mean().values
        avg_loss = pd.Series(loss).rolling(14).mean().values
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # 2. Advanced Features
        vol_ma20 = pd.Series(volumes).rolling(20).mean().values
        rvol = np.divide(volumes, vol_ma20, out=np.zeros_like(volumes), where=vol_ma20!=0)
        
        candle_range = highs - lows
        range_ma = pd.Series(candle_range).rolling(20).mean().values
        range_std = pd.Series(candle_range).rolling(20).std().values
        volatility_z = (candle_range - range_ma) / (range_std + 1e-10)
        
        range_span = highs - lows
        clv = ((closes - lows) - (highs - closes)) / (range_span + 1e-10)
        order_imbalance = clv * volumes 
        order_imbalance_ma = pd.Series(order_imbalance).rolling(5).mean().values

        ema_60 = pd.Series(closes).ewm(span=60, adjust=False).mean().values
        trend_align = np.where(closes > ema_60, 1, -1)

        def get_session(t):
            h = t.hour; m = t.minute; total_min = h * 60 + m
            if 570 <= total_min < 630: return 0 
            elif 630 <= total_min < 840: return 1 
            elif 840 <= total_min < 960: return 2 
            else: return 3
            
        session_bucket = np.array([get_session(t) for t in times])

        return vwap, squeeze_ratio, rsi, rvol, volatility_z, order_imbalance_ma, trend_align, session_bucket
        
    except Exception:
        return None, None, None, None, None, None, None, None

# =========================================================
# 📡 데이터 수집기 (Triple-Barrier & Curation)
# =========================================================

def get_active_tickers():
    print("🔍 활성 잡주 티커 검색 중...")
    url = f"https://api.polygon.io/v3/reference/tickers?market=stocks&active=true&sort=ticker&order=asc&limit=1000&apiKey={POLYGON_API_KEY}"
    tickers = []
    while url:
        res = requests.get(url).json()
        if 'results' not in res: break
        for item in res['results']:
            tickers.append(item['ticker'])
        if 'next_url' in res and len(tickers) < 3000: 
            url = res['next_url'] + f"&apiKey={POLYGON_API_KEY}"
        else: break
    print(f"✅ 총 {len(tickers)}개 티커 발견.")
    print("🎲 티커 무작위 셔플 (편향 제거)...")
    random.shuffle(tickers) 
    return tickers

def process_ticker(ticker):
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%d')
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}"
    
    try: res = requests.get(url, timeout=10).json()
    except: return None
    
    if 'results' not in res or len(res['results']) < 500: return None
    df = pd.DataFrame(res['results'])
    df = df.rename(columns={'v':'v', 'o':'o', 'c':'c', 'h':'h', 'l':'l', 't':'t'})
    
    avg_price = df['c'].mean()
    if not (MIN_PRICE <= avg_price <= MAX_PRICE): return None

    # 지표 계산
    indicators = calculate_indicators(df)
    if indicators[0] is None: return None
    vwap, squeeze, rsi, rvol, vol_z, order_imb, trend, session = indicators
    
    dataset = []
    closes = df['c'].values
    highs = df['h'].values
    lows = df['l'].values
    
    # 데이터 순회
    for i in range(60, len(df) - TIME_HORIZON):
        # 🧹 [Curation] 쓰레기 데이터 소각 (학습 효율 UP)
        if rvol[i] < 0.5 and vol_z[i] < 0.5: continue # 거래량/변동성 죽은 구간 제외
        if session[i] == 3: continue # 프리/애프터 중 의미 없는 구간 제외

        price_now = closes[i]
        
        # 🚧 [Labeling] Triple-Barrier Method
        label = -1 
        
        for j in range(1, TIME_HORIZON + 1):
            future_high = highs[i+j]
            future_low = lows[i+j]
            
            pct_change_high = (future_high - price_now) / price_now * 100
            pct_change_low = (future_low - price_now) / price_now * 100
            
            # 1. 익절 터치? (Win)
            if pct_change_high >= TARGET_PROFIT:
                label = 1
                break
            # 2. 손절 터치? (Loss)
            if pct_change_low <= STOP_LOSS:
                label = 0
                break
        
        # 3. 시간초과 (Draw) -> 버림 (애매한 데이터 학습 방지)
        if label == -1:
            continue 

        # Features 추출
        vwap_dist = ((price_now - vwap[i]) / vwap[i]) * 100 if vwap[i] != 0 else 0
        pump = ((price_now - closes[i-5]) / closes[i-5]) * 100 if closes[i-5] != 0 else 0
        recent_high = np.max(highs[max(0, i-200):i+1])
        pullback = ((recent_high - price_now) / recent_high) * 100 if recent_high > 0 else 0
        
        dataset.append({
            'ticker': ticker,
            'vwap_dist': round(vwap_dist, 2),
            'squeeze': round(squeeze[i], 2),
            'rsi': round(rsi[i], 2),
            'pump': round(pump, 2),
            'pullback': round(pullback, 2),
            'rvol': round(rvol[i], 2),
            'volatility_z': round(vol_z[i], 2),
            'order_imbalance': round(order_imb[i], 2),
            'trend_align': int(trend[i]),
            'session': int(session[i]),
            'label_win': label
        })
        
    return pd.DataFrame(dataset)

# =========================================================
# 🚀 메인 실행
# =========================================================
def main():
    if "YOUR_API_KEY" in POLYGON_API_KEY:
        print("❌ API Key 확인 필요")
        return

    tickers = get_active_tickers()
    
    # 파일명 변경 (V4)
    cols = ['ticker', 'vwap_dist', 'squeeze', 'rsi', 'pump', 'pullback', 
            'rvol', 'volatility_z', 'order_imbalance', 'trend_align', 'session', 'label_win']
            
    if not os.path.exists(OUTPUT_FILE):
        pd.DataFrame(columns=cols).to_csv(OUTPUT_FILE, index=False)
    
    print(f"🚀 V4.0 Triple-Barrier 데이터 채굴 시작...")
    
    total_wins = 0
    total_losses = 0
    count = 0
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        # 결과를 실시간으로 받아서 처리
        results = tqdm(executor.map(process_ticker, tickers), total=len(tickers))
        
        for df in results:
            if df is not None and not df.empty:
                # 저장
                df.to_csv(OUTPUT_FILE, mode='a', header=False, index=False)
                count += len(df)
                
                # 📊 실시간 비율 체크 (요청하신 기능)
                w = len(df[df['label_win'] == 1])
                l = len(df[df['label_win'] == 0])
                total_wins += w
                total_losses += l
                
                # tqdm 설명창에 실시간 비율 표시
                ratio = round(total_losses / total_wins, 1) if total_wins > 0 else "Inf"
                results.set_description(f"⚖️ Ratio {ratio}:1 (W:{total_wins})")

    print("\n" + "="*40)
    print("📊 [최종 데이터 밸런스 리포트]")
    print("="*40)
    print(f"✅ 총 데이터: {count:,}개")
    print(f"🏆 WIN (성공): {total_wins:,}개")
    print(f"💥 LOSS (실패): {total_losses:,}개")
    
    final_ratio = round(total_losses / total_wins, 2) if total_wins > 0 else 0
    print(f"⚖️ 최종 비율 (Fail : Win) = {final_ratio} : 1")
    
    if final_ratio <= 7:
        print("🎉 [판정] 완벽합니다! 바로 학습 돌리셔도 됩니다.")
    elif final_ratio <= 20:
        print("⚠️ [판정] 조금 불균형합니다. 학습 시 class_weight 적용 필수.")
    else:
        print("❌ [판정] 너무 불균형합니다. 필터링 조건을 더 완화하세요.")
    
    print(f"💾 파일 저장 완료: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()