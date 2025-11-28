import numpy as np
import pandas as pd

# =============================================================================
# PART 1. Standard Indicators (공통 지표)
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

def compute_atr_series(df, high_col='high', low_col='low', close_col='close', period=14):
    """전체 기간의 ATR을 한 번에 계산"""
    high = df[high_col]
    low = df[low_col]
    close = df[close_col]
    
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr

def compute_realized_vol_series(price_series, window=60):
    """로그 수익률 기반 변동성 계산"""
    log_ret = np.log(price_series).diff()
    rv = log_ret.rolling(window=window).apply(lambda x: np.sqrt(np.sum(x**2)))
    return rv * 10000  # 스케일 보정

def compute_fibo_pos(highs, lows, closes, lookback=600):
    """현재가가 최근 N초 파동 내에서 어디에 위치하는지 (0~1)"""
    rolling_low = lows.rolling(window=lookback, min_periods=1).min()
    rolling_high = highs.rolling(window=lookback, min_periods=1).max()
    
    rng = rolling_high - rolling_low
    rng = rng.replace(0, np.nan)
    
    pos = (closes - rolling_low) / rng
    return pos.fillna(0.5)

def compute_bb_squeeze(closes, window=20, mult=2.0, norm_window=300):
    """BB Width, Normalized Width, Squeeze Flag 계산"""
    sma = closes.rolling(window=window).mean()
    std = closes.rolling(window=window).std()
    upper = sma + (mult * std)
    lower = sma - (mult * std)
    
    bb_width = (upper - lower) / sma.replace(0, np.nan)
    bb_width = bb_width.fillna(0)
    
    bb_width_mean = bb_width.rolling(window=norm_window, min_periods=1).mean()
    bb_width_norm = bb_width / bb_width_mean.replace(0, np.nan)
    bb_width_norm = bb_width_norm.fillna(1.0)
    
    squeeze_flag = (bb_width_norm < 0.7).astype(int)
    
    return bb_width, bb_width_norm, squeeze_flag

def compute_rv_60(closes):
    """직전 60초 실현 변동성"""
    log_ret = np.log(closes / closes.shift(1))
    rv = log_ret.rolling(60).std() * np.sqrt(60)
    return rv.fillna(0)

def compute_vol_ratio_60(volumes):
    """직전 60초 거래량 / 10분 평균 거래량 비율"""
    vol_60 = volumes.rolling(60).sum()
    vol_600_mean = volumes.rolling(600).mean()
    
    ratio = vol_60 / (vol_600_mean * 60)
    return ratio.fillna(1.0).replace([np.inf, -np.inf], 1.0)


# =============================================================================
# PART 2. Live Bot Functions (Numpy 기반 - 실시간 봇용)
# =============================================================================

def classify_trade_sign(price, best_bid, best_ask):
    """
    [실시간용] 현재 체결이 매수(1)인지 매도(-1)인지 판별 (Lee-Ready)
    """
    if best_bid <= 0 or best_ask <= 0: return 0.0
    
    if price >= best_ask: return 1.0
    if price <= best_bid: return -1.0
    
    mid_price = (best_bid + best_ask) / 2
    if price > mid_price: return 1.0
    elif price < mid_price: return -1.0
        
    return 0.0

def compute_vpin(signed_vol_array):
    """
    [실시간용] VPIN (주문 독성) 계산 - Numpy Array 입력
    """
    # signed_vol_array: 0보다 크면 매수량, 작으면 매도량
    buy_vol = np.sum(signed_vol_array[signed_vol_array > 0])
    sell_vol = np.abs(np.sum(signed_vol_array[signed_vol_array < 0]))
    
    total_vol = buy_vol + sell_vol
    if total_vol == 0: return 0.0
        
    return abs(buy_vol - sell_vol) / total_vol

def compute_order_book_imbalance(bids, asks):
    """
    [실시간용] OBI (호가 잔량 불균형) 계산 - Numpy Array 입력
    """
    bid_vol = np.sum(bids)
    ask_vol = np.sum(asks)
    
    total = bid_vol + ask_vol
    if total == 0: return 0.0
        
    return (bid_vol - ask_vol) / total


# =============================================================================
# PART 3. Batch Processing Functions (Pandas 기반 - 백테스트/학습용)
# =============================================================================

def compute_signed_volume(df, price_col='close', bid_col='bid', ask_col='ask', size_col='volume'):
    """
    [학습용] 데이터프레임 전체 매수/매도 분류 (Lee-Ready)
    """
    if bid_col not in df.columns:
        # 호가 없으면 Tick Rule
        price_change = df[price_col].diff()
        sign = np.where(price_change > 0, 1, np.where(price_change < 0, -1, 0))
    else:
        # 호가 있으면 Mid Price Rule
        mid = (df[bid_col] + df[ask_col]) / 2
        sign = np.where(df[price_col] > mid, 1, np.where(df[price_col] < mid, -1, 0))
    
    return df[size_col] * sign

def compute_obi_series_from_quotes(quotes_df):
    """
    [학습용] Quotes CSV 데이터프레임 -> OBI 시리즈 반환
    """
    if 'bid_size' not in quotes_df.columns or 'ask_size' not in quotes_df.columns:
        return pd.Series(0, index=quotes_df.index)
        
    bids = quotes_df['bid_size']
    asks = quotes_df['ask_size']
    
    obi = (bids - asks) / (bids + asks)
    return obi.fillna(0)

def compute_vpin_series_from_trades(trades_df):
    """
    [학습용] Trades CSV -> VPIN 시리즈 반환 (근사치)
    """
    price_diff = trades_df['price'].diff()
    signs = np.where(price_diff > 0, 1, np.where(price_diff < 0, -1, 0))
    signed_vol = trades_df['size'] * signs
    
    # 60개 틱(또는 초) 윈도우로 계산
    rolling_buy = signed_vol.where(signed_vol > 0, 0).rolling(window=60, min_periods=1).sum()
    rolling_sell = signed_vol.where(signed_vol < 0, 0).abs().rolling(window=60, min_periods=1).sum()
    
    total = rolling_buy + rolling_sell
    vpin = (rolling_buy - rolling_sell).abs() / total.replace(0, np.nan)
    
    return vpin.fillna(0)