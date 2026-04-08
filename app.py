"""
app.py
──────
FastAPI application entry point.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Install in Stremio:
    http://localhost:8000/{b64_config}/manifest.json
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from router import router
from services.postgresql import PostgreSQLClient
from settings import ADDON_NAME, LOG_LEVEL
from utils.logger import setup_logging

setup_logging(LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await PostgreSQLClient.init_pools()
    yield
    await PostgreSQLClient.close_pools()


app = FastAPI(title=ADDON_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
)

app.include_router(router)

# Serve static files (configure.html etc.)
try:
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
except Exception:
    pass


@app.middleware("http")
async def handle_head(request: Request, call_next):
    # Stremio sends HEAD requests for connectivity probes — return 200 silently
    if request.method == "HEAD":
        return Response(status_code=200)
    return await call_next(request)


@app.exception_handler(Exception)
async def global_error(request: Request, exc: Exception):
    logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse({"streams": []}, status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
