"""Aturan validasi keluaran LLM (panduan bagian 4). Murni Python, tanpa dependensi."""
from __future__ import annotations

import re

_STOP = set(
    "yang dan di ke dari untuk pada adalah merupakan dengan atau itu ini dalam sebagai "
    "oleh akan dapat agar serta juga tidak ada suatu para".split()
)


def tokens(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def content_tokens(s: str) -> list[str]:
    return [t for t in tokens(s) if t not in _STOP]


def norm(s: str) -> str:
    return " ".join(tokens(s))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(content_tokens(a)), set(content_tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def grounding_ratio(answer: str, chunk: str) -> float:
    """Porsi kata kunci jawaban yang muncul di chunk sumber."""
    ans = content_tokens(answer) or tokens(answer)
    if not ans:
        return 0.0
    pool = set(tokens(chunk))
    return sum(t in pool for t in ans) / len(ans)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    return ""


def validate_question(
    q: dict,
    chunks: dict[int, str],
    requested_type: str,
    accepted_bodies: list[str],
    grounding_threshold: float = 0.6,
    dup_threshold: float = 0.8,
) -> list[str]:
    """Kembalikan daftar alasan penolakan. Kosong berarti soal lolos."""
    if not isinstance(q, dict):
        return ["item soal harus berupa objek"]

    errs: list[str] = []
    qtype = q.get("type")
    body = _text(q.get("body"))
    answer = _text(q.get("answer"))
    rationale = _text(q.get("rationale"))
    options = q.get("options")

    if qtype not in ("mcq", "short"):
        return [f"type tidak valid: {qtype!r}"]
    if requested_type != "mix" and qtype != requested_type:
        errs.append(f"tipe diminta {requested_type}, soal bertipe {qtype}")
    if not body:
        errs.append("body kosong atau bukan teks")
    if not answer:
        errs.append("answer kosong atau bukan teks")

    if qtype == "mcq":
        if not isinstance(options, list) or len(options) != 4:
            errs.append("mcq harus punya tepat 4 opsi")
        else:
            option_texts = [_text(option) for option in options]
            if any(isinstance(option, (dict, list)) for option in options):
                errs.append("opsi mcq harus berupa teks")
            normed = [norm(option) for option in option_texts]
            if len(set(normed)) != 4 or "" in normed:
                errs.append("opsi mcq harus 4 teks berbeda")
            if norm(answer) not in normed:
                errs.append("answer tidak ada di antara opsi")
    else:
        n = len(tokens(answer))
        if not 1 <= n <= 5:
            errs.append(f"jawaban short harus 1-5 kata (dapat {n})")
        if options:
            errs.append("short tidak boleh punya opsi")

    src = q.get("source_chunk")
    chunk = chunks.get(src) if isinstance(src, int) else None
    if chunk is None:
        errs.append(f"source_chunk {src!r} tidak ada di konteks yang dikirim")
    else:
        if answer and grounding_ratio(answer, chunk) < grounding_threshold:
            errs.append("kata kunci answer tidak ditemukan di chunk sumber (dugaan halusinasi)")
        if len(rationale) < 20:
            errs.append("rationale kosong atau terlalu pendek")
        elif len(set(content_tokens(rationale)) & set(content_tokens(chunk))) < 2:
            errs.append("rationale tidak merujuk isi chunk")

    if body and any(jaccard(body, b) >= dup_threshold for b in accepted_bodies):
        errs.append("hampir identik dengan soal lain")
    return errs
