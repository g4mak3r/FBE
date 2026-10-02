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
from fbe_flow.core.credentials import WindowsVault
from fbe_flow.core.database import Database
from fbe_flow.core.errors import Conflict, FlowError, InvalidInput, NotFound
from fbe_flow.core.instance import single_instance
from fbe_flow.core.integrations import AdapterRegistry, IntegrationAdapter
from fbe_flow.integrations import installed_adapters
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.chz.signing import WindowsSigner
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.fulfillment import Fulfillment
from fbe_flow.modules.marking import Marking
from fbe_flow.modules.operations import Operations, Worker
from fbe_flow.modules.records import Records
from fbe_flow.modules.sellers import Sellers
from fbe_flow.modules.settings import Settings
from fbe_flow.web.marking import router as marking_router
from fbe_flow.web.routes import router
from fbe_flow.web.wb import router as wb_router


def create_app(
    config: AppConfig | None = None,
    adapters: Iterable[IntegrationAdapter] | None = None,
    *,
    vault=None,
    signer=None,
) -> FastAPI:
    config = config or AppConfig(default_data_dir())
    vault = vault or WindowsVault(config.data_dir / "credentials")
    signer = signer or WindowsSigner(config.data_dir / "runtime")
    registry = AdapterRegistry(installed_adapters(vault, signer) if adapters is None else adapters)
    database = Database(config.database_path)
    operations = Operations(database, registry)
    worker = Worker(operations)
    connections = Connections(database, registry)
    marking = Marking(database, connections, operations, registry)
    fulfillment = Fulfillment(database, connections, operations, registry, marking)

    def execute(adapter, context, key, payload):
        module = fulfillment if adapter.key == "wb" else marking
        return module.execute(adapter, context, key, payload)

    def result_handler(conn, job, result):
        marking.apply_result(conn, job, result)
        fulfillment.apply_result(conn, job, result)

    def recovery_handler(conn):
        marking.recover(conn)
        fulfillment.recover(conn)

    def failure_handler(conn, job, code):
        marking.fail(conn, job, code)
        fulfillment.fail(conn, job, code)

    def idle_handler():
        marking.queue_due()
        fulfillment.queue_due()

    operations.executor = execute
    operations.result_handler = result_handler
    operations.recovery_handler = recovery_handler
    operations.failure_handler = failure_handler
    operations.idle_handler = idle_handler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with single_instance(config.data_dir):
            database.initialize()
            marking.refresh_capabilities()
            fulfillment.refresh_capabilities()
            if config.worker_enabled:
                worker.start()
            try:
                yield
            finally:
                if config.worker_enabled:
                    await asyncio.to_thread(worker.stop)

    app = FastAPI(title="FBE Flow", version="0.4.0", lifespan=lifespan)
    app.state.config = config
    app.state.registry = registry
    app.state.database = database
    app.state.sellers = Sellers(database)
    app.state.connections = connections
    app.state.marking = marking
    app.state.fulfillment = fulfillment
    app.state.vault = vault
    app.state.signer = signer
    app.state.settings = Settings(database)
    app.state.records = Records(database)
    app.state.operations = operations
    app.state.worker = worker
    web_dir = Path(__file__).parent / "web"
    app.state.templates = Jinja2Templates(directory=web_dir / "templates")
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
    app.include_router(router)
    app.include_router(marking_router)
    app.include_router(wb_router)

    @app.exception_handler(RemoteError)
    async def upstream_error(request: Request, exc: RemoteError):
        return JSONResponse(
            {
                "detail": f"Внешний API: {exc.code}. Проверьте доступ и повторите чтение позже",
                "code": exc.code,
                "errors": exc.details,
            },
            status_code=502,
        )

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
