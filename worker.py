# worker.py
import redis
import json
import os
import time
import sys
import asyncio 
import threading
import firebase_admin
from firebase_admin import credentials

from app import init_db

try:
    from STS_Engine import STSPipeline, STS_TARGET_COUNT, SniperBot # SniperBot도 import 필요
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다.")
    sys.exit(1)

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
r = redis.from_url(REDIS_URL)

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

# 웜업을 비동기로 실행하기 위한 헬퍼 함수
def run_warmup_in_background(bot):
    try:
        asyncio.run(bot.warmup())
    except Exception as e:
        print(f"⚠️ [Warmup Error] {e}")

def consumer():
    print("🧠 [Worker] Starting Logic Engine...", flush=True)
    
    init_db()
    init_firebase_worker()
    
    pipeline = STSPipeline()
    
    last_agg = {}
    last_quotes = {}
    last_manager_run = time.time()
    last_scan_run = time.time()
    
    # 🔥 [추가 1] 입사 시간 기록부
    bot_attach_times = {}

    print("🧠 [Worker] Ready. Waiting for Redis stream...", flush=True)

    while True:
        try:
            pop_result = r.brpop('ticker_stream', timeout=1)
            
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
            # Manager 로직 (여기가 핵심입니다!)
            # =========================================================
            now = time.time()

            if now - last_manager_run > 5.0:
                candidates = pipeline.selector.get_top_gainers_candidates(limit=10)
                
                if candidates:
                    pipeline.selector.save_candidates_to_db(candidates) # 4개 인자 해결된 버전 사용 중 가정
                    
                    target_top3 = pipeline.selector.get_best_snipers(candidates, limit=STS_TARGET_COUNT)
                    
                    current_set = set(pipeline.snipers.keys())
                    new_set = set(target_top3)
                    
                    # 🔥 [수정 2] Detach (60초 보호 로직 적용)
                    to_remove = current_set - new_set
                    for rem in to_remove:
                        # 입사한 지 얼마나 됐나 확인
                        attach_time = bot_attach_times.get(rem, 0)
                        alive_time = now - attach_time
                        
                        if alive_time < 60:
                            # 60초 미만이면 자르지 않고 봐줍니다 (continue)
                            # print(f"🛡️ [Protect] {rem} ({int(alive_time)}s). Keeping...", flush=True)
                            continue 
                        
                        # 60초 지났으면 진짜 삭제
                        if rem in pipeline.snipers: 
                            print(f"👋 [Worker] Detach: {rem}", flush=True)
                            del pipeline.snipers[rem]
                            if rem in bot_attach_times: del bot_attach_times[rem]
                    
                    # 🔥 [수정 3] Attach (입사 시간 기록 + 웜업)
                    for add in (new_set - current_set):
                        # 보호받는 봇 때문에 3개가 넘어가도, 신규 1등은 무조건 영입
                        if add not in pipeline.snipers:
                            print(f"🚀 [Worker] Attach: {add}", flush=True)
                            
                            new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.shared_model)
                            pipeline.snipers[add] = new_bot
                            
                            # 시간 기록
                            bot_attach_times[add] = time.time()
                            
                            # 웜업 실행 (쓰레드로 던져서 메인 로직 방해 안 되게 함)
                            threading.Thread(target=run_warmup_in_background, args=(new_bot,)).start()

                last_manager_run = now

            if now - last_scan_run > 300:
                pipeline.selector.garbage_collect()
                last_scan_run = now

        except Exception as e:
            print(f"❌ [Worker Error] {e}", flush=True)
            time.sleep(1)

if __name__ == "__main__":
    consumer()