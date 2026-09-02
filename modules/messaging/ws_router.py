import asyncio

import jwt
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.websocket import manager

logger = structlog.get_logger()

router = APIRouter(tags=["websocket"])


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = None,
):
    from core.security import decode_token

    if not token:
        logger.warning("WS rejected: No token provided")
        await websocket.close(code=4003)
        return

    try:
        payload = decode_token(token)
        user_id = payload.sub
        if not user_id:
            logger.warning("WS rejected: Token missing subject")
            await websocket.close(code=4003)
            return
        logger.info("WS token validated", user_id=user_id)
    except jwt.PyJWTError as e:
        logger.warning("WS rejected: Token validation failed", error=str(e))
        await websocket.close(code=4003)
        return

    await websocket.accept()
    await manager.connect(user_id, websocket)

    from core.redis import get_redis_client

    async def keep_online():
        try:
            async with get_redis_client() as client:
                key = f"user:online:{user_id}"
                while True:
                    await client.set(key, "1", ex=30)
                    await asyncio.sleep(20)
        except asyncio.CancelledError:
            try:
                async with get_redis_client() as client:
                    await client.delete(f"user:online:{user_id}")
            except Exception:
                pass
        except Exception as e:
            logger.error(
                "Error keeping user online status", error=str(e), user_id=user_id
            )

    online_task = asyncio.create_task(keep_online())

    try:
        while True:
            # Just keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect as error:
        logger.info(
            "WS disconnected",
            user_id=user_id,
            code=error.code,
            reason=error.reason or "client disconnected",
        )
    except Exception as error:
        logger.warning(
            "WS connection lost",
            user_id=user_id,
            error_type=type(error).__name__,
        )
    finally:
        online_task.cancel()
        manager.disconnect(user_id, websocket)
        try:
            async with get_redis_client() as client:
                await client.delete(f"user:online:{user_id}")
        except Exception:
            pass
