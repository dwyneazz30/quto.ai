from io import BytesIO
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app import store
from app.main import app
from app.store import SESSIONS


def make_pdf(text: str) -> bytes:
    buffer = BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 760, text)
    page.save()
    return buffer.getvalue()


class FakeGenerator:
    def __init__(self):
        self.source_text = ""

    async def generate(self, source_text, count, question_type, difficulty):
        self.source_text = source_text
        return NS(questions=[{
            "type": "mcq",
            "body": "Apa fungsi primary key?",
            "options": ["Identitas unik", "Menghapus data", "Mengubah tabel", "Menyimpan gambar"],
            "answer": "Identitas unik",
            "rationale": "Primary key memberi identitas unik pada setiap baris.",
        }])


class PdfFeatures(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.original_sessions = dict(SESSIONS)
        SESSIONS.clear()
        SESSIONS.update({"pdf-teacher-token": "lecturer-1", "pdf-student-token": "student-1"})
        store.COURSES["pdf-test-course"] = {
            "id": "pdf-test-course",
            "name": "Basis Data",
            "description": "Materi basis data",
            "lecturer_id": "lecturer-1",
            "student_ids": ["student-1"],
        }
        self.addCleanup(self.cleanup_state)

    def cleanup_state(self):
        SESSIONS.clear()
        SESSIONS.update(self.original_sessions)
        store.COURSES.pop("pdf-test-course", None)
        store.LMS_QUIZZES.pop("pdf-export-test", None)

    def test_import_pdf_extracts_text_and_creates_quiz(self):
        generator = FakeGenerator()
        source = "Primary key mengidentifikasi setiap baris tabel secara unik. " * 3
        with patch("app.main.get_generator", return_value=generator):
            response = self.client.post(
                "/api/lms/courses/pdf-test-course/quizzes/pdf",
                data={"title": "Quiz Basis Data", "count": "5", "type": "mix", "difficulty": "easy"},
                files={"file": ("materi.pdf", make_pdf(source), "application/pdf")},
                headers={"Authorization": "Bearer pdf-teacher-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Primary key", generator.source_text)
        self.assertEqual(response.json()["title"], "Quiz Basis Data")

    def test_import_rejects_non_pdf(self):
        response = self.client.post(
            "/api/lms/courses/pdf-test-course/quizzes/pdf",
            data={"title": "Quiz Basis Data"},
            files={"file": ("materi.txt", b"bukan pdf" * 10, "text/plain")},
            headers={"Authorization": "Bearer pdf-teacher-token"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "invalid_pdf")

    def test_export_pdf_hides_answers_from_students(self):
        store.LMS_QUIZZES["pdf-export-test"] = {
            "id": "pdf-export-test",
            "course_id": "pdf-test-course",
            "title": "Quiz Aman",
            "questions": [{
                "id": "q1",
                "type": "mcq",
                "body": "Apa fungsi primary key?",
                "options": ["Identitas unik", "Menghapus data"],
                "answer": "Jawaban Rahasia",
                "rationale": "Pembahasan Rahasia",
            }],
        }

        teacher_response = self.client.get(
            "/api/lms/quizzes/pdf-export-test/pdf",
            headers={"Authorization": "Bearer pdf-teacher-token"},
        )
        student_response = self.client.get(
            "/api/lms/quizzes/pdf-export-test/pdf",
            headers={"Authorization": "Bearer pdf-student-token"},
        )
        teacher_text = " ".join(page.extract_text() or "" for page in PdfReader(BytesIO(teacher_response.content)).pages)
        student_text = " ".join(page.extract_text() or "" for page in PdfReader(BytesIO(student_response.content)).pages)

        self.assertEqual(teacher_response.status_code, 200)
        self.assertEqual(student_response.status_code, 200)
        self.assertEqual(teacher_response.headers["content-type"], "application/pdf")
        self.assertIn("Jawaban Rahasia", teacher_text)
        self.assertNotIn("Jawaban Rahasia", student_text)
        self.assertNotIn("Pembahasan Rahasia", student_text)


if __name__ == "__main__":
    unittest.main()