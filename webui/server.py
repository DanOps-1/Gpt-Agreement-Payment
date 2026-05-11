from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .backend.routes import setup as setup_routes
from .backend.routes import auth as auth_routes
from .backend.routes import wizard as wizard_routes
from .backend.routes import preflight as preflight_routes
from .backend.routes import sniff as sniff_routes
from .backend.routes import config as config_routes
from .backend.routes import inventory as inventory_routes
from .backend.routes import run as run_routes
from .backend.routes import cloudflare_kv as cf_kv_routes
from .backend.routes import whatsapp as whatsapp_routes
from .backend.routes import link_state as link_state_routes
from .backend.routes import proxy as proxy_routes
from .backend.routes import auto_loop as auto_loop_routes


FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="Gpt-Agreement-Payment webui")
    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(wizard_routes.router)
    app.include_router(preflight_routes.router)
    app.include_router(sniff_routes.router)
    app.include_router(config_routes.router)
    app.include_router(inventory_routes.router)
    app.include_router(run_routes.router)
    app.include_router(cf_kv_routes.router)
    app.include_router(whatsapp_routes.router)
    app.include_router(link_state_routes.router)
    app.include_router(proxy_routes.router)
    app.include_router(auto_loop_routes.router)

    @app.get("/api/healthz")
    def healthz():
        return {"status": "ok"}

    if FRONTEND_DIST.exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            # Mount under both / and /webui/ so the same build serves direct
            # (127.0.0.1:8765/) and reverse-proxied (.../webui/) deployments.
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
            app.mount("/webui/assets", StaticFiles(directory=assets_dir), name="assets_webui")

        def _serve(full_path: str):
            if full_path.startswith("api/"):
                return FileResponse(FRONTEND_DIST / "index.html", status_code=404)
            f = FRONTEND_DIST / full_path
            try:
                f.resolve().relative_to(FRONTEND_DIST.resolve())
            except ValueError:
                return FileResponse(FRONTEND_DIST / "index.html")
            if f.is_file():
                return FileResponse(f)
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/webui/{full_path:path}")
        def spa_webui(full_path: str):
            return _serve(full_path)

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            return _serve(full_path)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)
