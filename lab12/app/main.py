"""Фабрика приложения FastAPI.

``create_app()`` собирает приложение: подключает роутеры и обработчики доменных ошибок,
добавляет заголовки безопасности, раздаёт веб-интерфейс (``GET /`` и ``/static``), а в
``lifespan`` закрывает пул соединений с БД при остановке. Запуск:
``uvicorn app.main:app`` (или ``uvicorn --factory app.main:create_app``).
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import dispose_engine
from app.errors import register_exception_handlers
from app.routers import analytics as analytics_router
from app.routers import appointments as appointments_router
from app.routers import auth as auth_router
from app.routers import doctors as doctors_router
from app.routers import medical_records as medical_records_router
from app.routers import patients as patients_router
from app.routers import prescriptions as prescriptions_router
from app.routers import users as users_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Строгая политика для всего приложения: только свои ресурсы, никаких inline-скриптов и стилей.
CSP_STRICT = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"

# Ослабленная политика ТОЛЬКО для /docs и /redoc. Swagger UI и ReDoc, которые отдаёт FastAPI:
#   * грузят скрипты и стили с cdn.jsdelivr.net (swagger-ui-dist, redoc);
#   * запускают инициализацию инлайн-скриптом в самой HTML-странице ('unsafe-inline');
#   * Swagger UI подставляет inline-стили, ReDoc берёт шрифты у Google Fonts;
#   * ReDoc создаёт web worker из blob:; иконки приходят с fastapi.tiangolo.com / cdn.redoc.ly.
# Исключение ограничено двумя страницами документации (по точному совпадению пути); на
# интерфейс клиники и на JSON API оно не распространяется. frame-ancestors остаётся 'none'.
CSP_DOCS = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self'; worker-src 'self' blob:; child-src 'self' blob:; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """ASGI-middleware: добавляет заголовки безопасности к каждому HTTP-ответу.

    Чистый ASGI (а не ``BaseHTTPMiddleware``): не буферизует тело ответа и не ломает
    потоковую отдачу статики. Заголовки выставляются в момент ``http.response.start``,
    поэтому попадают и в ответы с ошибками (404/422/401), сформированные обработчиками.
    """

    def __init__(self, app: ASGIApp, relaxed_csp_paths: Iterable[str] = ()) -> None:
        self.app = app
        self.relaxed_csp_paths = frozenset(relaxed_csp_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        csp = CSP_DOCS if path in self.relaxed_csp_paths else CSP_STRICT

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = csp
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл приложения: при остановке освобождает пул соединений."""
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр приложения."""
    app = FastAPI(title="Clinic management system", version="1.0.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.include_router(appointments_router.router)
    app.include_router(users_router.router)
    app.include_router(doctors_router.router)
    app.include_router(patients_router.router)
    app.include_router(prescriptions_router.router)
    app.include_router(medical_records_router.router)
    app.include_router(analytics_router.router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Проверка живости сервиса (без обращения к БД)."""
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Веб-интерфейс: единственная HTML-страница (разделы переключает app.js)."""
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},  # всегда проверять актуальность у сервера
        )

    # Остальные файлы интерфейса (app.css, app.js). Монтируется после роутеров API.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Пути документации берём из самого приложения: если их переименуют, исключение для CSP
    # переедет вместе с ними. Добавляется последним => middleware самый внешний.
    docs_paths = {
        path for path in (app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url) if path is not None
    }
    app.add_middleware(SecurityHeadersMiddleware, relaxed_csp_paths=docs_paths)

    return app


app = create_app()
