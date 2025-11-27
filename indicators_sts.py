import numpy as np
import pandas as pd

# =============================================================================
# 1. VWAP (Volume-Weighted Average Price) - 대량 데이터용
# =============================================================================
def compute_intraday_vwap_series(df, price_col='close', volume_col='volume'):
    """1년치 데이터를 넣어도 한방에 VWAP 라인을 그려주는 함수"""
    p = df[price_col].values
    v = df[volume_col].values
    
    # 누적 합계 계산 (Cumulative Sum)
    cum_pv = np.cumsum(p * v)
    cum_v = np.cumsum(v)
    
    # 0으로 나누기 방지
    vwap = np.divide(cum_pv, cum_v, out=np.zeros_like(cum_v, dtype=float), where=cum_v!=0)
    return pd.Series(vwap, index=df.index)

# =============================================================================
# 2. ATR (Average True Range) - 대량 데이터용
# =============================================================================
def compute_atr_series(df, high_col='high', low_col='low', close_col='close', period=14):
    """전체 기간의 ATR을 한 번에 계산"""
    high = df[high_col]
    low = df[low_col]
    close = df[close_col]
    
    # 1. 전일 종가 (Shift)
    prev_close = close.shift(1)
    
    # 2. True Range 계산 (세 가지 중 최대값)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # 3. ATR (이동 평균)
    atr = tr.rolling(window=period).mean()
    return atr

# =============================================================================
# 3. Realized Volatility (RV)
# =============================================================================
def compute_realized_vol_series(price_series, window=60):
    """로그 수익률 기반 변동성 계산"""
    log_ret = np.log(price_series).diff()
    # 제곱의 합 -> 제곱근 (표준편차와 유사)
    rv = log_ret.rolling(window=window).apply(lambda x: np.sqrt(np.sum(x**2)))
    return rv * 10000  # 스케일 보정

# =============================================================================
# 4. Signed Volume & VPIN (주문 독성)
# =============================================================================
def compute_signed_volume(df, price_col='close', bid_col='bid', ask_col='ask', size_col='volume'):
    """Lee-Ready 알고리즘으로 매수/매도 체결 분류"""
    # 호가 데이터가 없으면 틱 룰(가격 변화) 사용
    if bid_col not in df.columns:
        price_change = df[price_col].diff()
        sign = np.where(price_change > 0, 1, np.where(price_change < 0, -1, 0))
    else:
        # 호가 데이터가 있으면 Mid Price 기준 분류
        mid = (df[bid_col] + df[ask_col]) / 2
        sign = np.where(df[price_col] > mid, 1, np.where(df[price_col] < mid, -1, 0))
    
    return df[size_col] * sign

def compute_vpin_series(signed_vol_series, bucket_vol=10000):
    """VPIN (주문 불균형 독성 지표) 계산"""
    # 간단한 롤링 불균형 비율로 근사
    # (정석 VPIN은 버킷 단위지만, 백테스트용으로 롤링 윈도우 사용)
    buy_vol = signed_vol_series[signed_vol_series > 0].sum()
    sell_vol = signed_vol_series[signed_vol_series < 0].abs().sum()
    
    total = buy_vol + sell_vol
    if total == 0: return 0
    return abs(buy_vol - sell_vol) / total

# =============================================================================
# 5. OBI (Order Book Imbalance)
# =============================================================================
def compute_obi_series(df, bid_sz_col='bid_size', ask_sz_col='ask_size'):
    """호가 잔량 불균형 계산"""
    if bid_sz_col not in df.columns:
        return pd.Series(0, index=df.index) # 데이터 없으면 0
        
    bids = df[bid_sz_col]
    asks = df[ask_sz_col]
    
    obi = (bids - asks) / (bids + asks)
    return obi.fillna(0)
# [indicators_sts.py 파일 하단에 추가]

# =============================================================================
# 6. Fibonacci Position & Retracement
# =============================================================================
def compute_fibo_pos(highs, lows, closes, lookback=600):
    """
    현재가가 최근 N초(lookback) 파동 내에서 어디에 위치하는지 (0~1) 계산
    0: 최저점, 1: 최고점, 0.5: 중간
    """
    # 롤링 최저/최고 계산
    rolling_low = lows.rolling(window=lookback, min_periods=1).min()
    rolling_high = highs.rolling(window=lookback, min_periods=1).max()
    
    # 범위(Range) 계산 (0으로 나누기 방지)
    rng = rolling_high - rolling_low
    rng = rng.replace(0, np.nan) # 범위가 0이면 계산 불가
    
    # 위치 계산
    pos = (closes - rolling_low) / rng
    return pos.fillna(0.5) # 데이터 부족하거나 범위 0이면 중간값

# =============================================================================
# 7. Bollinger Squeeze Metrics
# =============================================================================
def compute_bb_squeeze(closes, window=20, mult=2.0, norm_window=300):
    """
    1) BB Width 계산
    2) BB Width Norm (과거 평균 대비 현재 폭 비율)
    3) Squeeze Flag (0.7 이하일 때 1)
    """
    # BB 계산
    sma = closes.rolling(window=window).mean()
    std = closes.rolling(window=window).std()
    upper = sma + (mult * std)
    lower = sma - (mult * std)
    
    # 1. 절대 폭 (Percent Width)
    # sma가 0이면 나눗셈 에러나므로 처리
    bb_width = (upper - lower) / sma.replace(0, np.nan)
    bb_width = bb_width.fillna(0)
    
    # 2. 정규화된 폭 (과거 N초 평균 대비 비율)
    bb_width_mean = bb_width.rolling(window=norm_window, min_periods=1).mean()
    bb_width_norm = bb_width / bb_width_mean.replace(0, np.nan)
    bb_width_norm = bb_width_norm.fillna(1.0) # 데이터 없으면 평균(1.0)으로 가정
    
    # 3. 스퀴즈 시그널 (과거 평균의 70% 이하로 수축 시)
    squeeze_flag = (bb_width_norm < 0.7).astype(int)
    
    return bb_width, bb_width_norm, squeeze_flag

# =============================================================================
# 8. (Optional) Advanced Volatility & Volume Ratio
# =============================================================================
def compute_rv_60(closes):
    """직전 60초 실현 변동성"""
    log_ret = np.log(closes / closes.shift(1))
    rv = log_ret.rolling(60).std() * np.sqrt(60)
    return rv.fillna(0)

def compute_vol_ratio_60(volumes):
    """직전 60초 거래량 / 10분 평균 거래량 비율"""
    vol_60 = volumes.rolling(60).sum()
    vol_600_mean = volumes.rolling(600).mean()
    
    ratio = vol_60 / (vol_600_mean * 60) # 단위 맞춤 (평균은 1초당 평균이므로 * 60)
    # 0 나누기 방지
    return ratio.fillna(1.0).replace([np.inf, -np.inf], 1.0)