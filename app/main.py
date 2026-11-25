"""Application factory, error handlers, and the two routes outside any resource.

Run with a single worker:
`uv run uvicorn app.main:app --port 8765 --timeout-graceful-shutdown 3`.
The long poll's wake-up signals are held in this process's memory (see
notify.py). On SIGTERM held polls end at once (`_on_shutdown_signal`); the
graceful-shutdown timeout bounds how long any other request can delay exit.
"""

import asyncio
import base64
import hashlib
import os
import re
import signal
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import config
from app.body_limit import BodyLimitMiddleware
from app.errors import ApiError, not_found, store_error
from app.notify import Notifier
from app.routers import auth, changes, locks, notes, teams
from app.store import make_store
from app.store.interface import StoreError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"
# The docs site, a build product of `mkdocs build` (see mkdocs.yml). Not in the
# source tree; scripts/run.sh and the Docker image build it.
SITE_DIR = Path(__file__).resolve().parent.parent / "site"

# The test page (`/`) loads its stylesheet and module scripts from `/static` and
# only calls this origin's API. Nothing inline runs, nothing from elsewhere loads,
# no form submits anywhere (the page's forms are handled by script; without it a
# login form must not put the password in a URL), nothing may frame it, and no
# response is sniffed as another type. `no-cache` makes the browser revalidate, so
# an edited file is picked up on reload.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}

# The docs site at /site/ gets its own policy instead of loosening the page's.
# Material for MkDocs puts a few small scripts inline (theme and scroll state),
# so each HTML file's policy allows exactly its own inline scripts by hash
# (`_site_csp`), never `'unsafe-inline'`: the site shares this origin, and so
# the test page's tokens, with the API. Its icons are CSS `data:` images. The
# theme is configured to load no fonts or scripts from other hosts, so `'self'`
# is enough and the site works offline.
SITE_CSP = (
    "default-src 'self'; script-src 'self'{hashes}; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_INLINE_SCRIPT = re.compile(rb"<script>(.*?)</script>", re.DOTALL)

# Status codes whose error code is not just the HTTP phrase in snake case.
_HTTP_ERROR_CODES = {413: "payload_too_large"}


def create_app(database_url: str | None = None, worker_threads: int = config.WORKER_THREADS) -> FastAPI:
    """Build the app against `database_url` (default: DATABASE_URL, else ./notes.db).

    `worker_threads` sizes both the thread pool that runs sync code and the
    connection pool, which app/store/sql.py explains must be no smaller.
    Tests shrink it to prove that many more requests than threads still
    never wait on a connection.
    """
    store = make_store(database_url or config.database_url(), pool_size=worker_threads)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Starlette runs sync handlers and dependencies through anyio's default
        # limiter (40 threads unless set). Set it here, on the serving event
        # loop, so the thread count and the pool cap are one number.
        anyio.to_thread.current_default_thread_limiter().total_tokens = worker_threads
        store.create_schema()
        loop = asyncio.get_running_loop()
        _on_shutdown_signal(lambda: loop.call_soon_threadsafe(notifier.stop))
        yield
        store.close()

    app = FastAPI(
        title="Team Notes API",
        summary="Notes shared by small teams, with an edit lock and change long-polling.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.notifier = notifier = Notifier(max_polls_per_user=config.MAX_POLLS_PER_USER)
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.MAX_BODY_BYTES)

    _register_error_handlers(app)
    for router in (
        auth.router,
        teams.router,
        teams.one_team_router,
        notes.router,
        locks.router,
        changes.router,
    ):
        app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Liveness check."""
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The manual test page. Looked up per request, so a missing file is a 404, not a crash."""
        if not INDEX_PATH.is_file():
            raise not_found("Test page")
        return FileResponse(INDEX_PATH, headers=PAGE_HEADERS)

    app.mount("/static", _StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    # `html=True`: `/site/x/` serves `x/index.html` and `/site/x` redirects there.
    app.mount("/site", _SiteFiles(directory=SITE_DIR, html=True, check_dir=False), name="site")
    return app


class _StaticFiles(StaticFiles):
    """The test page's stylesheet, scripts and icon, sent with the page's headers."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(PAGE_HEADERS)
        return response


class _SiteFiles(StaticFiles):
    """The docs site: its own headers, and the API's error shape for anything missing."""

    async def check_config(self) -> None:
        # Starlette checks the directory once, at the first request, and fails it
        # with a 500 if missing. get_response checks on every request instead.
        return

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Checked per request, so the app starts without a built site and serves it once built.
        if not Path(str(self.directory)).is_dir():
            raise ApiError(
                404,
                "not_found",
                "The docs site has not been built. Run `uv run --group docs mkdocs build` "
                "(scripts/run.sh and the Docker image do this), then reload.",
            )
        return await super().get_response(path, scope)

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        # In html mode Starlette would answer an unknown page with mkdocs's 404.html,
        # without these headers; keep the contract's JSON 404 instead.
        if path == "404.html":
            return "", None
        return super().lookup_path(path)

    def file_response(self, full_path: os.PathLike[str] | str, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        html = Path(full_path).read_bytes() if str(full_path).endswith(".html") else b""
        response.headers.update({**PAGE_HEADERS, "Content-Security-Policy": _site_csp(html)})
        return response


def _site_csp(html: bytes) -> str:
    """SITE_CSP allowing the inline `<script>` bodies of this one file, by SHA-256 hash."""
    hashes = sorted({base64.b64encode(hashlib.sha256(body).digest()).decode() for body in _INLINE_SCRIPT.findall(html)})
    return SITE_CSP.format(hashes="".join(f" 'sha256-{h}'" for h in hashes))


def _on_shutdown_signal(callback: Callable[[], None]) -> None:
    """Run `callback` when SIGINT or SIGTERM arrives, then the server's own handler.

    uvicorn stops in this order: stop accepting, wait for open requests
    (up to --timeout-graceful-shutdown), cancel the rest, then run the
    lifespan shutdown. A lifespan hook is therefore too late to end held
    polls, so this chains onto the signal handler uvicorn installed before
    startup. The callback only schedules work on the event loop: a signal
    handler can interrupt the loop thread while it holds the notifier's lock.

    Skipped when there is no handler to chain to, or off the main thread
    (TestClient runs the app in a worker thread, where signals cannot be set).
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(signum)
        if not callable(previous):
            continue

        def handler(sig: int, frame: object, previous: Callable[..., object] = previous) -> None:
            callback()
            previous(sig, frame)

        signal.signal(signum, handler)


def _register_error_handlers(app: FastAPI) -> None:
    """Make every error, including FastAPI's own, use the contract's shape."""

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _error_response(ApiError(422, "validation_error", "The request is not valid.", details=details))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Unknown routes (404 "not_found"), wrong methods (405 "method_not_allowed"),
        # unparseable bodies (400 "bad_request") and streamed bodies over the cap
        # (413, see body_limit.py) come through here.
        code = _HTTP_ERROR_CODES.get(exc.status_code) or HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        return _error_response(ApiError(exc.status_code, code, str(exc.detail), headers=exc.headers))

    @app.exception_handler(StoreError)
    async def store_refused(_request: Request, exc: StoreError) -> JSONResponse:
        # A domain refusal (409, 404, 403) or a busy database (503). Any other
        # database error is a real fault and reaches the catch-all 500 handler.
        return _error_response(store_error(exc))

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(ApiError(500, "internal_error", "Something went wrong on the server."))


def _error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status, headers=error.headers)


app = create_app()
