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
from firebase_admin import credentials, messaging

try:
    # ✅ [수정] STS_Engine에서 DB 관련 함수(init_db, get_db_connection)까지 모두 가져옵니다.
    from STS_Engine import (
        STSPipeline, 
        STS_TARGET_COUNT, 
        SniperBot, 
        DB_WORKER_POOL, 
        init_db,             # 추가됨
        get_db_connection    # 추가됨
    )
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다. 경로를 확인하세요.")
    sys.exit(1)

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
# 🔥 [추가] Cold Start 방지용 API Key 안전장치
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
if not POLYGON_API_KEY:
    print("⚠️ [Warning] 'POLYGON_API_KEY'가 없습니다! 재시작 시 데이터 복구(Snapshot) 기능이 작동하지 않습니다.", flush=True)
r = redis.from_url(REDIS_URL)

# [수정] Redis 블로킹 방지를 위한 스레드 풀 (시세 처리 + 알림 발송 = 최소 2개 필요)
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

# 웜업을 안전한 비동기 태스크로 실행하는 헬퍼 (기존 코드 유지)
def run_warmup_task(bot):
    try:
        # threading.Thread 대신 asyncio.create_task 사용 (충돌 해결 핵심)
        asyncio.create_task(bot.warmup())
    except Exception as e:
        print(f"⚠️ [Warmup Start Error] {e}")

def process_fcm_job():
    """
    Redis 'fcm_queue'에서 작업을 꺼내 실제 푸시를 쏘는 함수 (수정됨)
    """
    try:
        # 1. 큐에서 하나 꺼내기
        packed_data = r.rpop('fcm_queue')
        if not packed_data: return 

        # 2. 데이터 풀기
        task = json.loads(packed_data)
        ticker = task['ticker']
        score = task['score']
        
        # 3. DB에서 토큰 가져오기
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        conn.close() 

        if not subscribers: return

        # 🔥 [수정 1] 제목(title)과 내용(body)을 먼저 정의합니다! (순서 변경)
        if task.get('entry') and task.get('tp'):
            title = f"BUY {ticker} (Score: {score})"
            body = f"Entry: ${task['entry']} / TP: ${task['tp']}"
        else:
            title = f"SCAN {ticker} (Score: {score})"
            body = f"Current: ${task['price']}"

        # 4. 정규화된 알림 설정 (Android/iOS 표준)
        
        # 🔥 [수정 2] Android 설정에 제목과 내용을 직접 넣습니다.
        android_config = messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(
                title=title,    # 👈 갤럭시 필독 사항
                body=body,      # 👈 갤럭시 필독 사항
                sound='default', 
                click_action='FLUTTER_NOTIFICATION_CLICK'
            )
        )
        
        # 🔥 [수정 3] iOS 설정에도 제목과 내용을 넣습니다.
        apns_config = messaging.APNSConfig(
            headers={'apns-priority': '10'},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title, body=body), # 👈 아이폰 필독 사항
                    sound='default', 
                    content_available=True
                )
            )
        )

        # 데이터 페이로드
        data_payload = {
            'type': 'signal',
            'ticker': ticker,
            'price': str(task['price']), 
            'score': str(score),
            'click_action': 'FLUTTER_NOTIFICATION_CLICK'
        }

        print(f"📨 [Worker] Sending FCM: {title}", flush=True)

        # 5. 발송 루프
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
                    notification=messaging.Notification(title=title, body=body),
                    data=data_payload,
                    android=android_config,
                    apns=apns_config
                )
                messaging.send(msg)
                success += 1
            except Exception as e:
                if "registration-token-not-registered" in str(e): failed_tokens.append(token)

        # 토큰 청소
        if failed_tokens:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM fcm_tokens WHERE token = ANY(%s)", (failed_tokens,))
            conn.commit()
            conn.close()

    except Exception as e:
        print(f"❌ [Worker FCM Error] {e}", flush=True)

# 🔥 알림만 전담하는 독립적인 비동기 루프 (새로 추가됨)
async def fcm_consumer_loop():
    print("📨 [FCM Worker] Started independent notification loop", flush=True)
    loop = asyncio.get_running_loop()
    while True:
        try:
            # 0.1초마다 큐 확인 (메인 시세 처리와 상관없이 독립적으로 실행됨)
            await loop.run_in_executor(REDIS_POOL, process_fcm_job)
            await asyncio.sleep(0.1) 
        except Exception as e:
            print(f"❌ [FCM Loop Error] {e}", flush=True)
            await asyncio.sleep(1)


# 메인 루프를 비동기 함수로 변경
async def redis_consumer():
    print("🧠 [Worker] Starting Logic Engine (Async Redis Mode)...", flush=True)
    
    # DB 및 Firebase 초기화
    # (반드시 STS_Engine에서 가져온 init_db여야 함)
    init_db()
    init_firebase_worker()

    print("⏳ [System] Initializing Pipeline...", flush=True)
    
    # 파이프라인 생성 (여기서 TargetSelector가 스냅샷 로딩 시도)
    pipeline = STSPipeline()
    
    # 🔥 [수정] 스냅샷이 진짜로 로드됐는지 확인하는 로직 추가
    snapshot_count = len(pipeline.selector.snapshots)
    if snapshot_count > 0:
        print(f"✅ [System] Snapshot Loaded Successfully! ({snapshot_count} tickers ready)", flush=True)
    else:
        print("⚠️ [Warning] Snapshot is EMPTY! (Cold Start)", flush=True)
        print("   -> 장중 데이터가 쌓일 때까지 봇이 종목을 잘 못 잡을 수 있습니다.", flush=True)
    
    # 로컬 데이터 저장소
    last_agg = {}
    last_quotes = {}
    
    # 타이머
    last_manager_run = time.time()
    last_scan_run = time.time()
    
    # 입사 시간 기록부
    bot_attach_times = {}

    print("🧠 [Worker] Ready. Listening to 'ticker_stream' & 'fcm_queue'...", flush=True)
    asyncio.create_task(fcm_consumer_loop())

    # 현재 실행 중인 루프 가져오기
    loop = asyncio.get_running_loop()

    while True:
        try:
            # =========================================================
            # 1. 시세 데이터 처리
            # =========================================================
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

            # =========================================================
            # 3. Manager 로직 (종목 관리)
            # =========================================================
            now = time.time()

            if now - last_manager_run > 5.0:
                candidates = await loop.run_in_executor(
                    DB_WORKER_POOL,
                    partial(pipeline.selector.get_top_gainers_candidates, limit=10)
                )
                
                if candidates:
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
                            r.srem('focused_tickers', rem)
                    
                    # Attach
                    for add in (new_set - current_set):
                        if add not in pipeline.snipers:
                            print(f"🚀 [Worker] Attach: {add}", flush=True)
                            
                            new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.model_bytes)
                            pipeline.snipers[add] = new_bot
                            bot_attach_times[add] = time.time()
                            
                            run_warmup_task(new_bot)
                            r.sadd('focused_tickers', add)

                last_manager_run = now

            if now - last_scan_run > 300:
                pipeline.selector.garbage_collect()
                last_scan_run = now
            
            if not pop_result:
                await asyncio.sleep(0.01)

        except Exception as e:
            print(f"❌ [Worker Error] {e}", flush=True)
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