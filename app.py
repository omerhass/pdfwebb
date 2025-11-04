# -*- coding: utf-8 -*-
# FastAPI: PDF -> TXT + JPG (performans optimize)
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Any
from pathlib import Path
import tempfile, shutil, os

APP_TITLE = "PDF Scanner (TXT + JPG)"

# ---- Ayarlar ----
ENABLE_OCR_AUTO = True      # metin yoksa OCR dene
OCR_LANG = "ara+eng+tur"
POPPLER_PATH = None         # örn: r"C:\poppler\Library\bin"
OCR_DPI = 150               # bellek dostu DPI
OCR_MAX_PAGES = 300         # çok büyük dosyalarda sınır
JPG_QUALITY = 80            # çıktı görselleri kalite
MAX_UPLOAD_MB = 200         # istersen arttır/azalt

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------- Yardımcılar ----------
def mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


# ---------- Metin (pdfplumber) ----------
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


# ---------- Metin (OCR – akışlı, sayfa sayfa) ----------
def extract_text_ocr(pdf_path: Path, dpi: int, max_pages: int) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract

        kwargs = {}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH

        text_parts = []
        with tempfile.TemporaryDirectory() as tmp:
            # paths_only=True => belleğe resim objesi yerine dosya yolu döner
            paths = convert_from_path(
                str(pdf_path),
                dpi=dpi,
                fmt="jpg",
                output_folder=tmp,
                paths_only=True,
                thread_count=1,
            )

            if max_pages and len(paths) > max_pages:
                paths = paths[:max_pages]

            for i, img_path in enumerate(paths, 1):
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


# ---------- Gömülü görselleri JPG (sıkıştırmalı) ----------
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
                # RGB'ye çevir (alpha/CMYK varsa)
                if pix.alpha or (pix.colorspace and getattr(pix.colorspace, "n", 3) != 3):
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                (out_dir / f"page{p_i+1}_img{img_i}.jpg").write_bytes(
                    pix.tobytes("jpeg", quality=quality)
                )
                n += 1
    return n


# ---------- Sayfa: Yükleme ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "title": APP_TITLE})


# ---------- Upload & İşleme ----------
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

        # PDF'i diske yaz
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

        # 1) Metin -> .txt
        text = extract_text_normal(original_path)
        if ENABLE_OCR_AUTO and (not text or len(text) < 50):
            print("[i] Low text; trying OCR…")
            ocr_text = extract_text_ocr(original_path, dpi=OCR_DPI, max_pages=OCR_MAX_PAGES)
            text = ocr_text or text

        txt_path = outdir / (Path(f.filename).stem + ".txt")
        txt_path.write_text(text or "", encoding="utf-8")

        # 2) Görseller -> .jpg
        imgs_dir = outdir / (Path(f.filename).stem + "_images")
        count = extract_images_jpg(original_path, imgs_dir, quality=JPG_QUALITY)

        # Linkler
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


# ---------- Geçici dosyaları servis et ----------
@app.get("/tmp/{tmpid}/out/{path:path}")
def serve_tmp(tmpid: str, path: str):
    base = Path(tempfile.gettempdir()) / tmpid / "out"
    target = (base / Path(path)).resolve()
    # path traversal engeli
    if not str(target).startswith(str(base.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if target.exists() and target.is_file():
        return FileResponse(str(target), media_type="application/octet-stream", filename=target.name)
    return JSONResponse({"error": "file not found"}, status_code=404)
