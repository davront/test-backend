import asyncio
import json
import time

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from .models import TestAttempt, AttemptStatus
from .services import sync_expired_attempt

@database_sync_to_async
def sync_expired_attempt_async(attempt_id: int):
    try:
        attempt = TestAttempt.objects.get(pk=attempt_id)
        sync_expired_attempt(attempt)
    except TestAttempt.DoesNotExist:
        pass


def _auth_message_token(text_data):
    """Token from the streamer's first message {"type": "auth", "token": "..."}."""
    try:
        msg = json.loads(text_data or "")
    except (ValueError, TypeError):
        return None
    if isinstance(msg, dict) and msg.get("type") == "auth" and isinstance(msg.get("token"), str):
        return msg["token"]
    return None


_FRAME_PREFIXES = ("data:image/jpeg;base64,", "data:image/png;base64,")
_ACTIVE_CHECK_TTL = 5.0  # сек: проверка статуса попытки не на каждый кадр
_AUTH_TIMEOUT = 10.0  # сек: streamer без auth-сообщения отключается


def _group_name(attempt_id: int) -> str:
    return f"proctoring_{attempt_id}"


def _parse_jwt_user_id(token: str):
    """Extract user_id from JWT, verifying signature via simplejwt."""
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        validated = AccessToken(token)
        user_id = validated.get("user_id")
        if user_id is None:
            return None
        return int(user_id)
    except Exception:
        return None


class ProctoringConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for live proctoring.

    URL: ws/proctoring/<attempt_id>/
    Query params:
      - role=streamer  (test-taker) — first message {"type": "auth", "token": "<JWT>"}
        (legacy: ?token=<JWT>; token in URL ends up in access logs, avoid it)
      - role=watcher   (curator/staff) — authenticates via Django session

    Streamers must own the attempt and it must be IN_PROGRESS.
    Watchers must be staff with testing.view_testattempt.
    """

    async def connect(self):
        self.attempt_id = int(self.scope["url_route"]["kwargs"]["attempt_id"])
        self.group = _group_name(self.attempt_id)
        self.role = self._query_param("role")

        self.authed = False
        if self.role == "streamer":
            token = self._query_param("token")
            if token:
                if not await self._validate_streamer(token):
                    await self.close(code=4403)
                    return
                self.authed = True
        elif self.role == "watcher":
            if not await self._validate_watcher():
                await self.close(code=4403)
                return
        else:
            await self.close(code=4400)
            return

        if self.role == "watcher":
            await self.channel_layer.group_add(self.group, self.channel_name)

        self._deadline_task = None
        self._auth_timeout_task = None
        await self.accept()

        if self.role == "streamer" and not self.authed:
            self._auth_timeout_task = asyncio.ensure_future(self._close_if_not_authed())

        if self.role == "watcher":
            deadline = await self._get_deadline()
            if deadline:
                self._deadline_task = asyncio.ensure_future(self._schedule_expired_notify(deadline))

    async def disconnect(self, code):
        if getattr(self, "_deadline_task", None):
            self._deadline_task.cancel()
        if getattr(self, "_auth_timeout_task", None):
            self._auth_timeout_task.cancel()
        if getattr(self, "role", None) == "watcher":
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if self.role != "streamer":
            return

        if not self.authed:
            token = _auth_message_token(text_data)
            if not token or not await self._validate_streamer(token):
                await self.close(code=4403)
                return
            self.authed = True
            return

        now = time.monotonic()
        if now - getattr(self, "_active_checked_at", float("-inf")) >= _ACTIVE_CHECK_TTL:
            if not await self._is_attempt_active():
                await self.channel_layer.group_send(
                    self.group,
                    {
                        "type": "proctoring.expired",
                        "attempt_id": self.attempt_id,
                    },
                )
                await self.close(code=4408)
                return
            self._active_checked_at = now

        frame = text_data if text_data is not None else bytes_data
        if frame is None:
            return

        if isinstance(frame, bytes):
            import base64
            frame = "data:image/png;base64," + base64.b64encode(frame).decode("ascii")
        elif isinstance(frame, str):
            stripped = frame.strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        candidate = parsed.get("data") or parsed.get("frame")
                        if not isinstance(candidate, str):
                            return  # служебное сообщение, не кадр
                        frame = candidate
                except (ValueError, TypeError):
                    pass
            # Только картинка: иначе watcher загрузит в <img> произвольный URL.
            if not frame.startswith(_FRAME_PREFIXES):
                return

        await self.channel_layer.group_send(
            self.group,
            {
                "type": "proctoring.frame",
                "attempt_id": self.attempt_id,
                "frame": frame,
            },
        )

    async def proctoring_frame(self, event):
        await self.send(text_data=json.dumps({"type": "frame", "data": event["frame"]}))

    async def proctoring_expired(self, event):
        await self.send(text_data=json.dumps({"type": "expired", "attempt_id": event["attempt_id"]}))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _query_param(self, key: str) -> str:
        from urllib.parse import parse_qs
        query_string = self.scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        values = params.get(key)
        return values[0] if values else ""

    @database_sync_to_async
    def _validate_streamer(self, token: str) -> bool:
        user_id = _parse_jwt_user_id(token)
        if not user_id:
            return False
        try:
            attempt = TestAttempt.objects.select_related("test").get(pk=self.attempt_id)
        except TestAttempt.DoesNotExist:
            return False
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return False
        if attempt.is_expired():
            sync_expired_attempt(attempt)
            return False
        return attempt.user_id == user_id

    @database_sync_to_async
    def _validate_watcher(self) -> bool:
        # Watcher authenticates via Django session (admin is already logged in)
        user = self.scope.get("user")
        if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
            return False
        return user.is_staff and user.has_perm("testing.view_testattempt")

    @database_sync_to_async
    def _get_deadline(self):
        attempt = TestAttempt.objects.select_related("test").filter(pk=self.attempt_id).first()
        return attempt.effective_deadline() if attempt else None

    async def _close_if_not_authed(self):
        try:
            await asyncio.sleep(_AUTH_TIMEOUT)
            if not self.authed:
                await self.close(code=4403)
        except asyncio.CancelledError:
            pass

    async def _schedule_expired_notify(self, deadline):
        try:
            now = timezone.now()
            delay = max(0.0, (deadline - now).total_seconds())
            await asyncio.sleep(delay)
            await sync_expired_attempt_async(self.attempt_id)
            await self.send(text_data=json.dumps({"type": "expired", "attempt_id": self.attempt_id}))
            await self.close(1000)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @database_sync_to_async
    def _is_attempt_active(self) -> bool:
        attempt = TestAttempt.objects.select_related("test").filter(pk=self.attempt_id).first()
        if not attempt or attempt.status != AttemptStatus.IN_PROGRESS:
            return False
        if attempt.is_expired():
            sync_expired_attempt(attempt)
            return False
        return True
