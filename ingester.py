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
    while True:
        try:
            print("🔌 [Ingester] Polygon 접속 시도 중...", flush=True)
            
            # 🟢 [수정 1] 핑(Ping) 비활성화 (데이터 폭주시 1011 에러 방지)
            async with websockets.connect(
                WS_URI, 
                ping_interval=None,   # 클라이언트가 핑을 안 보냄
                ping_timeout=None,    # 퐁 응답을 기다리지 않음
                max_queue=None,       # 수신 버퍼 무제한
                close_timeout=10
            ) as ws:
                # 인증
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                auth_response = await ws.recv()
                print(f"🔑 [Ingester] 인증 결과: {auth_response}", flush=True)

                # 🟢 [수정 2] A.*(초봉), T.*(체결)만 구독
                # Q.*(호가)는 데이터가 너무 많아서 인제스터를 터트리므로 제외했습니다.
                await ws.send(json.dumps({"action": "subscribe", "params": "A.*,T.*"}))
                print(f"📡 [Ingester] 구독 완료 (A.*, T.*)", flush=True)

                counter = 0
                while True:
                    msg = await ws.recv()
                    
                    # Redis로 메시지 쏘기
                    await r.lpush('ticker_stream', msg)
                    
                    # 🟢 [수정 3] Redis 청소 최적화 (1000번에 한 번만 실행)
                    # 매번 ltrim을 하면 Redis가 힘들어합니다.
                    counter += 1
                    if counter >= 1000:
                        await r.ltrim('ticker_stream', 0, 5000)
                        counter = 0
                        
        except Exception as e:
            print(f"❌ [Ingester] 오류 발생: {e}. 5초 후 재접속...", flush=True)
            await asyncio.sleep(5)

if __name__ == "__main__":
    # 윈도우 호환성 설정
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(producer())
    except KeyboardInterrupt:
        print("🛑 [Ingester] 사용자에 의해 종료됨.")