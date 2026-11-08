"""Uji logika inti dengan klien Claude palsu (tanpa jaringan). Jalankan: python -m unittest -v"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.chunking import chunk_text, pick_context
from app.config import Settings
from app.generator import QuestionGenerator
from app.main import app
from app.prompts import TOOL_NAME
from app import store
from app.store import SESSIONS, USERS
from app.validation import validate_question

MATERI = open("sample_materi.txt", encoding="utf-8").read()
SET = Settings(api_key="x", model="fake", grounding_threshold=0.6, max_retries=2)

GOOD_MCQ = {
    "type": "mcq", "body": "Apa fungsi primary key dalam sebuah tabel?",
    "options": ["Mengidentifikasi setiap baris secara unik", "Mempercepat pencarian", "Menghapus data", "Menghubungkan dua tabel"],
    "answer": "Mengidentifikasi setiap baris secara unik",
    "rationale": "Teks menyatakan primary key adalah atribut yang mengidentifikasi setiap baris dalam tabel secara unik.",
    "bloom_level": "C1 · Mengingat", "source_chunk": 0,
}
GOOD_SHORT = {
    "type": "short", "body": "Properti transaksi yang berarti semua operasi berhasil atau seluruhnya dibatalkan disebut?",
    "options": None, "answer": "Atomicity",
    "rationale": "Teks menjelaskan atomicity berarti seluruh operasi dalam transaksi berhasil atau seluruhnya dibatalkan.",
    "bloom_level": "C1 · Mengingat", "source_chunk": 0,
}
HALLUCINATED = {**GOOD_MCQ, "body": "Siapa pencipta bahasa SQL?",
                "options": ["Edgar Codd", "Donald Chamberlin", "Larry Ellison", "Michael Stonebraker"],
                "answer": "Donald Chamberlin"}


def reply(questions):
    block = NS(type="tool_use", name=TOOL_NAME, input={"questions": questions})
    return NS(content=[block], usage=NS(input_tokens=100, output_tokens=50))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = NS(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def run(coro):
    return asyncio.run(coro)


class Chunking(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(len(chunk_text(MATERI)), 1)

    def test_long_text_overlaps(self):
        words = " ".join(f"w{i}" for i in range(1000))
        ch = chunk_text(words, 400, 50)
        self.assertGreater(len(ch), 2)
        self.assertEqual(ch[0].split()[-50:], ch[1].split()[:50])
        self.assertTrue(all(len(c.split()) <= 400 for c in ch))

    def test_pick_context_spread(self):
        ctx = pick_context([f"c{i}" for i in range(10)], 5)
        idxs = [i for i, _ in ctx]
        self.assertEqual(idxs[0], 0)
        self.assertEqual(idxs[-1], 9)
        self.assertEqual(len(idxs), 5)


class Validation(unittest.TestCase):
    chunks = {0: MATERI}

    def v(self, q, t="mix", acc=()):
        return validate_question(q, self.chunks, t, list(acc), 0.6)

    def test_good(self):
        self.assertEqual(self.v(GOOD_MCQ), [])
        self.assertEqual(self.v(GOOD_SHORT), [])

    def test_hallucination_rejected(self):
        self.assertTrue(any("halusinasi" in e for e in self.v(HALLUCINATED)))

    def test_mcq_rules(self):
        self.assertTrue(self.v({**GOOD_MCQ, "options": GOOD_MCQ["options"][:3]}))
        self.assertTrue(self.v({**GOOD_MCQ, "options": ["a", "a", "b", "c"]}))
        self.assertTrue(self.v({**GOOD_MCQ, "answer": "Tidak ada di opsi"}))

    def test_short_rules(self):
        self.assertTrue(self.v({**GOOD_SHORT, "answer": "satu dua tiga empat lima enam"}))

    def test_type_mismatch_and_duplicate(self):
        self.assertTrue(self.v(GOOD_SHORT, t="mcq"))
        self.assertTrue(self.v(GOOD_MCQ, acc=[GOOD_MCQ["body"]]))

    def test_bad_source_chunk(self):
        self.assertTrue(self.v({**GOOD_MCQ, "source_chunk": 99}))


class ProfileApi(unittest.TestCase):
    def setUp(self):
        self.temp_directory = TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        database_path_patch = patch.object(
            store,
            "DATABASE_PATH",
            Path(self.temp_directory.name) / "quto_test.db",
        )
        database_path_patch.start()
        self.addCleanup(database_path_patch.stop)
        SESSIONS.clear()
        USERS["student-1"]["name"] = "Budi Santoso"
        USERS["student-1"]["email"] = "mahasiswa@quto.id"
        USERS["student-1"]["password"] = "mahasiswa123"
        SESSIONS["token-student"] = "student-1"

    def test_update_profile_success(self):
        client = TestClient(app)
        response = client.patch(
            "/api/auth/profile",
            json={"name": "Budi Santoso Baru", "email": "budi@kampus.ac.id"},
            headers={"Authorization": "Bearer token-student"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Budi Santoso Baru")
        self.assertEqual(response.json()["email"], "budi@kampus.ac.id")
        self.assertEqual(USERS["student-1"]["name"], "Budi Santoso Baru")
        self.assertTrue(store.is_password_hash(USERS["student-1"]["password"]))
        self.assertTrue(store.verify_password("mahasiswa123", USERS["student-1"]["password"]))

    def test_demo_password_is_hashed_after_login(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"email": "mahasiswa@quto.id", "password": "mahasiswa123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(store.is_password_hash(USERS["student-1"]["password"]))

    def test_login_uses_selected_lecturer_role(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"email": "dosen@quto.id", "password": "dosen123", "role": "lecturer"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "lecturer")

    def test_login_rejects_role_mismatch(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"email": "mahasiswa@quto.id", "password": "mahasiswa123", "role": "lecturer"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "role_mismatch")

    def test_registered_user_persists_and_can_login_after_reload(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/register",
            json={
                "name": "Pengguna Persisten",
                "email": "persisten@quto.id",
                "password": "password123",
                "role": "student",
            },
        )
        self.assertEqual(response.status_code, 200)
        user_id = response.json()["user"]["id"]
        self.assertNotEqual(USERS[user_id]["password"], "password123")
        self.assertTrue(store.verify_password("password123", USERS[user_id]["password"]))

        USERS.pop(user_id)
        SESSIONS.clear()
        store.load_persisted_users()
        login_response = client.post(
            "/api/auth/login",
            json={"email": "persisten@quto.id", "password": "password123"},
        )

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["user"]["id"], user_id)
        USERS.pop(user_id, None)
        SESSIONS.clear()

    def test_public_registration_creates_students_and_rejects_lecturers(self):
        client = TestClient(app)
        student_response = client.post(
            "/api/auth/register",
            json={
                "name": "Mahasiswa Baru",
                "email": "mahasiswa.baru@kampus.ac.id",
                "password": "password123",
            },
        )
        self.assertEqual(student_response.status_code, 200)
        user_id = student_response.json()["user"]["id"]
        self.assertEqual(student_response.json()["user"]["role"], "student")
        self.addCleanup(USERS.pop, user_id, None)

        lecturer_response = client.post(
            "/api/auth/register",
            json={
                "name": "Dosen Baru",
                "email": "dosen.baru@kampus.ac.id",
                "password": "password123",
                "role": "lecturer",
            },
        )
        self.assertEqual(lecturer_response.status_code, 403)
        self.assertEqual(lecturer_response.json()["code"], "lecturer_invitation_required")
        self.assertNotIn(
            "dosen.baru@kampus.ac.id",
            [user["email"] for user in USERS.values()],
        )

    def test_public_registration_cannot_overwrite_demo_account(self):
        original = dict(USERS["student-1"])
        self.addCleanup(USERS.__setitem__, "student-1", original)
        response = TestClient(app).post(
            "/api/auth/register",
            json={
                "name": "Akun Diambil Alih",
                "email": "mahasiswa@quto.id",
                "password": "password123",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(USERS["student-1"]["name"], original["name"])
        self.assertEqual(USERS["student-1"]["password"], original["password"])

    def test_admin_can_issue_email_bound_one_time_lecturer_invitation(self):
        client = TestClient(app)
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
            admin_api_key="test-admin-key",
        )
        with patch("app.main.get_settings", return_value=settings):
            unauthorized = client.post(
                "/api/admin/lecturer-invitations",
                json={"email": "dosen.baru@kampus.ac.id"},
            )
            self.assertEqual(unauthorized.status_code, 401)

            invitation_response = client.post(
                "/api/admin/lecturer-invitations",
                json={"email": "Dosen.Baru@kampus.ac.id"},
                headers={"X-Admin-Key": "test-admin-key"},
            )
        self.assertEqual(invitation_response.status_code, 200)
        invitation = invitation_response.json()
        self.assertEqual(invitation["email"], "dosen.baru@kampus.ac.id")
        self.assertTrue(invitation["invitation_token"])

        mismatched_email = client.post(
            "/api/auth/register/lecturer",
            json={
                "name": "Dosen Baru",
                "email": "email.lain@kampus.ac.id",
                "password": "password123",
                "invitation_token": invitation["invitation_token"],
            },
        )
        self.assertEqual(mismatched_email.status_code, 403)
        self.assertEqual(
            mismatched_email.json()["code"],
            "invitation_email_mismatch",
        )

        registration = client.post(
            "/api/auth/register/lecturer",
            json={
                "name": "Dosen Baru",
                "email": invitation["email"],
                "password": "password123",
                "invitation_token": invitation["invitation_token"],
            },
        )
        self.assertEqual(registration.status_code, 200)
        self.assertEqual(registration.json()["user"]["role"], "lecturer")
        lecturer_id = registration.json()["user"]["id"]
        self.addCleanup(USERS.pop, lecturer_id, None)
        self.assertTrue(
            store.verify_password("password123", USERS[lecturer_id]["password"])
        )

        reused_invitation = client.post(
            "/api/auth/register/lecturer",
            json={
                "name": "Dosen Lain",
                "email": invitation["email"],
                "password": "password123",
                "invitation_token": invitation["invitation_token"],
            },
        )
        self.assertEqual(reused_invitation.status_code, 400)
        self.assertEqual(
            reused_invitation.json()["code"],
            "invalid_invitation",
        )

    def test_expired_lecturer_invitation_is_rejected(self):
        invitation_token = store.new_id()
        store.issue_lecturer_invitation(
            "dosen.kadaluarsa@kampus.ac.id",
            invitation_token,
            int(time.time()) - 1,
        )
        response = TestClient(app).post(
            "/api/auth/register/lecturer",
            json={
                "name": "Dosen Kedaluwarsa",
                "email": "dosen.kadaluarsa@kampus.ac.id",
                "password": "password123",
                "invitation_token": invitation_token,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invitation_expired")

    def test_lecturer_invitations_are_disabled_without_admin_secret(self):
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
        )
        with patch("app.main.get_settings", return_value=settings):
            response = TestClient(app).post(
                "/api/admin/lecturer-invitations",
                json={"email": "dosen.baru@kampus.ac.id"},
                headers={"X-Admin-Key": "unconfigured"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "admin_invitations_disabled")

    def test_local_demo_lecturer_can_register_and_login(self):
        client = TestClient(app, client=("127.0.0.1", 12345))
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
            enable_demo_lecturer_registration=True,
        )
        with patch("app.main.get_settings", return_value=settings):
            invitation_response = client.post(
                "/api/auth/demo/lecturer-invitation",
            )
        self.assertEqual(invitation_response.status_code, 200)
        invitation = invitation_response.json()
        self.assertEqual(invitation["email"], "dosen.demo@quto.id")
        self.assertEqual(invitation["name"], "Dosen Demo")
        self.assertEqual(invitation["demo_password"], "dosen123")

        registration = client.post(
            "/api/auth/register/lecturer",
            json={
                "name": invitation["name"],
                "email": invitation["email"],
                "password": invitation["demo_password"],
                "invitation_token": invitation["invitation_token"],
            },
        )
        self.assertEqual(registration.status_code, 200)
        self.assertEqual(registration.json()["user"]["role"], "lecturer")
        user_id = registration.json()["user"]["id"]
        session_token = registration.json()["token"]
        self.addCleanup(USERS.pop, user_id, None)
        self.addCleanup(SESSIONS.pop, session_token, None)

        login = client.post(
            "/api/auth/login",
            json={
                "email": invitation["email"],
                "password": invitation["demo_password"],
            },
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["role"], "lecturer")

        with patch("app.main.get_settings", return_value=settings):
            second_invitation = client.post(
                "/api/auth/demo/lecturer-invitation",
            )
        self.assertEqual(second_invitation.status_code, 409)
        self.assertEqual(
            second_invitation.json()["code"],
            "demo_lecturer_already_registered",
        )

    def test_local_demo_lecturer_registration_rejects_remote_clients(self):
        client = TestClient(app, client=("192.0.2.10", 12345))
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
            enable_demo_lecturer_registration=True,
        )
        with patch("app.main.get_settings", return_value=settings):
            response = client.post("/api/auth/demo/lecturer-invitation")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "local_demo_only")

    def test_local_demo_lecturer_registration_requires_explicit_opt_in(self):
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
        )
        client = TestClient(app, client=("127.0.0.1", 12345))
        with patch("app.main.get_settings", return_value=settings):
            response = client.post("/api/auth/demo/lecturer-invitation")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["code"],
            "demo_lecturer_registration_disabled",
        )

    def test_update_profile_rejects_duplicate_email(self):
        USERS["lecturer-1"]["email"] = "dosen@quto.id"
        client = TestClient(app)
        response = client.patch(
            "/api/auth/profile",
            json={"name": "Budi Santoso", "email": "dosen@quto.id"},
            headers={"Authorization": "Bearer token-student"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("email", response.json()["message"].lower())

    def test_reset_password_success(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/reset-password",
            json={"email": "mahasiswa@quto.id", "password": "baru12345"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Password berhasil diubah. Silakan masuk kembali.")
        self.assertTrue(store.verify_password("baru12345", USERS["student-1"]["password"]))
        self.assertNotIn("token-student", SESSIONS)

        USERS.pop("student-1")
        store.load_persisted_users()
        login_response = client.post(
            "/api/auth/login",
            json={"email": "mahasiswa@quto.id", "password": "baru12345"},
        )
        self.assertEqual(login_response.status_code, 200)

    def test_reset_password_disabled_for_public_demo(self):
        settings = Settings(
            api_key=None,
            model="fake",
            grounding_threshold=0.6,
            max_retries=2,
            disable_password_reset=True,
        )
        with patch("app.main.get_settings", return_value=settings):
            response = TestClient(app).post(
                "/api/auth/reset-password",
                json={"email": "mahasiswa@quto.id", "password": "baru12345"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "password_reset_disabled")
        self.assertEqual(USERS["student-1"]["password"], "mahasiswa123")

    def test_reset_password_rejects_short_password(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/reset-password",
            json={"email": "mahasiswa@quto.id", "password": "123"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(USERS["student-1"]["password"], "mahasiswa123")

    def test_reset_password_rejects_unknown_email(self):
        client = TestClient(app)
        response = client.post(
            "/api/auth/reset-password",
            json={"email": "tidak-terdaftar@quto.id", "password": "password123"},
        )
        self.assertEqual(response.status_code, 404)

    def test_lecturer_can_view_quiz_submissions(self):
        submission_id = "submission-review-test"
        SESSIONS["token-lecturer"] = "lecturer-1"
        store.SUBMISSIONS[submission_id] = {
            "id": submission_id,
            "quiz_id": "quiz-primary-key",
            "student_id": "student-1",
            "score": 50,
            "correct": 1,
            "total": 2,
            "answers": {
                "question-1": {
                    "given": "Mengidentifikasi setiap baris secara unik",
                    "correct": True,
                },
                "question-2": {"given": "Salah", "correct": False},
            },
        }
        self.addCleanup(SESSIONS.pop, "token-lecturer", None)
        self.addCleanup(store.SUBMISSIONS.pop, submission_id, None)

        response = TestClient(app).get(
            "/api/lms/quizzes/quiz-primary-key/submissions",
            headers={"Authorization": "Bearer token-lecturer"},
        )

        self.assertEqual(response.status_code, 200)
        submission = response.json()["submissions"][0]
        self.assertEqual(submission["student_name"], "Budi Santoso")
        self.assertEqual(submission["score"], 50)
        self.assertEqual(submission["answers"][0]["given"], "Mengidentifikasi setiap baris secara unik")
        self.assertFalse(submission["answers"][1]["correct"])

    def test_student_cannot_view_quiz_submissions(self):
        response = TestClient(app).get(
            "/api/lms/quizzes/quiz-primary-key/submissions",
            headers={"Authorization": "Bearer token-student"},
        )

        self.assertEqual(response.status_code, 403)

    def test_other_lecturer_cannot_view_quiz_submissions(self):
        other_lecturer_id = "other-lecturer-submission-test"
        store.USERS[other_lecturer_id] = {
            "id": other_lecturer_id,
            "name": "Dosen Lain",
            "email": "dosen-lain@example.com",
            "password": "unused",
            "role": "lecturer",
        }
        SESSIONS["token-other-lecturer"] = other_lecturer_id
        self.addCleanup(store.USERS.pop, other_lecturer_id, None)
        self.addCleanup(SESSIONS.pop, "token-other-lecturer", None)

        response = TestClient(app).get(
            "/api/lms/quizzes/quiz-primary-key/submissions",
            headers={"Authorization": "Bearer token-other-lecturer"},
        )

        self.assertEqual(response.status_code, 403)


class EnrollmentPersistence(unittest.TestCase):
    def test_lecturer_dashboard_includes_enrolled_student_names(self):
        course = store.COURSES["course-basis-data"]
        student_id = "roster-dashboard-test-student"
        store.USERS[student_id] = {
            "id": student_id,
            "name": "Mahasiswa Roster Test",
            "email": "roster-test@example.com",
            "password": "unused",
            "role": "student",
        }
        course["student_ids"].append(student_id)
        SESSIONS["token-roster-lecturer"] = "lecturer-1"
        self.addCleanup(course["student_ids"].remove, student_id)
        self.addCleanup(store.USERS.pop, student_id, None)
        self.addCleanup(SESSIONS.pop, "token-roster-lecturer", None)

        response = TestClient(app).get(
            "/api/lms/dashboard",
            headers={"Authorization": "Bearer token-roster-lecturer"},
        )

        self.assertEqual(response.status_code, 200)
        course_data = next(
            item for item in response.json()["courses"]
            if item["id"] == course["id"]
        )
        self.assertEqual(course_data["student_count"], len(course_data["students"]))
        self.assertIn("Mahasiswa Roster Test", course_data["students"])

    def test_course_enrollment_reloads_from_database(self):
        with TemporaryDirectory() as temp_directory:
            original_database_path = store.DATABASE_PATH
            store.DATABASE_PATH = Path(temp_directory) / "enrollment_test.db"
            self.addCleanup(setattr, store, "DATABASE_PATH", original_database_path)

            course_id = "enrollment-persistence-test-course"
            user_id = "enrollment-persistence-test-student"
            course = {
                "id": course_id,
                "name": "Course Uji",
                "description": "Deskripsi uji",
                "lecturer_id": "lecturer-test",
                "student_ids": [],
            }
            student = {
                "id": user_id,
                "name": "Mahasiswa Uji",
                "email": "enrollment-test@example.com",
                "password": store.hash_password("temporary-password"),
                "role": "student",
            }
            store.COURSES[course_id] = course
            store.USERS[user_id] = student
            self.addCleanup(store.USERS.pop, user_id, None)

            store.persist_course(course)
            store.persist_user(student)
            store.persist_course_enrollment(course_id, user_id)
            store.COURSES.pop(course_id)
            store.load_persisted_courses()
            course = store.COURSES[course_id]
            store.load_persisted_enrollments()

            self.assertEqual(course["lecturer_id"], "lecturer-test")
            self.assertEqual(course["name"], "Course Uji")
            self.assertEqual(course["student_ids"], [user_id])
            self.addCleanup(store.COURSES.pop, course_id, None)


class SharedCoursesApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user_ids = []
        self.tokens = []

    def add_user(self, user_id, role):
        store.USERS[user_id] = {
            "id": user_id,
            "name": f"Pengguna {role}",
            "email": f"{user_id}@example.com",
            "password": "unused",
            "role": role,
        }
        token = f"shared-courses-{user_id}"
        SESSIONS[token] = user_id
        self.user_ids.append(user_id)
        self.tokens.append(token)
        self.addCleanup(store.USERS.pop, user_id, None)
        self.addCleanup(SESSIONS.pop, token, None)
        return {"Authorization": f"Bearer {token}"}

    def test_registered_student_sees_all_default_courses(self):
        headers = self.add_user("shared-courses-student", "student")

        response = self.client.get("/api/lms/dashboard", headers=headers)

        self.assertEqual(response.status_code, 200)
        courses = response.json()["courses"]
        self.assertEqual(len(store.DEFAULT_COURSES), 20)
        self.assertEqual(len(courses), len(store.DEFAULT_COURSES))
        self.assertEqual(len({course["name"] for course in courses}), 20)
        self.assertEqual(
            {course["id"] for course in courses},
            {course["id"] for course in store.DEFAULT_COURSES},
        )

    def test_registered_lecturer_can_create_quiz_in_every_default_course(self):
        headers = self.add_user("shared-courses-lecturer", "lecturer")

        class FakeGenerator:
            async def generate(self, *_):
                return NS(questions=[GOOD_MCQ])

        with patch("app.main.get_generator", return_value=FakeGenerator()):
            for course in store.DEFAULT_COURSES:
                response = self.client.post(
                    f"/api/lms/courses/{course['id']}/quizzes",
                    headers=headers,
                    json={
                        "title": f"Quiz {course['name']}",
                        "text": "Materi untuk pengujian pembuatan quiz pada course bersama.",
                        "count": 1,
                        "type": "mcq",
                        "difficulty": "easy",
                    },
                )
                self.assertEqual(response.status_code, 200, course["id"])
                self.assertEqual(response.json()["course_id"], course["id"])
                self.addCleanup(store.LMS_QUIZZES.pop, response.json()["id"], None)


class Generator(unittest.TestCase):
    def test_happy_path(self):
        c = FakeClient([reply([GOOD_MCQ, GOOD_SHORT])])
        r = run(QuestionGenerator(c, SET).generate(MATERI, 2, "mix", "easy"))
        self.assertEqual(len(r.questions), 2)
        self.assertEqual(r.warnings, [])
        self.assertEqual(c.calls[0]["tool_choice"]["name"], TOOL_NAME)
        self.assertIn("[chunk 0]", c.calls[0]["messages"][0]["content"])
        self.assertEqual((r.input_tokens, r.output_tokens), (100, 50))

    def test_retry_replaces_rejected(self):
        c = FakeClient([reply([GOOD_MCQ, HALLUCINATED]), reply([GOOD_SHORT])])
        r = run(QuestionGenerator(c, SET).generate(MATERI, 2, "mix", "easy"))
        self.assertEqual(len(r.questions), 2)
        self.assertEqual(len(r.rejected), 1)
        self.assertEqual(c.calls[1]["messages"][0]["content"].count("Jumlah soal: 1"), 1)
        self.assertIn("halusinasi", c.calls[1]["messages"][0]["content"])  # umpan balik dikirim ulang

    def test_gives_up_after_max_retries(self):
        c = FakeClient([reply([HALLUCINATED])] * 3)
        r = run(QuestionGenerator(c, SET).generate(MATERI, 1, "mcq", "easy"))
        self.assertEqual(len(r.questions), 0)
        self.assertEqual(r.api_calls, 3)  # 1 percobaan + 2 ulang
        self.assertTrue(r.warnings)

    def test_extra_questions_truncated(self):
        c = FakeClient([reply([GOOD_MCQ, GOOD_SHORT])])
        r = run(QuestionGenerator(c, SET).generate(MATERI, 1, "mix", "easy"))
        self.assertEqual(len(r.questions), 1)

    def test_text_json_fallback(self):
        txt = NS(type="text", text='```json\n{"questions": [' + __import__("json").dumps(GOOD_MCQ) + "]}\n```")
        c = FakeClient([NS(content=[txt], usage=None)])
        r = run(QuestionGenerator(c, SET).generate(MATERI, 1, "mcq", "easy"))
        self.assertEqual(len(r.questions), 1)

    def test_unparseable_output_retries(self):
        bad = NS(content=[NS(type="text", text="maaf, tidak bisa")], usage=None)
        c = FakeClient([bad, reply([GOOD_MCQ])])
        r = run(QuestionGenerator(c, SET).generate(MATERI, 1, "mcq", "easy"))
        self.assertEqual(len(r.questions), 1)


if __name__ == "__main__":
    unittest.main()
