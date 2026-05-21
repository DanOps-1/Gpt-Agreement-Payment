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
from .backend.routes import run_parallel as run_parallel_routes
from .backend.routes import cloudflare_kv as cf_kv_routes
from .backend.routes import whatsapp as whatsapp_routes
from .backend.routes import link_state as link_state_routes
from .backend.routes import proxy as proxy_routes
from .backend.routes import auto_loop as auto_loop_routes
from .backend.routes import outlook as outlook_routes
from .backend.routes import promo_links as promo_links_routes


FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="Gpt-Agreement-Payment webui")
    api_routers = [
        setup_routes.router,
        auth_routes.router,
        wizard_routes.router,
        preflight_routes.router,
        sniff_routes.router,
        config_routes.router,
        inventory_routes.router,
        run_routes.router,
        run_parallel_routes.router,
        cf_kv_routes.router,
        whatsapp_routes.router,
        link_state_routes.router,
        proxy_routes.router,
        auto_loop_routes.router,
        outlook_routes.router,
        promo_links_routes.router,
    ]
    for router in api_routers:
        app.include_router(router)
        app.include_router(router, prefix="/webui")

    @app.get("/api/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/webui/api/healthz")
    def healthz_webui():
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
