"""Uji generator dengan model sungguhan, tanpa FastAPI.

  export LLM_API_KEY=...
  python -m scripts.try_generate sample_materi.txt --count 5 --type mix --difficulty medium
"""
import argparse
import asyncio
import json

from app.config import get_settings
from app.generator import QuestionGenerator, make_client


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--type", default="mix", choices=["mcq", "short", "mix"])
    ap.add_argument("--difficulty", default="medium", choices=["easy", "medium", "hard"])
    ap.add_argument("--json", action="store_true", help="cetak hasil mentah sebagai JSON")
    a = ap.parse_args()

    s = get_settings()
    gen = QuestionGenerator(make_client(s.api_key, s.base_url), s)
    text = open(a.file, encoding="utf-8").read()
    res = await gen.generate(text, a.count, a.type, a.difficulty)

    if a.json:
        print(json.dumps(res.__dict__, ensure_ascii=False, indent=2))
        return
    for i, q in enumerate(res.questions, 1):
        print(f"\n{i}. [{q['type']}] {q['body']}")
        for j, o in enumerate(q["options"] or []):
            print(f"   {'ABCD'[j]}. {o}")
        print(f"   Jawaban : {q['answer']}\n   Bahasan : {q['rationale']}\n   Level   : {q['bloom_level']} (chunk {q['source_chunk']})")
    print(f"\nLolos {len(res.questions)}/{a.count} | ditolak {len(res.rejected)} | panggilan API {res.api_calls} "
          f"| token in/out {res.input_tokens}/{res.output_tokens}")
    for w in res.warnings:
        print("PERINGATAN:", w)
    for r in res.rejected:
        print("DITOLAK:", r["question"].get("body", "?")[:70], "->", "; ".join(r["errors"]))


asyncio.run(main())
