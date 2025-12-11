# worker.py
import redis
import json
import os
import time
import sys
import firebase_admin
from firebase_admin import credentials

# ------------------------------------------------------------------
# 🔥 [수정 완료] 기존 STS_Engine.py 파일에서 로직을 가져옵니다.
# ------------------------------------------------------------------
from app import init_db

try:
    # 사용자님의 파일명이 'STS_Engine.py'이므로 대소문자 정확히 입력
    from STS_Engine import STSPipeline, STS_TARGET_COUNT
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다.")
    print("   파일 이름이 'STS_Engine.py'가 맞는지 확인해주세요.")
    sys.exit(1)
# ------------------------------------------------------------------

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
r = redis.from_url(REDIS_URL)

def init_firebase_worker():
    if firebase_admin._apps: return
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR: return
        # JSON 파싱 (줄바꿈 문자 등 예외처리)
        json_str = FIREBASE_ADMIN_SDK_JSON_STR.strip()
        if json_str.startswith("'") and json_str.endswith("'"):
            json_str = json_str[1:-1]
            
        try:
            cred_dict = json.loads(json_str)
        except json.JSONDecodeError:
            fixed_str = json_str.replace('\\n', '\n')
            cred_dict = json.loads(fixed_str)

        firebase_admin.initialize_app(credentials.Certificate(cred_dict))
        print("✅ [Worker] Firebase Init Done", flush=True)
    except Exception as e:
        print(f"⚠️ [Worker] Firebase Init Warning: {e}", flush=True)

def consumer():
    print("🧠 [Worker] Starting Logic Engine...", flush=True)
    
    # 1. 초기화
    init_db()
    init_firebase_worker()
    
    # 2. 봇 파이프라인 생성 (STS_Engine.py의 클래스 사용)
    pipeline = STSPipeline()
    
    last_agg = {}
    last_quotes = {}
    last_manager_run = time.time()
    last_scan_run = time.time()

    print("🧠 [Worker] Ready. Waiting for Redis stream...", flush=True)

    while True:
        try:
            # 3. Redis에서 데이터 꺼내기 (Blocking)
            # 타임아웃 1초를 줘서 데이터가 없어도 주기적으로 매니저 로직이 돌게 함
            pop_result = r.brpop('ticker_stream', timeout=1)
            
            if pop_result:
                _, msg = pop_result
                data = json.loads(msg)
                
                # 4. 데이터 처리 (계산 로직)
                for item in data:
                    ev = item.get('ev')
                    t = item.get('sym')
                    
                    if ev == 'A':
                        pipeline.selector.update(item)
                        last_agg[t] = item
                        
                        # 봇 강제 구동 (데이터가 들어오면 즉시 계산)
                        if t in pipeline.snipers:
                            pipeline.snipers[t].update_dashboard_db(
                                {'p': item['c'], 's': item['v'], 't': item['e']}, 
                                last_quotes.get(t, {'bids':[],'asks':[]}), 
                                item
                            )
                    elif ev == 'Q':
                        last_quotes[t] = {
                            'bids': [{'p':item.get('bp'),'s':item.get('bs')}], 
                            'asks': [{'p':item.get('ap'),'s':item.get('as')}]
                        }
                    elif ev == 'T' and t in pipeline.snipers:
                        # 🔥 여기서 풀파워 계산 (STS_Engine의 로직 수행)
                        pipeline.snipers[t].update_dashboard_db(
                            item, 
                            last_quotes.get(t, {'bids':[],'asks':[]}), 
                            last_agg.get(t)
                        )

            # =========================================================
            # 5. 주기적 작업 (Manager & Scanner)
            # =========================================================
            now = time.time()

            # (1) 종목 선정 (5초 주기)
            if now - last_manager_run > 5.0:
                candidates = pipeline.selector.get_top_gainers_candidates(limit=10)
                
                if candidates:
                    # 스캐너 DB 저장 (가격 갱신)
                    pipeline.selector.save_candidates_to_db(pipeline.selector.snapshots.values())
                    
                    # Top 3 선정
                    target_top3 = pipeline.selector.get_best_snipers(candidates, limit=STS_TARGET_COUNT)
                    
                    current_set = set(pipeline.snipers.keys())
                    new_set = set(target_top3)
                    
                    # 봇 제거 (Detach)
                    for rem in (current_set - new_set):
                        if rem in pipeline.snipers: 
                            print(f"👋 [Worker] Detach: {rem}", flush=True)
                            del pipeline.snipers[rem]
                    
                    # 봇 추가 (Attach)
                    for add in (new_set - current_set):
                        if add not in pipeline.snipers:
                            print(f"🚀 [Worker] Attach: {add}", flush=True)
                            # 🔥 [수정 완료] STS_Engine에서 가져옴
                            from STS_Engine import SniperBot
                            pipeline.snipers[add] = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.shared_model)
                
                last_manager_run = now

            # (2) 메모리 청소 (5분 주기)
            if now - last_scan_run > 300:
                pipeline.selector.garbage_collect()
                last_scan_run = now

        except Exception as e:
            print(f"❌ [Worker Error] {e}", flush=True)
            # 에러 나도 죽지 않음
            time.sleep(1)

if __name__ == "__main__":
    consumer()