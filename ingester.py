import asyncio
import websockets
import redis.asyncio as redis
import os
import json

# --- 설정 ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
WS_URI = "wss://socket.polygon.io/stocks"

# Redis 연결
r = redis.from_url(REDIS_URL)

async def producer():
    # 현재 구독 중인 종목들을 기억하는 집합 (메모리)
    current_subs = set()

    while True:
        try:
            print("🔌 [Ingester] Polygon 접속 시도 중...", flush=True)
            
            async with websockets.connect(
                WS_URI, 
                ping_interval=None,
                max_queue=None,
                close_timeout=10
            ) as ws:
                # 1. 인증
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                _ = await ws.recv()
                print("🔑 [Ingester] 인증 성공", flush=True)

                # 2. 기본 스캐너 데이터(A.*)는 무조건 구독
                # T.*(전체 체결)도 너무 많으면 빼는 게 좋지만, 일단 둡니다.
                # (호가 Q.*는 절대 전체 구독하지 않음!)
                base_params = "A.*" 
                await ws.send(json.dumps({"action": "subscribe", "params": base_params}))
                print(f"📡 [Ingester] 기본 구독 완료 ({base_params})", flush=True)

                # 3. 데이터 수신 및 동적 구독 관리 루프
                # (수신과 구독 관리를 동시에 하기 위해 asyncio.gather 대신 루프 내 처리)
                last_check_time = 0
                
                while True:
                    # [A] 데이터 수신 (타임아웃을 줘서 주기적으로 구독 관리 로직이 돌게 함)
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        await r.lpush('ticker_stream', msg)
                        
                        # Redis 청소 (가끔씩)
                        if hash(msg) % 1000 == 0:
                            await r.ltrim('ticker_stream', 0, 5000)
                            
                    except asyncio.TimeoutError:
                        # 데이터가 안 들어와도 루프는 돕니다 (구독 관리 위해)
                        pass

                    # [B] 동적 구독 관리 (Smart Subscription) - 0.5초마다 실행
                    now = asyncio.get_running_loop().time()
                    if now - last_check_time > 1.0: # 1초 주기로 체크
                        
                        # 1. Redis에서 현재 Worker가 보고 있는 Top 3 종목 가져오기
                        # (Worker가 'focused_tickers'라는 Set에 종목심볼을 넣어줘야 함)
                        targets = await r.smembers('focused_tickers')
                        desired_targets = {t.decode('utf-8') for t in targets}
                        
                        # 2. 변경사항 확인
                        to_add = desired_targets - current_subs
                        to_remove = current_subs - desired_targets
                        
                        # 3. 구독 추가 (Q.종목, T.종목)
                        if to_add:
                            params = []
                            for t in to_add:
                                params.append(f"Q.{t}") # 호가 (가장 중요)
                                params.append(f"T.{t}") # 체결 (정밀 분석용)
                            
                            req = {"action": "subscribe", "params": ",".join(params)}
                            await ws.send(json.dumps(req))
                            print(f"➕ [Smart Sub] 구독 추가: {to_add}", flush=True)
                            current_subs.update(to_add)

                        # 4. 구독 해제 (데이터 낭비 방지)
                        if to_remove:
                            params = []
                            for t in to_remove:
                                params.append(f"Q.{t}")
                                params.append(f"T.{t}")
                            
                            req = {"action": "unsubscribe", "params": ",".join(params)}
                            await ws.send(json.dumps(req))
                            print(f"➖ [Smart Sub] 구독 해제: {to_remove}", flush=True)
                            current_subs.difference_update(to_remove)
                            
                        last_check_time = now

        except Exception as e:
            print(f"❌ [Ingester] 오류: {e}. 3초 후 재접속...", flush=True)
            current_subs.clear() # 재접속 시 구독 정보 초기화
            await asyncio.sleep(3)

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(producer())
    except KeyboardInterrupt:
        print("🛑 종료")