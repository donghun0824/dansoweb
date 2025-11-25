import pandas as pd
import numpy as np
import xgboost as xgb
import os
import warnings

warnings.filterwarnings('ignore')

# =========================================================
# ⚙️ 설정 (V16.0: RSI Filter - 오후장 정밀 타격)
# =========================================================
DATA_FILE = "training_data_v4.csv"
MODEL_FILE = "sniper_model_advanced.json"

def calculate_oar_features(df):
    print("🧮 OAR(Microstructure) 지표 재계산 중...")
    df['imbalance_score'] = np.log1p(df['order_imbalance'].clip(lower=0))
    df['oar_calc'] = (df['rvol'].clip(0, 5) * df['imbalance_score']) * (1 / (df['volatility_z'].abs() + 0.5))
    df['prev_oar'] = df.groupby('ticker')['oar_calc'].shift(1).fillna(0)
    df['oar_delta'] = df['oar_calc'] - df['prev_oar']
    return df

def run_backtest():
    print("📂 데이터 로딩 중... (V16.0 RSI Logic)")
    if not os.path.exists(DATA_FILE): return

    try:
        df = pd.read_csv(DATA_FILE)
        df = df.sort_values(by=['ticker']).reset_index(drop=True)
        df = df.replace([np.inf, -np.inf], np.nan).dropna()
        df = calculate_oar_features(df)
        print(f"✅ 데이터 준비 완료: {len(df):,}개")
    except Exception as e:
        print(e); return

    if not os.path.exists(MODEL_FILE): return
    try:
        model = xgb.XGBClassifier()
        model.load_model(MODEL_FILE)
    except Exception as e:
        print(e); return

    feature_cols = [
        'vwap_dist', 'squeeze', 'rsi', 'pump', 'pullback', 
        'rvol', 'volatility_z', 'order_imbalance', 'trend_align', 'session'
    ]
    
    print("⚡ AI 신경망 추론 시작...")
    try:
        probs = model.predict_proba(df[feature_cols])[:, 1]
        df['ai_score'] = probs * 100
    except: return
    
    results = []
    
    cnt = {
        'total': 0, '1_gate': 0, '2_final_signal': 0
    }
    
    print(f"\n⚔️ [V16.0] 오후장 RSI(50~75) 필터 적용 시뮬레이션...")
    
    for i, row in df.iterrows():
        cnt['total'] += 1
        
        # 1. Base Gate
        if (row['trend_align'] == 1) and (0.2 <= row['vwap_dist'] <= 3.5):
            cnt['1_gate'] += 1
            
            session = row['session']
            is_valid = False
            
            # ===================================================
            # 🌤️ [Session 0] Legend Mode (75% WR)
            # ===================================================
            if session == 0:
                if (1.5 <= row['pump'] <= 5.5) and \
                   (0.8 <= row['oar_delta'] <= 5.0) and \
                   (row['rvol'] >= 1.5) and \
                   (row['ai_score'] >= 50): 
                    is_valid = True

            # ===================================================
            # 🔒 [Session 1] Iron Dome (100% WR)
            # ===================================================
            elif session == 1:
                if (1.0 <= row['pump'] <= 2.5) and \
                   (row['oar_delta'] >= 2.0) and \
                   (row['rvol'] >= 5.0) and \
                   (row['ai_score'] >= 70): 
                    is_valid = True

            # ===================================================
            # 🎯 [Session 2] RSI Sniper Logic
            # 문제: 오후장은 가짜 펌핑이 많음
            # 해결: RSI 50~75 구간에서만 진입 (과열 방지 + 모멘텀 확인)
            # ===================================================
            elif session >= 2:
                # [신규] RSI 필터: 50 이상(상승세) & 75 이하(과열 아님)
                if (50 <= row['rsi'] <= 75):
                    
                    # VWAP 거리: 2.0% 이내로 더 좁힘 (확실한 눌림목)
                    if row['vwap_dist'] <= 2.0:
                        
                        # RVOL: 3.0 이상 (오후에는 거래량이 확실해야 함)
                        # Pump: 1.0 ~ 3.5%
                        if (1.0 <= row['pump'] <= 3.5) and \
                           (1.0 <= row['oar_delta'] <= 5.0) and \
                           (row['rvol'] >= 3.0) and \
                           (row['ai_score'] >= 60): # AI 기준 상향
                            is_valid = True

            if is_valid:
                # OAR 기본 필터
                if row['oar_calc'] > 2.0:
                    cnt['2_final_signal'] += 1
                    
                    results.append({
                        'win': row['label_win'],
                        'score': row['ai_score'],
                        'pump': row['pump'],
                        'rsi': row['rsi'],
                        'session': row['session']
                    })

    res_df = pd.DataFrame(results)
    
    print("\n" + "="*50)
    print(f"🔍 [V16.0 결과] RSI Logic Applied")
    print(f"1️⃣ 전체 데이터 : {cnt['total']:,}")
    print(f"🎯 Final Signal : {cnt['2_final_signal']:,}")
    print("="*50)
    
    if len(res_df) == 0:
        print("\n❌ 진입 횟수 0회.")
        return

    total_trades = len(res_df)
    total_wins = res_df['win'].sum()
    total_wr = (total_wins / total_trades) * 100
    
    print(f"\n🔥 [Final Performance]")
    print(f"✅ 총 진입: {total_trades}회")
    print(f"🏆 최종 승률: {total_wr:.2f}%")
    print("-" * 30)
    
    print("\n⏰ [Session Analysis - RSI Effect]")
    session_stats = res_df.groupby('session')['win'].agg(['count', 'mean'])
    session_stats['mean'] = session_stats['mean'] * 100
    session_stats.columns = ['Count', 'WinRate(%)']
    print(session_stats)

if __name__ == "__main__":
    run_backtest()