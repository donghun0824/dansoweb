# worker.py
import redis
import json
import os
import time
import sys
import asyncio 
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import firebase_admin
from firebase_admin import credentials

# [필수] DB 설정 가져오기
from app import init_db

try:
    # 우리가 수정한 STS_Engine에서 필요한 클래스와 변수들 가져오기
    # DB_WORKER_POOL은 STS_Engine에 정의되어 있어야 합니다. 없다면 아래에서 새로 정의해도 됩니다.
    from STS_Engine import STSPipeline, STS_TARGET_COUNT, SniperBot, DB_WORKER_POOL
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다. 경로를 확인하세요.")
    sys.exit(1)

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
r = redis.from_url(REDIS_URL)

# Redis 블로킹 방지를 위한 별도 스레드 풀 (메인 루프 멈춤 방지용)
REDIS_POOL = ThreadPoolExecutor(max_workers=1)

def init_firebase_worker():
    if firebase_admin._apps: return
    try:
        if not FIREBASE_ADMIN_SDK_JSON_STR: return
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

# 웜업을 안전한 비동기 태스크로 실행하는 헬퍼
def run_warmup_task(bot):
    try:
        # threading.Thread 대신 asyncio.create_task 사용 (충돌 해결 핵심)
        asyncio.create_task(bot.warmup())
    except Exception as e:
        print(f"⚠️ [Warmup Start Error] {e}")

# 메인 루프를 비동기 함수로 변경
async def redis_consumer():
    print("🧠 [Worker] Starting Logic Engine (Async Redis Mode)...", flush=True)
    
    # DB 및 Firebase 초기화
    init_db()
    init_firebase_worker()
    
    # 파이프라인 생성
    pipeline = STSPipeline()
    
    # 로컬 데이터 저장소
    last_agg = {}
    last_quotes = {}
    
    # 타이머
    last_manager_run = time.time()
    last_scan_run = time.time()
    
    # 입사 시간 기록부
    bot_attach_times = {}

    print("🧠 [Worker] Ready. Listening to 'ticker_stream'...", flush=True)

    # 현재 실행 중인 루프 가져오기
    loop = asyncio.get_running_loop()

    while True:
        try:
            # [핵심 수정 1] Redis brpop을 별도 스레드에서 실행하여 메인 루프 차단 방지
            # 이제 Redis가 데이터를 기다리는 동안에도 봇은 다른 일(매매, 웜업)을 할 수 있습니다.
            pop_result = await loop.run_in_executor(
                REDIS_POOL, 
                partial(r.brpop, 'ticker_stream', timeout=1)
            )
            
            if pop_result:
                _, msg = pop_result
                data = json.loads(msg)
                
                for item in data:
                    ev = item.get('ev')
                    t = item.get('sym')
                    
                    if ev == 'A':
                        pipeline.selector.update(item)
                        last_agg[t] = item
                        if t in pipeline.snipers:
                            # 봇 로직 업데이트 (내부적으로 최적화됨)
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
                        pipeline.snipers[t].update_dashboard_db(
                            item, 
                            last_quotes.get(t, {'bids':[],'asks':[]}), 
                            last_agg.get(t)
                        )

            # =========================================================
            # Manager 로직 (비동기 호환 수정)
            # =========================================================
            now = time.time()

            if now - last_manager_run > 5.0:
                # [핵심 수정 2] 무거운 DB 읽기 작업을 스레드 풀로 격리
                candidates = await loop.run_in_executor(
                    DB_WORKER_POOL,
                    partial(pipeline.selector.get_top_gainers_candidates, limit=10)
                )
                
                if candidates:
                    # 저장 로직은 TargetSelector 내부에서 안전하게 처리됨 ('P' 에러 해결됨)
                    # pipeline.selector.save_candidates_to_db(candidates) -> get_top_gainers 내부 호출 가정 시 생략 가능
                    # 만약 get_top_gainers 안에서 호출 안 한다면 아래 주석 해제:
                    # pipeline.selector.save_candidates_to_db(candidates)
                    
                    target_top3 = pipeline.selector.get_best_snipers(candidates, limit=STS_TARGET_COUNT)
                    
                    current_set = set(pipeline.snipers.keys())
                    new_set = set(target_top3)
                    
                    # Detach (60초 보호)
                    to_remove = current_set - new_set
                    for rem in to_remove:
                        attach_time = bot_attach_times.get(rem, 0)
                        alive_time = now - attach_time
                        
                        if alive_time < 60:
                            continue 
                        
                        if rem in pipeline.snipers: 
                            print(f"👋 [Worker] Detach: {rem}", flush=True)
                            del pipeline.snipers[rem]
                            if rem in bot_attach_times: del bot_attach_times[rem]
                    
                    # Attach
                    for add in (new_set - current_set):
                        if add not in pipeline.snipers:
                            print(f"🚀 [Worker] Attach: {add}", flush=True)
                            
                            # [수정] model_bytes 사용 (Engine 업데이트 반영)
                            new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.model_bytes)
                            pipeline.snipers[add] = new_bot
                            bot_attach_times[add] = time.time()
                            
                            # [핵심 수정 3] 웜업을 비동기 태스크로 실행 (스레드 생성 에러 해결)
                            run_warmup_task(new_bot)

                last_manager_run = now

            if now - last_scan_run > 300:
                pipeline.selector.garbage_collect()
                last_scan_run = now
            
            # Redis 데이터가 없어서 빨리 돌 때 CPU 과부하 방지
            if not pop_result:
                await asyncio.sleep(0.01)

        except Exception as e:
            print(f"❌ [Worker Error] {e}", flush=True)
            # 에러가 나면 잠시 대기
            await asyncio.sleep(1)

if __name__ == "__main__":
    # 윈도우 호환성
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        # 비동기 루프 시작
        asyncio.run(redis_consumer())
    except KeyboardInterrupt:
        print("🛑 [Worker] Stopped by user.")