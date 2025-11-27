import pandas as pd
import numpy as np
import os
import glob
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
import indicators_sts as ind # 우리가 만든 지표 모듈 재사용

# ==============================================================================
# 1. FEATURE ENGINEERING ENGINE
# ==============================================================================
def process_ticker_data(date, ticker):
    """저장된 CSV를 읽어와서 Feature를 추출"""
    base_path = f"datasets/{date}/{ticker}"
    
    try:
        df_trades = pd.read_csv(f"{base_path}/trades.csv")
        # df_quotes = pd.read_csv(f"{base_path}/quotes.csv") # Quotes는 용량 문제로 없을 수도 있음
        # df_agg = pd.read_csv(f"{base_path}/agg.csv")
    except:
        return None

    # 데이터 전처리 및 병합 (Timestamp 기준 정렬)
    df_trades['t'] = pd.to_datetime(df_trades['sip_timestamp'], unit='ns')
    df_trades.set_index('t', inplace=True)
    
    # 1초 봉으로 변환
    ohlcv = df_trades['price'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
    volume = df_trades['size'].resample('1s').sum()
    tick_count = df_trades['size'].resample('1s').count() # Tick Speed
    
    df = pd.concat([ohlcv, volume, tick_count], axis=1)
    df.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
    df = df.dropna(subset=['close']) # 거래 없는 초 제거

    # --- [Feature Generation] ---
    
    # 1. Basic Indicators
    df['vwap'] = ind.compute_intraday_vwap_series(df, 'close', 'volume')
    df['vwap_dist'] = np.where(df['vwap'] != 0, (df['close'] - df['vwap']) / df['vwap'] * 100, 0)
    df['tick_accel'] = df['tick_speed'].diff()
    
    # 2. Placeholders (실제 Quote 데이터 연동 시 교체)
    # 실제로는 Quotes 데이터가 있어야 정확하지만, 지금은 시뮬레이션용 난수 사용 (필요시 수정)
    df['obi'] = np.random.uniform(-1, 1, len(df)) 
    df['obi_mom'] = df['obi'].diff()
    df['vpin'] = np.random.uniform(0, 1, len(df)) 

    # 🔥 [NEW] 3. Fibonacci Metrics
    df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=600)
    df['fibo_dist_382'] = np.abs(df['fibo_pos'] - 0.382)
    df['fibo_dist_618'] = np.abs(df['fibo_pos'] - 0.618)

    # 🔥 [NEW] 4. Squeeze Metrics
    df['bb_width'], df['bb_width_norm'], df['squeeze_flag'] = \
        ind.compute_bb_squeeze(df['close'], window=20, mult=2, norm_window=300)

    # 🔥 [NEW] 5. Advanced Volatility & Volume (Optional)
    df['rv_60'] = ind.compute_rv_60(df['close'])
    df['vol_ratio_60'] = ind.compute_vol_ratio_60(df['volume'])

    # Labeling (10분 후 수익률)
    indexer = pd.api.indexers.FixedForwardWindowIndexer(window_size=600)
    df['future_max'] = df['high'].rolling(window=indexer, min_periods=1).max()
    
    # 0으로 나누기 방지
    df['target_return'] = np.where(df['close'] != 0, (df['future_max'] - df['close']) / df['close'] * 100, 0)
    
    # 성공 여부 (5% 이상 상승 시 1, 아니면 0)
    df['is_winner'] = (df['target_return'] > 5.0).astype(int)
    
    # 🔥 [수정] 결측치(NaN) 및 무한대(Inf) 완벽 제거
    features = [
        'obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist',
        'fibo_pos', 'fibo_dist_382', 'fibo_dist_618',
        'bb_width_norm', 'squeeze_flag', 'rv_60', 'vol_ratio_60',
        'target_return', 'is_winner'
    ]
    
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df_clean = df[features].dropna()
    
    return df_clean

# ==============================================================================
# 2. BACKTEST & OPTIMIZATION
# ==============================================================================
def train_weights(all_data):
    """머신러닝으로 최적 가중치 산출"""
    # 🔥 [수정] 학습 전 한 번 더 데이터 클리닝
    all_data.replace([np.inf, -np.inf], np.nan, inplace=True)
    all_data.dropna(inplace=True)
    
    if len(all_data) == 0:
        print("❌ 학습 가능한 데이터가 없습니다.")
        return None

    print("\n🤖 [AI] 최적 가중치 학습 중 (Extended Features)...")
    
    # Feature List Updated
    X = all_data[[
        'obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist',
        'fibo_pos', 'fibo_dist_382', 'fibo_dist_618',
        'bb_width_norm', 'squeeze_flag', 'rv_60', 'vol_ratio_60'
    ]]
    y = all_data['target_return'] 
    
    # 선형 회귀로 가중치 뽑기
    model = LinearRegression()
    model.fit(X, y)
    
    weights = dict(zip(X.columns, model.coef_))
    
    print(f"✅ 최적화된 가중치 발견!")
    for k, v in weights.items():
        print(f"   {k.ljust(15)}: {v:.4f}")
    
    return weights

def run_backtest():
    all_features = []
    
    # 저장된 데이터셋 순회
    dirs = glob.glob("datasets/*/*")
    print(f"📂 총 {len(dirs)}개 종목 데이터 로딩 중...")
    
    # 테스트용으로 50개만 로딩 (전체 다 하려면 [:50] 제거)
    for i, d in enumerate(dirs[:50]): 
        parts = d.split(os.sep)
        if len(parts) < 3: continue 
        date, ticker = parts[-2], parts[-1]
        
        # print(f"Processing {ticker}...", end='\r') 
        df = process_ticker_data(date, ticker)
        if df is not None and not df.empty:
            all_features.append(df)
            
    if not all_features:
        print("\n❌ 데이터가 없습니다. data_collector.py를 먼저 실행하세요.")
        return

    full_df = pd.concat(all_features)
    print(f"\n📊 총 {len(full_df)}개 데이터 포인트 분석 시작")
    
    # AI 학습
    weights = train_weights(full_df)
    
    if not weights:
        print("❌ 학습 실패: 유효한 가중치를 찾지 못했습니다.")
        return

    # 시뮬레이션 (찾은 가중치 적용)
    # Score = Sum(Feature * Weight)
    full_df['sts_score'] = 0
    for col, w in weights.items():
        full_df['sts_score'] += full_df[col] * w
                           
    # 결과 분석 (상위 5% 점수일 때 진입했다면?)
    threshold = full_df['sts_score'].quantile(0.95)
    entries = full_df[full_df['sts_score'] > threshold]
    
    win_rate = entries['is_winner'].mean() * 100
    avg_return = entries['target_return'].mean()
    
    print(f"\n📈 [백테스트 결과]")
    print(f"   Score Threshold (상위 5%): {threshold:.4f}")
    print(f"   진입 횟수: {len(entries)}")
    print(f"   승률 (Win Rate): {win_rate:.2f}% (목표: 10분 내 +5%)")
    print(f"   평균 수익률: {avg_return:.2f}%")

if __name__ == "__main__":
    run_backtest()