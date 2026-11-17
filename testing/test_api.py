from datetime import timedelta

from channels.db import database_sync_to_async
from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .constants import TEST_CURATOR_GROUP
from .services import sync_expired_attempt
from .models import (
    AnswerOption,
    AttemptResponse,
    AttemptSessionEvent,
    AttemptStatus,
    Question,
    Test,
    TestAttempt,
)


class ApiFlowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="student", password="pass12345")
        self.staff = User.objects.create_user(
            username="admin", password="pass12345", is_staff=True
        )
        self.dept = Group.objects.create(name="Отдел А")
        self.user.groups.add(self.dept)

        self.test_obj = Test.objects.create(
            title="API тест",
            time_limit_seconds=600,
            is_active=True,
        )
        self.test_obj.allowed_groups.add(self.dept)

        q = Question.objects.create(
            test=self.test_obj, text="2+2", order=1, points=1
        )
        self.opt_ok = AnswerOption.objects.create(
            question=q, text="4", is_correct=True
        )
        AnswerOption.objects.create(question=q, text="5", is_correct=False)

    def _login(self, username, password):
        url = reverse("api-login")
        r = self.client.post(
            url, {"username": username, "password": password}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("access", r.data)
        self.assertIn("user", r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")

    def test_login_and_me(self):
        self._login("student", "pass12345")
        r = self.client.get(reverse("api-me"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["username"], "student")
        self.assertEqual(r.data["full_name"], "student")
        self.assertEqual(len(r.data["departments"]), 1)
        self.assertEqual(r.data["departments"][0]["name"], "Отдел А")

    def test_patch_me(self):
        self._login("student", "pass12345")
        r = self.client.patch(
            reverse("api-me"),
            {"first_name": "Иван", "email": "ivan@example.com"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Иван")
        me = self.client.get(reverse("api-me"))
        self.assertEqual(me.data["full_name"], "Иван")

    def test_tests_list_and_detail_no_correct_flags(self):
        self._login("student", "pass12345")
        r = self.client.get(reverse("api-test-list"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["count"], 1)

        rid = r.data["results"][0]["id"]
        self.client.post(reverse("api-test-start-attempt", kwargs={"pk": rid}))
        d = self.client.get(reverse("api-test-detail", kwargs={"pk": rid}))
        self.assertEqual(d.status_code, status.HTTP_200_OK)
        opt = d.data["questions"][0]["options"][0]
        self.assertIn("id", opt)
        self.assertIn("text", opt)
        self.assertNotIn("is_correct", opt)

    def test_session_event_logging_during_attempt(self):
        self._login("student", "pass12345")
        r = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        aid = r.data["id"]

        # page_hidden — сохраняется и немедленно терминирует попытку
        ev = self.client.post(
            reverse("api-attempt-session-event", kwargs={"pk": aid}),
            {
                "event_type": "page_hidden",
                "leave_count": 1,
                "duration_away_ms": None,
                "meta": {"visibilityState": "hidden"},
            },
            format="json",
        )
        self.assertEqual(ev.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ev.data["event_type"], "page_hidden")
        self.assertTrue(ev.data["attempt_terminated"])
        row = AttemptSessionEvent.objects.get(pk=ev.data["id"])
        self.assertEqual(row.attempt_id, aid)
        self.assertEqual(row.user_id, self.user.pk)
        self.assertEqual(row.leave_count, 1)

        # Попытка уже TERMINATED — любые новые события отклоняются
        vis = self.client.post(
            reverse("api-attempt-session-event", kwargs={"pk": aid}),
            {"event_type": "page_visible", "leave_count": 1, "duration_away_ms": 4200},
            format="json",
        )
        self.assertEqual(vis.status_code, status.HTTP_400_BAD_REQUEST)

        # В истории только одно событие (page_hidden)
        detail = self.client.get(reverse("api-attempt-detail", kwargs={"pk": aid}))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertIn("session_events", detail.data)
        self.assertEqual(len(detail.data["session_events"]), 1)
        self.assertEqual(detail.data["session_events"][0]["event_type"], "page_hidden")
        self.assertEqual(detail.data["status"], "terminated")

        # Повторная проверка: закрытая попытка по-прежнему отклоняет события
        closed = self.client.post(
            reverse("api-attempt-session-event", kwargs={"pk": aid}),
            {"event_type": "page_hidden"},
            format="json",
        )
        self.assertEqual(closed.status_code, status.HTTP_400_BAD_REQUEST)

    def test_start_submit_complete_flow(self):
        self._login("student", "pass12345")
        r = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        aid = r.data["id"]

        r2 = self.client.post(
            reverse("api-attempt-answer", kwargs={"pk": aid}),
            {
                "question_id": self.test_obj.questions.first().pk,
                "option_id": self.opt_ok.pk,
            },
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)

        r3 = self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid}))
        self.assertEqual(r3.status_code, status.HTTP_200_OK)
        self.assertEqual(r3.data["status"], "completed")

    def test_cannot_start_second_attempt_after_complete(self):
        self._login("student", "pass12345")
        r = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        aid = r.data["id"]
        self.client.post(
            reverse("api-attempt-answer", kwargs={"pk": aid}),
            {
                "question_id": self.test_obj.questions.first().pk,
                "option_id": self.opt_ok.pk,
            },
            format="json",
        )
        detail = self.client.get(reverse("api-attempt-detail", kwargs={"pk": aid}))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        # Последний ответ не закрывает попытку — нужен явный complete.
        self.assertEqual(detail.data["status"], "in_progress")
        self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid}))
        r2 = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("один", r2.data.get("detail", "").lower())

    def test_attempt_list_staff_sees_all(self):
        self._login("student", "pass12345")
        self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.client.credentials()
        self._login("admin", "pass12345")
        r = self.client.get(reverse("api-attempt-list"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(r.data["count"], 1)

    def test_staff_create_test_includes_correct_flags(self):
        self._login("admin", "pass12345")
        payload = {
            "title": "Из API",
            "description": "",
            "time_limit_seconds": 120,
            "is_active": True,
            "allowed_group_ids": [self.dept.pk],
            "questions": [
                {
                    "text": "2+2=?",
                    "order": 1,
                    "points": 1,
                    "options": [
                        {"text": "4", "is_correct": True},
                        {"text": "5", "is_correct": False},
                        {"text": "3", "is_correct": False},
                        {"text": "22", "is_correct": False},
                    ],
                }
            ],
        }
        r = self.client.post(reverse("api-test-list"), payload, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        opt = r.data["questions"][0]["options"][0]
        self.assertIn("is_correct", opt)
        self.assertTrue(opt["is_correct"])

        tid = r.data["id"]
        self.client.credentials()
        self._login("student", "pass12345")
        self.client.post(reverse("api-test-start-attempt", kwargs={"pk": tid}))
        d = self.client.get(reverse("api-test-detail", kwargs={"pk": tid}))
        self.assertEqual(d.status_code, status.HTTP_200_OK)
        opt2 = d.data["questions"][0]["options"][0]
        self.assertNotIn("is_correct", opt2)

    def test_staff_also_cannot_start_second_attempt(self):
        self._login("admin", "pass12345")
        r1 = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        aid = r1.data["id"]
        self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid}))
        r2 = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resume_returns_progress_and_next_question(self):
        q2 = Question.objects.create(
            test=self.test_obj, text="3+3", order=2, points=1
        )
        q2_ok = AnswerOption.objects.create(question=q2, text="6", is_correct=True)
        AnswerOption.objects.create(question=q2, text="7", is_correct=False)

        self._login("student", "pass12345")
        started = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        aid = started.data["id"]
        nq = started.data["next_question"]
        self.assertIsNotNone(nq)
        first_qid = nq["id"]
        correct_opt = AnswerOption.objects.get(question_id=first_qid, is_correct=True)

        answer = self.client.post(
            reverse("api-attempt-answer", kwargs={"pk": aid}),
            {
                "question_id": first_qid,
                "option_id": correct_opt.pk,
            },
            format="json",
        )
        self.assertEqual(answer.status_code, status.HTTP_200_OK)

        # Имитируем повторный вход: получаем текущую попытку по тому же старт-эндпоинту.
        self.client.credentials()
        self._login("student", "pass12345")
        resumed = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        )
        self.assertEqual(resumed.status_code, status.HTTP_200_OK)
        self.assertEqual(resumed.data["id"], aid)
        self.assertEqual(resumed.data["questions_total"], 2)
        self.assertEqual(resumed.data["questions_answered"], 1)
        self.assertEqual(len(resumed.data["responses"]), 1)
        self.assertEqual(
            resumed.data["responses"][0]["selected_option_id"], correct_opt.pk
        )
        second_id = resumed.data["next_question"]["id"]
        self.assertNotEqual(second_id, first_qid)
        self.assertEqual(
            {first_qid, second_id},
            set(self.test_obj.questions.values_list("pk", flat=True)),
        )
        option_ids = [o["id"] for o in resumed.data["next_question"]["options"]]
        other_correct = AnswerOption.objects.get(question_id=second_id, is_correct=True)
        self.assertIn(other_correct.id, option_ids)

    def test_last_answer_can_be_changed_before_complete(self):
        self._login("student", "pass12345")
        aid = self.client.post(
            reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk})
        ).data["id"]
        q = self.test_obj.questions.first()
        wrong = q.options.get(is_correct=False)
        url = reverse("api-attempt-answer", kwargs={"pk": aid})
        r1 = self.client.post(url, {"question_id": q.pk, "option_id": wrong.pk}, format="json")
        self.assertEqual(r1.data["status"], "in_progress")
        r2 = self.client.post(url, {"question_id": q.pk, "option_id": self.opt_ok.pk}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        done = self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid}))
        self.assertEqual(done.data["status"], "completed")
        self.assertEqual(float(done.data["score_earned"]), 1.0)


class IntegrityAndPermissionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="emp", password="pass12345")
        self.test_obj = Test.objects.create(title="Integrity", is_active=True)
        self.questions = []
        for i in range(3):
            q = Question.objects.create(test=self.test_obj, text=f"Q{i}", order=i, points=1)
            AnswerOption.objects.create(question=q, text="ok", is_correct=True)
            AnswerOption.objects.create(question=q, text="no", is_correct=False)
            self.questions.append(q)

    def _start(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(reverse("api-test-start-attempt", kwargs={"pk": self.test_obj.pk}))
        return r.data["id"]

    def _answer(self, aid, q, correct=True):
        opt = q.options.get(is_correct=correct)
        return self.client.post(
            reverse("api-attempt-answer", kwargs={"pk": aid}),
            {"question_id": q.pk, "option_id": opt.pk},
            format="json",
        )

    def test_score_hidden_while_in_progress(self):
        aid = self._start()
        r = self._answer(aid, self.questions[0])
        self.assertIsNone(r.data["score_earned"])
        self.assertIsNone(self.client.get(reverse("api-attempt-list")).data["results"][0]["score_earned"])
        self.assertEqual(TestAttempt.objects.get(pk=aid).score_earned, 0)
        done = self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid}))
        self.assertEqual(float(done.data["score_earned"]), 1.0)

    def test_answer_outside_question_sequence_rejected(self):
        self.test_obj.questions_to_show = 1
        self.test_obj.save()
        aid = self._start()
        seq = TestAttempt.objects.get(pk=aid).question_sequence
        outside = next(q for q in self.questions if q.pk not in seq)
        r = self._answer(aid, outside)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(AttemptResponse.objects.filter(attempt_id=aid).exists())

    def test_detail_hides_questions_until_attempt_started(self):
        self.client.force_authenticate(self.user)
        url = reverse("api-test-detail", kwargs={"pk": self.test_obj.pk})
        before = self.client.get(url)
        self.assertEqual(before.data["questions"], [])
        self.assertEqual(before.data["question_count"], 3)
        self._start()
        self.assertEqual(len(self.client.get(url).data["questions"]), 3)

    def test_edit_cannot_wipe_answer_history(self):
        aid = self._start()
        self._answer(aid, self.questions[0])
        staff = User.objects.create_user(username="ed", password="pass12345", is_staff=True)
        self.client.force_authenticate(staff)
        url = reverse("api-test-detail", kwargs={"pk": self.test_obj.pk})
        # Варианты без id — старые удалились бы вместе с ответом.
        payload = {
            "title": "Integrity",
            "questions": [
                {
                    "id": q.pk, "text": q.text, "order": q.order, "points": 1,
                    "options": [{"text": o.text, "is_correct": o.is_correct} for o in q.options.all()],
                }
                for q in self.questions
            ],
        }
        self.assertEqual(self.client.put(url, payload, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        # PATCH с частью вопросов не удаляет остальные.
        patch = {"questions": [{
            "id": self.questions[1].pk, "text": "new", "order": 1, "points": 1,
            "options": [{"id": o.pk, "text": o.text, "is_correct": o.is_correct}
                        for o in self.questions[1].options.all()],
        }]}
        self.assertEqual(self.client.patch(url, patch, format="json").status_code, status.HTTP_200_OK)
        self.assertEqual(self.test_obj.questions.count(), 3)
        self.assertEqual(AttemptResponse.objects.filter(attempt_id=aid).count(), 1)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_409_CONFLICT)

    def test_staff_cannot_act_on_foreign_attempt(self):
        aid = self._start()
        staff = User.objects.create_user(username="st", password="pass12345", is_staff=True)
        self.client.force_authenticate(staff)
        self.assertEqual(
            self.client.get(reverse("api-attempt-detail", kwargs={"pk": aid})).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(self._answer(aid, self.questions[0]).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.post(reverse("api-attempt-complete", kwargs={"pk": aid})).status_code,
            status.HTTP_403_FORBIDDEN,
        )


    def test_staff_taking_test_sees_only_own_sequence(self):
        self.test_obj.questions_to_show = 1
        self.test_obj.save()
        self.user.is_staff = True
        self.user.save()
        aid = self._start()
        seq = TestAttempt.objects.get(pk=aid).question_sequence
        d = self.client.get(reverse("api-test-detail", kwargs={"pk": self.test_obj.pk}))
        self.assertEqual([q["id"] for q in d.data["questions"]], seq)

    def test_untimed_attempt_closes_when_conduct_period_ends(self):
        aid = self._start()
        self._answer(aid, self.questions[0])
        self._answer(aid, self.questions[1], correct=False)
        ends = timezone.now() - timedelta(seconds=1)
        Test.objects.filter(pk=self.test_obj.pk).update(conduct_ends_at=ends)
        attempt = TestAttempt.objects.get(pk=aid)
        self.assertIsNone(attempt.deadline_at)
        sync_expired_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.TIMED_OUT)
        self.assertEqual(float(attempt.score_earned), 1.0)
        self.assertEqual(attempt.finished_at, ends)

    def test_page_unload_is_recorded_without_termination(self):
        aid = self._start()
        r = self.client.post(
            reverse("api-attempt-session-event", kwargs={"pk": aid}),
            {"event_type": "page_unload"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertFalse(r.data["attempt_terminated"])
        self.assertEqual(TestAttempt.objects.get(pk=aid).status, AttemptStatus.IN_PROGRESS)

    def test_export_keeps_formula_like_text_as_string(self):
        import io

        import openpyxl

        from .admin import export_attempts_xlsx

        self.user.first_name = '=HYPERLINK("http://evil","x")'
        self.user.username = "+998901234567"
        self.user.save()
        aid = self._start()
        resp = export_attempts_xlsx(None, None, TestAttempt.objects.filter(pk=aid))
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual(ws["C2"].data_type, "s")
        self.assertEqual(ws["C2"].value, '=HYPERLINK("http://evil","x")')
        self.assertEqual(ws["D2"].value, "+998901234567")


class AdminPermissionTests(TestCase):
    def test_non_superuser_cannot_edit_privilege_flags(self):
        from django.contrib import admin as dj_admin
        from django.test import RequestFactory

        editor = User.objects.create_user("editor", password="x", is_staff=True)
        editor.user_permissions.add(*Permission.objects.filter(codename__in=("add_user", "change_user")))
        editor = User.objects.get(pk=editor.pk)
        emp = User.objects.create_user("emp3", password="x")
        request = RequestFactory().get("/")
        request.user = editor
        model_admin = dj_admin.site._registry[User]
        ro = model_admin.get_readonly_fields(request, emp)
        for name in ("is_staff", "is_superuser", "user_permissions"):
            self.assertIn(name, ro)
        self.assertNotIn("is_superuser", model_admin.get_form(request, emp).base_fields)

    def test_curator_cannot_touch_staff_accounts(self):
        root = User.objects.create_superuser("root", password="Root-pass-123")
        curator = User.objects.create_user("cur", password="x", is_staff=True)
        curator.groups.add(Group.objects.get(name=TEST_CURATOR_GROUP))
        emp = User.objects.create_user("emp2", password="x")
        self.client.force_login(curator)
        r = self.client.post(
            reverse("admin:auth_user_password_change", args=[root.pk]),
            {"password1": "Hacked-pw-9876", "password2": "Hacked-pw-9876", "usable_password": "true"},
        )
        self.assertNotEqual(r.status_code, 302)
        root.refresh_from_db()
        self.assertTrue(root.check_password("Root-pass-123"))
        g = Group.objects.get(name=TEST_CURATOR_GROUP)
        before = g.permissions.count()
        all_perms = list(Permission.objects.values_list("pk", flat=True))
        self.client.post(
            reverse("admin:auth_group_change", args=[g.pk]),
            {"name": g.name, "permissions": all_perms},
        )
        self.assertEqual(g.permissions.count(), before)
        # Обычный сотрудник по-прежнему редактируется куратором.
        self.assertEqual(
            self.client.get(reverse("admin:auth_user_change", args=[emp.pk])).status_code, 200
        )

    def test_staff_without_perms_cannot_import_or_proctor(self):
        plain = User.objects.create_user("plain", password="x", is_staff=True)
        self.client.force_login(plain)
        self.assertEqual(self.client.get(reverse("admin:testing_test_import")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:testing_test_proctoring")).status_code, 403)
        root = User.objects.create_superuser("root2", password="x")
        self.client.force_login(root)
        self.assertEqual(self.client.get(reverse("admin:testing_test_import")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:testing_test_proctoring")).status_code, 200)


class ProctoringConsumerTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cam", password="pass12345")
        test = Test.objects.create(title="WS", is_active=True)
        self.attempt = TestAttempt.objects.create(user=self.user, test=test)

    async def _connect(self):
        from channels.testing import WebsocketCommunicator

        from config.routing import application

        comm = WebsocketCommunicator(
            application,
            f"/ws/proctoring/{self.attempt.pk}/?role=streamer",
            headers=[(b"host", b"localhost"), (b"origin", b"http://localhost")],
        )
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        return comm

    async def test_streamer_auth_via_first_message(self):
        from rest_framework_simplejwt.tokens import AccessToken

        token = str(await database_sync_to_async(AccessToken.for_user)(self.user))
        comm = await self._connect()
        await comm.send_json_to({"type": "auth", "token": token})
        self.assertTrue(await comm.receive_nothing())  # не закрыт
        await comm.disconnect()

    async def test_streamer_without_auth_is_closed_after_timeout(self):
        from unittest import mock

        with mock.patch("testing.consumers._AUTH_TIMEOUT", 0.05):
            comm = await self._connect()
            out = await comm.receive_output(timeout=1)
        self.assertEqual(out, {"type": "websocket.close", "code": 4403})
        await comm.disconnect()

    async def test_streamer_bad_first_message_closes(self):
        comm = await self._connect()
        await comm.send_json_to({"type": "frame", "data": "x"})
        out = await comm.receive_output()
        self.assertEqual(out, {"type": "websocket.close", "code": 4403})
        await comm.disconnect()
