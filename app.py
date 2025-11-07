# -*- coding: utf-8 -*-
# FastAPI app: Images -> PDF  +  Merge PDF  +  Word/ODT -> PDF

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED
import time, os, subprocess, tempfile

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Pillow 10 compatibility
try:
    from PIL.Image import Resampling
    LANCZOS = Resampling.LANCZOS
except Exception:
    LANCZOS = Image.LANCZOS

APP_TITLE = "PDF Web — أدوات بسيطة"
app = FastAPI(title=APP_TITLE)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year

# ====== LibreOffice (مسار soffice) ======
SOFFICE_PATH = os.getenv(
    "SOFFICE_PATH",
    r"C:\Program Files\LibreOffice\program\soffice.exe"  # عدّل إذا مختلف
)

# ====== إعدادات عامة ======
MAX_IMAGES = 300

# ---------------- Helpers ----------------
def to_a4(img: Image.Image, dpi: int = 300, margin_mm: int = 8) -> Image.Image:
    a4_w, a4_h = int(8.27 * dpi), int(11.69 * dpi)
    bg = Image.new("RGB", (a4_w, a4_h), "white")
    margin_px = int((margin_mm / 25.4) * dpi)
    max_w, max_h = a4_w - 2 * margin_px, a4_h - 2 * margin_px

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    r = min(max_w / img.width, max_h / img.height)
    new_size = (max(1, int(img.width * r)), max(1, int(img.height * r)))
    img = img.resize(new_size, LANCZOS)

    x = margin_px + (max_w - img.width) // 2
    y = margin_px + (max_h - img.height) // 2
    bg.paste(img, (x, y))
    return bg

# ---------------- Health ----------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

# ---------------- Home (صور → PDF) ----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("convert_images.html", {"request": request, "title": "صور إلى PDF"})

# ---------------- صور → PDF ----------------
@app.post("/api/images-to-pdf")
async def images_to_pdf(
    images: List[UploadFile] = File(...),
    outfile: str = Form("images.pdf"),
    order: str = Form("name"),     # name | mtime | as_is
    per_file: str = Form(None),    # "1" => ملف PDF لكل صورة
    fit_a4: str = Form(None)       # "1" => ضبط على A4
):
    if not images:
        return JSONResponse({"error": "لم تُرسل أي صور."}, status_code=400)
    if len(images) > MAX_IMAGES:
        return JSONResponse({"error": f"عدد الصور كبير ({len(images)}). الحد الأقصى {MAX_IMAGES}."}, status_code=400)

    if order == "name":
        images.sort(key=lambda f: (f.filename or "").lower())
    elif order == "mtime":
        images.sort(key=lambda f: getattr(getattr(f, "spooled", None), "mtime", time.time()))
    elif order == "as_is":
        pass
    else:
        images.sort(key=lambda f: (f.filename or "").lower())

    merged_pages: List[Image.Image] = []
    singles: List[Tuple[str, bytes]] = []

    for f in images:
        if not (f.content_type or "").startswith("image/"):
            return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)
        try:
            data = await f.read()
            img = Image.open(BytesIO(data))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")
            if fit_a4 == "1":
                img = to_a4(img)

            if per_file == "1":
                buf = BytesIO()
                img.save(buf, format="PDF")
                buf.seek(0)
                name = (f.filename or "image").rsplit(".", 1)[0] + ".pdf"
                singles.append((name, buf.read()))
                img.close()
            else:
                merged_pages.append(img)
        except Exception as e:
            return JSONResponse({"error": f"فشل قراءة {f.filename}: {e}"}, status_code=400)

    if per_file == "1":
        zip_bytes = BytesIO()
        with ZipFile(zip_bytes, "w", ZIP_DEFLATED) as zf:
            for name, blob in singles:
                zf.writestr(name, blob)
        zip_bytes.seek(0)
        return StreamingResponse(
            zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="images_pdf.zip"'}
        )

    if not merged_pages:
        return JSONResponse({"error": "لا توجد صور صالحة."}, status_code=400)

    pdf_bytes = BytesIO()
    first, rest = merged_pages[0], merged_pages[1:]
    first.save(pdf_bytes, format="PDF", save_all=True, append_images=rest)
    pdf_bytes.seek(0)
    first.close()
    for p in rest: p.close()

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'}
    )

# ---------------- واجهة دمج PDF ----------------
@app.get("/merge/pdf", response_class=HTMLResponse)
def merge_pdf_page(request: Request):
    return templates.TemplateResponse("merge_pdf.html", {"request": request, "title": "دمج ملفات PDF"})

# ---------------- API: دمج PDF ----------------
@app.post("/api/merge-pdf")
async def merge_pdf(
    files: List[UploadFile] = File(...),
    outfile: str = Form("merged.pdf"),
    order: str = Form("name"),   # name | as_is
):
    if not files:
        return JSONResponse({"error": "لم تُرسل أي ملفات."}, status_code=400)

    if order == "name":
        files.sort(key=lambda f: (f.filename or "").lower())

    writer = PdfWriter()
    total_pages = 0
    errors = []

    for f in files:
        name = f.filename or "file.pdf"
        if not (name.lower().endswith(".pdf") or (f.content_type or "").endswith("pdf")):
            errors.append(f"{name}: ليس PDF")
            continue

        data = await f.read()
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")  # محاولة كلمة مرور فارغة
                except Exception:
                    errors.append(f"{name}: ملف محمي بكلمة مرور")
                    continue

            for p in reader.pages:
                writer.add_page(p)
                total_pages += 1
        except Exception as e:
            errors.append(f"{name}: خطأ القراءة ({e})")

    if total_pages == 0:
        return JSONResponse({"error": "لم يتم العثور على صفحات صالحة للدمج.", "details": errors}, status_code=400)

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    headers = {"Content-Disposition": f'attachment; filename="{outfile}"'}
    return StreamingResponse(buf, media_type="application/pdf", headers=headers)

# ---------------- Word/ODT → PDF ----------------
@app.get("/convert/word", response_class=HTMLResponse)
def word_page(request: Request):
    return templates.TemplateResponse("convert_word.html", {"request": request, "title": "Word/ODT إلى PDF"})

@app.post("/api/word-to-pdf")
async def word_to_pdf(file: UploadFile = File(...), outfile: str = Form(None)):
    name = (file.filename or "").lower()
    allowed = (".docx", ".doc", ".odt", ".rtf")
    if not name.endswith(allowed):
        return JSONResponse({"error": "الرجاء رفع ملف Word/ODT صالح (docx/doc/odt/rtf)."}, status_code=400)

    base_stem = Path(name).stem

    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / name
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        data = await file.read()
        in_path.write_bytes(data)

        if not Path(SOFFICE_PATH).exists():
            return JSONResponse({"error": f"لم يتم العثور على LibreOffice في: {SOFFICE_PATH}"}, status_code=500)

        cmd = [
            SOFFICE_PATH,
            "--headless", "--norestore", "--invisible",
            "--convert-to", "pdf",
            "--outdir", str(out_dir),
            str(in_path)
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        out_pdf = out_dir / f"{base_stem}.pdf"
        if not out_pdf.exists():
            msg = (proc.stderr or proc.stdout or "conversion failed").strip()
            return JSONResponse({"error": f"فشل التحويل: {msg}"}, status_code=500)

        pdf_bytes = out_pdf.read_bytes()

    if not outfile:
        outfile = f"{base_stem}.pdf"
    elif not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'}
    )
