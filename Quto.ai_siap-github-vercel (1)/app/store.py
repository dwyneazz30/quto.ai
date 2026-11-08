"""Penyimpanan sementara LMS; akun disimpan di SQLite, data lainnya in-memory."""
from __future__ import annotations

import json
import os
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Literal
import uuid

# Di Vercel filesystem proyek read-only; hanya /tmp yang bisa ditulis (sementara).
DATABASE_PATH = (
    Path("/tmp/quto_data.db")
    if os.getenv("VERCEL")
    else Path(__file__).resolve().parent.parent / "quto_data.db"
)
PASSWORD_HASH_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 60 * 60 * 12

MATERIALS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
QUIZZES: dict[str, dict] = {}
USERS: dict[str, dict] = {
    "lecturer-1": {
        "id": "lecturer-1",
        "name": "Dosen",
        "email": "dosen@gmail.com",
        "password": "dosen123",
        "role": "lecturer",
    },
    "student-1": {
        "id": "student-1",
        "name": "Mahasiswa",
        "email": "mahasiswa@gmail.com",
        "password": "mahasiswa123",
        "role": "student",
    },
    "lecturer-legacy": {
        "id": "lecturer-legacy",
        "name": "Dr. Anita Rahma",
        "email": "dosen@quto.id",
        "password": "dosen123",
        "role": "lecturer",
    },
    "student-legacy": {
        "id": "student-legacy",
        "name": "Budi Santoso",
        "email": "mahasiswa@quto.id",
        "password": "mahasiswa123",
        "role": "student",
    },
}
SESSIONS: dict[str, dict] = {}
COURSES: dict[str, dict] = {}
LMS_QUIZZES: dict[str, dict] = {}
SUBMISSIONS: dict[str, dict] = {}
# token -> {user_id, expires_at (unix timestamp)}
PASSWORD_RESET_TOKENS: dict[str, dict] = {}


def create_session(user_id: str) -> str:
    token = new_id()
    SESSIONS[token] = {"user_id": user_id, "expires_at": time.time() + SESSION_TTL_SECONDS}
    return token


def get_session_user_id(token: str) -> str | None:
    session = SESSIONS.get(token)
    if session is None:
        return None
    if not isinstance(session, dict):
        user_id = session
        SESSIONS[token] = {"user_id": user_id, "expires_at": time.time() + SESSION_TTL_SECONDS}
        return user_id
    if session.get("expires_at", 0) <= time.time():
        SESSIONS.pop(token, None)
        return None
    return session.get("user_id")


def clear_user_sessions(user_id: str) -> None:
    for token, session in list(SESSIONS.items()):
        sid = session.get("user_id") if isinstance(session, dict) else session
        if sid == user_id:
            SESSIONS.pop(token, None)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def is_password_hash(password: str) -> bool:
    return password.startswith("pbkdf2_sha256$")


def verify_password(password: str, stored_password: str) -> bool:
    if not is_password_hash(stored_password):
        return hmac.compare_digest(password, stored_password)

    try:
        algorithm, iterations, salt, expected = stored_password.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt),
            int(iterations),
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _connect_users() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS course_enrollments (
            course_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (course_id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS courses (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            lecturer_id TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lms_quizzes (
            id TEXT PRIMARY KEY,
            course_id TEXT NOT NULL,
            title TEXT NOT NULL,
            creator_id TEXT NOT NULL,
            questions_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            quiz_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            correct INTEGER NOT NULL,
            total INTEGER NOT NULL,
            score INTEGER NOT NULL,
            answers_json TEXT NOT NULL,
            UNIQUE (quiz_id, student_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lecturer_invitations (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        )
        """
    )
    return connection


def issue_lecturer_invitation(email: str, token: str, expires_at: int) -> None:
    now = int(time.time())
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with closing(_connect_users()) as connection:
        connection.execute(
            """
            UPDATE lecturer_invitations
            SET used_at = ?
            WHERE email = ? AND used_at IS NULL
            """,
            (now, email),
        )
        connection.execute(
            """
            INSERT INTO lecturer_invitations (token_hash, email, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, email, now, expires_at),
        )
        connection.commit()


def create_lecturer_from_invitation(
    user: dict,
    token: str,
) -> Literal[
    "created",
    "invalid_invitation",
    "invitation_expired",
    "invitation_email_mismatch",
    "email_exists",
]:
    now = int(time.time())
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    stored_password = user["password"]
    if not is_password_hash(stored_password):
        stored_password = hash_password(stored_password)

    with closing(_connect_users()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        invitation = connection.execute(
            """
            SELECT email, expires_at, used_at
            FROM lecturer_invitations
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if invitation is None or invitation[2] is not None:
            return "invalid_invitation"
        if invitation[1] <= now:
            return "invitation_expired"
        if invitation[0].lower() != user["email"].lower():
            return "invitation_email_mismatch"
        if connection.execute(
            "SELECT 1 FROM users WHERE email = ?",
            (user["email"],),
        ).fetchone():
            return "email_exists"

        try:
            connection.execute(
                """
                INSERT INTO users (id, name, email, password, role)
                VALUES (?, ?, ?, ?, 'lecturer')
                """,
                (
                    user["id"],
                    user["name"],
                    user["email"],
                    stored_password,
                ),
            )
        except sqlite3.IntegrityError:
            return "email_exists"
        connection.execute(
            "UPDATE lecturer_invitations SET used_at = ? WHERE token_hash = ?",
            (now, token_hash),
        )
        connection.commit()
    user["password"] = stored_password
    return "created"


def load_persisted_users() -> None:
    with closing(_connect_users()) as connection:
        rows = connection.execute(
            "SELECT id, name, email, password, role FROM users"
        ).fetchall()
    for user_id, name, email, password, role in rows:
        USERS[user_id] = {
            "id": user_id,
            "name": name,
            "email": email,
            "password": password,
            "role": role,
        }


def persist_user(user: dict) -> None:
    if not is_password_hash(user["password"]):
        user["password"] = hash_password(user["password"])
    with closing(_connect_users()) as connection:
        connection.execute(
            """
            INSERT INTO users (id, name, email, password, role)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                email = excluded.email,
                password = excluded.password,
                role = excluded.role
            """,
            (user["id"], user["name"], user["email"], user["password"], user["role"]),
        )
        connection.commit()


def persist_course(course: dict) -> None:
    with closing(_connect_users()) as connection:
        connection.execute(
            """
            INSERT INTO courses (id, name, description, lecturer_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                lecturer_id = excluded.lecturer_id
            """,
            (
                course["id"],
                course["name"],
                course.get("description") or "",
                course["lecturer_id"],
            ),
        )
        connection.commit()


def load_persisted_courses() -> None:
    with closing(_connect_users()) as connection:
        rows = connection.execute(
            "SELECT id, name, description, lecturer_id FROM courses"
        ).fetchall()
    for course_id, name, description, lecturer_id in rows:
        is_global = course_id.startswith("course-") or lecturer_id in ("all", "system", "global", "lecturer-1")
        if course_id in COURSES:
            COURSES[course_id]["name"] = name
            COURSES[course_id]["description"] = description
            COURSES[course_id]["lecturer_id"] = lecturer_id
            COURSES[course_id]["is_global"] = is_global
        else:
            COURSES[course_id] = {
                "id": course_id,
                "name": name,
                "description": description,
                "lecturer_id": lecturer_id,
                "student_ids": [],
                "is_global": is_global,
            }


def persist_lms_quiz(quiz: dict) -> None:
    with closing(_connect_users()) as connection:
        connection.execute(
            """
            INSERT INTO lms_quizzes (id, course_id, title, creator_id, questions_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                course_id = excluded.course_id,
                title = excluded.title,
                creator_id = excluded.creator_id,
                questions_json = excluded.questions_json
            """,
            (
                quiz["id"],
                quiz["course_id"],
                quiz["title"],
                quiz["creator_id"],
                json.dumps(quiz.get("questions") or [], ensure_ascii=False),
            ),
        )
        connection.commit()


def load_persisted_lms_quizzes() -> None:
    with closing(_connect_users()) as connection:
        rows = connection.execute(
            "SELECT id, course_id, title, creator_id, questions_json FROM lms_quizzes"
        ).fetchall()
    for quiz_id, course_id, title, creator_id, questions_json in rows:
        try:
            questions = json.loads(questions_json) if questions_json else []
        except (TypeError, ValueError):
            questions = []
        LMS_QUIZZES[quiz_id] = {
            "id": quiz_id,
            "course_id": course_id,
            "title": title,
            "creator_id": creator_id,
            "questions": questions if isinstance(questions, list) else [],
        }


def persist_submission(submission: dict) -> None:
    with closing(_connect_users()) as connection:
        connection.execute(
            """
            INSERT INTO submissions (id, quiz_id, student_id, correct, total, score, answers_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                quiz_id = excluded.quiz_id,
                student_id = excluded.student_id,
                correct = excluded.correct,
                total = excluded.total,
                score = excluded.score,
                answers_json = excluded.answers_json
            """,
            (
                submission["id"],
                submission["quiz_id"],
                submission["student_id"],
                int(submission.get("correct", 0)),
                int(submission.get("total", 0)),
                int(submission.get("score", 0)),
                json.dumps(submission.get("answers") or {}, ensure_ascii=False),
            ),
        )
        connection.commit()


def load_persisted_submissions() -> None:
    with closing(_connect_users()) as connection:
        rows = connection.execute(
            "SELECT id, quiz_id, student_id, correct, total, score, answers_json FROM submissions"
        ).fetchall()
    for submission_id, quiz_id, student_id, correct, total, score, answers_json in rows:
        try:
            answers = json.loads(answers_json) if answers_json else {}
        except (TypeError, ValueError):
            answers = {}
        SUBMISSIONS[submission_id] = {
            "id": submission_id,
            "quiz_id": quiz_id,
            "student_id": student_id,
            "correct": int(correct or 0),
            "total": int(total or 0),
            "score": int(score or 0),
            "answers": answers if isinstance(answers, dict) else {},
        }


def seed_demo_users() -> None:
    demo_defaults = [
        {
            "id": "lecturer-1",
            "name": "Dosen",
            "email": "dosen@gmail.com",
            "password": "dosen123",
            "role": "lecturer",
        },
        {
            "id": "student-1",
            "name": "Mahasiswa",
            "email": "mahasiswa@gmail.com",
            "password": "mahasiswa123",
            "role": "student",
        },
    ]
    with closing(_connect_users()) as connection:
        for demo in demo_defaults:
            row = connection.execute(
                "SELECT id, name, email, password, role FROM users WHERE email = ? COLLATE NOCASE",
                (demo["email"],),
            ).fetchone()
            if not row:
                hashed = hash_password(demo["password"])
                connection.execute(
                    "INSERT INTO users (id, name, email, password, role) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, email = excluded.email, password = excluded.password, role = excluded.role",
                    (demo["id"], demo["name"], demo["email"], hashed, demo["role"]),
                )
                connection.commit()
                USERS[demo["id"]] = {
                    "id": demo["id"],
                    "name": demo["name"],
                    "email": demo["email"],
                    "password": hashed,
                    "role": demo["role"],
                }
            else:
                USERS[row[0]] = {
                    "id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "password": row[3],
                    "role": row[4],
                }


load_persisted_users()
seed_demo_users()


DEFAULT_COURSES = [
    {
        "id": "course-basis-data",
        "name": "Basis Data & Desain Relasional",
        "description": "Konsep basis data relasional, ERD, SQL query, normalisasi tabel, dan optimasi indeks performa.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-web-dev",
        "name": "Pemrograman Web Modern",
        "description": "Pengembangan frontend & backend responsif, arsitektur REST API, HTTP methods, dan otentikasi token.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-algoritma",
        "name": "Algoritma & Struktur Data",
        "description": "Analisis kompleksitas Big-O, stack, queue, linked list, tree, graf, serta algoritma searching & sorting.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-ai-ml",
        "name": "Kecerdasan Buatan & Machine Learning",
        "description": "Prinsip supervised & unsupervised learning, neural network, feature engineering, dan metrik evaluasi model.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-jaringan",
        "name": "Jaringan Komputer & Keamanan Siber",
        "description": "Model referensi OSI, protokol TCP/IP, addressing & subnetting, routing, firewall, serta enkripsi data.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-os",
        "name": "Sistem Operasi & Arsitektur Komputer",
        "description": "Manajemen proses & thread, algoritma penjadwalan CPU, memori virtual, paging, dan manajemen storage.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-rpl",
        "name": "Rekayasa Perangkat Lunak",
        "description": "Metodologi Agile/Scrum, SDLC, use case & sequence diagram UML, software testing, dan clean architecture.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-mobile",
        "name": "Pengembangan Aplikasi Mobile",
        "description": "Arsitektur aplikasi mobile Android & iOS, lifecycle activity/komponen, reactive UI, dan state management.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-ui-ux",
        "name": "Desain Antarmuka & Pengalaman Pengguna (UI/UX)",
        "description": "Prinsip Human-Centered Design, wireframing, interactive prototyping, usability testing, dan design system.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-cloud",
        "name": "Cloud Computing & DevOps",
        "description": "Layanan cloud IaaS/PaaS/SaaS, containerisasi Docker, orkestrasi, CI/CD automated pipeline, dan skalabilitas.",
        "lecturer_id": "lecturer-1",
        "student_ids": ["student-1"],
        "is_global": True,
    },
    {
        "id": "course-analitik-data",
        "name": "Analitik Data & Visualisasi",
        "description": "Pengolahan data, statistik deskriptif, eksplorasi dataset, dan penyajian insight melalui visualisasi yang efektif.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-python",
        "name": "Pemrograman Python Terapan",
        "description": "Sintaks Python, fungsi dan modul, pemrograman berorientasi objek, otomasi, serta pengolahan berkas.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-game-dev",
        "name": "Pengembangan Game Interaktif",
        "description": "Game loop, input pemain, collision detection, desain level, animasi, dan prinsip pengalaman bermain.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-iot",
        "name": "Internet of Things & Sistem Tertanam",
        "description": "Mikrokontroler, sensor dan aktuator, komunikasi perangkat, protokol IoT, serta pemrosesan data perangkat.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-big-data",
        "name": "Big Data & Data Engineering",
        "description": "Pipeline ETL, penyimpanan terdistribusi, pemrosesan batch dan streaming, serta kualitas dan tata kelola data.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-blockchain",
        "name": "Blockchain & Kontrak Pintar",
        "description": "Struktur blok dan konsensus, jaringan terdistribusi, aset digital, serta desain dan pengujian smart contract.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-manajemen-proyek-ti",
        "name": "Manajemen Proyek Teknologi Informasi",
        "description": "Perencanaan ruang lingkup, estimasi, pengelolaan risiko, komunikasi tim, dan pemantauan proyek TI.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-etika-digital",
        "name": "Etika Profesi & Hukum Digital",
        "description": "Etika profesi teknologi, privasi, hak kekayaan intelektual, tanggung jawab digital, dan regulasi informasi.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-komputasi-grafis",
        "name": "Komputasi Grafis & Multimedia",
        "description": "Representasi citra, transformasi geometri, rendering dasar, pemrosesan multimedia, dan format media digital.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
    {
        "id": "course-english-tech",
        "name": "Bahasa Inggris untuk Teknologi",
        "description": "Kosakata teknis, membaca dokumentasi, menulis laporan, dan komunikasi profesional dalam lingkungan teknologi.",
        "lecturer_id": "lecturer-1",
        "student_ids": [],
        "is_global": True,
    },
]

DEFAULT_QUIZZES = [
    {
        "id": "quiz-basis-data",
        "course_id": "course-basis-data",
        "title": "Kuis Normalisasi & Primary Key",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-bd-1",
                "type": "mcq",
                "body": "Apa fungsi utama primary key dalam sebuah tabel basis data relasional?",
                "options": [
                    "Mengidentifikasi setiap baris (record) secara unik",
                    "Menghapus duplikasi baris secara otomatis",
                    "Mengurutkan baris data secara alfabetis",
                    "Menyimpan file multimedia",
                ],
                "answer": "Mengidentifikasi setiap baris (record) secara unik",
                "rationale": "Primary key menjamin keunikan identitas dari setiap record dalam satu tabel.",
            },
            {
                "id": "q-bd-2",
                "type": "short",
                "body": "Bentuk normalisasi yang mensyaratkan tidak adanya ketergantungan transitif disebut bentuk normal ke berapa?",
                "options": None,
                "answer": "3NF",
                "rationale": "Third Normal Form (3NF) menghilangkan ketergantungan fungsional transitif.",
            },
        ],
    },
    {
        "id": "quiz-web-dev",
        "course_id": "course-web-dev",
        "title": "Kuis Arsitektur RESTful API & HTTP",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-wd-1",
                "type": "mcq",
                "body": "Metode HTTP manakah yang bersifat idempoten dan digunakan untuk mengganti resource secara utuh?",
                "options": ["PUT", "POST", "PATCH", "DELETE"],
                "answer": "PUT",
                "rationale": "PUT bersifat idempoten dan mengirimkan seluruh representasi resource yang diperbarui.",
            },
            {
                "id": "q-wd-2",
                "type": "short",
                "body": "Status code HTTP berapakah yang menandakan bahwa resource baru telah berhasil dibuat di server?",
                "options": None,
                "answer": "201",
                "rationale": "HTTP 201 Created mengindikasikan pembuatan resource baru berhasil.",
            },
        ],
    },
    {
        "id": "quiz-algoritma",
        "course_id": "course-algoritma",
        "title": "Kuis Kompleksitas Waktu & Tree",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-algo-1",
                "type": "mcq",
                "body": "Berapa kompleksitas waktu pencarian rata-rata pada Balanced Binary Search Tree?",
                "options": ["O(log n)", "O(n)", "O(n log n)", "O(1)"],
                "answer": "O(log n)",
                "rationale": "Balanced BST membagi ruang pencarian menjadi separuh pada tiap langkah.",
            },
            {
                "id": "q-algo-2",
                "type": "short",
                "body": "Struktur data linier yang bekerja dengan prinsip Last In First Out (LIFO) adalah?",
                "options": None,
                "answer": "Stack",
                "rationale": "Stack beroperasi dengan prinsip LIFO.",
            },
        ],
    },
    {
        "id": "quiz-ai-ml",
        "course_id": "course-ai-ml",
        "title": "Kuis Dasar Machine Learning & Evaluasi",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-ai-1",
                "type": "mcq",
                "body": "Paradigma machine learning di mana model dilatih menggunakan data yang sudah berlabel disebut?",
                "options": [
                    "Supervised Learning",
                    "Unsupervised Learning",
                    "Reinforcement Learning",
                    "Semi-supervised Learning",
                ],
                "answer": "Supervised Learning",
                "rationale": "Supervised learning memetakan input ke output menggunakan data latih berlabel.",
            },
            {
                "id": "q-ai-2",
                "type": "short",
                "body": "Metrik evaluasi yang mengukur proporsi kasus positif aktual yang berhasil ditebak benar oleh model disebut?",
                "options": None,
                "answer": "Recall",
                "rationale": "Recall mengukur sensitivitas model terhadap kelas positif yang sebenarnya.",
            },
        ],
    },
    {
        "id": "quiz-jaringan",
        "course_id": "course-jaringan",
        "title": "Kuis Model OSI & Protokol Jaringan",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-net-1",
                "type": "mcq",
                "body": "Layer pada model OSI yang bertanggung jawab atas penentuan rute (routing) dan pengalamatan logis adalah?",
                "options": [
                    "Network Layer",
                    "Transport Layer",
                    "Data Link Layer",
                    "Physical Layer",
                ],
                "answer": "Network Layer",
                "rationale": "Network Layer (Lapisan 3) bertugas mengelola routing dan alamat IP.",
            },
            {
                "id": "q-net-2",
                "type": "short",
                "body": "Protokol transport yang menyediakan pengiriman data connection-oriented andal dan berurutan adalah?",
                "options": None,
                "answer": "TCP",
                "rationale": "TCP menjamin keandalan pengiriman paket data end-to-end.",
            },
        ],
    },
    {
        "id": "quiz-os",
        "course_id": "course-os",
        "title": "Kuis Penjadwalan CPU & Memori Virtual",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-os-1",
                "type": "mcq",
                "body": "Fenomena di mana sistem menghabiskan waktu lebih banyak untuk aktivitas paging daripada eksekusi proses disebut?",
                "options": ["Thrashing", "Deadlock", "Starvation", "Race Condition"],
                "answer": "Thrashing",
                "rationale": "Thrashing terjadi saat memori utama tidak mencukupi sehingga frekuensi page fault meningkat drastis.",
            },
            {
                "id": "q-os-2",
                "type": "short",
                "body": "Algoritma penjadwalan CPU preemptive yang menggunakan pembagian waktu (time quantum) adalah?",
                "options": None,
                "answer": "Round Robin",
                "rationale": "Round Robin membagi jatah waktu CPU secara bergiliran menggunakan quantum time.",
            },
        ],
    },
    {
        "id": "quiz-rpl",
        "course_id": "course-rpl",
        "title": "Kuis Metodologi Agile & UML Modeling",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-rpl-1",
                "type": "mcq",
                "body": "Diagram UML manakah yang memodelkan interaksi antar-objek dalam urutan kronologis waktu?",
                "options": [
                    "Sequence Diagram",
                    "Class Diagram",
                    "Use Case Diagram",
                    "State Machine Diagram",
                ],
                "answer": "Sequence Diagram",
                "rationale": "Sequence diagram fokus pada urutan pesan yang ditukarkan antar-objek sepanjang waktu.",
            },
            {
                "id": "q-rpl-2",
                "type": "short",
                "body": "Pertemuan singkat harian tim Scrum untuk menyelaraskan pekerjaan dan kendala disebut Daily?",
                "options": None,
                "answer": "Standup",
                "rationale": "Daily Standup merupakan rapat harian berdurasi 15 menit dalam Scrum.",
            },
        ],
    },
    {
        "id": "quiz-mobile",
        "course_id": "course-mobile",
        "title": "Kuis Lifecycle & Arsitektur Mobile",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-mob-1",
                "type": "mcq",
                "body": "Pada siklus hidup Activity Android, method manakah yang dipanggil tepat sebelum layar siap menerima interaksi pengguna?",
                "options": ["onResume", "onStart", "onCreate", "onPause"],
                "answer": "onResume",
                "rationale": "onResume menandakan activity berada di foreground dan siap berinteraksi.",
            },
            {
                "id": "q-mob-2",
                "type": "short",
                "body": "Pola pemisahan logika aplikasi dan UI yang terdiri dari Model, View, dan ViewModel disingkat?",
                "options": None,
                "answer": "MVVM",
                "rationale": "Model-View-ViewModel (MVVM) adalah standar arsitektur modern pada pengembangan aplikasi mobile.",
            },
        ],
    },
    {
        "id": "quiz-ui-ux",
        "course_id": "course-ui-ux",
        "title": "Kuis Prinsip Desain UI/UX & Heuristik",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-ui-1",
                "type": "mcq",
                "body": "Hukum UX yang menyatakan bahwa waktu untuk mengambil keputusan meningkat seiring bertambahnya pilihan adalah?",
                "options": ["Hick's Law", "Fitts's Law", "Miller's Law", "Jakob's Law"],
                "answer": "Hick's Law",
                "rationale": "Hick's Law menjelaskan pengaruh jumlah stimulus atau alternatif terhadap durasi pengambilan keputusan.",
            },
            {
                "id": "q-ui-2",
                "type": "short",
                "body": "Representasi sketsa hitam-putih kerangka visual struktur halaman web atau aplikasi disebut?",
                "options": None,
                "answer": "Wireframe",
                "rationale": "Wireframe digunakan untuk memetakan arsitektur informasi dan layout tanpa elemen visual dekoratif.",
            },
        ],
    },
    {
        "id": "quiz-cloud",
        "course_id": "course-cloud",
        "title": "Kuis Containerisasi Docker & Konsep Cloud",
        "creator_id": "lecturer-1",
        "questions": [
            {
                "id": "q-cld-1",
                "type": "mcq",
                "body": "Teknologi isolasi tingkat OS yang membungkus kode program dan dependensinya dalam paket ringan disebut?",
                "options": ["Container", "Hypervisor", "Virtual Machine", "BIOS"],
                "answer": "Container",
                "rationale": "Container membungkus aplikasi beserta dependensinya secara terisolasi di atas kernel host.",
            },
            {
                "id": "q-cld-2",
                "type": "short",
                "body": "Model layanan cloud di mana penyedia menyediakan platform komputasi dan runtime sehingga pengembang hanya fokus pada aplikasi disebut?",
                "options": None,
                "answer": "PaaS",
                "rationale": "Platform as a Service (PaaS) mengabstraksi manajemen infrastruktur dasar.",
            },
        ],
    },
]


def seed_lms() -> None:
    for course_data in DEFAULT_COURSES:
        cid = course_data["id"]
        if cid not in COURSES:
            COURSES[cid] = {
                "id": cid,
                "name": course_data["name"],
                "description": course_data["description"],
                "lecturer_id": course_data["lecturer_id"],
                "student_ids": list(course_data["student_ids"]),
                "is_global": True,
            }
        else:
            COURSES[cid]["name"] = course_data["name"]
            COURSES[cid]["description"] = course_data["description"]
            COURSES[cid]["is_global"] = True
            for sid in course_data["student_ids"]:
                if sid not in COURSES[cid]["student_ids"]:
                    COURSES[cid]["student_ids"].append(sid)
        persist_course(COURSES[cid])

    for quiz_data in DEFAULT_QUIZZES:
        qid = quiz_data["id"]
        if qid not in LMS_QUIZZES:
            quiz = dict(quiz_data)
            LMS_QUIZZES[qid] = quiz
            persist_lms_quiz(quiz)


def persist_course_enrollment(course_id: str, user_id: str) -> None:
    with closing(_connect_users()) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO course_enrollments (course_id, user_id) VALUES (?, ?)",
            (course_id, user_id),
        )
        connection.commit()


def load_persisted_enrollments() -> None:
    with closing(_connect_users()) as connection:
        rows = connection.execute(
            "SELECT course_id, user_id FROM course_enrollments"
        ).fetchall()
    for course_id, user_id in rows:
        course = COURSES.get(course_id)
        if course is not None and user_id in USERS and user_id not in course["student_ids"]:
            course["student_ids"].append(user_id)


seed_lms()
load_persisted_courses()
load_persisted_lms_quizzes()
load_persisted_submissions()
load_persisted_enrollments()


def new_id() -> str:
    return str(uuid.uuid4())
