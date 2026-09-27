"""HTTP API and static UI.

Run with: python -m uvicorn apps.insurance_claims.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import PACKAGE_DIR, Clock, ConfigError, Settings, SystemClock
from .domain import (
    ApiKeyRequest,
    ConsoleOut,
    CreateSessionOut,
    CreateSessionRequest,
    MessageRequest,
    ModelOut,
    OutboxOut,
    ProviderOut,
    SessionOut,
    SessionState,
    SessionView,
    TurnOut,
)
from .engine import ConversationEngine, ModelKeyRejectedError, ModelUnavailableError, SessionGoneError
from .fixture_loader import load_fixtures
from .keys import KeyEntry, KeyStore, key_hint
from .llm.adapter import LLMAdapter, LLMAuthError, LLMError, adapter_for_key, create_adapter, key_model_name
from .llm.providers import PROVIDERS
from .mailer import EmailSender
from .store import SessionRecord, SessionStore
from .transcript import transcript_json, transcript_markdown
from .views import console_turns, message_out, session_view

SESSION_COOKIE = "sop_session"
# Holds the ID of an API key entered in the console (keys.py), never the key.
KEY_COOKIE = "sop_key"
KEY_CHECK_SECONDS = 20
STATIC_DIR = PACKAGE_DIR / "static"
UI_PAGES = ("/", "/console")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        # The chat page embeds the SOP console in a frame, so same-origin framing is allowed.
        "default-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def create_app(
    settings: Settings | None = None,
    *,
    adapter: LLMAdapter | None = None,
    clock: Clock | None = None,
    store: SessionStore | None = None,
    mailer: EmailSender | None = None,
    keys: KeyStore | None = None,
    key_adapter: Callable[[str, str, str | None], Awaitable[LLMAdapter]] | None = None,
) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
    fixtures = load_fixtures(settings.fixtures_dir)
    if settings.consent_scenario not in fixtures.consent_scenarios:
        raise ConfigError(
            f"CONSENT_SCENARIO={settings.consent_scenario!r} is not defined in consent_scenarios.json."
        )
    if adapter is None:
        adapter = create_adapter(settings)
    if clock is None:
        clock = SystemClock()
    if store is None:
        store = SessionStore()
    if keys is None:
        keys = KeyStore()
    if key_adapter is None:
        key_adapter = partial(adapter_for_key, settings)
    engine = ConversationEngine(adapter, settings, clock, fixtures, mailer)

    app = FastAPI(title="SOP-guided claims support agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fixtures = fixtures

    def view(state: SessionState) -> SessionView:
        return session_view(state, engine.as_of_date(state))

    def find_record(request: Request, *, watch_only: bool = False) -> SessionRecord:
        session_id = request.cookies.get(SESSION_COOKIE)
        lookup = store.peek if watch_only else store.get
        record = lookup(session_id) if session_id else None
        if record is None:
            raise ApiError(401, "session_expired", "There is no active conversation. Start a new one.")
        return record

    async def current_record(request: Request) -> SessionRecord:
        return find_record(request)

    async def watched_record(request: Request) -> SessionRecord:
        """The session as the SOP console sees it; its polling doesn't count as activity."""
        return find_record(request, watch_only=True)

    def key_entry(request: Request) -> tuple[str | None, KeyEntry | None]:
        key_id = request.cookies.get(KEY_COOKIE)
        return key_id, keys.get(key_id) if key_id else None

    providers = [
        ProviderOut(id=p.id, label=p.label, default_model=p.default_model, models=list(p.models))
        for p in PROVIDERS.values()
    ]
    # With no key anywhere, the page offers the server's provider first, else OpenAI.
    default_provider = settings.ai_provider if settings.ai_provider in PROVIDERS else "openai"

    def model_out(entry: KeyEntry | None) -> ModelOut:
        if entry is not None:
            model, source, hint = entry.adapter, "yours", entry.hint
        elif adapter.is_real_model:
            model, source, hint = adapter, "server", None
        else:
            name = key_model_name(settings, default_provider)
            return ModelOut(
                source="none",
                real_model=False,
                provider=default_provider,
                provider_label=PROVIDERS[default_provider].label,
                model=name,
                providers=providers,
            )
        return ModelOut(
            source=source,
            real_model=model.is_real_model,
            provider=model.provider,
            provider_label=PROVIDERS[model.provider].label if model.provider in PROVIDERS else model.provider,
            model=model.model,
            key_hint=hint,
            providers=providers,
        )

    def outbox(state: SessionState) -> OutboxOut:
        return OutboxOut(emails=state.outbox if state.access_granted else [])

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # The pages and their scripts change together, so the browser checks for a new version
        # every time; a cached old script next to a new page would break the page.
        if request.url.path in UI_PAGES or request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        message = "The request is invalid."
        if errors:
            location = ".".join(str(part) for part in errors[0].get("loc", ()) if part != "body")
            detail = str(errors[0].get("msg", "invalid value")).removeprefix("Value error, ")
            message = f"{location}: {detail}" if location else detail
        return error_response(422, "invalid_request", message)

    @app.exception_handler(ModelKeyRejectedError)
    async def handle_key_rejected(request: Request, exc: ModelKeyRejectedError) -> JSONResponse:
        return error_response(
            403,
            "invalid_key",
            "The model provider rejected the API key. Enter a valid key in the SOP console.",
        )

    @app.exception_handler(ModelUnavailableError)
    async def handle_model_unavailable(request: Request, exc: ModelUnavailableError) -> JSONResponse:
        return error_response(
            503, "model_unavailable", "The assistant is temporarily unavailable. Please try again."
        )

    @app.exception_handler(SessionGoneError)
    async def handle_session_gone(request: Request, exc: SessionGoneError) -> JSONResponse:
        return error_response(401, "session_expired", "This conversation has ended. Start a new one.")

    # Starlette re-raises after this response is sent, so the server still logs the traceback.
    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "Something went wrong. Please try again.")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "provider": adapter.provider,
            "model": adapter.model,
            "real_model": adapter.is_real_model,
            "model_configured": settings.model_configured,
            "email_mode": settings.email_mode,
        }

    @app.post("/api/sessions", status_code=201)
    async def create_session(
        request: Request, response: Response, body: CreateSessionRequest | None = None
    ) -> CreateSessionOut:
        body = body or CreateSessionRequest()
        scenario = body.consent_scenario or settings.consent_scenario
        if scenario not in fixtures.consent_scenarios:
            raise ApiError(422, "invalid_request", "consent_scenario: unknown scenario")
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            store.delete(previous)
        state, welcome = engine.open_session(store.new_session_id(), body.date_mode, scenario)
        store.add(state)
        response.set_cookie(
            SESSION_COOKIE,
            state.session_id,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
        return CreateSessionOut(reply=message_out(welcome), session=view(state))

    @app.get("/api/session")
    async def get_session(record: SessionRecord = Depends(current_record)) -> SessionOut:
        return SessionOut(
            session=view(record.state),
            messages=[message_out(m) for m in record.state.messages],
        )

    @app.post("/api/session/messages")
    async def post_message(
        request: Request, body: MessageRequest, record: SessionRecord = Depends(current_record)
    ) -> TurnOut:
        _, entry = key_entry(request)
        result = await engine.handle_message(record, body.message, entry.adapter if entry else None)
        return TurnOut(
            reply=message_out(result.reply),
            session=view(record.state),
            plan=result.plan,
            trace=result.trace,
        )

    @app.get("/api/session/outbox")
    async def get_outbox(record: SessionRecord = Depends(current_record)) -> OutboxOut:
        return outbox(record.state)

    def console_view(state: SessionState) -> ConsoleOut:
        return ConsoleOut(session=view(state), turns=console_turns(state), outbox=outbox(state))

    @app.get("/api/session/console")
    async def get_console(record: SessionRecord = Depends(watched_record)) -> ConsoleOut:
        return console_view(record.state)

    @app.get("/api/session/export")
    async def export_transcript(
        format: Literal["markdown", "json"] = "markdown",
        record: SessionRecord = Depends(watched_record),
    ) -> Response:
        """The console's view of this conversation as a file to download."""
        now = datetime.now(UTC)
        console = console_view(record.state)
        if format == "json":
            content, media_type, suffix = transcript_json(console, now), "application/json", "json"
        else:
            content, media_type, suffix = transcript_markdown(console, now), "text/markdown", "md"
        filename = f"claims-conversation-{now:%Y%m%d-%H%M%S}.{suffix}"
        return Response(
            content,
            media_type=f"{media_type}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.delete("/api/session", status_code=204)
    async def delete_session(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            store.delete(session_id)
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
        )
        return response

    @app.get("/api/model")
    async def get_model(request: Request) -> ModelOut:
        return model_out(key_entry(request)[1])

    @app.put("/api/model/key")
    async def set_key(request: Request, response: Response, body: ApiKeyRequest) -> ModelOut:
        """Use this API key for this browser's conversations, once the provider accepts it."""
        api_key = body.api_key.get_secret_value()
        try:
            model = await asyncio.wait_for(key_adapter(body.provider, api_key, body.model), KEY_CHECK_SECONDS)
        except LLMAuthError:
            label = PROVIDERS[body.provider].label
            raise ApiError(422, "invalid_key", f"{label} didn't accept this API key.") from None
        except (LLMError, TimeoutError) as exc:
            reason = str(exc) or "the check timed out"
            raise ApiError(422, "key_check_failed", f"The key couldn't be used: {reason}.") from None
        old_id, _ = key_entry(request)
        if old_id:
            keys.delete(old_id)
        key_id = keys.add(model, key_hint(api_key))
        response.set_cookie(
            KEY_COOKIE, key_id, httponly=True, samesite="strict", secure=settings.cookie_secure, path="/"
        )
        return model_out(keys.get(key_id))

    @app.delete("/api/model/key")
    async def remove_key(request: Request, response: Response) -> ModelOut:
        key_id, _ = key_entry(request)
        if key_id:
            keys.delete(key_id)
        response.delete_cookie(
            KEY_COOKIE, path="/", httponly=True, samesite="strict", secure=settings.cookie_secure
        )
        return model_out(None)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/console", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "console.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
