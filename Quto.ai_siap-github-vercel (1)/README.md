# Quto.id – backend LMS/generator soal (FastAPI + model open source)

Backend untuk generator soal dan LMS sederhana (`quto_backend_guide.md`, bagian 2–4 sebagai dasar generator). Sudah ada auth/session, course/quiz LMS, upload materi teks, generate quiz (sinkron LMS dan job async), serta ekspor PDF. Parser PDF/DOCX untuk endpoint materi `/courses/{id}/materials` belum ada; pembuatan quiz dari PDF tersedia di endpoint LMS terpisah. Quiz/submission LMS dan data akun/course/enrollment disimpan di SQLite (`quto_data.db`); job/material/generator-quiz async dan session login masih in-memory.

## Menjalankan
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && export $(grep -v '^#' .env | xargs)   # isi LLM_API_KEY (+ LLM_BASE_URL, QUTO_MODEL)
```

## 0. Buka aplikasinya
`uvicorn app.main:app --reload` lalu buka http://localhost:8000 (responsif untuk HP dan desktop).

### Windows PowerShell
```powershell
cd D:\Quto.ai\Quto.ai
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Ganti nilai contoh LLM_API_KEY di .env dengan API key Groq yang valid jika memakai generator AI.
# Jangan bagikan API key; di layanan hosting, atur sebagai environment variable server.
python -m uvicorn app.main:app --reload
```

## Publikasi demo di Render

Repo menyertakan `render.yaml` untuk membuat web service demo gratis. Push source ke repo GitHub, pilih **New > Blueprint** di Render, lalu hubungkan repo tersebut.

- Gunakan database demo bersih di Render; jangan unggah `quto_data.db` lokal karena berisi data akun/mahasiswa.
- Blueprint sengaja tidak mengatur `LLM_API_KEY`, sehingga API key AI lokal tidak terekspos atau bisa dihabiskan pengunjung. Generate quiz dengan AI memerlukan key terpisah dan pembatasan penggunaan.
- Endpoint reset password lama (`/api/auth/reset-password`) sudah dinonaktifkan; alur yang tersedia memakai token (`/request` + `/confirm`). Pastikan konfigurasi dan batas akses demo sesuai kebutuhan publik.
- Filesystem paket gratis bersifat sementara; perubahan akun/course dapat hilang saat instance dibuat ulang. Jangan gunakan Blueprint demo ini untuk kelas sungguhan.

Akun pengguna disimpan permanen di `quto_data.db` pada folder project, termasuk setelah server restart. Password disimpan dalam bentuk hash. File database ini diabaikan oleh Git. LMS quiz dan submission juga disimpan di SQLite; job/material/generator-quiz async serta session login masih in-memory dan dapat hilang saat server restart.

Dosen dapat membuat course dan quiz dari materi, sedangkan mahasiswa dapat membuka quiz, mengirim jawaban, dan melihat nilai.

Sepuluh course tambahan (total 20 course default) tersedia untuk semua mahasiswa dan dosen, termasuk akun yang baru didaftarkan. Dosen dapat memilih course tujuan saat membuat quiz atau memulai pembuatan quiz langsung dari kartu course.

## Deploy ke Vercel

Repo ini sudah menyertakan `index.py` (entrypoint FastAPI di root) dan `vercel.json`. Jangan menambahkan `rewrites` ke `/api/index`: Vercel akan meneruskan path hasil rewrite ke FastAPI sehingga semua halaman menjadi 404.

1. Push repo ke GitHub, lalu di Vercel pilih **Add New > Project** dan impor repo tersebut. Framework dibiarkan terdeteksi otomatis (Python/FastAPI), tanpa build command khusus.
2. Di **Settings > Environment Variables** isi: `LLM_API_KEY` (key Groq), `QUTO_MODEL` (opsional, default `openai/gpt-oss-120b`), `QUTO_ADMIN_API_KEY` (secret acak panjang), dan `QUTO_DISABLE_PASSWORD_RESET=true`.
3. Deploy. Jangan pernah meng-commit `.env` atau `quto_data.db`.

**Batasan Vercel (serverless):** filesystem hanya bisa ditulis di `/tmp` dan bersifat sementara, serta session login/job disimpan di memori per instance. Akun yang didaftarkan dan sesi login bisa hilang saat instance baru dibuat, atau tidak terlihat oleh instance lain. Cocok untuk demo; untuk pemakaian sungguhan pindahkan database ke layanan eksternal (mis. Postgres/Turso) dan sesi ke penyimpanan persisten, atau gunakan Render (`render.yaml`).

## Registrasi akun
- Registrasi publik hanya membuat akun mahasiswa. Saat login, pilih role yang sesuai dengan akun Anda (Dosen atau Mahasiswa).
- Akun dosen dibuat melalui undangan sekali pakai yang terkait ke alamat email dosen.
- Atur `QUTO_ADMIN_API_KEY` di `.env` atau environment service dengan secret acak yang panjang. Pada Render, tambahkan sebagai environment variable di dashboard. Admin menerbitkan undangan melalui `POST /api/admin/lecturer-invitations` memakai header `X-Admin-Key`; token berlaku 72 jam secara default (maksimal 7 hari) dan hanya bisa digunakan sekali.
- Berikan `email` dan `invitation_token` kepada dosen. Dosen memilih **Gunakan undangan dosen** pada formulir daftar, lalu mendaftar dengan email yang sama. Token tidak dikirim otomatis melalui email.
- Untuk mencoba alur registrasi dan login dosen di komputer lokal, set `QUTO_ENABLE_DEMO_LECTURER_REGISTRATION=true` di `.env` lalu restart server. Tautan **Demo lokal dosen** hanya muncul di `localhost`; server juga menolak permintaan non-loopback. Pada formulir registrasi, kredensial demo ditampilkan dan diisi otomatis: `dosen.demo@quto.id` / `dosen123`. Setelah akun dibuat, kredensial yang sama bisa diisi otomatis dari bagian akun demo pada halaman login. Demo memakai satu akun khusus dengan undangan sekali pakai 30 menit. Jangan aktifkan opsi ini di produksi.

Contoh PowerShell untuk menerbitkan undangan:
```powershell
# Isi dengan nilai QUTO_ADMIN_API_KEY yang sama seperti di konfigurasi server.
$env:QUTO_ADMIN_API_KEY = "<secret-admin-yang-sudah-dikonfigurasi>"
$invite = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/api/admin/lecturer-invitations `
  -Headers @{ "X-Admin-Key" = $env:QUTO_ADMIN_API_KEY } `
  -ContentType "application/json" `
  -Body '{"email":"dosen@kampus.ac.id"}'
$invite.invitation_token
```

## 1. Uji generator dengan model sungguhan (tanpa server)
```bash
python -m scripts.try_generate sample_materi.txt --count 5 --type mix --difficulty medium
```
Cetak soal yang lolos, soal yang ditolak beserta alasannya, jumlah panggilan API, dan token.

## 2. Uji lewat API
```bash
uvicorn app.main:app --reload
```
Buka http://localhost:8000 untuk UI LMS utama. Untuk alur generator async (butuh login dosen): `POST /courses/{id}/materials` (field `text`) → `POST /quizzes/generate` → polling `GET /jobs/{id}` → `GET /quizzes/{quiz_id}`. Endpoint ini membutuhkan Bearer token dosen dan akses course yang sesuai.

## 3. Uji logika tanpa API key
```bash
python -m unittest discover -v      # jalankan unit tests, termasuk klien palsu
```

## Struktur
- `app/chunking.py` – chunk 400 kata, overlap 50, pilih konteks merata
- `app/prompts.py` – prompt dari panduan + skema tool `submit_questions`
- `app/validation.py` – aturan validasi soal (4 opsi, jawaban singkat, grounding/anti-halusinasi, duplikat, penolakan item tidak valid)
- `app/generator.py` – panggil LLM, validasi, ulangi maksimal `QUTO_MAX_RETRIES`
- `app/main.py` – endpoint FastAPI LMS/generator; endpoint lama `/dev/generate` tidak disediakan
- `app/static/lms.html` – UI LMS utama yang disajikan di `/`

## Yang perlu kamu putuskan/ganti
- Anti-halusinasi saat ini heuristik kata kunci (`QUTO_GROUNDING_THRESHOLD`). Soal `medium`/`hard` yang jawabannya diparafrase bisa ikut tertolak; sesuaikan angkanya setelah melihat hasil nyata.
- Cek duplikat memakai kemiripan kata (Jaccard), bukan embedding. Ganti saat pgvector aktif.
- Pasangan `BackgroundTasks` hanya untuk uji. Untuk produksi pindahkan `_run_job` ke Celery/RQ.
- Session login, materi upload, job async, dan generator-quiz async masih in-memory. Untuk deploy production/multi-instance, pindahkan ke storage persisten/terdistribusi.
- Model open source: `app/llm.py` menerjemahkan panggilan ke API kompatibel OpenAI (Groq, OpenRouter, Together, Ollama). Ganti penyedia cukup lewat `.env`.
- Model kecil (di bawah 8B) sering gagal berbahasa Indonesia/format JSON; mulai dari model yang lebih kuat dan naikkan `QUTO_MAX_RETRIES` bila perlu.
