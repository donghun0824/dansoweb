import pandas as pd
import numpy as np
import os
import glob
import pickle
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, classification_report
import indicators_sts as ind # 지표 모듈 재사용

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
DATA_DIR = "datasets"
MODEL_FILE = "sts_xgboost_model.json"
TARGET_PROFIT = 0.02  # 목표: 3분 내 2% 수익 (스캘핑 최적화)
STOP_LOSS = -0.01     # 손절: -1% 이내 방어

# ==============================================================================
# 2. FEATURE ENGINEERING (데이터 가공)
# ==============================================================================
def process_ticker_data_xgb(date, ticker):
    """XGBoost용 고밀도 데이터셋 생성"""
    base_path = f"{DATA_DIR}/{date}/{ticker}"
    try:
        df_trades = pd.read_csv(f"{base_path}/trades.csv")
    except:
        return None

    # 전처리
    df_trades['t'] = pd.to_datetime(df_trades['sip_timestamp'], unit='ns')
    df_trades.set_index('t', inplace=True)
    
    # 1초 봉 변환
    ohlcv = df_trades['price'].resample('1s').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last'})
    volume = df_trades['size'].resample('1s').sum()
    tick_count = df_trades['size'].resample('1s').count()
    
    df = pd.concat([ohlcv, volume, tick_count], axis=1)
    df.columns = ['open', 'high', 'low', 'close', 'volume', 'tick_speed']
    df.dropna(subset=['close'], inplace=True)

    # --- [Feature Generation] ---
    # 1. Basic Indicators
    df['vwap'] = ind.compute_intraday_vwap_series(df, 'close', 'volume')
    df['vwap_dist'] = np.where(df['vwap'] != 0, (df['close'] - df['vwap']) / df['vwap'] * 100, 0)
    df['tick_accel'] = df['tick_speed'].diff()
    
    # 2. Advanced Metrics (Placeholders -> Real Logic needed for production)
    df['obi'] = np.random.uniform(-1, 1, len(df)) 
    df['obi_mom'] = df['obi'].diff()
    df['vpin'] = np.random.uniform(0, 1, len(df)) 

    # 3. Structural Metrics
    df['fibo_pos'] = ind.compute_fibo_pos(df['high'], df['low'], df['close'], lookback=600)
    df['fibo_dist_382'] = np.abs(df['fibo_pos'] - 0.382)
    
    df['bb_width'], df['bb_width_norm'], df['squeeze_flag'] = \
        ind.compute_bb_squeeze(df['close'], window=20, mult=2, norm_window=300)

    df['rv_60'] = ind.compute_rv_60(df['close'])
    df['vol_ratio_60'] = ind.compute_vol_ratio_60(df['volume'])

    # 4. Labeling (Binary Classification)
    # "3분(180초) 안에 2% 이상 오르고, 그 전에 -1% 손절 안 당하면 성공(1)"
    indexer = pd.api.indexers.FixedForwardWindowIndexer(window_size=180)
    future_high = df['high'].rolling(window=indexer, min_periods=1).max()
    future_low = df['low'].rolling(window=indexer, min_periods=1).min()
    
    # 수익률 계산
    max_profit = (future_high - df['close']) / df['close']
    max_loss = (future_low - df['close']) / df['close']
    
    # 성공 조건: (최대 수익 >= 2%) AND (최대 손실 > -1%)
    df['target'] = ((max_profit >= TARGET_PROFIT) & (max_loss > STOP_LOSS)).astype(int)
    
    # Data Cleaning
    features = [
        'obi', 'obi_mom', 'tick_accel', 'vpin', 'vwap_dist',
        'fibo_pos', 'fibo_dist_382', 'bb_width_norm', 'squeeze_flag', 
        'rv_60', 'vol_ratio_60', 'target'
    ]
    
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    return df[features].dropna()

# ==============================================================================
# 3. XGBOOST TRAINING & BACKTEST
# ==============================================================================
def run_xgb_optimization():
    print(f"🚀 [XGBoost 엔진 시동] 목표: 승률 극대화 (Target: 3분 내 {TARGET_PROFIT*100}% 수익)")
    
    all_data = []
    dirs = glob.glob(f"{DATA_DIR}/*/*")
    print(f"📂 데이터셋 로딩 중 ({len(dirs)}개 종목)...")
    
    for i, d in enumerate(dirs[:100]): # 학습 속도를 위해 100개만 샘플링 (전체 하려면 제한 해제)
        parts = d.split(os.sep)
        if len(parts) < 3: continue 
        df = process_ticker_data_xgb(parts[-2], parts[-1])
        if df is not None and not df.empty:
            all_data.append(df)
            
    if not all_data:
        print("❌ 데이터 없음. data_collector.py 실행 필요.")
        return

    full_df = pd.concat(all_data)
    print(f"📊 총 {len(full_df):,}개 데이터 포인트 확보.")
    print(f"🔥 정답(성공) 비율: {full_df['target'].mean()*100:.2f}% (데이터 불균형 확인)")

    # 학습 데이터 분리
    feature_cols = [c for c in full_df.columns if c != 'target']
    X = full_df[feature_cols]
    y = full_df['target']
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    # 모델 학습 (XGBClassifier)
    print("\n🤖 [AI] XGBoost 모델 학습 중...")
    model = xgb.XGBClassifier(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        objective='binary:logistic',
        n_jobs=-1,
        eval_metric='logloss',
        # scale_pos_weight: 불균형 데이터 보정 (성공 케이스에 가중치)
        scale_pos_weight=(len(y) - y.sum()) / y.sum()
    )
    
    model.fit(X_train, y_train)
    
    # 모델 저장
    model.save_model(MODEL_FILE)
    print(f"✅ 모델 저장 완료: {MODEL_FILE}")

    # 평가
    y_pred_prob = model.predict_proba(X_test)[:, 1] # 성공 확률 (0~1)
    
    # 중요도 분석
    print("\n🏆 [Feature Importance] AI가 중요하게 본 지표")
    print("="*50)
    imp = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    print(imp)
    print("="*50)

    # 시뮬레이션: 최적 컷라인(Threshold) 찾기
    print("\n📈 [정밀 시뮬레이션] 확률 컷라인별 성과 분석")
    print(f"{'Threshold':<10} | {'Trades':<8} | {'Win Rate':<10} | {'Precision':<10}")
    print("-" * 45)
    
    best_threshold = 0.5
    best_win_rate = 0
    
    for thr in [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]:
        # 해당 확률 이상일 때만 진입
        entries = y_test[y_pred_prob >= thr]
        if len(entries) < 10: continue # 표본 너무 적으면 패스
        
        win_rate = entries.mean() * 100
        print(f"{thr:<10} | {len(entries):<8} | {win_rate:.2f}%     | {'⭐⭐⭐⭐⭐' if win_rate > 80 else ''}")
        
        if win_rate > best_win_rate and len(entries) > 50:
            best_win_rate = win_rate
            best_threshold = thr

    print("\n🏁 [최종 결론]")
    print(f"   AI 추천 진입 확률 컷라인: {best_threshold}")
    print(f"   예상 승률: {best_win_rate:.2f}%")
    print(f"   👉 실전 엔진에서는 model.predict_proba() 값이 {best_threshold} 이상일 때만 Fire!")

if __name__ == "__main__":
    run_xgb_optimization()