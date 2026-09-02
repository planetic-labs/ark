import logging
import os

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_session
from core.exceptions import AppError
from core.lifespan import lifespan
from core.middleware import RateLimitMiddleware
from modules.auth.router import router as auth_router
from modules.messaging.router import router as messaging_router
from modules.messaging.ws_router import router as ws_router
from modules.users.roles_router import router as roles_router
from modules.users.router import router as users_router
from modules.users.services_router import router as services_router

# Setup logging
logger = structlog.get_logger()
# Ping timeouts are normal disconnects for mobile and browser clients. The
# WebSocket route logs them as one structured event instead of a library trace.
logging.getLogger("websockets").setLevel(logging.CRITICAL)

app = FastAPI(
    title="Ark Messenger API",
    version="2026.5.23",
    description="Corporate messenger backend for closed communities",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# Create static/uploads directory and mount it
os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Global error", error=str(exc), path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


app.include_router(auth_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")
app.include_router(roles_router, prefix="/api/v1")
app.include_router(services_router, prefix="/api/v1")
app.include_router(messaging_router, prefix="/api/v1")
app.include_router(ws_router)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


app.add_middleware(RateLimitMiddleware)


@app.get("/")
async def root():
    return {"message": "Welcome to Ark Messenger API", "version": "2026.5.23"}


@app.get("/health")
async def health_check(
    session: AsyncSession = Depends(get_session),
):
    import sqlalchemy as sa

    from core.redis import get_redis_client

    checks = {"db": "ok", "redis": "ok"}
    try:
        await session.execute(sa.text("SELECT 1"))
    except Exception:
        checks["db"] = "error"
    try:
        async with get_redis_client() as redis:
            await redis.ping()
    except Exception:
        checks["redis"] = "error"
    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", **checks}


@app.get("/.well-known/jwks.json")
async def jwks():
    from core.security import get_jwks

    return get_jwks()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
