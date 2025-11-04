# -*- coding: utf-8 -*-
# FastAPI app: PDF -> (TXT + JPG)  +  Images -> PDF

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Any
from pathlib import Path
from io import BytesIO
import tempfile, shutil, os

# ====== App meta ======
APP_TITLE = "PDF Scanner (TXT + JPG) + Images→PDF"
app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
    text_parts = []
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

        kwargs = {}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH

        text_parts = []
        with tempfile.TemporaryDirectory() as tmp:
            paths = convert_from_path(
                str(pdf_path),
                dpi=dpi,
                fmt="jpg",
                output_folder=tmp,
                paths_only=True,
                thread_count=1,
                **kwargs
            )

            if max_pages and len(paths) > max_pages:
                paths = paths[:max_pages]

            for img_path in paths:
                page_text = pytesseract.image_to_string(img_path, lang=OCR_LANG)
                text_parts.append(page_text)
                try:
                    os.remove(img_path)
                except:
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

# ================= Routes =================

# ---- Index (رفع PDF) ----
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "title": APP_TITLE})

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
        {"request": request, "title": APP_TITLE, "results": results}
    )

# ---- serve temp files ----
@app.get("/tmp/{tmpid}/out/{path:path}")
def serve_tmp(tmpid: str, path: str):
    base = Path(tempfile.gettempdir()) / tmpid / "out"
    target = (base / Path(path)).resolve()
    if not str(target).startswith(str(base.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if target.exists() and target.is_file():
        return FileResponse(str(target), media_type="application/octet-stream", filename=target.name)
    return JSONResponse({"error": "file not found"}, status_code=404)

# ---- UI: Images -> PDF ----
@app.get("/convert/images", response_class=HTMLResponse)
def convert_images_page(request: Request):
    return templates.TemplateResponse("convert_images.html", {"request": request})

# ---- API: Images -> single PDF ----
@app.post("/api/convert/images-to-pdf")
async def images_to_pdf(
    images: List[UploadFile] = File(...),
    outfile: str = Form("images.pdf")
):
    from PIL import Image  # Pillow

    if not images:
        return {"error": "لم تُرسل أي صور."}

    pil_images = []
    for f in images:
        if not (f.content_type or "").startswith("image/"):
            return {"error": f"الملف {f.filename} ليس صورة."}
        data = await f.read()
        img = Image.open(BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        pil_images.append(img)

    pdf_bytes = BytesIO()
    first, rest = pil_images[0], pil_images[1:]
    first.save(pdf_bytes, format="PDF", save_all=True, append_images=rest)
    pdf_bytes.seek(0)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"
    headers = {"Content-Disposition": f'attachment; filename="{outfile}"'}
    return StreamingResponse(pdf_bytes, media_type="application/pdf", headers=headers)
