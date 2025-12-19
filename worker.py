# [worker.py] 최종 수정본 (Async Redis Fix + Hybrid Logic)

import redis.asyncio as redis  # 비동기 Redis 라이브러리
import json
import os
import time
import sys
import asyncio 
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import firebase_admin
from firebase_admin import credentials, messaging

try:
    from STS_Engine import (
        STSPipeline, 
        STS_TARGET_COUNT, 
        SniperBot, 
        DB_WORKER_POOL, 
        init_db,             
        get_db_connection    
    )
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다.", flush=True)
    sys.exit(1)

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

if not POLYGON_API_KEY:
    print("⚠️ [Warning] 'POLYGON_API_KEY' Missing!", flush=True)

# 비동기 Redis 클라이언트 생성
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

def run_warmup_task(bot):
    try:
        asyncio.create_task(bot.warmup())
    except Exception as e:
        print(f"⚠️ [Warmup Start Error] {e}")

# [알림 처리] 비동기 함수로 변경 (Redis await 사용 위함)
async def process_fcm_job():
    try:
        # 1. [수정] 비동기 Redis 사용 (await 필수)
        packed_data = await r.rpop('fcm_queue')
        if not packed_data: return 

        task = json.loads(packed_data)
        ticker = task['ticker']
        score = task['score']
        
        # 2. DB 작업은 동기식이므로 별도 스레드에서 실행 (스캐너 멈춤 방지)
        loop = asyncio.get_running_loop()
        
        # DB 읽기 헬퍼 함수
        def fetch_subscribers():
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT token, min_score FROM fcm_tokens")
                subs = cursor.fetchall()
                cursor.close()
                return subs
            finally:
                pass # 커넥션 풀 사용 중이므로 닫지 않음

        subscribers = await loop.run_in_executor(DB_WORKER_POOL, fetch_subscribers)

        if not subscribers: return

        if task.get('entry') and task.get('tp'):
            title = f"BUY {ticker} (Score: {score})"
            body = f"Entry: ${task['entry']} / TP: ${task['tp']}"
        else:
            title = f"SCAN {ticker} (Score: {score})"
            body = f"Current: ${task['price']}"

        # [유지] Data-only Payload (New content available 방지)
        data_payload = {
            'title': title,   
            'body': body,     
            'ticker': str(ticker),
            'price': str(task['price']), 
            'score': str(score),
            'click_action': '/'
        }

        print(f"📨 [Worker] Sending Data-only FCM: {title}", flush=True)

        init_firebase_worker()
        
        success = 0
        failed_tokens = []

        for row in subscribers:
            token = row[0]
            try:
                user_min = int(row[1]) if row[1] is not None else 0
                if float(score) < user_min: continue
            except: pass

            try:
                # notification 없이 data만 보냄
                msg = messaging.Message(token=token, data=data_payload)
                messaging.send(msg)
                success += 1
            except Exception as e:
                if "registration-token-not-registered" in str(e) or "not-found" in str(e): 
                    failed_tokens.append(token)

        # 토큰 청소 (비동기 래핑)
        if failed_tokens:
            def clean_tokens(tokens):
                conn = get_db_connection()
                try:
                    c = conn.cursor()
                    c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (tokens,))
                    conn.commit()
                    c.close()
                finally:
                    pass
            await loop.run_in_executor(DB_WORKER_POOL, partial(clean_tokens, failed_tokens))

    except Exception as e:
        print(f"❌ [Worker FCM Error] {e}", flush=True)

async def fcm_consumer_loop():
    print("📨 [FCM Worker] Started independent notification loop", flush=True)
    while True:
        try:
            # [수정] 직접 await 호출 (async 함수이므로 executor 불필요)
            await process_fcm_job()
            await asyncio.sleep(0.1) 
        except Exception as e:
            print(f"❌ [FCM Loop Error] {e}", flush=True)
            await asyncio.sleep(1)

async def send_test_notification():
    print("🔔 [Test] Sending startup notification...", flush=True)
    try:
        payload = {
            'ticker': "TEST-BOT",
            'price': "123.45",
            'score': "99",
            'entry': "120.00",
            'tp': "130.00"
        }
        # [수정] await r.lpush 사용 (비동기)
        await r.lpush('fcm_queue', json.dumps(payload))
    except Exception as e:
        print(f"❌ [Test] Failed: {e}", flush=True)

# 스캐너 태스크 (기존 로직 유지)
async def task_global_scan(pipeline, bot_attach_times):
    print("🔭 [Scanner] Started (Hybrid Mode: 2s Interval)", flush=True)
    loop = asyncio.get_running_loop()
    
    while True:
        try:
            # API Polling
            await loop.run_in_executor(DB_WORKER_POOL, pipeline.selector.refresh_market_snapshot)

            # Scanning
            candidates = await loop.run_in_executor(
                DB_WORKER_POOL,
                partial(pipeline.selector.get_top_gainers_candidates, limit=10)
            )
            
            if candidates:
                target_top3 = pipeline.selector.get_best_snipers(candidates, limit=STS_TARGET_COUNT)
                current_set = set(pipeline.snipers.keys())
                new_set = set(target_top3)
                
                # Detach
                to_remove = current_set - new_set
                now = time.time()
                for rem in to_remove:
                    attach_time = bot_attach_times.get(rem, 0)
                    if now - attach_time < 60: continue 
                    
                    if rem in pipeline.snipers: 
                        print(f"👋 [Worker] Detach: {rem}", flush=True)
                        del pipeline.snipers[rem]
                        if rem in bot_attach_times: del bot_attach_times[rem]
                        await r.srem('focused_tickers', rem) # Async Redis
                
                # Attach
                for add in (new_set - current_set):
                    if add not in pipeline.snipers:
                        print(f"🚀 [Worker] Attach: {add}", flush=True)
                        new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.model_bytes)
                        pipeline.snipers[add] = new_bot
                        bot_attach_times[add] = now
                        run_warmup_task(new_bot)
                        await r.sadd('focused_tickers', add) # Async Redis

            pipeline.selector.garbage_collect()
            await asyncio.sleep(2)

        except Exception as e:
            print(f"⚠️ Scanner Error: {e}", flush=True)
            await asyncio.sleep(5)

# 메인 루프
async def redis_consumer():
    print("🧠 [Worker] Starting Logic Engine (Async Redis Mode)...", flush=True)
    
    init_db()
    init_firebase_worker()
    await send_test_notification()

    print("⏳ [System] Initializing Pipeline...", flush=True)
    pipeline = STSPipeline()
    
    last_agg = {}
    last_quotes = {}
    bot_attach_times = {}

    print("🧠 [Worker] Ready. Listening to 'ticker_stream' & 'fcm_queue'...", flush=True)
    
    # 두 개의 태스크 병렬 실행
    asyncio.create_task(fcm_consumer_loop())
    asyncio.create_task(task_global_scan(pipeline, bot_attach_times))

    # 메인 시세 처리 루프
    while True:
        try:
            # [수정] await r.brpop 직접 호출 (비동기이므로 executor 불필요)
            pop_result = await r.brpop('ticker_stream', timeout=1)
            
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
            
            if not pop_result:
                await asyncio.sleep(0.01)

        except Exception as e:
            print(f"❌ [Worker Error] {e}", flush=True)
            await asyncio.sleep(1)

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(redis_consumer())
    except KeyboardInterrupt:
        print("🛑 [Worker] Stopped by user.")