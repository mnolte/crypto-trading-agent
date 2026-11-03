"""Browser and SMTP notification settings plus the side-effect-free test surface."""

from __future__ import annotations

import asyncio
import json
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import repo
from app.api.deps import AppState, get_state, require_token
from app.config import SecretStoreError, get_secret, secret_status, set_secret
from app.db import utcnow
from app.models import NotificationSettings

router = APIRouter(prefix="/api/notifications")


class SmtpPasswordIn(BaseModel):
    password: str


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
def put_notifications(
    settings: NotificationSettings,
    state: Annotated[AppState, Depends(get_state)],
) -> NotificationSettings:
    state.settings.notifications = settings
    session = state.session_factory()
    try:
        state.settings_version = repo.save_settings(session, state.settings)
    finally:
        session.close()
    return settings


def _send_email(settings: NotificationSettings, subject: str, body: str) -> None:
    smtp = settings.smtp
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.sender
    message["To"] = smtp.recipient
    message.set_content(body)
    client_class = smtplib.SMTP
    with client_class(smtp.host, smtp.port, timeout=10) as client:
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
        task = asyncio.create_task(self.notify(event, title, body))
        self.state.tasks.append(task)
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
            if rendered is not None and await self._deliver_browser(title, rendered):
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
                task = asyncio.create_task(
                    self._deliver_email(key, settings, title, rendered, repeats)
                )
                self.state.tasks.append(task)
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

    async def _deliver_browser(self, title: str, body: str) -> bool:
        delivered = False
        payload = json.dumps(
            {"type": "alert", "data": {"title": title, "body": body}},
            separators=(",", ":"),
        )
        for websocket in list(self.state.ws_clients):
            try:
                await websocket.send_text(payload)
                delivered = True
            except Exception:
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
            await asyncio.to_thread(_send_email, settings, title, body)
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
        except Exception:
            session = self.state.session_factory()
            try:
                repo.add_activity(session, "error", "notification delivery failed")
                session.commit()
            finally:
                session.close()
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
            except Exception:
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
            )
            email_sent = True
        except (OSError, smtplib.SMTPException) as exc:
            raise HTTPException(status_code=502, detail=f"SMTP test failed: {exc}") from exc
    return {"browser": browser_sent, "email": email_sent}
