# ingester.py
import asyncio
import websockets
import redis.asyncio as redis
import os
import json

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')
WS_URI = "wss://socket.polygon.io/stocks"

r = redis.from_url(REDIS_URL)

async def producer():
    while True:
        try:
            print("🔌 [Ingester] Connecting to Polygon...", flush=True)
            async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                _ = await ws.recv()
                await ws.send(json.dumps({"action": "subscribe", "params": "A.*,T.*,Q.*"}))
                print(f"📡 [Ingester] Subscribed.", flush=True)

                while True:
                    msg = await ws.recv()
                    # 데이터를 가공하지 않고 그대로 넘김 (동일 입력 보장)
                    await r.lpush('ticker_stream', msg)
                    # 메모리 보호 (5000개 제한)
                    await r.ltrim('ticker_stream', 0, 5000)
        except Exception as e:
            print(f"❌ [Ingester] Error: {e}. Reconnecting...", flush=True)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(producer())