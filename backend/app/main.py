from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.route import router as navigation_router
from app.api.mission import router as mission_router
from app.api.science import router as science_router


# ============================================================
# FastAPI Application Setup
# ============================================================

app = FastAPI(
    title="MarsWalk Intelligence",
    description="NASA-powered Mars exploration and route planning platform.",
    version="0.1.0"
)


# ============================================================
# Static Mars Map Tiles
# ============================================================

class CachedStaticFiles(StaticFiles):
    """StaticFiles + an explicit Cache-Control header.

    Tiles never change unless the processed data is regenerated, so the
    browser should not re-ask the server (through the tunnel) for each
    one on every page load.
    """

    def __init__(self, *args, cache_control: str = "public, max-age=86400", **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = self._cache_control
        return response


PROJECT_ROOT = Path(__file__).resolve().parents[2]

TILES_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "jezero"
    / "tiles"
    / "ctx_ortho"
)

# Ensure tiles directory exists before mounting to avoid RuntimeError in test environments
TILES_PATH.mkdir(parents=True, exist_ok=True)

app.mount(
    "/tiles/ctx",
    CachedStaticFiles(directory=TILES_PATH),
    name="ctx-tiles",
)


CRISM_ASSETS_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "jezero"
    / "science"
    / "crism"
)

CRISM_ASSETS_PATH.mkdir(parents=True, exist_ok=True)

app.mount(
    "/science/crism",
    StaticFiles(directory=CRISM_ASSETS_PATH),
    name="crism-assets",
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# API Routers
# ============================================================

app.include_router(
    navigation_router,
    prefix="/api/v1",
    tags=["Navigation"]
)

app.include_router(
    mission_router
)

app.include_router(
    science_router
)


# ============================================================
# Utility Endpoints
# ============================================================

@app.get("/")
def root():
    return {
        "project": "MarsWalk Intelligence",
        "status": "online"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }
