"""Prompt generator soal. Isi mengikuti quto_backend_guide.md bagian 3.

Perbedaan dari panduan: keluaran dipaksa lewat tool use (skema JSON terjamin),
bukan "JSON saja di teks", sehingga tidak perlu menebak blok kode atau teks pembuka.
"""

BLOOM = {
    "easy": "C1 · Mengingat",
    "medium": "C2 · Memahami",
    "hard": "C4 · Menganalisis",
}

SYSTEM_PROMPT = """Kamu adalah pembuat soal ujian. Buat soal HANYA dari teks sumber yang diberikan.
Jangan memakai pengetahuan di luar teks. Jika teks tidak cukup untuk membuat
soal yang diminta, kembalikan lebih sedikit soal. Tulis dalam Bahasa Indonesia.
Kirim hasilnya dengan memanggil tool submit_questions, tanpa teks lain."""

TOOL_NAME = "submit_questions"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Kirim daftar soal yang sudah dibuat dari teks sumber.",
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["mcq", "short"]},
                        "body": {"type": "string"},
                        "options": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                            "description": "4 opsi untuk mcq, null untuk short",
                        },
                        "answer": {
                            "type": "string",
                            "description": "Untuk mcq: teks opsi yang benar (bukan huruf). Untuk short: 1-5 kata.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Pembahasan yang merujuk kalimat sumber.",
                        },
                        "bloom_level": {"type": "string"},
                        "source_chunk": {
                            "type": "integer",
                            "description": "Nomor chunk sumber, sesuai label [chunk N].",
                        },
                    },
                    "required": ["type", "body", "options", "answer", "rationale", "bloom_level", "source_chunk"],
                },
            }
        },
        "required": ["questions"],
    },
}

_TYPE_DESC = {
    "mcq": "mcq (pilihan ganda 4 opsi)",
    "short": "short (isian singkat)",
    "mix": "mix (campuran mcq dan short)",
}


def build_user_prompt(
    chunks: list[tuple[int, str]],
    count: int,
    qtype: str,
    difficulty: str,
    existing_bodies: list[str] | None = None,
    feedback: list[str] | None = None,
) -> str:
    parts = [
        f"""Tingkat kesulitan: {difficulty}
  easy   = C1 Mengingat (fakta langsung dari teks)
  medium = C2 Memahami (parafrase, hubungan antar konsep)
  hard   = C4 Menganalisis (perbandingan, sebab-akibat, penerapan)
Jumlah soal: {count}
Tipe: {_TYPE_DESC[qtype]}
bloom_level yang dipakai: {BLOOM[difficulty]}

Setiap soal berisi: type, body, options (4 opsi berbeda untuk mcq, null untuk short),
answer, rationale (jelaskan dan rujuk kalimat sumber), bloom_level,
source_chunk (nomor chunk). Untuk mcq, answer harus sama persis dengan salah satu opsi.
Untuk short, answer maksimal 5 kata dan harus kata/frasa yang ada di teks sumber."""
    ]
    if existing_bodies:
        parts.append("Soal berikut SUDAH ada, jangan buat yang mirip:\n" + "\n".join(f"- {b}" for b in existing_bodies))
    if feedback:
        parts.append("Percobaan sebelumnya ditolak karena:\n" + "\n".join(f"- {f}" for f in feedback))
    parts.append("TEKS SUMBER:\n" + "\n\n".join(f"[chunk {i}] {t}" for i, t in chunks))
    return "\n\n".join(parts)
