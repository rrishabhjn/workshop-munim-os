from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routers import cron, dashboard, domains, telegram

settings = get_settings()

app = FastAPI(
    title="Workshop Munim OS API",
    version="0.2.0",
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.app_base_url, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.get("/api/public-config")
async def public_config() -> dict[str, str]:
    """Return only public Supabase client config so browser OAuth works even if build args are omitted."""
    s = get_settings()
    return {
        "supabase_url": s.supabase_url,
        "supabase_publishable_key": s.supabase_publishable_key,
    }


app.include_router(telegram.router)
app.include_router(domains.router)
app.include_router(dashboard.router)
app.include_router(cron.router)

# Serve built React PWA / Telegram Mini App (/production and /munim deep links supported)
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if FRONTEND_DIST.exists():
    @app.get("/")
    @app.get("/production")
    @app.get("/production/{rest_of_path:path}")
    @app.get("/munim")
    @app.get("/munim/{rest_of_path:path}")
    async def spa_domain_routes(rest_of_path: str = "") -> FileResponse:
        _ = rest_of_path
        return FileResponse(
            str(FRONTEND_DIST / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
