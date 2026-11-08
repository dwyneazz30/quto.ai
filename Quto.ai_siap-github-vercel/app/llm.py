"""Adapter model open source (Groq, OpenRouter, Together, Ollama, dll.) via API kompatibel OpenAI.
Bentuknya meniru klien Claude (`client.messages.create`) agar generator.py dan tes tetap berlaku."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS


class OpenAICompatClient:
    def __init__(self, api_key: str, base_url: str):
        from openai import AsyncOpenAI

        self._c = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.messages = NS(create=self._create)

    async def _create(self, *, model, max_tokens, system, messages, tools=None, tool_choice=None, **_):
        kw = {}
        if tools:
            t = tools[0]
            kw["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}]
            kw["tool_choice"] = {"type": "function", "function": {"name": t["name"]}}
        r = await self._c.chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=0.4,
            messages=[{"role": "system", "content": system}, *messages], **kw)
        msg, blocks = r.choices[0].message, []
        for tc in msg.tool_calls or []:
            try:
                blocks.append(NS(type="tool_use", name=tc.function.name, input=json.loads(tc.function.arguments)))
            except ValueError:
                pass  # jatuh ke fallback teks / ulang di generator
        if not blocks:
            blocks.append(NS(type="text", text=msg.content or ""))
        u = r.usage
        return NS(content=blocks, usage=NS(input_tokens=getattr(u, "prompt_tokens", 0),
                                           output_tokens=getattr(u, "completion_tokens", 0)))
