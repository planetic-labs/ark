import asyncio
import json

import structlog
from fastapi import WebSocket

from core.redis import get_redis_client

logger = structlog.get_logger()


class ConnectionManager:
    def __init__(self):
        # active_connections: { user_id: [WebSocket, ...] }
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        logger.info(
            "WS Connected", user_id=user_id, count=len(self.active_connections[user_id])
        )

    def disconnect(self, user_id: str, websocket: WebSocket):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            data = json.dumps(message)
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(data)
                except Exception:
                    pass


manager = ConnectionManager()


MAX_RECONNECT_DELAY = 60
INITIAL_RECONNECT_DELAY = 1


async def redis_broadcast_reader() -> None:
    """Listen to Redis and broadcast to local connected users with reconnect logic"""
    delay = INITIAL_RECONNECT_DELAY
    while True:
        try:
            redis = get_redis_client()
            pubsub = redis.pubsub()
            await pubsub.subscribe("chat_events")
            logger.info("Redis broadcast reader connected")
            delay = INITIAL_RECONNECT_DELAY

            async for message in pubsub.listen():
                if message["type"] == "message":
                    event = json.loads(message["data"])
                    target_users = event.get("target_user_ids", [])
                    for user_id in target_users:
                        await manager.send_personal_message(event["payload"], user_id)

        except asyncio.CancelledError:
            logger.info("Redis broadcast reader task cancelled")
            break
        except Exception as error:
            logger.error(
                "Redis listener failed, reconnecting", error=str(error), delay=delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)
