"""Pecah teks menjadi chunk 300-500 kata dengan tumpang tindih ~50 kata (panduan bagian 2)."""
from __future__ import annotations


def chunk_text(text: str, target_words: int = 400, overlap: int = 50) -> list[str]:
    words = text.split()
    if not words:
        return []
    if len(words) <= target_words:
        return [" ".join(words)]
    step = target_words - overlap
    chunks: list[str] = []
    for start in range(0, len(words), step):
        piece = words[start:start + target_words]
        # sisa yang terlalu pendek sudah tercakup oleh overlap chunk sebelumnya
        if chunks and len(piece) <= overlap:
            break
        chunks.append(" ".join(piece))
        if start + target_words >= len(words):
            break
    return chunks


def pick_context(chunks: list[str], count: int) -> list[tuple[int, str]]:
    """Ambil chunk tersebar merata. Mengembalikan [(idx_asli, teks)]."""
    if not chunks:
        return []
    k = min(len(chunks), max(3, count))
    if k >= len(chunks):
        return list(enumerate(chunks))
    step = (len(chunks) - 1) / (k - 1) if k > 1 else 0
    idxs = sorted({round(i * step) for i in range(k)})
    return [(i, chunks[i]) for i in idxs]
