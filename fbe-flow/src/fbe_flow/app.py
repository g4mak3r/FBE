import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fbe_flow.config import AppConfig, default_data_dir
from fbe_flow.core.database import Database
from fbe_flow.core.errors import Conflict, FlowError, InvalidInput, NotFound
from fbe_flow.core.instance import single_instance
from fbe_flow.core.integrations import AdapterRegistry, IntegrationAdapter
from fbe_flow.integrations import installed_adapters
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.operations import Operations, Worker
from fbe_flow.modules.records import Records
from fbe_flow.modules.sellers import Sellers
from fbe_flow.modules.settings import Settings
from fbe_flow.web.routes import router


def create_app(
    config: AppConfig | None = None, adapters: Iterable[IntegrationAdapter] | None = None
) -> FastAPI:
    config = config or AppConfig(default_data_dir())
    registry = AdapterRegistry(installed_adapters() if adapters is None else adapters)
    database = Database(config.database_path)
    operations = Operations(database, registry)
    worker = Worker(operations)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with single_instance(config.data_dir):
            database.initialize()
            if config.worker_enabled:
                worker.start()
            try:
                yield
            finally:
                if config.worker_enabled:
                    await asyncio.to_thread(worker.stop)

    app = FastAPI(title="FBE Flow", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.registry = registry
    app.state.database = database
    app.state.sellers = Sellers(database)
    app.state.connections = Connections(database, registry)
    app.state.settings = Settings(database)
    app.state.records = Records(database)
    app.state.operations = operations
    app.state.worker = worker
    web_dir = Path(__file__).parent / "web"
    app.state.templates = Jinja2Templates(directory=web_dir / "templates")
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Raw input may contain secrets or non-finite numbers that JSON cannot serialize.
        errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(FlowError)
    async def domain_error(request: Request, exc: FlowError):
        status = {NotFound: 404, InvalidInput: 422, Conflict: 409}.get(type(exc), 400)
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected_origin = str(request.base_url).rstrip("/")
            if request.headers.get("x-fbe-flow") != "1" or (
                origin is not None and origin != expected_origin
            ):
                return JSONResponse({"detail": "Требуется локальный запрос"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Требуется JSON"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        # /docs uses its own Swagger CDN assets; the application shell is fully local.
        if response.headers.get("content-type", "").startswith(
            "text/html"
        ) and request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    return app
