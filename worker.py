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
from firebase_admin import credentials, messaging # [수정] messaging 모듈 추가

# [필수] DB 설정 가져오기
from app import init_db, get_db_connection # [수정] get_db_connection 추가 (토큰 조회용)

try:
    # 우리가 수정한 STS_Engine에서 필요한 클래스와 변수들 가져오기
    from STS_Engine import STSPipeline, STS_TARGET_COUNT, SniperBot, DB_WORKER_POOL
except ImportError:
    print("❌ [Worker Error] 'STS_Engine.py'를 찾을 수 없습니다. 경로를 확인하세요.")
    sys.exit(1)

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
FIREBASE_ADMIN_SDK_JSON_STR = os.environ.get('FIREBASE_ADMIN_SDK_JSON')
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

# 🔥 [추가] 알림 큐 처리 함수 (정규화된 방식)
def process_fcm_job():
    """
    Redis 'fcm_queue'에서 작업을 꺼내 실제 푸시를 쏘는 함수
    """
    try:
        # 1. 큐에서 하나 꺼내기 (Non-blocking rpop 사용)
        packed_data = r.rpop('fcm_queue')
        
        if not packed_data: return # 할 일 없으면 리턴

        # 2. 데이터 풀기
        task = json.loads(packed_data)
        ticker = task['ticker']
        score = task['score']
        
        # 3. DB에서 토큰 가져오기 (직접 수행)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT token, min_score FROM fcm_tokens")
        subscribers = cursor.fetchall()
        cursor.close()
        conn.close() # 바로 반납

        if not subscribers: return

        # 4. 정규화된 알림 설정 (Android/iOS 표준)
        # [Android] 중요도 높음 + 기본 소리
        android_config = messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(sound='default', click_action='FLUTTER_NOTIFICATION_CLICK')
        )
        # [iOS] 즉시 전송 + 기본 소리
        apns_config = messaging.APNSConfig(
            headers={'apns-priority': '10'},
            payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', content_available=True))
        )

        # 내용 구성
        if task.get('entry') and task.get('tp'):
            title = f"BUY {ticker} (Score: {score})"
            body = f"Entry: ${task['entry']} / TP: ${task['tp']}"
        else:
            title = f"SCAN {ticker} (Score: {score})"
            body = f"Current: ${task['price']}"

        # 데이터 페이로드
        data_payload = {
            'type': 'signal',
            'ticker': ticker,
            'price': str(task['price']), # 문자열 안전 변환
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

        # [여기에 붙여넣기]
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

    print("🧠 [Worker] Ready. Listening to 'ticker_stream' & 'fcm_queue'...", flush=True)
    asyncio.create_task(fcm_consumer_loop())

    # 현재 실행 중인 루프 가져오기
    loop = asyncio.get_running_loop()

    while True:
        try:
            # =========================================================
            # 1. 시세 데이터 처리 (기존 로직)
            # =========================================================
            # [핵심 수정 1] Redis brpop을 별도 스레드에서 실행
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
                # [핵심 수정 2] 무거운 DB 읽기 작업을 스레드 풀로 격리
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
                            
                            # [수정] model_bytes 사용 (Engine 업데이트 반영)
                            new_bot = SniperBot(add, pipeline.logger, pipeline.selector, pipeline.model_bytes)
                            pipeline.snipers[add] = new_bot
                            bot_attach_times[add] = time.time()
                            
                            # [핵심 수정 3] 웜업을 비동기 태스크로 실행 (스레드 생성 에러 해결)
                            run_warmup_task(new_bot)
                            r.sadd('focused_tickers', add)

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