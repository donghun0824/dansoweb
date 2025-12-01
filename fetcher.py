import time
import requests
import os
import json
import redis
import logging

# 설정
API_KEY = os.environ.get('POLYGON_API_KEY')
REDIS_URL = os.environ.get('REDIS_URL')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [Fetcher] %(message)s')
logger = logging.getLogger("Fetcher")

try:
    r = redis.from_url(REDIS_URL)
    r.ping()
    logger.info("✅ Redis 연결 성공")
except Exception as e:
    logger.error(f"❌ Redis 연결 실패: {e}")
    exit(1)

def fetch_and_cache():
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey={API_KEY}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if 'tickers' in data:
            # Redis에 저장 (Key: 'market_snapshot')
            # 다른 봇들이 이 키를 조회해서 씀
            r.set('market_snapshot', json.dumps(data['tickers']))
            # logger.info("데이터 갱신 완료 (Snapshot Updated)") # 로그 너무 많으면 주석
        else:
            logger.warning("데이터 비어있음")
            
    except Exception as e:
        logger.error(f"API 호출 오류: {e}")

def main():
    logger.info("🔥 데이터 배달부 시작 (Polygon -> Redis)")
    while True:
        start_time = time.time()
        
        fetch_and_cache()
        
        # 1초 주기 유지 (API 속도 제한 고려)
        elapsed = time.time() - start_time
        sleep_time = max(0, 1.0 - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()