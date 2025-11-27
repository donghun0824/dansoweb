import requests
import pandas as pd
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
# 사용자 API 키 적용
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

BASE_URL = "https://api.polygon.io"
DATA_DIR = "datasets"

# 🔥 [수정됨] 수집 기간: 최근 3개월 (90일)
END_DATE = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
START_DATE = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

print(f"📅 수집 기간 설정: {START_DATE} ~ {END_DATE} (최근 3개월)")

# ==============================================================================
# 2. COLLECTOR FUNCTIONS
# ==============================================================================

def get_daily_gainers(date):
    """해당 날짜의 Top Gainers 10개 추출 (수정됨)"""
    url = f"{BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true&apiKey={POLYGON_API_KEY}"
    try:
        res = requests.get(url).json()
        if 'results' not in res: return []
        
        df = pd.DataFrame(res['results'])
        
        if df.empty or 'v' not in df.columns or 'c' not in df.columns:
            return []

        # 거래량 100만불 이상 & 5% 이상 상승 종목 필터링
        df['dollar_vol'] = df['v'] * df['c']
        candidates = df[(df['dollar_vol'] > 1_000_000) & 
                        ((df['c'] - df['o']) / df['o'] > 0.05)]
        
        if candidates.empty: return []

        # 상승률 순 정렬 후 Top 10만 추출 (20 -> 10으로 변경)
        candidates['change'] = (candidates['c'] - candidates['o']) / candidates['o']
        top_10 = candidates.sort_values('change', ascending=False).head(10)['T'].tolist()
        return top_10
    except Exception as e:
        print(f"❌ [Error] {date} Gainers fetch failed: {e}")
        return []

def download_ticker_data(ticker, date):
    """Tick(Trades), Quote, Aggregate 데이터 다운로드"""
    save_dir = f"{DATA_DIR}/{date}/{ticker}"
    
    # 이미 Trades와 Quotes가 둘 다 있으면 스킵
    if os.path.exists(save_dir) and \
       os.path.exists(f"{save_dir}/trades.csv") and \
       os.path.exists(f"{save_dir}/quotes.csv"):
        print(f"⏩ {date} | {ticker} All data exists. Skipping.")
        return

    os.makedirs(save_dir, exist_ok=True)
    
    # 1. Aggregates (1min)
    try:
        url_agg = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}"
        res = requests.get(url_agg).json()
        if 'results' in res:
            pd.DataFrame(res['results']).to_csv(f"{save_dir}/agg.csv", index=False)
    except: pass

    # 2. Trades (Ticks)
    try:
        url_trade = f"{BASE_URL}/v3/trades/{ticker}?timestamp={date}&limit=50000&apiKey={POLYGON_API_KEY}"
        res = requests.get(url_trade).json()
        if 'results' in res:
            pd.DataFrame(res['results']).to_csv(f"{save_dir}/trades.csv", index=False)
    except: pass

    # 3. Quotes (NBBO) - 🔥 [중요] 주석 해제됨 (다운로드 실행)
    try:
        # Quotes는 데이터가 많아서 limit를 최대로 늘림
        url_quote = f"{BASE_URL}/v3/quotes/{ticker}?timestamp={date}&limit=50000&apiKey={POLYGON_API_KEY}"
        res = requests.get(url_quote).json()
        if 'results' in res:
            pd.DataFrame(res['results']).to_csv(f"{save_dir}/quotes.csv", index=False)
            # print(f"   └─ Quotes saved for {ticker}") # 로그 너무 많으면 주석 처리
    except Exception as e: 
        print(f"   ⚠️ Quote download failed: {e}")
    
    print(f"✅ {date} | {ticker} Data Saved")

def main():
    if not POLYGON_API_KEY:
        print("❌ Error: API Key Missing!")
        return

    # 영업일 기준 날짜 리스트
    dates = pd.date_range(start=START_DATE, end=END_DATE, freq='B') 
    
    print(f"📊 총 수집 예정일: {len(dates)}일 (Top 10 종목/일)")
    print(f"⚠️ 주의: 호가(Quotes) 데이터 포함으로 용량이 큽니다. 디스크 공간을 확인하세요.")

    for d in dates:
        date_str = d.strftime('%Y-%m-%d')
        print(f"\n📅 Processing {date_str}...")
        
        gainers = get_daily_gainers(date_str)
        
        if not gainers:
            print("   No gainers found or holiday.")
            continue

        print(f"   Targets: {gainers}")
        
        # 워커 수 5 유지
        with ThreadPoolExecutor(max_workers=5) as executor:
            for ticker in gainers:
                executor.submit(download_ticker_data, ticker, date_str)
                time.sleep(0.1) 

if __name__ == "__main__":
    main()