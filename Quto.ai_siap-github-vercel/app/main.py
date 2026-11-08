from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from io import BytesIO
import secrets
import sqlite3
from xml.sax.saxutils import escape

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from . import store
from .config import get_settings
from .generator import QuestionGenerator, make_client
from .schemas import (
    CourseCreateRequest,
    GenerateRequest,
    LecturerInvitationRequest,
    LecturerRegisterRequest,
    LoginRequest,
    ProfileUpdateRequest,
    QuizCreateRequest,
    QuizSubmitRequest,
    RegisterRequest,
    ResetPasswordRequest,
    ResetPasswordRequestInit,
    ResetPasswordConfirm,
)

app = FastAPI(title="Quto.id API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])  # batasi saat produksi
DEMO_LECTURER_EMAIL = "dosen.demo@quto.id"
DEMO_LECTURER_NAME = "Dosen Demo"
DEMO_LECTURER_PASSWORD = "dosen123"


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(__file__.replace("main.py", "static/lms.html"))


def public_user(user: dict) -> dict:
    return {key: user[key] for key in ("id", "name", "email", "role")}


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, {"code": "unauthorized", "message": "Silakan login terlebih dahulu."})
    token = authorization.removeprefix("Bearer ").strip()
    user_id = store.get_session_user_id(token)
    if not user_id or user_id not in store.USERS:
        raise HTTPException(401, {"code": "invalid_session", "message": "Sesi login tidak valid atau sudah berakhir."})
    return store.USERS[user_id]


def require_role(user: dict, role: str) -> None:
    if user["role"] != role:
        raise HTTPException(403, {"code": "forbidden", "message": "Akses ini khusus untuk pengguna dengan role yang sesuai."})


def course_for_user(course_id: str, user: dict) -> dict:
    course = store.COURSES.get(course_id)
    if not course:
        raise HTTPException(404, {"code": "course_not_found", "message": "Course tidak ditemukan."})
    is_global = course.get("is_global", False) or course.get("lecturer_id") in ("all", "system", "global", "lecturer-1")
    if user["role"] == "lecturer" and not is_global and course["lecturer_id"] != user["id"]:
        raise HTTPException(403, {"code": "forbidden", "message": "Anda bukan dosen course ini."})
    if user["role"] == "student" and not is_global and user["id"] not in course["student_ids"]:
        raise HTTPException(403, {"code": "not_enrolled", "message": "Anda belum terdaftar di course ini."})
    return course


def quiz_summary(quiz: dict, user: dict) -> dict:
    submission = next(
        (item for item in store.SUBMISSIONS.values()
         if item["quiz_id"] == quiz["id"] and item["student_id"] == user["id"]),
        None,
    )
    return {
        "id": quiz["id"],
        "course_id": quiz["course_id"],
        "title": quiz["title"],
        "question_count": len(quiz["questions"]),
        "submitted": submission is not None,
        "score": submission["score"] if submission else None,
    }


INVALID_LOGIN = HTTPException(
    401,
    {"code": "invalid_credentials", "message": "Email, password, atau role tidak valid."},
)


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    user = next((item for item in store.USERS.values() if item["email"].lower() == email), None)
    if not user or not store.verify_password(req.password, user["password"]):
        raise INVALID_LOGIN
    if req.role and user["role"] != req.role:
        raise HTTPException(
            403,
            {"code": "role_mismatch", "message": "Pilih sesuai role yang terdaftar."},
        )
    if not store.is_password_hash(user["password"]):
        user["password"] = store.hash_password(req.password)
        store.persist_user(user)
    token = store.create_session(user["id"])
    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    name = req.name.strip()
    email = req.email.strip().lower()
    if req.role != "student":
        raise HTTPException(
            403,
            {
                "code": "lecturer_invitation_required",
                "message": "Akun dosen hanya dapat dibuat menggunakan undangan administrator.",
            },
        )
    existing = next((u for u in store.USERS.values() if u["email"].lower() == email), None)
    if existing:
        raise HTTPException(409, {"code": "email_exists", "message": "Email sudah terdaftar."})
    user_id = store.new_id()
    user = {
        "id": user_id,
        "name": name,
        "email": email,
        "password": store.hash_password(req.password),
        "role": "student",
    }
    try:
        store.persist_user(user)
    except sqlite3.IntegrityError:
        raise HTTPException(409, {"code": "email_exists", "message": "Email sudah terdaftar."})
    store.USERS[user_id] = user
    token = store.create_session(user_id)
    return {"token": token, "user": public_user(store.USERS[user_id])}


@app.post("/api/admin/lecturer-invitations")
async def create_lecturer_invitation(
    req: LecturerInvitationRequest,
    x_admin_key: str | None = Header(default=None),
):
    admin_api_key = get_settings().admin_api_key
    if not admin_api_key:
        raise HTTPException(
            503,
            {
                "code": "admin_invitations_disabled",
                "message": "Pembuatan undangan dosen belum dikonfigurasi.",
            },
        )
    if not x_admin_key or not secrets.compare_digest(x_admin_key, admin_api_key):
        raise HTTPException(
            401,
            {"code": "invalid_admin_key", "message": "Kunci administrator tidak valid."},
        )

    email = req.email.strip().lower()
    if any(user["email"].lower() == email for user in store.USERS.values()):
        raise HTTPException(409, {"code": "email_exists", "message": "Email sudah terdaftar."})

    token = secrets.token_urlsafe(32)
    expires_at = int(datetime.now(timezone.utc).timestamp()) + req.expires_in_hours * 3600
    store.issue_lecturer_invitation(email, token, expires_at)
    return {
        "email": email,
        "invitation_token": token,
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
    }


@app.post("/api/auth/demo/lecturer-invitation")
async def create_demo_lecturer_invitation(request: Request):
    if not get_settings().enable_demo_lecturer_registration:
        raise HTTPException(
            503,
            {
                "code": "demo_lecturer_registration_disabled",
                "message": "Demo registrasi dosen lokal belum diaktifkan.",
            },
        )
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(
            403,
            {
                "code": "local_demo_only",
                "message": "Demo registrasi dosen hanya tersedia dari komputer lokal.",
            },
        )
    if any(
        user["email"].lower() == DEMO_LECTURER_EMAIL
        for user in store.USERS.values()
    ):
        raise HTTPException(
            409,
            {
                "code": "demo_lecturer_already_registered",
                "message": (
                    "Akun demo dosen sudah terdaftar. Gunakan email dan password "
                    "demo yang ditampilkan pada halaman login."
                ),
            },
        )

    invitation_token = secrets.token_urlsafe(32)
    expires_at = int(datetime.now(timezone.utc).timestamp()) + 30 * 60
    store.issue_lecturer_invitation(
        DEMO_LECTURER_EMAIL,
        invitation_token,
        expires_at,
    )
    return {
        "name": DEMO_LECTURER_NAME,
        "email": DEMO_LECTURER_EMAIL,
        "invitation_token": invitation_token,
        "demo_password": DEMO_LECTURER_PASSWORD,
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
    }


@app.post("/api/auth/register/lecturer")
async def register_lecturer(req: LecturerRegisterRequest):
    email = req.email.strip().lower()
    user = {
        "id": store.new_id(),
        "name": req.name.strip(),
        "email": email,
        "password": req.password,
        "role": "lecturer",
    }
    result = store.create_lecturer_from_invitation(user, req.invitation_token)
    if result != "created":
        errors = {
            "invalid_invitation": (
                400,
                "invalid_invitation",
                "Undangan tidak valid atau sudah digunakan.",
            ),
            "invitation_expired": (
                400,
                "invitation_expired",
                "Undangan sudah kedaluwarsa. Minta undangan baru kepada administrator.",
            ),
            "invitation_email_mismatch": (
                403,
                "invitation_email_mismatch",
                "Email harus sama dengan email yang menerima undangan.",
            ),
            "email_exists": (409, "email_exists", "Email sudah terdaftar."),
        }
        status_code, code, message = errors[result]
        raise HTTPException(status_code, {"code": code, "message": message})

    store.USERS[user["id"]] = user
    token = store.create_session(user["id"])
    return {"token": token, "user": public_user(user)}


@app.get("/api/auth/me")
async def me(authorization: str | None = Header(default=None)):
    return public_user(require_user(authorization))


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        store.SESSIONS.pop(authorization.removeprefix("Bearer ").strip(), None)
    return {"ok": True}


@app.post("/api/auth/reset-password/request")
def reset_password_request(req: ResetPasswordRequestInit):
    """
    Langkah 1 – minta token reset.
    Mengembalikan token yang harus disalin pengguna ke form konfirmasi.
    Token berlaku 1 jam dan hanya bisa dipakai sekali.
    Selalu mengembalikan 200 agar penyerang tidak bisa menebak apakah email terdaftar
    — token hanya ada di response jika email benar-benar ditemukan.
    """
    import time, secrets as _secrets
    email = req.email.strip().lower()
    user = next((u for u in store.USERS.values() if u["email"].lower() == email), None)
    if not user:
        # Respons identik agar enumerasi email tidak mungkin
        return {"ok": True, "message": "Jika email terdaftar, token reset telah dikirim."}

    # Hapus token lama milik user ini (satu token aktif per user)
    for old_tok in [t for t, v in list(store.PASSWORD_RESET_TOKENS.items()) if v["user_id"] == user["id"]]:
        store.PASSWORD_RESET_TOKENS.pop(old_tok, None)

    token = _secrets.token_urlsafe(32)
    store.PASSWORD_RESET_TOKENS[token] = {
        "user_id": user["id"],
        "expires_at": time.time() + 3600,  # 1 jam
    }
    return {
        "ok": True,
        "message": "Token reset berhasil dibuat. Salin token berikut dan masukkan ke form konfirmasi.",
        "reset_token": token,
    }


@app.post("/api/auth/reset-password/confirm")
def reset_password_confirm(req: ResetPasswordConfirm):
    """
    Langkah 2 – konfirmasi dengan token dan password baru.
    Token dihapus setelah dipakai (single-use).
    """
    import time
    entry = store.PASSWORD_RESET_TOKENS.get(req.token)
    if not entry:
        raise HTTPException(400, {"code": "invalid_token", "message": "Token tidak valid atau sudah dipakai."})
    if time.time() > entry["expires_at"]:
        store.PASSWORD_RESET_TOKENS.pop(req.token, None)
        raise HTTPException(400, {"code": "token_expired", "message": "Token sudah kedaluwarsa. Minta token baru."})

    user = store.USERS.get(entry["user_id"])
    if not user:
        store.PASSWORD_RESET_TOKENS.pop(req.token, None)
        raise HTTPException(404, {"code": "account_not_found", "message": "Akun tidak ditemukan."})

    # Hapus token (single-use)
    store.PASSWORD_RESET_TOKENS.pop(req.token, None)

    updated_user = {**user, "password": store.hash_password(req.password)}
    store.persist_user(updated_user)
    user.update(updated_user)

    # Invalidasi semua sesi aktif milik user ini
    store.clear_user_sessions(user["id"])

    return {"ok": True, "message": "Password berhasil diubah. Silakan masuk kembali."}


# ---------------------------------------------------------------------------
# Endpoint lama dinonaktifkan — mengizinkan ganti password hanya dengan email
# tanpa verifikasi apapun; digantikan oleh /request + /confirm di atas.
# ---------------------------------------------------------------------------
@app.post("/api/auth/reset-password", deprecated=True)
def reset_password(_req: ResetPasswordRequest):
    raise HTTPException(
        410,
        {
            "code": "endpoint_removed",
            "message": (
                "Endpoint ini dihapus karena tidak aman. "
                "Gunakan POST /api/auth/reset-password/request lalu "
                "POST /api/auth/reset-password/confirm."
            ),
        },
    )


@app.patch("/api/auth/profile")
async def update_profile(req: ProfileUpdateRequest, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    updated_user = user.copy()
    changing_password = req.password is not None
    changing_email = req.email is not None and req.email.strip().lower() != user["email"].lower()

    if changing_password or changing_email:
        if not req.current_password or not store.verify_password(req.current_password, user["password"]):
            raise HTTPException(403, {"code": "current_password_required", "message": "Password saat ini diperlukan dan harus benar."})

    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(422, {"code": "invalid_name", "message": "Nama wajib diisi."})
        updated_user["name"] = name

    if changing_email:
        email = req.email.strip().lower()
        if any(item["email"].lower() == email and item["id"] != user["id"] for item in store.USERS.values()):
            raise HTTPException(409, {"code": "email_exists", "message": "Email sudah dipakai pengguna lain."})
        updated_user["email"] = email

    if changing_password:
        updated_user["password"] = store.hash_password(req.password)

    try:
        store.persist_user(updated_user)
    except sqlite3.IntegrityError:
        raise HTTPException(409, {"code": "email_exists", "message": "Email sudah dipakai pengguna lain."})
    user.update(updated_user)
    if changing_password:
        store.clear_user_sessions(user["id"])

    return public_user(user)


@app.get("/api/lms/dashboard")
async def dashboard(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    if user["role"] == "lecturer":
        courses = [
            course for course in store.COURSES.values()
            if course.get("is_global", False) or course.get("lecturer_id") in (user["id"], "all", "system", "global", "lecturer-1")
        ]
        return {
            "role": user["role"],
            "courses": [
                {
                    **course,
                    "student_count": len(course["student_ids"]),
                    "students": [
                        store.USERS[student_id]["name"]
                        for student_id in course["student_ids"]
                        if student_id in store.USERS
                    ],
                    "quizzes": [quiz_summary(quiz, user) for quiz in store.LMS_QUIZZES.values()
                                if quiz["course_id"] == course["id"]],
                }
                for course in courses
            ],
        }

    courses = [
        course for course in store.COURSES.values()
        if course.get("is_global", False) or user["id"] in course["student_ids"]
    ]
    return {
        "role": user["role"],
        "courses": [
            {
                **course,
                "enrolled": user["id"] in course["student_ids"] or course.get("is_global", False),
                "quizzes": [quiz_summary(quiz, user) for quiz in store.LMS_QUIZZES.values()
                            if quiz["course_id"] == course["id"]],
            }
            for course in courses
        ],
    }


@app.post("/api/lms/courses")
async def create_course(req: CourseCreateRequest, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "lecturer")
    course_id = store.new_id()
    store.COURSES[course_id] = {
        "id": course_id,
        "name": req.name,
        "description": req.description,
        "lecturer_id": user["id"],
        "student_ids": [],
    }
    store.persist_course(store.COURSES[course_id])
    return store.COURSES[course_id]


@app.get("/api/lms/courses")
async def list_courses(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {
        "courses": [
            {
                "id": course["id"],
                "name": course["name"],
                "description": course.get("description", ""),
                "lecturer_id": course["lecturer_id"],
                "is_global": course.get("is_global", False),
                "enrolled": user["id"] in course["student_ids"] or course.get("is_global", False),
                "student_count": len(course["student_ids"]),
                "quiz_count": len([q for q in store.LMS_QUIZZES.values() if q["course_id"] == course["id"]]),
            }
            for course in store.COURSES.values()
        ]
    }


@app.post("/api/lms/courses/{course_id}/enroll")
async def enroll(course_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "student")
    course = store.COURSES.get(course_id)
    if not course:
        raise HTTPException(404, {"code": "course_not_found", "message": "Course tidak ditemukan."})
    if user["id"] not in course["student_ids"]:
        course["student_ids"].append(user["id"])
    store.persist_course_enrollment(course_id, user["id"])
    return {**course, "enrolled": True}


@app.get("/api/lms/courses/{course_id}")
async def get_course(course_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    course = course_for_user(course_id, user)
    return {
        **course,
        "quizzes": [quiz_summary(quiz, user) for quiz in store.LMS_QUIZZES.values()
                    if quiz["course_id"] == course_id],
    }


@app.post("/api/lms/courses/{course_id}/quizzes")
async def create_quiz(course_id: str, req: QuizCreateRequest, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    course = course_for_user(course_id, user)
    require_role(user, "lecturer")
    try:
        result = await get_generator().generate(req.text, req.count, req.type, req.difficulty)
    except HTTPException:
        raise
    except Exception as exc:
        if _is_llm_error(exc):
            raise _api_error(exc, "Pembuatan quiz") from exc
        raise HTTPException(
            500,
            {"code": "quiz_generation_failed", "message": "Terjadi kesalahan saat membuat quiz."},
        ) from exc
    if not result.questions:
        raise HTTPException(422, {"code": "quiz_generation_failed", "message": "Tidak ada soal yang lolos validasi."})
    return save_generated_quiz(course, user, req.title, result.questions)


def _api_error(exc: Exception, action: str) -> HTTPException:
    message = str(exc).strip() or exc.__class__.__name__
    lower = message.lower()
    status_code = exc.response.status_code if hasattr(exc, "response") and getattr(exc, "response", None) is not None else None
    if isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in lower or "timed out" in lower:
        return HTTPException(
            503,
            {"code": "llm_timeout", "message": f"{action} tidak dapat diselesaikan karena layanan AI tidak merespons tepat waktu. Coba lagi."},
        )
    if status_code == 401 or status_code == 403 or "api key" in lower or "authentication" in lower or "unauthorized" in lower:
        return HTTPException(
            502,
            {"code": "llm_auth_error", "message": f"{action} gagal karena konfigurasi API key AI tidak valid. Periksa LLM_API_KEY server."},
        )
    if status_code == 429 or "rate limit" in lower or "too many requests" in lower:
        return HTTPException(
            503,
            {"code": "llm_rate_limited", "message": f"{action} gagal karena batas permintaan layanan AI tercapai. Coba lagi beberapa saat."},
        )
    if status_code is not None and 400 <= status_code < 500:
        return HTTPException(
            502,
            {"code": "llm_request_failed", "message": f"{action} gagal karena permintaan ke layanan AI ditolak. Periksa konfigurasi model atau materi."},
        )
    return HTTPException(
        502,
        {"code": "llm_unavailable", "message": f"{action} gagal karena layanan AI sedang bermasalah. Coba lagi."},
    )


def _is_llm_error(exc: Exception) -> bool:
    module = exc.__class__.__module__ or ""
    name = exc.__class__.__name__
    if module.startswith("openai") or module.startswith("groq") or module.startswith("httpx"):
        return True
    if name in {"APIError", "APIConnectionError", "APITimeoutError", "RateLimitError", "AuthenticationError"}:
        return True
def save_generated_quiz(course: dict, user: dict, title: str, questions: list[dict]) -> dict:
    from .validation import norm as _norm
    normalized_questions = []
    for question in questions:
        q = dict(question)
        # For MCQ, replace `answer` with the exact option text that matches after
        # normalization, so that scoring (which now uses norm()) is always consistent.
        if q.get("type") == "mcq" and q.get("options") and q.get("answer"):
            norm_answer = _norm(q["answer"])
            matched = next((opt for opt in q["options"] if _norm(opt) == norm_answer), None)
            if matched:
                q["answer"] = matched  # use exact option text, punctuation and all
        normalized_questions.append(q)
    quiz_id = store.new_id()
    quiz = {
        "id": quiz_id,
        "course_id": course["id"],
        "title": title,
        "creator_id": user["id"],
        "questions": [{"id": store.new_id(), **q} for q in normalized_questions],
    }
    store.LMS_QUIZZES[quiz_id] = quiz
    store.persist_lms_quiz(quiz)
    return quiz_summary(quiz, user)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise HTTPException(422, {"code": "encrypted_pdf", "message": "PDF berkunci tidak didukung."})
        if len(reader.pages) > 200:
            raise HTTPException(422, {"code": "pdf_too_many_pages", "message": "PDF maksimal 200 halaman."})
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except HTTPException:
        raise
    except (PdfReadError, ValueError, OSError) as error:
        raise HTTPException(422, {"code": "invalid_pdf", "message": "Isi PDF tidak dapat dibaca."}) from error


@app.post("/api/lms/courses/{course_id}/quizzes/pdf")
async def create_quiz_from_pdf(
    course_id: str,
    title: str = Form(...),
    count: int = Form(5),
    question_type: str = Form("mix", alias="type"),
    difficulty: str = Form("medium"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = require_user(authorization)
    course = course_for_user(course_id, user)
    require_role(user, "lecturer")
    if not title.strip():
        raise HTTPException(422, {"code": "invalid_title", "message": "Judul quiz wajib diisi."})
    if not 1 <= count <= 15:
        raise HTTPException(422, {"code": "invalid_count", "message": "Jumlah soal harus 1 sampai 15."})
    if question_type not in {"mcq", "short", "mix"}:
        raise HTTPException(422, {"code": "invalid_type", "message": "Tipe soal tidak valid."})
    if difficulty not in {"easy", "medium", "hard"}:
        raise HTTPException(422, {"code": "invalid_difficulty", "message": "Tingkat kesulitan tidak valid."})

    pdf_bytes = await file.read(15 * 1024 * 1024 + 1)
    if len(pdf_bytes) > 15 * 1024 * 1024:
        raise HTTPException(413, {"code": "pdf_too_large", "message": "Ukuran PDF maksimal 15 MB."})
    if not (file.filename or "").lower().endswith(".pdf") or not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(422, {"code": "invalid_pdf", "message": "Pilih file PDF yang valid."})

    source_text = await run_in_threadpool(_extract_pdf_text, pdf_bytes)
    if len(source_text) < 50:
        raise HTTPException(422, {"code": "empty_pdf_text", "message": "PDF tidak berisi teks yang dapat dibaca. PDF hasil scan perlu OCR."})

    try:
        result = await get_generator().generate(source_text, count, question_type, difficulty)
    except HTTPException:
        raise
    except Exception as exc:
        if _is_llm_error(exc):
            raise _api_error(exc, "Pembuatan quiz dari PDF") from exc
        raise HTTPException(
            500,
            {"code": "quiz_generation_failed", "message": "Terjadi kesalahan saat membuat quiz dari PDF."},
        ) from exc
    if not result.questions:
        raise HTTPException(422, {"code": "quiz_generation_failed", "message": "Tidak ada soal yang lolos validasi."})
    return save_generated_quiz(course, user, title.strip(), result.questions)


@app.get("/api/lms/quizzes/{quiz_id}")
async def get_lms_quiz(quiz_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    quiz = store.LMS_QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Quiz tidak ditemukan."})
    course_for_user(quiz["course_id"], user)
    if user["role"] == "lecturer":
        return quiz
    return {
        **quiz,
        "questions": [
            {key: question[key] for key in ("id", "type", "body", "options")}
            for question in quiz["questions"]
        ],
    }


@app.get("/api/lms/quizzes/{quiz_id}/submissions")
async def get_quiz_submissions(
    quiz_id: str,
    authorization: str | None = Header(default=None),
):
    user = require_user(authorization)
    require_role(user, "lecturer")
    quiz = store.LMS_QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Quiz tidak ditemukan."})
    course_for_user(quiz["course_id"], user)

    submissions = []
    for submission in store.SUBMISSIONS.values():
        if submission["quiz_id"] != quiz_id:
            continue
        student = store.USERS.get(submission["student_id"])
        answers = submission.get("answers", {})
        submissions.append({
            "student_name": student["name"] if student else "Akun tidak tersedia",
            "score": submission["score"],
            "correct": submission["correct"],
            "total": submission["total"],
            "answers": [
                {
                    "question": question["body"],
                    "given": answers.get(question["id"], {}).get("given", ""),
                    "correct_answer": question.get("answer", ""),
                    "correct": answers.get(question["id"], {}).get("correct", False),
                }
                for question in quiz["questions"]
            ],
        })
    return {"submissions": submissions}


@app.get("/api/lms/quizzes/{quiz_id}/pdf")
async def export_lms_quiz_pdf(quiz_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    quiz = store.LMS_QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Quiz tidak ditemukan."})
    course_for_user(quiz["course_id"], user)

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title=quiz["title"],
    )
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "QuizTitle", parent=base_styles["Title"], alignment=TA_LEFT,
        textColor=colors.HexColor("#163b3a"), spaceAfter=8,
    )
    question_style = ParagraphStyle(
        "QuizQuestion", parent=base_styles["BodyText"], fontName="Helvetica-Bold",
        leading=17, spaceBefore=12, spaceAfter=6,
    )
    body_style = ParagraphStyle("QuizBody", parent=base_styles["BodyText"], leading=15)
    story = [
        Paragraph(escape(quiz["title"]), title_style),
        Paragraph(f"{len(quiz['questions'])} soal", body_style),
        Spacer(1, 8 * mm),
    ]
    include_answers = user["role"] == "lecturer"
    for index, question in enumerate(quiz["questions"], start=1):
        story.append(Paragraph(f"{index}. {escape(question['body'])}", question_style))
        for option_index, option in enumerate(question.get("options") or []):
            marker = chr(65 + option_index)
            story.append(Paragraph(f"{marker}. {escape(option)}", body_style))
        if question.get("type") == "short" and not include_answers:
            story.extend([Spacer(1, 4 * mm), Paragraph("Jawaban: __________________________________", body_style)])
        if include_answers:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(f"<b>Jawaban:</b> {escape(question.get('answer', ''))}", body_style))
            if question.get("rationale"):
                story.append(Paragraph(f"<b>Pembahasan:</b> {escape(question['rationale'])}", body_style))
        story.append(Spacer(1, 4 * mm))
    document.build(story)
    return Response(
        content=output.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="quto-quiz-{quiz_id[:8]}.pdf"'},
    )


@app.get("/api/lms/quizzes/{quiz_id}/submission")
async def get_my_submission(quiz_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "student")
    quiz = store.LMS_QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Quiz tidak ditemukan."})
    course_for_user(quiz["course_id"], user)
    submission = next(
        (item for item in store.SUBMISSIONS.values()
         if item["quiz_id"] == quiz_id and item["student_id"] == user["id"]),
        None,
    )
    if not submission:
        raise HTTPException(404, {"code": "submission_not_found", "message": "Belum ada jawaban yang dikumpulkan untuk quiz ini."})
    answers = submission.get("answers", {})
    return {
        "id": submission["id"],
        "quiz_id": quiz_id,
        "quiz_title": quiz["title"],
        "score": submission["score"],
        "correct": submission["correct"],
        "total": submission["total"],
        "answers": [
            {
                "question": question["body"],
                "given": answers.get(question["id"], {}).get("given", ""),
                "correct_answer": question.get("answer", ""),
                "correct": answers.get(question["id"], {}).get("correct", False),
                "rationale": question.get("rationale", ""),
            }
            for question in quiz["questions"]
        ],
    }


@app.post("/api/lms/quizzes/{quiz_id}/submit")
async def submit_quiz(quiz_id: str, req: QuizSubmitRequest, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "student")
    quiz = store.LMS_QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Quiz tidak ditemukan."})
    course_for_user(quiz["course_id"], user)
    existing = next(
        (item for item in store.SUBMISSIONS.values()
         if item["quiz_id"] == quiz_id and item["student_id"] == user["id"]),
        None,
    )
    if existing:
        raise HTTPException(
            409,
            {
                "code": "already_submitted",
                "message": f"Quiz ini sudah pernah dikumpulkan dengan nilai {existing['score']}.",
            },
        )
    course = store.COURSES.get(quiz["course_id"])
    if course and user["id"] not in course["student_ids"]:
        course["student_ids"].append(user["id"])
        store.persist_course_enrollment(quiz["course_id"], user["id"])
    from .validation import norm as _norm
    correct = 0
    answers = {}
    for question in quiz["questions"]:
        given = req.answers.get(question["id"], "")
        is_correct = _norm(given) == _norm(question["answer"])
        correct += int(is_correct)
        answers[question["id"]] = {"given": given, "correct": is_correct}
    score = round(correct / len(quiz["questions"]) * 100) if quiz["questions"] else 0
    submission_id = store.new_id()
    submission = {
        "id": submission_id,
        "quiz_id": quiz_id,
        "student_id": user["id"],
        "correct": correct,
        "total": len(quiz["questions"]),
        "score": score,
        "answers": answers,
    }
    store.SUBMISSIONS[submission_id] = submission
    store.persist_submission(submission)
    return submission

_generator: QuestionGenerator | None = None


def get_generator() -> QuestionGenerator:
    global _generator
    if _generator is None:
        s = get_settings()
        if not s.api_key:
            raise HTTPException(
                503,
                {
                    "code": "llm_api_key_missing",
                    "message": "Generator AI belum dikonfigurasi. Ganti nilai contoh LLM_API_KEY di file .env dengan API key Groq yang valid, lalu restart server.",
                },
            )
        _generator = QuestionGenerator(make_client(s.api_key, s.base_url), s)
    return _generator


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    # Bentuk galat mengikuti kontrak OpenAPI: {code, message}
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)


# --- Materi (versi teks saja; parser PDF/DOCX menyusul) -------------------
@app.post("/courses/{course_id}/materials", status_code=202)
async def upload_material(
    course_id: str,
    title: str = Form(...),
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
    authorization: str | None = Header(default=None),
):
    user = require_user(authorization)
    require_role(user, "lecturer")
    course_for_user(course_id, user)
    if file is not None:
        raise HTTPException(501, {"code": "not_implemented", "message": "Parser PDF/DOCX belum dibuat. Kirim field 'text'."})
    if not text or not text.strip():
        raise HTTPException(422, {"code": "empty_material", "message": "Isi 'text' atau unggah file."})
    mid = store.new_id()
    store.MATERIALS[mid] = {
        "id": mid,
        "course_id": course_id,
        "title": title,
        "source": "text",
        "state": "ready",
        "error": None,
        "raw_text": text,
        "creator_id": user["id"],
    }
    return {k: v for k, v in store.MATERIALS[mid].items() if k != "raw_text"}


# --- Generasi asinkron ------------------------------------------------------
async def _run_job(job_id: str, req: GenerateRequest):
    job = store.JOBS[job_id]
    job["state"] = "running"
    try:
        material = store.MATERIALS[req.material_id]
        res = await get_generator().generate(material["raw_text"], req.count, req.type, req.difficulty)
        if not res.questions:
            raise ValueError("Tidak ada soal yang lolos validasi. " + " ".join(res.warnings))
        qid = store.new_id()
        store.QUIZZES[qid] = {
            "id": qid, "course_id": req.course_id, "title": req.title,
            "start_at": req.start_at.isoformat(), "duration_minutes": req.duration_minutes,
            "difficulty": req.difficulty, "state": "draft", "question_count": len(res.questions),
            "warnings": res.warnings,
            "questions": [{"id": store.new_id(), "position": i + 1, **q} for i, q in enumerate(res.questions)],
        }
        job.update(state="done", quiz_id=qid)
    except Exception as e:  # noqa: BLE001 - job harus selalu berakhir di done/failed
        if _is_llm_error(e):
            mapped = _api_error(e, "Pembuatan quiz")
            detail = mapped.detail if isinstance(mapped.detail, dict) else {"message": str(mapped.detail)}
            job.update(state="failed", error=detail.get("message") or "Layanan AI sedang bermasalah.")
        else:
            job.update(state="failed", error=str(e) or e.__class__.__name__)
    finally:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.post("/quizzes/generate", status_code=202)
async def generate(req: GenerateRequest, bg: BackgroundTasks, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "lecturer")
    course_for_user(req.course_id, user)
    material = store.MATERIALS.get(req.material_id)
    if not material:
        raise HTTPException(404, {"code": "material_not_found", "message": "Materi tidak ditemukan."})
    if material["course_id"] != req.course_id:
        raise HTTPException(403, {"code": "forbidden", "message": "Materi tidak terkait dengan course ini."})
    creator_id = material.get("creator_id")
    if creator_id and creator_id != user["id"]:
        raise HTTPException(403, {"code": "forbidden", "message": "Anda bukan pemilik materi ini."})
    jid = store.new_id()
    store.JOBS[jid] = {
        "id": jid,
        "state": "queued",
        "quiz_id": None,
        "error": None,
        "course_id": req.course_id,
        "creator_id": user["id"],
    }
    bg.add_task(_run_job, jid, req)
    return {k: v for k, v in store.JOBS[jid].items() if k != "creator_id"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "lecturer")
    job = store.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, {"code": "job_not_found", "message": "Job tidak ditemukan."})
    if job.get("course_id"):
        course_for_user(job["course_id"], user)
    creator_id = job.get("creator_id")
    if creator_id and creator_id != user["id"]:
        raise HTTPException(403, {"code": "forbidden", "message": "Anda bukan pemilik job ini."})
    return {k: v for k, v in job.items() if k != "creator_id"}


@app.get("/quizzes/{quiz_id}")
async def get_quiz(quiz_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_role(user, "lecturer")
    quiz = store.QUIZZES.get(quiz_id)
    if not quiz:
        raise HTTPException(404, {"code": "quiz_not_found", "message": "Kuis tidak ditemukan."})
    if quiz.get("course_id"):
        course_for_user(quiz["course_id"], user)
    return quiz
