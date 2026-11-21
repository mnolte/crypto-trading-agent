"""Browser and SMTP notification settings plus the side-effect-free test surface."""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import repo, settings_service
from app import tasks as tasks_mod
from app.api import errors as api_errors
from app.api.deps import AppState, broadcast_settings, get_state, require_token
from app.config import SecretStoreError, get_secret, secret_status, set_secret
from app.db import utcnow
from app.models import NotificationSettings, SmtpSettings

router = APIRouter(prefix="/api/notifications")


class SmtpPasswordIn(BaseModel):
    password: str


class SmtpSaveIn(BaseModel):
    """PUT /api/notifications body (item 025): the SMTP block only — the
    rest of the notification settings save through PUT /api/settings."""

    smtp: SmtpSettings
    confirm: bool = False


@router.get("/smtp-password")
def smtp_password_status() -> dict[str, object]:
    return secret_status("smtp")


@router.put("/smtp-password", dependencies=[Depends(require_token)])
def put_smtp_password(body: SmtpPasswordIn) -> dict[str, object]:
    if not body.password.strip():
        raise HTTPException(status_code=422, detail="password must not be empty")
    try:
        set_secret("smtp", body.password)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return secret_status("smtp")


@router.get("")
def get_notifications(
    state: Annotated[AppState, Depends(get_state)],
) -> NotificationSettings:
    return state.settings.notifications


@router.put("", dependencies=[Depends(require_token)])
async def put_notifications(
    body: SmtpSaveIn,
    state: Annotated[AppState, Depends(get_state)],
) -> NotificationSettings:
    state.settings.notifications.smtp = body.smtp
    session = state.session_factory()
    try:
        try:
            state.settings_version = settings_service.save(
                session,
                state.settings,
                reason="Email settings tested and saved",
                source="action",
                confirm_looser=body.confirm,
            )
        except settings_service.LooserNeedsConfirm as exc:
            return api_errors.api_error(  # type: ignore[return-value]
                409,
                "This change loosens a limit — confirm to save it.",
                code="looser_needs_confirm",
                extra={"changes": exc.changes},
            )
    finally:
        session.close()
    await broadcast_settings(state)
    return state.settings.notifications


# In-app toast severity per notification event (PR 14): critical toasts
# stay until dismissed, warning and info close after 10 s.
_TOAST_SEVERITY = {
    "kill_switch": "critical",
    "daily_loss": "warning",
    "pair_stale": "warning",
    "source_failing": "warning",
    "model_failing": "warning",
    "risk_control": "warning",
    "trade": "info",
    "review": "info",
}


def _send_email(settings: NotificationSettings, subject: str, body: str, timeout_s: float) -> None:
    smtp = settings.smtp
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.sender
    message["To"] = smtp.recipient
    message.set_content(body)
    client_class = smtplib.SMTP
    with client_class(smtp.host, smtp.port, timeout=timeout_s) as client:
        if smtp.tls:
            client.starttls()
        password = get_secret("smtp")
        if smtp.username and password:
            client.login(smtp.username, password)
        client.send_message(message)


class NotificationDispatcher:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._last_sent: dict[tuple[str, str, str], datetime] = {}
        self._repeats: dict[tuple[str, str, str], int] = {}
        self._inflight: set[tuple[str, str, str]] = set()

    def schedule(self, event: str, title: str, body: str) -> None:
        task = tasks_mod.spawn(
            self.notify,
            event,
            title,
            body,
            name=f"notify-{event}",
            restart=False,
            track=lambda t: self.state.tasks.append(t),
        )
        task.add_done_callback(
            lambda completed: (
                self.state.tasks.remove(completed) if completed in self.state.tasks else None
            )
        )

    async def notify(self, event: str, title: str, body: str) -> None:
        settings = self.state.settings.notifications
        channels = settings.events.get(event)
        if channels is None:
            return
        now = utcnow()
        if settings.browser_enabled and channels.browser:
            key = (event, title, "browser")
            rendered, repeats = self._render_or_suppress(key, body, now)
            if rendered is not None and await self._deliver_browser(event, title, rendered):
                self._last_sent[key] = now
                self._record(key, now, repeats)
        if settings.email_enabled and channels.email:
            key = (event, title, "email")
            if key in self._inflight:
                self._repeats[key] = self._repeats.get(key, 0) + 1
                return
            rendered, repeats = self._render_or_suppress(key, body, now)
            if rendered is not None:
                self._inflight.add(key)
                task = tasks_mod.spawn(
                    self._deliver_email,
                    key,
                    settings,
                    title,
                    rendered,
                    repeats,
                    name="notify-email",
                    restart=False,
                    track=lambda t: self.state.tasks.append(t),
                )
                task.add_done_callback(
                    lambda completed: (
                        self.state.tasks.remove(completed)
                        if completed in self.state.tasks
                        else None
                    )
                )

    def _render_or_suppress(
        self, key: tuple[str, str, str], body: str, now: datetime
    ) -> tuple[str | None, int]:
        """(rendered body, repeats folded in) — None suppresses the send."""
        last = self._last_sent.get(key)
        if last is None:
            # Cold start: the alerts table carries the last send across restarts.
            # key is (event, title, channel); last_alert_at wants (event, channel, title).
            session = self.state.session_factory()
            try:
                last = repo.last_alert_at(session, key[0], key[2], key[1])
            finally:
                session.close()
        if last is not None and now - last < timedelta(minutes=30):
            self._repeats[key] = self._repeats.get(key, 0) + 1
            return None, 0
        repeats = self._repeats.pop(key, 0)
        return (f"{body} ({repeats} repeats)" if repeats else body), repeats

    def _record(self, key: tuple[str, str, str], now: datetime, repeats: int) -> None:
        """Persist a sent alert so the 30-minute repeat rule survives restarts."""
        session = self.state.session_factory()
        try:
            repo.add_alert(session, key[0], key[2], key[1], repeats=repeats, at=now)
            session.commit()
        finally:
            session.close()

    async def _deliver_browser(self, event: str, title: str, body: str) -> bool:
        delivered = False
        severity = _TOAST_SEVERITY.get(event, "info")
        payload = json.dumps(
            {
                "type": "alert",
                "data": {
                    "event": event,
                    "severity": severity,
                    "title": title,
                    "body": body,
                },
            },
            separators=(",", ":"),
        )
        for websocket in list(self.state.ws_clients):
            try:
                await websocket.send_text(payload)
                delivered = True
            except Exception:  # noqa: BLE001 - a closed socket shows as any error
                self.state.ws_clients.discard(websocket)
        return delivered

    async def _deliver_email(
        self,
        key: tuple[str, str, str],
        settings: NotificationSettings,
        title: str,
        body: str,
        repeats: int,
    ) -> None:
        try:
            await asyncio.to_thread(
                _send_email,
                settings,
                title,
                body,
                self.state.settings.timeouts.smtp_s,
            )
            self._last_sent[key] = utcnow()
            self._record(key, self._last_sent[key], repeats)
            session = self.state.session_factory()
            try:
                repo.resolve_matching_errors(
                    session,
                    "notification delivery failed",
                    "SMTP delivery recovered",
                )
                session.commit()
            finally:
                session.close()
            if self.state.log_store is not None:
                for p in self.state.log_store.unreviewed_problems():
                    if p.get("event") == "notify.email_failed":
                        self.state.log_store.review_problem(
                            p["fingerprint"],
                            "fixed",
                            "SMTP delivery recovered",
                            utcnow(),
                        )
        except Exception as exc:
            logging.getLogger("notify").exception(
                "notification delivery failed: %s",
                exc,
                extra={"event": "notify.email_failed"},
            )
        finally:
            self._inflight.discard(key)


@router.post("/test", dependencies=[Depends(require_token)])
async def test_notifications(
    state: Annotated[AppState, Depends(get_state)],
) -> dict[str, bool]:
    settings = state.settings.notifications
    browser_sent = False
    if settings.browser_enabled:
        payload = (
            '{"type":"alert","data":{"title":"Trading Agent · test notification",'
            '"body":"Notifications are configured correctly."}}'
        )
        for websocket in list(state.ws_clients):
            try:
                await websocket.send_text(payload)
                browser_sent = True
            except Exception:  # noqa: BLE001 - a closed socket shows as any error
                state.ws_clients.discard(websocket)
    email_sent = False
    if settings.email_enabled:
        if not settings.smtp.host or not settings.smtp.sender or not settings.smtp.recipient:
            raise HTTPException(
                status_code=422, detail="SMTP host, sender and recipient are required"
            )
        try:
            await asyncio.to_thread(
                _send_email,
                settings,
                "Trading Agent · test notification",
                "Notifications are configured correctly.",
                state.settings.timeouts.smtp_s,
            )
            email_sent = True
        except (OSError, smtplib.SMTPException) as exc:
            raise HTTPException(status_code=502, detail=f"SMTP test failed: {exc}") from exc
    return {"browser": browser_sent, "email": email_sent}
