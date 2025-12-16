import numpy as np
import pandas as pd

# =============================================================================
# PART 1. Standard Indicators (공통 지표 - Series 반환)
# =============================================================================

# indicators_sts.py

def compute_rsi_series(series, period=14):
    """
    [NEW] RSI(상대강도지수) 계산 함수
    - Schwab 가이드: 30 이하 과매도, 70 이상 과매수
    - 추세장에서는 50~55가 지지선 역할
    """
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)

    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()

    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50) # 데이터 부족 시 50(중립) 반환

def compute_intraday_vwap_series(df, price_col='close', volume_col='volume'):
    """1년치 데이터를 넣어도 한방에 VWAP 라인을 그려주는 함수"""
    p = df[price_col].values
    v = df[volume_col].values
    
    # 누적 합계 계산
    cum_pv = np.cumsum(p * v)
    cum_v = np.cumsum(v)
    
    # 0으로 나누기 방지
    vwap = np.divide(cum_pv, cum_v, out=np.zeros_like(cum_v, dtype=float), where=cum_v!=0)
    return pd.Series(vwap, index=df.index).ffill()

def compute_vwap_slope_series(vwap_series, window=5):
    """[NEW] VWAP 기울기 계산 (5초 변화량)"""
    # (현재 - 5초전) / 5초전 * 10000 (Basis Point)
    slope = (vwap_series.diff(window) / (vwap_series.shift(window) + 1e-9)) * 10000
    return slope.fillna(0)

def compute_rvol_series(volume_series, window=60):
    """[NEW] RVOL 계산 (1분 평균 대비)"""
    vol_ma = volume_series.rolling(window=window).mean()
    rvol = volume_series / (vol_ma + 1e-9)
    return rvol.fillna(0)

def compute_pump_accel_series(price_series, short_win=60, long_win=300):
    """[NEW] 상승 가속도 (Pump Acceleration)"""
    # 5분(300초) 등락률의 1분(60초) 변화량
    pump_long = price_series.pct_change(periods=long_win)
    accel = pump_long.diff(periods=short_win)
    return accel.fillna(0) * 100 # % 단위 변환

def compute_atr_series(df, high_col='high', low_col='low', close_col='close', period=60):
    """[Updated] 1분(60초) 기준 정밀 ATR 계산"""
    high = df[high_col]
    low = df[low_col]
    close = df[close_col]
    
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr.fillna(method='bfill')

def compute_fibo_pos(highs, lows, closes, lookback=600):
    """현재가가 최근 10분(600초) 파동 내에서 어디에 위치하는지 (0~1)"""
    rolling_low = lows.rolling(window=lookback, min_periods=1).min()
    rolling_high = highs.rolling(window=lookback, min_periods=1).max()
    
    rng = rolling_high - rolling_low
    rng = rng.replace(0, np.nan)
    
    pos = (closes - rolling_low) / rng
    return pos.fillna(0.5)

def compute_bb_squeeze(closes, window=30, mult=2.0, norm_window=60):
    """[Updated] BB Squeeze Ratio (30초/60초 기준)"""
    sma = closes.rolling(window=window).mean()
    std = closes.rolling(window=window).std()
    
    # 밴드 폭
    bb_width = (std * 4) / sma.replace(0, np.nan)
    bb_width = bb_width.fillna(0)
    
    # 스퀴즈 비율 (현재폭 / 평균폭)
    bb_width_mean = bb_width.rolling(window=norm_window, min_periods=1).mean()
    squeeze_ratio = bb_width / (bb_width_mean + 1e-9)
    squeeze_ratio = squeeze_ratio.fillna(1.0)
    
    # 플래그 (하위 호환용)
    squeeze_flag = (squeeze_ratio < 0.7).astype(int)
    
    return bb_width, squeeze_ratio, squeeze_flag

def compute_rv_60(closes):
    """직전 60초 실현 변동성 (스케일 보정 포함)"""
    log_ret = np.log(closes / closes.shift(1))
    # V9.3 엔진과 동일하게 * 100 추가
    rv = log_ret.rolling(60).std() * np.sqrt(60) * 100 
    return rv.fillna(0)

def compute_vol_ratio_60(volumes):
    """[Legacy] 구버전 호환용 (10분 평균 대비)"""
    vol_60 = volumes.rolling(60).sum()
    vol_600_mean = volumes.rolling(600).mean()
    ratio = vol_60 / (vol_600_mean * 60)
    return ratio.fillna(1.0).replace([np.inf, -np.inf], 1.0)

# indicators_sts.py 의 PART 1 영역 맨 아래에 추가

def compute_bb_bandwidth(closes, window=20):
    """
    [NEW] 볼린저 밴드 폭 (Squeeze 탐지용 - 단일 값 반환)
    - sts.py의 get_metrics에서 사용됨
    - 밴드폭이 과거 60초 평균 대비 얼마나 좁은지(Ratio) 반환
    """
    sma = closes.rolling(window=window).mean()
    std = closes.rolling(window=window).std()
    
    # 밴드폭 (Upper - Lower)
    width = (std * 4)
    
    # 스퀴즈 비율 (현재폭 / 60초 평균폭)
    # 1.0 이하면 최근 평균보다 폭이 좁아짐 (압축)
    avg_width = width.rolling(window=60, min_periods=1).mean()
    squeeze_ratio = width / (avg_width + 1e-9)
    
    return squeeze_ratio.fillna(1.0)

# indicators_sts.py (PART 1 맨 아래에 추가)

def compute_stochastic_series(highs, lows, closes, k_period=14):
    """[NEW] 스토캐스틱 Fast %K 계산"""
    low_min = lows.rolling(window=k_period).min()
    high_max = highs.rolling(window=k_period).max()
    
    k = 100 * ((closes - low_min) / (high_max - low_min + 1e-9))
    return k.fillna(50)

# [V6.1 NEW] 엔진 업데이트로 추가된 필수 지표들

def compute_volatility_ratio(series, short_win=20, long_win=120):
    """
    [V6.1] 단기/장기 변동성 비율 (Vol Ratio)
    - 엔진의 df['vol_ratio'] 계산 로직과 동일
    """
    short_vol = series.pct_change().rolling(short_win).std()
    long_vol = series.pct_change().rolling(long_win).std()
    
    # 0으로 나누기 방지
    ratio = short_vol / (long_vol + 1e-9)
    return ratio.fillna(1.0)

def compute_efficiency_ratio(series, window=20):
    """
    [V6.1] 효율성 지수 (Efficiency Ratio)
    - 엔진의 df['hurst'] 계산을 위한 기초 값
    - (순수 이동 거리 / 전체 이동 경로)
    """
    change = series.diff(window).abs()
    path = series.diff().abs().rolling(window).sum()
    
    er = change / (path + 1e-9)
    return er.fillna(0.5)


# =============================================================================
# PART 2. Live Bot Functions (Numpy 기반 - 실시간 봇용)
# =============================================================================

def classify_trade_sign(price, best_bid, best_ask):
    """[실시간용] Lee-Ready 알고리즘"""
    if best_bid <= 0 or best_ask <= 0: return 0.0
    if price >= best_ask: return 1.0
    if price <= best_bid: return -1.0
    mid = (best_bid + best_ask) / 2
    if price > mid: return 1.0
    elif price < mid: return -1.0
    return 0.0

def compute_vpin(signed_vol_array):
    """[실시간용] VPIN (주문 독성)"""
    buy_vol = np.sum(signed_vol_array[signed_vol_array > 0])
    sell_vol = np.abs(np.sum(signed_vol_array[signed_vol_array < 0]))
    total = buy_vol + sell_vol
    if total == 0: return 0.0
    return abs(buy_vol - sell_vol) / total

def compute_order_book_imbalance(bids, asks):
    """[실시간용] OBI (호가 불균형)"""
    bid_vol = np.sum(bids)
    ask_vol = np.sum(asks)
    total = bid_vol + ask_vol
    if total == 0: return 0.0
    return (bid_vol - ask_vol) / total


# =============================================================================
# PART 3. Batch Processing Functions (Pandas 기반 - 학습용)
# =============================================================================

def compute_signed_volume(df, price_col='close', bid_col='bid', ask_col='ask', size_col='volume'):
    if bid_col not in df.columns:
        price_change = df[price_col].diff()
        sign = np.where(price_change > 0, 1, np.where(price_change < 0, -1, 0))
    else:
        mid = (df[bid_col] + df[ask_col]) / 2
        sign = np.where(df[price_col] > mid, 1, np.where(df[price_col] < mid, -1, 0))
    return df[size_col] * sign

def compute_obi_series_from_quotes(quotes_df):
    if 'bid_size' not in quotes_df.columns or 'ask_size' not in quotes_df.columns:
        return pd.Series(0, index=quotes_df.index)
    bids = quotes_df['bid_size']
    asks = quotes_df['ask_size']
    obi = (bids - asks) / (bids + asks)
    return obi.fillna(0)

def compute_vpin_series_from_trades(trades_df):
    price_diff = trades_df['price'].diff()
    signs = np.where(price_diff > 0, 1, np.where(price_diff < 0, -1, 0))
    signed_vol = trades_df['size'] * signs
    
    rolling_buy = signed_vol.where(signed_vol > 0, 0).rolling(window=60, min_periods=1).sum()
    rolling_sell = signed_vol.where(signed_vol < 0, 0).abs().rolling(window=60, min_periods=1).sum()
    
    total = rolling_buy + rolling_sell
    vpin = (rolling_buy - rolling_sell).abs() / total.replace(0, np.nan)
    return vpin.fillna(0)