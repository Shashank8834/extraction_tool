import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import Base, engine
from .routers import admin, public

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Create tables on startup (simple MVP; swap for Alembic migrations later).
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Client Intake & Extraction Tool")

# Content Security Policy: only same-origin assets, no framing, images may use
# data: URIs. Blocks injected inline/remote scripts if OCR text ever slips past
# template escaping.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.base_url.startswith("https"),
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

app.include_router(public.router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def root():
    return RedirectResponse("/admin")
