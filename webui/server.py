#╔══════════════════════════════════════════════╗
#║             👑:anzi开发by:anzi👑             ║
#╚══════════════════════════════════════════════╝
import mimetypes  # 导入文件类型工具
from pathlib import Path  # 导入路径工具
from fastapi import FastAPI  # 导入接口应用类
from fastapi.responses import FileResponse  # 导入文件响应类
from fastapi.staticfiles import StaticFiles  # 导入静态文件服务
from .backend.routes import auth as auth_routes  # 导入登录路由
from .backend.routes import config as config_routes  # 导入配置路由
from .backend.routes import preflight as preflight_routes  # 导入预检路由
from .backend.routes import run as run_routes  # 导入运行路由
from .backend.routes import setup as setup_routes  # 导入安装路由
from .backend.routes import sniff as sniff_routes  # 导入抓包路由
from .backend.routes import wizard as wizard_routes  # 导入向导路由

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"  # 定义前端构建目录
mimetypes.add_type("image/webp", ".webp")  # 注册守宫精灵表类型

def create_app() -> FastAPI:  # 创建服务应用
    app = FastAPI(title="gpt-mitm webui")  # 创建后端应用
    app.include_router(setup_routes.router)  # 挂载安装接口
    app.include_router(auth_routes.router)  # 挂载登录接口
    app.include_router(wizard_routes.router)  # 挂载向导接口
    app.include_router(preflight_routes.router)  # 挂载预检接口
    app.include_router(sniff_routes.router)  # 挂载抓包接口
    app.include_router(config_routes.router)  # 挂载配置接口
    app.include_router(run_routes.router)  # 挂载运行接口

    @app.get("/api/healthz")  # 注册健康检查接口
    def healthz():  # 定义健康检查函数
        return {"status": "ok"}  # 返回正常状态

    if FRONTEND_DIST.exists():  # 检查前端目录是否存在
        assets_dir = FRONTEND_DIST / "assets"  # 定义静态资源目录
        if assets_dir.exists():  # 检查静态资源目录
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")  # 挂载前端资源

        @app.get("/{full_path:path}")  # 注册单页应用入口
        def spa(full_path: str):  # 处理前端路径
            if full_path.startswith("api/"):  # 防止接口路径误入前端
                return FileResponse(FRONTEND_DIST / "index.html", status_code=404)  # 返回前端错误页
            file_path = FRONTEND_DIST / full_path  # 拼出请求文件路径
            try:  # 检查路径安全
                file_path.resolve().relative_to(FRONTEND_DIST.resolve())  # 确认文件在前端目录内
            except ValueError:  # 捕获越界路径
                return FileResponse(FRONTEND_DIST / "index.html")  # 返回前端首页
            if file_path.is_file():  # 检查真实文件
                return FileResponse(file_path)  # 返回静态文件
            return FileResponse(FRONTEND_DIST / "index.html")  # 返回前端首页

    return app  # 返回应用实例

if __name__ == "__main__":  # 直接运行时启动
    import uvicorn  # 导入服务器工具
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)  # 启动本地服务
