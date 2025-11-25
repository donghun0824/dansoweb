import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score
import joblib
import os
import gc

# =========================================================
# ⚙️ 설정 (고급 데이터 / 10분 타겟 / 1,500만 개)
# =========================================================
DATA_FILE = "training_data_advanced.csv"
MODEL_FILE = "sniper_model_advanced.json"
ROW_LIMIT = 15000000

def train():
    print(f"📂 데이터 로딩 중... ({DATA_FILE})")
    
    try:
        feature_cols = [
            'vwap_dist', 'squeeze', 'rsi', 'pump', 'pullback', 
            'rvol', 'volatility_z', 'order_imbalance', 'trend_align', 'session'
        ]
        load_cols = feature_cols + ['label_win']
        
        if ROW_LIMIT > 0:
            df = pd.read_csv(DATA_FILE, nrows=ROW_LIMIT, usecols=load_cols)
        else:
            df = pd.read_csv(DATA_FILE, usecols=load_cols)
            
        print(f"✅ 로딩 완료: {len(df):,}개 행")
    except FileNotFoundError:
        print("❌ 파일을 찾을 수 없습니다.")
        return

    # 전처리
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    
    X = df[feature_cols]
    y = df['label_win']
    
    del df
    gc.collect()

    # 데이터 분리
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # ⚖️ [핵심] 가중치 조정 (Strict Mode)
    # 단순 비율(scale_weight)을 그대로 쓰면 뻥튀기가 심하므로제곱근(sqrt)을 씌워 완화함
    num_neg = (y_train == 0).sum()
    num_pos = (y_train == 1).sum()
    raw_weight = num_neg / num_pos
    strict_weight = np.sqrt(raw_weight) 
    
    print(f"⚖️ 데이터 비율: {raw_weight:.1f}:1 -> 가중치 적용: {strict_weight:.2f}배 (과대평가 방지)")

    # 🧠 [핵심] 모델 파라미터 (AI 기강 잡기)
    model = xgb.XGBClassifier(
        n_estimators=5000,    # 끈기 있게 학습
        learning_rate=0.005,  # 아주 천천히, 꼼꼼하게 (0.01 -> 0.005)
        max_depth=8,          # 너무 깊게 파지 않음 (과적합 방지)
        min_child_weight=50,  # 확실한 패턴만 인정 (노이즈 제거)
        gamma=2.0,            # 가지치기 강화 (엄격함)
        subsample=0.7,
        colsample_bytree=0.7,
        scale_pos_weight=strict_weight, # 완화된 가중치 적용
        max_delta_step=1,     # 승률 뻥튀기 억제 장치
        n_jobs=-1,
        random_state=42,
        tree_method='hist',
        eval_metric='logloss'
    )

    print("🚀 AI 심층 학습 시작... (Strict Mode ON)")
    model.fit(X_train, y_train)

    # 평가
    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, preds)
    prec = precision_score(y_test, preds)

    print("\n" + "="*40)
    print(f"🔥 [Final] 모델 성능 리포트")
    print("="*40)
    print(f"✅ 전체 정확도: {acc*100:.2f}%")
    print(f"🎯 정밀도 (Precision): {prec*100:.2f}%")
    
    # 중요도 분석
    print("\n📊 [핵심] 승패를 가르는 결정적 요인")
    importances = model.feature_importances_
    indices = np.argsort(importances)[::-1]
    for i in range(len(feature_cols)):
        print(f"{i+1}. {feature_cols[indices[i]]}: {importances[indices[i]]:.4f}")

    # 📈 [진실의 시간] 구간별 승률 검증
    print("\n📈 [진실의 시간] AI 확신도별 실제 승률")
    check_df = pd.DataFrame({'prob': probs, 'actual': y_test.values})
    
    def check_bucket(min_p, max_p, label):
        mask = (check_df['prob'] >= min_p) & (check_df['prob'] < max_p)
        subset = check_df[mask]
        count = len(subset)
        if count > 0:
            win_rate = subset['actual'].mean() * 100
            print(f"{label} (예측 {int(min_p*100)}~{int(max_p*100)}%) -> 실제 승률: {win_rate:.2f}% (발견: {count}건)")
        else:
            print(f"{label} -> 데이터 없음")

    check_bucket(0.90, 1.01, "💎 [Diamond] 절대 확신")
    check_bucket(0.80, 0.90, "🥇 [Gold] 강력 추천")
    check_bucket(0.70, 0.80, "🥈 [Silver] 좋은 기회")
    check_bucket(0.60, 0.70, "🥉 [Bronze] 해볼 만함")

    # 🎯 [핵심] 상위 15% 엘리트 구간 분석 (Lift Analysis)
    print("\n📊 [최종 점검] 상위 15% 엘리트 구간 분석")
    check_df_sorted = check_df.sort_values(by='prob', ascending=False)
    top_n_percent = 0.15
    cut_index = int(len(check_df_sorted) * top_n_percent)
    top_segment = check_df_sorted.iloc[:cut_index]
    
    if len(top_segment) > 0:
        min_score = top_segment['prob'].min() * 100
        max_score = top_segment['prob'].max() * 100
        real_win = top_segment['actual'].mean() * 100
        
        print(f"🎯 분석 대상: 상위 {top_n_percent*100}% (총 {len(top_segment):,}건)")
        print(f"🛡️ 점수 커트라인: {min_score:.1f}점 ~ {max_score:.1f}점")
        print(f"🏆 이 구간의 실제 승률: {real_win:.2f}%")
        
        if real_win >= 90: print("👉 [결론] 미쳤습니다. 무조건 진입입니다.")
        elif real_win >= 80: print("👉 [결론] 훌륭합니다. OAR 필터와 조합하면 최강입니다.")
        elif real_win >= 60: print("👉 [결론] 준수합니다. 분할 매수로 접근하세요.")
        else: print("👉 [결론] 아직 위험합니다. 10분 3%는 너무 가혹한 기준일 수 있습니다.")

    model.save_model(MODEL_FILE)
    print(f"\n💾 모델 저장 완료: {MODEL_FILE}")

if __name__ == "__main__":
    train()