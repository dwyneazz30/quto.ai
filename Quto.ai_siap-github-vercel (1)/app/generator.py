"""Pipeline generasi soal: pilih konteks -> panggil model -> validasi -> ulangi bila perlu."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .chunking import chunk_text, pick_context
from .config import Settings
from .prompts import BLOOM, SYSTEM_PROMPT, TOOL_NAME, TOOL_SCHEMA, build_user_prompt
from .validation import _text, validate_question


@dataclass
class GenerationResult:
    questions: list[dict]
    warnings: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # untuk debugging / laporan akurasi
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


def make_client(api_key: str | None, base_url: str | None = None):
    """Klien model open source (API kompatibel OpenAI). Untuk Ollama lokal, api_key boleh berisi 'ollama'."""
    if not api_key:
        raise RuntimeError("LLM_API_KEY belum diatur.")
    from .llm import OpenAICompatClient

    return OpenAICompatClient(api_key, base_url or "https://api.groq.com/openai/v1")


def _extract_questions(resp) -> list[dict]:
    """Ambil daftar soal dari respons tool use; fallback ke JSON di teks."""
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            qs = block.input.get("questions")
            if isinstance(qs, list):
                return qs
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)  # ValueError bila bukan JSON -> ditangani pemanggil
    return data["questions"]


class QuestionGenerator:
    def __init__(self, client, settings: Settings):
        self.client = client
        self.s = settings

    async def generate(self, material_text: str, count: int, qtype: str, difficulty: str) -> GenerationResult:
        chunks = chunk_text(material_text)
        if not chunks:
            raise ValueError("Materi kosong.")
        context = pick_context(chunks, count)
        chunk_map = dict(context)

        result = GenerationResult(questions=[])
        feedback: list[str] = []

        for attempt in range(1 + self.s.max_retries):
            need = count - len(result.questions)
            if need <= 0:
                break
            prompt = build_user_prompt(
                context, need, qtype, difficulty,
                existing_bodies=[q["body"] for q in result.questions],
                feedback=feedback[-5:],
            )
            resp = await self.client.messages.create(
                model=self.s.model,
                max_tokens=self.s.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
            result.api_calls += 1
            usage = getattr(resp, "usage", None)
            if usage:
                result.input_tokens += getattr(usage, "input_tokens", 0)
                result.output_tokens += getattr(usage, "output_tokens", 0)

            try:
                raw = _extract_questions(resp)
            except (ValueError, KeyError, TypeError) as e:
                feedback.append(f"keluaran tidak bisa di-parse ({e})")
                continue

            feedback = []
            for item in raw[:need]:  # abaikan kelebihan soal
                if not isinstance(item, dict):
                    result.rejected.append({"attempt": attempt, "question": item, "errors": ["item soal harus berupa objek"]})
                    feedback.append("item soal harus berupa objek")
                    continue
                errs = validate_question(
                    item, chunk_map, qtype,
                    accepted_bodies=[x["body"] for x in result.questions],
                    grounding_threshold=self.s.grounding_threshold,
                )
                if errs:
                    result.rejected.append({"attempt": attempt, "question": item, "errors": errs})
                    feedback.extend(errs)
                    continue
                body = _text(item.get("body"))
                answer = _text(item.get("answer"))
                rationale = _text(item.get("rationale"))
                options = item.get("options") if item["type"] == "mcq" else None
                result.questions.append({
                    "type": item["type"],
                    "body": body,
                    "options": [_text(option) for option in options] if isinstance(options, list) else None,
                    "answer": answer,
                    "rationale": rationale,
                    "bloom_level": item.get("bloom_level") or BLOOM[difficulty],
                    "difficulty": difficulty,
                    "source_chunk": item["source_chunk"],
                })

        if len(result.questions) < count:
            result.warnings.append(
                f"Hanya {len(result.questions)} dari {count} soal yang lolos validasi. "
                "Materi mungkin kurang panjang, atau tambah materi lalu coba lagi."
            )
        return result
