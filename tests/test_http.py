"""HTTP-level guards that belong to no single route.

The body cap, a busy database, connection settings, response headers, the docs site, and the OpenAPI error docs. Backs
the contract's Limits (request body), the 413 and 503 rows of its status table, and its Other section. The busy test
uses a real lock held by a second connection (`helpers.signups_blocked_by_another_writer`), so it runs on both
databases. The PRAGMA test is SQLite only; the Postgres connection settings are checked in `test_store.py`.
"""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, main
from app.main import create_app
from helpers import Api, assert_error, signups_blocked_by_another_writer

# Body cap (413)


def test_declared_body_over_the_cap_is_refused_before_anything_runs(client: TestClient) -> None:
    """A body declaring more than 1,000,000 bytes is 413 `payload_too_large`, even anonymously.

    The cap is middleware and answers before the app runs, so no body is parsed before authentication and an anonymous
    60 MB body is never accepted.
    """
    # Anonymous, on a route that would otherwise parse the body first.
    response = client.post(
        "/auth/signup",
        content=b"x" * (config.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert_error(response, 413, "payload_too_large")


def test_declared_body_over_the_cap_is_refused_on_any_route(client: TestClient) -> None:
    """The cap applies to every route, including one that takes no body (`GET /health`)."""
    response = client.request("GET", "/health", content=b"x" * (config.MAX_BODY_BYTES + 1))
    assert_error(response, 413, "payload_too_large")


def test_streamed_body_over_the_cap_is_refused(client: TestClient) -> None:
    """No Content-Length: the bytes are counted as they arrive.

    A cap that only read `Content-Length` would be bypassed by a chunked upload. The test also checks no length was
    sent, so it exercises the path it means to.
    """
    chunk = b" " * 65_536

    def body() -> Iterator[bytes]:
        for _ in range(config.MAX_BODY_BYTES // len(chunk) + 2):
            yield chunk

    response = client.post("/auth/signup", content=body(), headers={"Content-Type": "application/json"})

    assert response.request.headers.get("content-length") is None
    assert_error(response, 413, "payload_too_large")


def test_largest_legal_note_fits_under_the_cap(api: Api) -> None:
    """The largest legal note body, sent as the contract asks (UTF-8, unescaped), fits under the byte cap.

    100,000 four-byte characters sent as unescaped UTF-8 are 400 KB, well under 1 MB. The cap does refuse some legal
    bodies sent otherwise: the same characters `\\u`-escaped as surrogate pairs are 1.2 MB and get 413, which the
    contract warns about.
    """
    alice = api.signup("alice")
    team_id = api.create_team(alice)
    note = {"title": "Big", "body": "\U0001f600" * config.MAX_BODY_LENGTH}
    headers = {**alice.headers, "Content-Type": "application/json"}
    unescaped = json.dumps(note, ensure_ascii=False).encode()  # 400 KB
    escaped = json.dumps(note).encode()  # 1.2 MB: "\ud83d\ude00" per character

    response = api.client.post(f"/teams/{team_id}/notes", content=unescaped, headers=headers)

    assert response.status_code == 201, response.text
    assert len(escaped) > config.MAX_BODY_BYTES
    assert_error(api.client.post(f"/teams/{team_id}/notes", content=escaped, headers=headers), 413, "payload_too_large")


# Busy database (503)


@pytest.fixture
def impatient_client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app whose writes give up waiting for another writer after 50 ms instead of 5 s."""
    monkeypatch.setattr(config, "BUSY_TIMEOUT_MS", 50)
    with TestClient(create_app(database_url)) as client:
        yield client


def test_locked_database_is_503_busy_with_retry_after(impatient_client: TestClient, database_url: str) -> None:
    """A write that cannot get the database's lock in time is 503 `busy` with `Retry-After: 1`.

    The retry succeeds because nothing was written. Without the mapping to `busy`, SQLite's "database is locked" reaches
    clients as a 500.
    """
    signup = {"email": "a@example.com", "password": "correct horse", "name": "a"}
    with signups_blocked_by_another_writer(database_url):
        response = impatient_client.post("/auth/signup", json=signup)

    assert_error(response, 503, "busy")
    assert response.headers["Retry-After"] == "1"
    # Nothing was written, so the retry succeeds.
    assert impatient_client.post("/auth/signup", json=signup).status_code == 201


@pytest.mark.sqlite_only
def test_connections_have_the_documented_pragmas(app: FastAPI, client: TestClient) -> None:
    """Every SQLite connection has WAL, foreign keys on, the busy timeout and `synchronous=FULL`.

    SQLite leaves foreign keys off by default, and `FULL` makes an acknowledged write survive a power loss. SQLite only.
    """
    with app.state.store.engine.connect() as connection:
        pragma = connection.exec_driver_sql
        assert pragma("PRAGMA journal_mode").scalar() == "wal"
        assert pragma("PRAGMA foreign_keys").scalar() == 1
        assert pragma("PRAGMA busy_timeout").scalar() == config.BUSY_TIMEOUT_MS
        assert pragma("PRAGMA synchronous").scalar() == 2  # FULL


# Headers


PAGE_CSP = "default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/static/style.css", "text/css"),
        ("/static/js/app.js", "text/javascript"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_test_page_files_have_their_type_and_security_headers(client: TestClient, path: str, content_type: str) -> None:
    """The test page and its files come with their content type, the page CSP, `nosniff` and `no-cache`.

    Case ids are `<path>-<content type>`.
    """
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Type"].split(";")[0] == content_type
    assert response.headers["Content-Security-Policy"] == PAGE_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-cache"


class _PageParser(HTMLParser):
    """Collects what the CSP must allow: external files, and anything inline."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.inline: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.inline += [f"<{tag} {name}>" for name, _ in attrs if name == "style" or name.startswith("on")]
        if tag == "style":
            self.inline.append("<style>")
        if tag == "script":
            if values.get("src"):
                self.scripts.append(str(values["src"]))
            else:
                self._in_script = True
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(str(values["href"]))

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline.append("<script> with a body")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False


def test_test_page_has_nothing_inline_and_every_file_it_loads_is_served(client: TestClient) -> None:
    """The CSP forbids inline code and styles, and nosniff refuses a script or stylesheet of the wrong type.

    So the page must have nothing inline, and every module it imports, followed through the import graph, must be served
    as JavaScript. A new module or inline handler that the browser would block fails here instead of silently in the
    browser.
    """
    parser = _PageParser()
    parser.feed(client.get("/").text)
    assert parser.inline == []
    assert parser.scripts and parser.stylesheets

    # Follow the module graph: every `import ... from './x.js'` must be served as JavaScript.
    pending, seen = list(parser.scripts), set()
    while pending:
        url = pending.pop()
        if url in seen:
            continue
        seen.add(url)
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["Content-Type"].split(";")[0] == "text/javascript", url
        assert response.headers["X-Content-Type-Options"] == "nosniff", url
        for name in re.findall(r"""^import .* from '\./([\w.-]+)';$""", response.text, re.MULTILINE):
            pending.append(url.rsplit("/", 1)[0] + "/" + name)
    assert len(seen) > 1
    for url in parser.stylesheets:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["Content-Type"].split(";")[0] == "text/css", url


def test_unknown_static_file_is_a_contract_404(client: TestClient) -> None:
    """An unknown static file is 404 `not_found`, and a POST to a static file 405, both in the error shape."""
    assert_error(client.get("/static/nope.js"), 404, "not_found")
    assert_error(client.post("/static/style.css"), 405, "method_not_allowed")


def test_test_page_links_to_the_docs_site_and_the_api_explorer(client: TestClient) -> None:
    """The test page links to `/site/` and `/docs`."""
    hrefs = re.findall(r'<a href="([^"]+)"', client.get("/").text)
    assert {"/site/", "/docs"} <= set(hrefs)


# Docs site (/site/)


@pytest.fixture
def site_client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], TestClient]:
    """A client for an app whose docs site directory is the given path (built or not)."""

    def start(site_dir: Path) -> TestClient:
        monkeypatch.setattr(main, "SITE_DIR", site_dir)
        return TestClient(create_app(database_url))

    return start


def test_docs_site_not_built_is_a_contract_404_saying_how_to_build_it(
    site_client: Callable[[Path], TestClient], tmp_path: Path
) -> None:
    """Before `mkdocs build`, every `/site` URL is 404 `not_found` saying how to build it.

    The app itself still runs without the site.
    """
    with site_client(tmp_path / "not-built") as client:
        for path in ("/site", "/site/", "/site/API_CONTRACT/"):
            error = assert_error(client.get(path), 404, "not_found")
            assert "mkdocs build" in error["message"], path
        assert client.get("/health").status_code == 200  # the app runs without it


def test_docs_site_is_served_with_its_own_policy_allowing_only_its_inline_scripts(
    site_client: Callable[[Path], TestClient], tmp_path: Path
) -> None:
    """The docs site's CSP allows exactly the hashes of the inline scripts in the page served, never `unsafe-inline`.

    The site shares an origin, and a tab's stored token, with the test page, so an injected script there could act as
    the user. Uses a fake site in a temporary directory. Also pins the redirect of directory URLs, the JSON 404 for
    unknown pages (not mkdocs's `404.html`), and that the test page keeps its own stricter policy.
    """
    script = "var palette = 1;"
    (tmp_path / "guide").mkdir()
    (tmp_path / "index.html").write_text(f"<!doctype html><title>Docs</title><script>{script}</script>")
    (tmp_path / "guide" / "index.html").write_text("<!doctype html><title>Guide</title>")
    (tmp_path / "404.html").write_text("<!doctype html><title>Lost</title>")

    with site_client(tmp_path) as client:
        home = client.get("/site/")
        assert home.status_code == 200 and "Docs" in home.text
        assert home.headers["X-Content-Type-Options"] == "nosniff"
        csp = home.headers["Content-Security-Policy"]
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"script-src 'self' 'sha256-{digest}';" in csp
        assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
        # A page with no inline script allows none.
        assert "script-src 'self';" in client.get("/site/guide/").headers["Content-Security-Policy"]
        # Directory URLs without the slash redirect, as mkdocs links expect.
        moved = client.get("/site/guide", follow_redirects=False)
        assert moved.status_code == 307 and moved.headers["Location"].endswith("/site/guide/")
        # Unknown pages get the API's error shape, not mkdocs's 404.html.
        assert_error(client.get("/site/nope/"), 404, "not_found")
        # The test page keeps its own, stricter policy.
        assert client.get("/").headers["Content-Security-Policy"] == PAGE_CSP


def test_login_response_is_not_cached(client: TestClient) -> None:
    """The login response, which carries the token, is sent with `Cache-Control: no-store`."""
    Api(client).signup("alice")
    response = client.post("/auth/login", json={"email": "alice@example.com", "password": "correct horse"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


# OpenAPI


ERROR_SCHEMAS = {"#/components/schemas/ErrorOut", "#/components/schemas/ValidationErrorOut"}
PUBLIC = {("post", "/auth/signup"), ("post", "/auth/login"), ("get", "/health")}


def _operations(app: FastAPI) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(method, path, operation) for every operation in the OpenAPI document."""
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            yield method, path, operation


def test_openapi_documents_errors_in_the_real_shape(app: FastAPI) -> None:
    """Every operation in the OpenAPI document lists its error responses in the app's real error schema.

    FastAPI's default `HTTPValidationError`, a shape the app never sends, must be absent. The rules: every route but
    `/health` can be 503; every route but sign-up, login and health needs a token (401); team routes answer non-members
    404; routes with a body list 413 and 422; `/changes` lists 429. Without these, the generated docs describe none
    of the errors clients actually get.
    """
    schemas = app.openapi()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas  # FastAPI's default 422 shape is not what we send
    error_body = schemas["ErrorBody"]
    assert set(error_body["required"]) == {"code", "message"}

    for method, path, operation in _operations(app):
        where = f"{method.upper()} {path}"
        responses = operation["responses"]
        statuses = {int(status) for status in responses if status[0] in "45"}

        for status in statuses:
            ref = responses[str(status)]["content"]["application/json"]["schema"]["$ref"]
            assert ref in ERROR_SCHEMAS, f"{where} {status} is not in the error shape"
        if "422" in responses:
            assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith("ValidationErrorOut")

        if (method, path) == ("get", "/health"):
            assert not statuses, where
            continue
        assert 503 in statuses, f"{where} touches the database and can be busy"
        if (method, path) not in PUBLIC:
            assert 401 in statuses, f"{where} needs a token"
        if path.startswith("/teams/{team_id}"):
            assert 404 in statuses, f"{where} answers non-members with 404"
        if "requestBody" in operation:
            assert {413, 422} <= statuses, f"{where} takes a body"
        if operation.get("parameters"):
            assert 422 in statuses, f"{where} has parameters"
        if path.endswith("/changes"):
            assert 429 in statuses, where
