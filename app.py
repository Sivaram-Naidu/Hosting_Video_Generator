import os, re, uuid, base64, secrets, threading, zipfile, shutil, subprocess, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from dotenv import load_dotenv

from pipeline import run_pipeline
from storage import SharePointStorage

load_dotenv()
API_KEY = os.environ["SARVAM_API_KEY"].strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()           # empty = no login
MAX_CLIP_WORKERS = int(os.getenv("MAX_CLIP_WORKERS", "1"))     # keep low on 512 MB hosts
STORAGE = SharePointStorage.from_env()                          # None = keep videos local only

app = FastAPI(title="TuringIQ Video Generator")
OUTPUTS = Path("outputs")
WORK_ROOT = Path(os.getenv("WORK_ROOT", "work"))
IST = timezone(timedelta(hours=5, minutes=30))

# In-memory job registry
jobs: dict = {}
_busy = threading.Lock()   # one video at a time — keeps memory usage predictable


# ── Password protection (HTTP Basic, any username) ────────────────────────────

@app.middleware("http")
async def require_password(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/healthz":
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw.encode(), APP_PASSWORD.encode())
            except Exception:
                pass
        if not ok:
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="TuringIQ Video Generator"'})
    return await call_next(request)


# ── Helpers ───────────────────────────────────────────────────────────────────

def natural_key(p: str) -> int:
    nums = re.findall(r"\d+", os.path.basename(p))
    return int(nums[0]) if nums else 0


def clear_previous_files():
    """Wipe all files from the previous job so disk/memory never accumulates."""
    for d in (OUTPUTS, WORK_ROOT):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)


def video_basename(upload_name: str) -> str:
    """001_<ppt name> — serial continues from the files already in SharePoint."""
    stem = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]+', "_", Path(upload_name).stem)
    stem = stem.strip(" ._")[:80] or "video"
    if STORAGE:
        try:
            return f"{STORAGE.next_serial():03d}_{stem}"
        except Exception:
            pass   # SharePoint unreachable — fall back to a timestamp so names stay unique
    return f"{stem}_{datetime.now(IST):%Y%m%d_%H%M%S}"


def find_soffice():
    for cand in ("soffice", "libreoffice",
                 r"C:\Program Files\LibreOffice\program\soffice.exe"):
        path = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if path:
            return path
    return None


def pptx_to_images_libreoffice(soffice: str, pptx_path: str, img_dir: str) -> list:
    """PPTX/PPT -> PDF (LibreOffice) -> one 1920x1080 PNG per page (PyMuPDF)."""
    import fitz  # PyMuPDF
    fitz.TOOLS.mupdf_display_errors(False)   # LibreOffice PDFs trigger harmless structure-tree warnings

    pdf_dir = os.path.join(img_dir, "_pdf")
    profile = Path(img_dir, "_lo_profile").resolve().as_uri()
    try:
        subprocess.run([
            soffice, f"-env:UserInstallation={profile}",
            "--headless", "--convert-to", "pdf", "--outdir", pdf_dir, pptx_path,
        ], check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"LibreOffice conversion failed: {e.stderr.decode(errors='ignore')[-500:]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("LibreOffice conversion timed out")

    pdfs = list(Path(pdf_dir).glob("*.pdf"))
    if not pdfs:
        raise RuntimeError("LibreOffice produced no PDF — is the presentation valid?")

    paths = []
    with fitz.open(pdfs[0]) as doc:
        for i, page in enumerate(doc, 1):
            zoom = min(1920 / page.rect.width, 1080 / page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img_path = os.path.join(img_dir, f"slide_{i:03d}.png")
            pix.save(img_path)
            paths.append(img_path)
    shutil.rmtree(pdf_dir, ignore_errors=True)
    return paths


def pptx_to_images_powerpoint(pptx_path: str, img_dir: str) -> list:
    """Fallback for local Windows runs without LibreOffice."""
    import win32com.client
    ppt = prs = None
    try:
        ppt = win32com.client.Dispatch("PowerPoint.Application")
        prs = ppt.Presentations.Open(os.path.abspath(pptx_path), ReadOnly=True, WithWindow=False)
        paths = []
        for i in range(1, prs.Slides.Count + 1):
            img_path = os.path.abspath(os.path.join(img_dir, f"slide_{i:03d}.png"))
            prs.Slides(i).Export(img_path, "PNG", 1920, 1080)
            paths.append(img_path)
        return paths
    finally:
        if prs:
            try: prs.Close()
            except Exception: pass
        if ppt:
            try: ppt.Quit()
            except Exception: pass


def pptx_to_images(pptx_path: str, img_dir: str) -> list:
    soffice = find_soffice()
    if soffice:
        return pptx_to_images_libreoffice(soffice, pptx_path, img_dir)
    if sys.platform == "win32":
        return pptx_to_images_powerpoint(pptx_path, img_dir)
    raise RuntimeError("No PPTX converter available (install LibreOffice)")


def extract_zip_images(zip_path: str, img_dir: str) -> list:
    paths = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if name.startswith("__MACOSX/") or base.startswith("._"):
                continue
            if base.lower().endswith((".png", ".jpg", ".jpeg")):
                dest = os.path.join(img_dir, base)
                with z.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                paths.append(dest)
    return sorted(paths, key=natural_key)


# ── Background job runner ─────────────────────────────────────────────────────

def run_job(job_id: str, upload_path: str, texts: list, basename: str,
            draft: bool, speaker: str, language: str, target_duration_s: float):
    job = jobs[job_id]
    output_path = OUTPUTS / f"{basename}.mp4"
    srt_path = OUTPUTS / f"{basename}.srt"
    img_dir = os.path.join(os.path.dirname(upload_path), "images")
    os.makedirs(img_dir, exist_ok=True)

    def cb(pct, msg):
        # Pipeline gets 5–90 %, upload gets the rest
        job.update(progress=5 + int(pct * 0.85), message=msg)

    try:
        job.update(status="processing", progress=1, message="Converting slides to images…")
        if upload_path.lower().endswith((".pptx", ".ppt")):
            images = pptx_to_images(upload_path, img_dir)
        else:
            images = extract_zip_images(upload_path, img_dir)

        if not images:
            raise RuntimeError("No images found in the uploaded file")
        if len(images) != len(texts):
            raise RuntimeError(
                f"Slide count mismatch: {len(images)} slides but {len(texts)} narration "
                f"blocks (separated by ---). They must match."
            )

        run_pipeline(
            images=images,
            texts=texts,
            output_path=str(output_path),
            srt_path=str(srt_path),
            api_key=API_KEY,
            speaker=speaker,
            language=language,
            draft=draft,
            target_duration_s=target_duration_s,
            max_clip_workers=MAX_CLIP_WORKERS,
            progress_cb=cb,
        )
        job.update(output=str(output_path), filename=output_path.name)

        if STORAGE:
            job.update(progress=92, message="Uploading to SharePoint…")
            try:
                job["sharepoint_url"] = STORAGE.upload(str(output_path))["web_url"]
                job["sharepoint_srt_url"] = STORAGE.upload(str(srt_path))["web_url"]
            except Exception as e:
                job["upload_error"] = f"SharePoint upload failed: {e}"

        job.update(status="done", progress=100, message="Done!")
    except Exception as e:
        job.update(status="error", error=str(e))
    finally:
        # Intermediate files are no longer needed; the final video stays only
        # until the next job starts (so it can still be downloaded directly).
        shutil.rmtree(os.path.dirname(upload_path), ignore_errors=True)
        _busy.release()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("static/index.html").read_text(encoding="utf-8")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/config")
def config():
    return {"sharepoint": STORAGE is not None}


@app.post("/generate")
async def generate(
    slides_file: UploadFile = File(..., description="PPTX or ZIP of slide images"),
    transcript: str = Form(..., description="Narration text, slides separated by ---"),
    speaker: str = Form("ishita"),
    language: str = Form("en-IN"),
    draft: bool = Form(True),
    target_minutes: float = Form(0.0),   # 0 = no slowdown
):
    texts = [s.strip() for s in transcript.split("---") if s.strip()]
    if not texts:
        raise HTTPException(400, "Transcript is empty or has no slides")

    fname = slides_file.filename or ""
    if not fname.lower().endswith((".pptx", ".ppt", ".zip")):
        raise HTTPException(400, "Upload a .pptx, .ppt, or .zip file")

    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "Another video is being generated right now. "
                                 "Please try again in a few minutes.")
    try:
        # Previous job's files are wiped before starting the next one
        clear_previous_files()
        jobs.clear()

        job_dir = WORK_ROOT / uuid.uuid4().hex
        job_dir.mkdir(parents=True)
        upload_path = str(job_dir / ("slides" + Path(fname).suffix.lower()))
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(slides_file.file, f)

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Queued…",
            "output": None,
            "filename": None,
            "error": None,
            "sharepoint_url": None,
            "sharepoint_srt_url": None,
            "upload_error": None,
        }

        target_duration_s = target_minutes * 60 if target_minutes > 0 else None
        threading.Thread(
            target=run_job,
            args=(job_id, upload_path, texts, video_basename(fname),
                  draft, speaker, language, target_duration_s),
            daemon=True,
        ).start()
    except Exception:
        _busy.release()
        raise

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found (the server may have restarted)")
    return {k: v for k, v in jobs[job_id].items() if k != "output"}


def _finished_file(job_id: str, suffix: str) -> Path:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(400, "Video not ready yet")
    path = Path(job["output"]).with_suffix(suffix)
    if not path.exists():
        raise HTTPException(410, "File was cleared — open it from SharePoint instead")
    return path


@app.get("/download/{job_id}")
def download(job_id: str):
    path = _finished_file(job_id, ".mp4")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/download-srt/{job_id}")
def download_srt(job_id: str):
    path = _finished_file(job_id, ".srt")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@app.get("/videos")
def list_videos():
    if not STORAGE:
        return []
    try:
        return STORAGE.list_videos()
    except Exception:
        return []


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
