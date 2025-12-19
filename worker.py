# [worker.py] 최종 수정본 (Hybrid Mode + Data-only FCM + Async Scan)

import redis
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
    # STS_Engine에서 필요한 클래스 및 함수 임포트
    from STS_Engine import (
        STSPipeline, 
        STS_TARGET_COUNT, 
        SniperBot, 
        DB_WORKER_POOL, 
        init_db,             
        get_db_connection    
    )
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다. 경로를 확인하세요.")
    sys.exit(1)

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

if not POLYGON_API_KEY:
    print("⚠️ [Warning] 'POLYGON_API_KEY'가 없습니다! 데이터 복구 기능이 작동하지 않습니다.", flush=True)

r = redis.from_url(REDIS_URL)
REDIS_POOL = ThreadPoolExecutor(max_workers=2)

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

# [알림 처리] 데이터 전용 메시지 발송 함수
def process_fcm_job():
    try:
        packed_data = r.rpop('fcm_queue')
        if not packed_data: return 

        task = json.loads(packed_data)
        ticker = task['ticker']
        score = task['score']
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        conn.close() 

        if not subscribers: return

        if task.get('entry') and task.get('tp'):
            title = f"BUY {ticker} (Score: {score})"
            body = f"Entry: ${task['entry']} / TP: ${task['tp']}"
        else:
            title = f"SCAN {ticker} (Score: {score})"
            body = f"Current: ${task['price']}"

        # 🔥 [핵심] notification 없음, data에 모든 정보 포함
        data_payload = {
            'title': title,   
            'body': body,     
            'ticker': str(ticker),
            'price': str(task['price']), 
            'score': str(score),
            'click_action': '/'
        }

        print(f"📨 [Worker] Sending Data-only FCM: {title}", flush=True)

        success = 0
        failed_tokens = []
        
        if not firebase_admin._apps: init_firebase_worker()

        for row in subscribers:
            token = row[0]
            user_min = row[1] if (len(row) > 1 and row[1] is not None) else 0
            
            try:
                if float(score) < user_min: continue
            except: pass

            try:
                msg = messaging.Message(
                    token=token,
                    data=data_payload # Only Data!
                )
                messaging.send(msg)
                success += 1
            except Exception as e:
                if "registration-token-not-registered" in str(e) or "not-found" in str(e): 
                    failed_tokens.append(token)

        if failed_tokens:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            conn.close()

    except Exception as e:
        print(f"❌ [Worker FCM Error] {e}", flush=True)

async def fcm_consumer_loop():
    print("📨 [FCM Worker] Started independent notification loop", flush=True)
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(REDIS_POOL, process_fcm_job)
            await asyncio.sleep(0.1) 
        except Exception as e:
            print(f"❌ [FCM Loop Error] {e}", flush=True)
            await asyncio.sleep(1)

async def send_test_notification():
    """앱 켜지면 무조건 알림 하나 보내서 테스트"""
    print("🔔 [Test] Sending startup notification...", flush=True)
    try:
        payload = {
            'ticker': "TEST-BOT",
            'price': "123.45",
            'score': "99",
            'entry': "120.00",
            'tp': "130.00"
        }
        await r.lpush('fcm_queue', json.dumps(payload))
    except Exception as e:
        print(f"❌ [Test] Failed: {e}")

# 🔥 [핵심 추가] 스캐너 루프 (별도 태스크로 분리)
# 여기서 2초마다 API를 때리고(refresh_market_snapshot), 종목을 고릅니다.
async def task_global_scan(pipeline, bot_attach_times):
    print("🔭 [Scanner] Started (Hybrid Mode: 2s Interval)", flush=True)
    loop = asyncio.get_running_loop()
    
    while True:
        try:
            # 1. [API Polling] 데이터 강제 갱신
            await loop.run_in_executor(
                DB_WORKER_POOL, 
                pipeline.selector.refresh_market_snapshot # 👈 2초마다 호출됨
            )

            # 2. [Scanning] 후보군 선별
            candidates = await loop.run_in_executor(
                DB_WORKER_POOL,
                partial(pipeline.selector.get_top_gainers_candidates, limit=10)
            )
            
            # 3. [Management] 봇 붙이기/떼기
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
                        r.srem('focused_tickers', rem)
                
                # Attach
                for add in (new_set - current_set):
                    if add not in pipeline.snipers:
                        print(f"🚀 [Worker] Attach: {add}", flush=True)
                        new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.model_bytes)
                        pipeline.snipers[add] = new_bot
                        bot_attach_times[add] = now
                        run_warmup_task(new_bot)
                        r.sadd('focused_tickers', add)

            # 4. [Cleanup]
            pipeline.selector.garbage_collect()
            
            # 5. [Wait] 2초 대기 (유료 플랜이라 2초도 널널함)
            await asyncio.sleep(2)

        except Exception as e:
            print(f"⚠️ Scanner Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)

# 메인 루프 (이제는 시세 처리만 담당)
async def redis_consumer():
    print("🧠 [Worker] Starting Logic Engine (Async Redis Mode)...", flush=True)
    
    init_db()
    init_firebase_worker()
    await send_test_notification()

    print("⏳ [System] Initializing Pipeline...", flush=True)
    pipeline = STSPipeline()
    
    # 로컬 데이터 저장소
    last_agg = {}
    last_quotes = {}
    bot_attach_times = {}

    print("🧠 [Worker] Ready. Listening to 'ticker_stream' & 'fcm_queue'...", flush=True)
    
    # 🔥 태스크 분리 실행
    asyncio.create_task(fcm_consumer_loop())
    asyncio.create_task(task_global_scan(pipeline, bot_attach_times)) # 스캐너 별도 실행

    loop = asyncio.get_running_loop()

    while True:
        try:
            # 시세 데이터 처리 (WebSocket에서 넘어온 데이터)
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