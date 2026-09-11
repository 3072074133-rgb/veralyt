from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import PROJECT_ROOT, settings
from .observability import bind_log_context, configure_logging, duration_ms, log_event
from .repository import repository
from .worker import worker


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    log_event(logger, "application.starting")
    repository.initialize()
    await worker.start()
    yield
    await worker.stop()
    log_event(logger, "application.stopped")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started_at = time.perf_counter()
    with bind_log_context(request_id=request_id):
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed",
                extra={"event_fields": {"event": "request.failed", "method": request.method, "path": request.url.path, "duration_ms": duration_ms(started_at)}},
            )
            raise
        response.headers["X-Request-ID"] = request_id
        log_event(
            logger,
            "request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms(started_at),
        )
        return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", f"http://127.0.0.1:{settings.port}"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "model": settings.ollama_model,
        "embedding_model": settings.ollama_embedding_model,
    }


web_dist = PROJECT_ROOT / "web" / "dist"
if web_dist.exists():
    assets = web_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        requested = (web_dist / path).resolve()
        if web_dist.resolve() in requested.parents and requested.is_file():
            return FileResponse(requested)
        return FileResponse(web_dist / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "docs": "/docs", "message": "前端尚未构建"}
