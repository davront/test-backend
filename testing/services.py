"""
Логика старта попытки, сохранения ответов и завершения по таймауту.
Частичный результат: каждый ответ пишется в БД сразу; при обрыве/таймауте
уже сохранённые ответы остаются, пересчитывается score_earned.
"""

from __future__ import annotations

import random

from django.db import transaction
from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from django.utils import timezone
from rest_framework.settings import api_settings

from .models import (
    AnswerOption,
    AttemptResponse,
    AttemptStatus,
    Question,
    Test,
    TestAttempt,
    TerminationReason,
)


class AttemptError(Exception):
    """Бизнес-ошибка (нельзя ответить, попытка закрыта и т.д.)."""


def user_can_access_test(user, test: Test) -> bool:
    if not test.is_active:
        return False
    if not test.is_conduct_period_open():
        return False
    groups = test.allowed_groups.all()
    if not groups.exists():
        return True
    return test.allowed_groups.filter(pk__in=user.groups.values_list("pk", flat=True)).exists()


def user_has_finished_attempt_for_test(user, test: Test) -> bool:
    """Есть ли у пользователя завершённая попытка по этому тесту (не «в процессе»)."""
    return TestAttempt.objects.filter(user=user, test=test).exclude(
        status=AttemptStatus.IN_PROGRESS
    ).exists()


def start_attempt(user, test: Test) -> TestAttempt:
    if not getattr(user, "is_staff", False) and not user_can_access_test(user, test):
        raise AttemptError("Нет доступа к этому тесту.")
    if user_has_finished_attempt_for_test(user, test):
        raise AttemptError(
            "Этот тест можно пройти только один раз. Повторное прохождение недоступно."
        )
    now = timezone.now()
    deadline = None
    if test.time_limit_seconds:
        deadline = now + timezone.timedelta(seconds=test.time_limit_seconds)
        # Таймер не обещает больше времени, чем осталось до конца окна проведения
        if test.conduct_ends_at and test.conduct_ends_at < deadline:
            deadline = test.conduct_ends_at

    # Каждая группа — свой пул со своим лимитом; вопросы без группы — отдельный пул.
    # Лимит теста (questions_to_show) применяется к объединённому набору.
    q_ids = []
    for group in test.question_groups.order_by("order", "id"):
        gq_ids = list(group.questions.values_list("pk", flat=True))
        n = group.questions_to_show
        if n and n < len(gq_ids):
            gq_ids = random.sample(gq_ids, n)
        q_ids.extend(gq_ids)
    q_ids.extend(
        test.questions.filter(group__isnull=True)
        .order_by("order", "id")
        .values_list("pk", flat=True)
    )
    n = test.questions_to_show
    if n and n < len(q_ids):
        q_ids = random.sample(q_ids, n)
    else:
        random.shuffle(q_ids)

    max_pts = (
        Question.objects.filter(pk__in=q_ids).aggregate(total=Sum("points"))["total"] or 0
    ) if q_ids else 0

    return TestAttempt.objects.create(
        user=user,
        test=test,
        status=AttemptStatus.IN_PROGRESS,
        deadline_at=deadline,
        score_max=max_pts,
        question_sequence=q_ids,
    )


def _lock_attempt(pk) -> TestAttempt:
    """Блокирует строку попытки (только её, of=self); тест — тем же запросом для is_expired."""
    return (
        TestAttempt.objects.select_related("test")
        .select_for_update(of=("self",))
        .get(pk=pk)
    )


def _ensure_in_progress(attempt: TestAttempt) -> None:
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise AttemptError("Попытка уже завершена.")
    if attempt.is_expired():
        _close_timed_out(attempt)
        raise AttemptError("Время вышло; сохранены ответы, отправленные до дедлайна.")


# Тип поля для score_earned / score_max
_SCORE_FIELD = DecimalField(max_digits=10, decimal_places=2)


def _earned_subquery() -> Coalesce:
    """Сумма баллов за правильные ответы — коррелированный подзапрос."""
    return Coalesce(
        Subquery(
            AttemptResponse.objects
            .filter(attempt_id=OuterRef("pk"), is_correct=True)
            .values("attempt_id")
            .annotate(s=Sum("question__points"))
            .values("s")[:1],
            output_field=_SCORE_FIELD,
        ),
        Value(0, output_field=_SCORE_FIELD),
    )


def _save_scores_atomically(
    attempt: TestAttempt,
    extra_fields: dict | None = None,
) -> None:
    updates: dict = {
        "score_earned": _earned_subquery(),
        **(extra_fields or {}),
    }
    TestAttempt.objects.filter(pk=attempt.pk).update(**updates)
    refresh = ["score_earned"] + [
        k for k in (extra_fields or {}) if k != "score_earned"
    ]
    attempt.refresh_from_db(fields=refresh)


def _close_timed_out(attempt: TestAttempt, now=None) -> None:
    now = now or timezone.now()
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return
    deadline = attempt.effective_deadline()
    _save_scores_atomically(attempt, {
        "status": AttemptStatus.TIMED_OUT,
        "finished_at": min(now, deadline) if deadline else now,
    })


def submit_answer(
    attempt: TestAttempt,
    question: Question,
    selected_option: AnswerOption,
) -> AttemptResponse:
    """Сохранить/обновить ответ на вопрос (upsert по паре попытка+вопрос)."""
    with transaction.atomic():
        attempt = _lock_attempt(attempt.pk)
        if attempt.is_expired():
            _close_timed_out(attempt)
            raise AttemptError("Время истекло.")
        _ensure_in_progress(attempt)
        if question.test_id != attempt.test_id:
            raise AttemptError("Вопрос не из этого теста.")
        if selected_option.question_id != question.id:
            raise AttemptError("Вариант не относится к вопросу.")
        seq = attempt.question_sequence
        if seq is not None and question.id not in seq:
            raise AttemptError("Вопрос не входит в эту попытку.")
        obj, _ = AttemptResponse.objects.update_or_create(
            attempt=attempt,
            question=question,
            defaults={
                "selected_option": selected_option,
                "is_correct": selected_option.is_correct,
            },
        )
        # Попытка не закрывается на последнем ответе: до complete_attempt
        # пользователь может изменить любой ответ. score_earned не пересчитываем —
        # иначе рост балла после ответа раскрывает верный вариант; считается при закрытии.
        return obj


def complete_attempt(attempt: TestAttempt) -> TestAttempt:
    """Нормальное завершение (все вопросы или пользователь нажал «готово»)."""
    with transaction.atomic():
        attempt = _lock_attempt(attempt.pk)
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return attempt
        if attempt.is_expired():
            _close_timed_out(attempt)
            return attempt
        _save_scores_atomically(attempt, {
            "status": AttemptStatus.COMPLETED,
            "finished_at": timezone.now(),
        })
        return attempt


def abandon_attempt(attempt: TestAttempt) -> TestAttempt:
    with transaction.atomic():
        attempt = _lock_attempt(attempt.pk)
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return attempt
        _save_scores_atomically(attempt, {
            "status": AttemptStatus.ABANDONED,
            "finished_at": timezone.now(),
        })
        return attempt


def terminate_attempt(attempt: TestAttempt, reason: str) -> TestAttempt:
    """Принудительное закрытие (смена вкладки/приложения): score_earned = 0."""
    with transaction.atomic():
        attempt = _lock_attempt(attempt.pk)
        if attempt.status != AttemptStatus.IN_PROGRESS:
            return attempt
        # Не пересчитываем score_earned — при нарушении он обнуляется.
        TestAttempt.objects.filter(pk=attempt.pk).update(
            status=AttemptStatus.TERMINATED,
            finished_at=timezone.now(),
            termination_reason=reason,
            score_earned=Value(0, output_field=_SCORE_FIELD),
        )
        attempt.refresh_from_db(
            fields=["status", "finished_at", "termination_reason", "score_earned"]
        )
        return attempt


def sync_expired_attempt(attempt: TestAttempt) -> TestAttempt:
    """Если дедлайн прошёл, закрыть попытку (для фоновых задач / перед ответом)."""
    with transaction.atomic():
        attempt = _lock_attempt(attempt.pk)
        if attempt.status == AttemptStatus.IN_PROGRESS and attempt.is_expired():
            _close_timed_out(attempt)
        return attempt


def get_request_client_ip(request) -> str | None:
    num_proxies = api_settings.NUM_PROXIES
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded and num_proxies:
        # Та же логика, что у DRF throttling: адрес, добавленный ближайшим доверенным прокси.
        addrs = forwarded.split(",")
        return addrs[-min(num_proxies, len(addrs))].strip() or None
    if forwarded and num_proxies is None:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR")
