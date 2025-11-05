# -*- coding: utf-8 -*-
# FastAPI app: PDF -> (TXT + JPG)  +  Images -> PDF

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List
from zipfile import ZipFile, ZIP_DEFLATED

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    JSONResponse,
    StreamingResponse,
    PlainTextResponse,
)
from fastapi.templating import Jinja2Templates
from PIL import Image
import tempfile
import shutil
import os
import time

# ====== App meta ======
APP_TITLE = "PDF Scanner (TXT + JPG) + Images→PDF"
app = FastAPI(title=APP_TITLE)

# Templates (اجعل سنة الفوتر ديناميكية بطريقة سليمة)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year

# ====== Settings ======
ENABLE_OCR_AUTO = True          # لا يوجد نص؟ جرّب OCR
OCR_LANG = "ara+eng+tur"
POPPLER_PATH = None             # مثال: r"C:\poppler\Library\bin"
OCR_DPI = 150
OCR_MAX_PAGES = 300
JPG_QUALITY = 80
MAX_UPLOAD_MB = 200

# ---------- Helpers ----------
def mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


# ---------- Text (pdfplumber) ----------
def extract_text_normal(pdf_path: Path) -> str:
    import pdfplumber

    text_parts: List[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for p in pdf.pages:
                t = p.extract_text() or ""
                if t.strip():
                    text_parts.append(t)
    except Exception as e:
        print("[!] pdfplumber error:", e)
    return ("\n".join(text_parts)).strip()


# ---------- OCR (stream page-by-page) ----------
def extract_text_ocr(pdf_path: Path, dpi: int, max_pages: int) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract

        kwargs: Dict[str, Any] = {}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH

        text_parts: List[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            paths = convert_from_path(
                str(pdf_path),
                dpi=dpi,
                fmt="jpg",
                output_folder=tmp,
                paths_only=True,
                thread_count=1,
                **kwargs,
            )

            if max_pages and len(paths) > max_pages:
                paths = paths[:max_pages]

            for img_path in paths:
                page_text = pytesseract.image_to_string(img_path, lang=OCR_LANG)
                text_parts.append(page_text)
                try:
                    os.remove(img_path)
                except Exception:
                    pass

        return ("\n".join(text_parts)).strip()
    except Exception as e:
        print("[!] OCR failed (stream):", e)
        return ""


# ---------- Embedded images -> JPG ----------
def extract_images_jpg(pdf_path: Path, out_dir: Path, quality: int) -> int:
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        print(f"[!] pymupdf not available: {e}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with fitz.open(str(pdf_path)) as doc:
        for p_i in range(len(doc)):
            page = doc[p_i]
            for img_i, info in enumerate(page.get_images(full=True), 1):
                xref = info[0]
                pix = fitz.Pixmap(doc, xref)
                # convert to RGB if needed (alpha/CMYK)
                if pix.alpha or (pix.colorspace and getattr(pix.colorspace, "n", 3) != 3):
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                (out_dir / f"page{p_i+1}_img{img_i}.jpg").write_bytes(
                    pix.tobytes("jpeg", quality=quality)
                )
                n += 1
    return n


# ---- مساعد: تركيب الصورة على صفحة A4 اختيارياً ----
def to_a4(img: Image.Image, dpi: int = 300, margin_mm: int = 8) -> Image.Image:
    a4_w, a4_h = int(8.27 * dpi), int(11.69 * dpi)
    bg = Image.new("RGB", (a4_w, a4_h), "white")

    margin_px = int((margin_mm / 25.4) * dpi)
    max_w, max_h = a4_w - 2 * margin_px, a4_h - 2 * margin_px

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    ratio = min(max_w / img.width, max_h / img.height)
    new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
    img_resized = img.resize(new_size, Image.LANCZOS)

    x = margin_px + (max_w - img_resized.width) // 2
    y = margin_px + (max_h - img_resized.height) // 2
    bg.paste(img_resized, (x, y))
    return bg


# ================= Routes =================

# Health check مفيد لـ Render
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ---- Index (رفع PDF) ----
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "title": APP_TITLE},
    )


# ---- Upload PDFs -> (TXT + JPGs) ----
@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: List[UploadFile] = File(...)):
    tmpdir = Path(tempfile.mkdtemp())
    outdir = tmpdir / "out"
    outdir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            results.append({"input": f.filename, "error": "Sadece PDF kabul edilir."})
            continue

        # save PDF to disk
        original_path = tmpdir / f.filename
        with open(original_path, "wb") as w:
            shutil.copyfileobj(f.file, w)

        size = mb(original_path.stat().st_size)
        if size > MAX_UPLOAD_MB:
            results.append({
                "input": f.filename,
                "error": f"Dosya çok büyük ({size:.1f} MB). Limit: {MAX_UPLOAD_MB} MB."
            })
            continue

        # 1) text -> .txt
        text = extract_text_normal(original_path)
        if ENABLE_OCR_AUTO and (not text or len(text) < 50):
            print("[i] Low text; trying OCR…")
            text = extract_text_ocr(original_path, dpi=OCR_DPI, max_pages=OCR_MAX_PAGES) or text

        txt_path = outdir / (Path(f.filename).stem + ".txt")
        txt_path.write_text(text or "", encoding="utf-8")

        # 2) images -> .jpg
        imgs_dir = outdir / (Path(f.filename).stem + "_images")
        count = extract_images_jpg(original_path, imgs_dir, quality=JPG_QUALITY)

        # links
        txt_link = f"/tmp/{tmpdir.name}/out/{txt_path.name}"
        img_links = []
        if imgs_dir.exists():
            for p in sorted(imgs_dir.glob("*.jpg")):
                img_links.append(f"/tmp/{tmpdir.name}/out/{imgs_dir.name}/{p.name}")

        results.append({
            "input": f.filename,
            "txt_link": txt_link,
            "images_links": img_links,
            "images_count": count
        })

    return templates.TemplateResponse(
        "result_txt_imgs.html",
        {"request": request, "title": APP_TITLE, "results": results},
    )


# ---- serve temp files ----
@app.get("/tmp/{tmpid}/out/{path:path}")
def serve_tmp(tmpid: str, path: str):
    base = Path(tempfile.gettempdir()) / tmpid / "out"
    target = (base / Path(path)).resolve()
    if not str(target).startswith(str(base.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if target.exists() and target.is_file():
        return FileResponse(
            str(target),
            media_type="application/octet-stream",
            filename=target.name,
        )
    return JSONResponse({"error": "file not found"}, status_code=404)


# ---- UI: Images -> PDF ----
@app.get("/convert/images", response_class=HTMLResponse)
def convert_images_page(request: Request):
    return templates.TemplateResponse(
        "convert_images.html",
        {"request": request, "title": APP_TITLE},
    )


# ---- API: Images -> PDF (دمج أو ملف لكل صورة + خيار A4) ----
@app.post("/api/images-to-pdf")
async def images_to_pdf(
    images: List[UploadFile] = File(...),
    outfile: str = Form("images.pdf"),
    order: str = Form("name"),     # name | mtime | as_is
    per_file: str = Form(None),    # "1" => كل صورة ملف PDF مستقل
    fit_a4: str = Form(None),      # "1" => ضبط على صفحة A4
):
    if not images:
        return JSONResponse({"error": "لم تُرسل أي صور."}, status_code=400)

    # ترتيب الملفات (اسم/تاريخ/كما أُرسلت)
    if order == "name":
        images.sort(key=lambda f: (f.filename or "").lower())
    elif order == "mtime":
        # قد لا يتوفر mtime من المتصفح؛ هذا ترتيب تقريبي بدون كسر التنفيذ
        images.sort(key=lambda f: getattr(getattr(f, "spooled", None), "mtime", time.time()))

    pil_list: List[Image.Image] = []
    single_pdfs: List[tuple[str, bytes]] = []

    for f in images:
        if not (f.content_type or "").startswith("image/"):
            return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)

        data = await f.read()
        img = Image.open(BytesIO(data))

        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        if fit_a4 == "1":
            img = to_a4(img, dpi=300)

        if per_file == "1":
            buf = BytesIO()
            img.save(buf, format="PDF")
            buf.seek(0)
            name = (f.filename or "image").rsplit(".", 1)[0] + ".pdf"
            single_pdfs.append((name, buf.read()))
        else:
            pil_list.append(img)

    if per_file == "1":
        # ZIP لكل ملف PDF منفصل
        zip_bytes = BytesIO()
        with ZipFile(zip_bytes, "w", ZIP_DEFLATED) as zf:
            for name, blob in single_pdfs:
                zf.writestr(name, blob)
        zip_bytes.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="images_pdf.zip"'}
        return StreamingResponse(zip_bytes, media_type="application/zip", headers=headers)

    # دمج في PDF واحد
    if not pil_list:
        return JSONResponse({"error": "لا توجد صور صالحة للدمج."}, status_code=400)

    pdf_bytes = BytesIO()
    first, rest = pil_list[0], pil_list[1:]
    first.save(pdf_bytes, format="PDF", save_all=True, append_images=rest)
    pdf_bytes.seek(0)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"
    headers = {"Content-Disposition": f'attachment; filename="{outfile}"'}
    return StreamingResponse(pdf_bytes, media_type="application/pdf", headers=headers)


# للتشغيل المحلي:
# uvicorn app:app --reload
