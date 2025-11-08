from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED
import os, time, subprocess, tempfile, shutil

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse, Response
)
from fastapi.templating import Jinja2Templates

from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageFile, ExifTags
import img2pdf

ImageFile.LOAD_TRUNCATED_IMAGES = True

APP_TITLE = "PDF Web — أدوات بسيطة (Mobile-Ready)"
app = FastAPI(title=APP_TITLE)

# ------------ Templates ------------
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year
templates.env.globals["site_name"] = "PDF Web"

MAX_IMAGES = 300

# ------------ Helpers ------------
_EXIF_ORIENTATION_TAG = next((k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None)

def _auto_orient(img: Image.Image) -> Image.Image:
    try:
        exif = img.getexif()
        if not exif:
            return img
        orientation = exif.get(_EXIF_ORIENTATION_TAG)
        if orientation == 3:
            return img.rotate(180, expand=True)
        elif orientation == 6:
            return img.rotate(270, expand=True)
        elif orientation == 8:
            return img.rotate(90, expand=True)
    except Exception:
        pass
    return img

def _ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        return img.convert("RGB")
    if img.mode == "L":
        return img.convert("RGB")
    return img

def _compress_to_jpeg_bytes(img: Image.Image, quality: int = 85) -> bytes:
    img = _ensure_rgb(img)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()

def _layout_a4_with_margins(margin_mm: float = 8.0):
    a4_w_pt, a4_h_pt = img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)
    left = right = top = bottom = img2pdf.mm_to_pt(margin_mm)
    def _fun(imgwidthpx, imgheightpx, ndpi, metadata):
        page_width, page_height = a4_w_pt, a4_h_pt
        box_w, box_h = page_width - left - right, page_height - top - bottom
        img_aspect = imgwidthpx / float(imgheightpx)
        box_aspect = box_w / float(box_h)
        if img_aspect > box_aspect:
            w = box_w; h = w / img_aspect
        else:
            h = box_h; w = h * img_aspect
        x = (page_width - w) / 2.0
        y = (page_height - h) / 2.0
        return (page_width, page_height, x, y, w, h)
    return _fun

# ------------ Health / Infra ------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

# HEAD / لفحص Render
@app.head("/")
def home_head():
    return Response(status_code=200)

# منع 404 للأيقونة
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

# ads.txt لأدسنس (غيّر CLIENT_ID)
@app.get("/ads.txt", response_class=PlainTextResponse)
def ads_txt():
    # استبدل ca-pub-XXXXXXXXXXXXXXX بالمعرّف الخاص بك من AdSense
    return "google.com, pub-0000000000000000, DIRECT, f08c47fec0942fa0"

# robots.txt وسايت ماب بسيط
@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return "User-agent: *\nAllow: /\nSitemap: /sitemap.txt"

@app.get("/sitemap.txt", response_class=PlainTextResponse)
def sitemap_txt():
    base = ""
    # Render يحقن PUBLIC_URL كمتغير بيئة أحياناً؛ لو غير متاح اتركه فارغ/نسبي
    base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    paths = ["/", "/merge/pdf", "/compress/pdf", "/about", "/privacy", "/cookies", "/contact"]
    return "\n".join([(base + p) if base else p for p in paths])

# ------------ Pages ------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("convert_images.html", {"request": request, "title": "صور إلى PDF", "active": "img2pdf"})

@app.get("/merge/pdf", response_class=HTMLResponse)
def merge_pdf_page(request: Request):
    return templates.TemplateResponse("merge_pdf.html", {"request": request, "title": "دمج ملفات PDF", "active": "merge"})

# صفحة ضغط PDF
@app.get("/compress/pdf", response_class=HTMLResponse)
def compress_pdf_page(request: Request):
    return templates.TemplateResponse("compress_pdf.html", {"request": request, "title": "ضغط PDF", "active": "compress"})

# صفحات قانونية/معلومات
@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse("about.html", {"request": request, "title": "من نحن"})

@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request, "title": "سياسة الخصوصية"})

@app.get("/cookies", response_class=HTMLResponse)
def cookies(request: Request):
    return templates.TemplateResponse("cookies.html", {"request": request, "title": "سياسة ملفات الارتباط"})

@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request, "title": "اتصل بنا"})

# ------------ APIs ------------
@app.post("/api/images-to-pdf")
async def images_to_pdf(
    images: List[UploadFile] = File(...),
    outfile: str = Form("images.pdf"),
    order: str = Form("name"),       # name | mtime | as_is
    per_file: str = Form(None),      # "1" => PDF per image (zipped)
    style: str = Form("full_bleed"), # full_bleed | a4_margins
    compress: str = Form(None),      # "1" to recompress non-JPEG for smaller PDFs
):
    if not images:
        return JSONResponse({"error": "لم تُرسل أي صور."}, status_code=400)
    if len(images) > MAX_IMAGES:
        return JSONResponse({"error": f"عدد الصور كبير ({len(images)}). الحد الأقصى {MAX_IMAGES}."}, status_code=400)

    if order == "name":
        images.sort(key=lambda f: (f.filename or "").lower())
    elif order == "mtime":
        images.sort(key=lambda f: getattr(getattr(f, "spooled", None), "mtime", time.time()))

    singles: List[Tuple[str, bytes]] = []
    layout_fun = _layout_a4_with_margins(8.0) if style == "a4_margins" else None
    pdf_writer_bytes = BytesIO()

    if per_file == "1":
        for f in images:
            if not (f.content_type or "").startswith("image/"):
                return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)
            try:
                data = await f.read()
                with Image.open(BytesIO(data)) as img:
                    img = _auto_orient(img)
                    if compress == "1" and (img.format or "").upper() != "JPEG":
                        data_bytes = _compress_to_jpeg_bytes(img)
                    else:
                        data_bytes = data if (img.format or "").upper() == "JPEG" else _compress_to_jpeg_bytes(img)
                pdf_bytes = img2pdf.convert(data_bytes) if style == "full_bleed" \
                            else img2pdf.convert(data_bytes, layout_fun=layout_fun)
                name = (f.filename or "image").rsplit(".", 1)[0] + ".pdf"
                singles.append((name, pdf_bytes))
            except Exception as e:
                return JSONResponse({"error": f"فشل معالجة {f.filename}: {e}"}, status_code=400)

        zip_bytes = BytesIO()
        with ZipFile(zip_bytes, "w", ZIP_DEFLATED) as zf:
            for name, blob in singles:
                zf.writestr(name, blob)
        zip_bytes.seek(0)
        return StreamingResponse(
            zip_bytes, media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="images_pdf.zip"'}
        )

    try:
        img_streams: List[bytes] = []
        for f in images:
            if not (f.content_type or "").startswith("image/"):
                return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)
            data = await f.read()
            with Image.open(BytesIO(data)) as img:
                img = _auto_orient(img)
                if compress == "1" and (img.format or "").upper() != "JPEG":
                    data_bytes = _compress_to_jpeg_bytes(img)
                else:
                    data_bytes = data if (img.format or "").upper() == "JPEG" else _compress_to_jpeg_bytes(img)
            img_streams.append(data_bytes)

        pdf_data = img2pdf.convert(img_streams) if style == "full_bleed" \
                   else img2pdf.convert(img_streams, layout_fun=layout_fun)
        pdf_writer_bytes.write(pdf_data)
    except Exception as e:
        return JSONResponse({"error": f"فشل إنشاء PDF: {e}"}, status_code=500)

    pdf_writer_bytes.seek(0)
    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        pdf_writer_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'}
    )

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
        return JSONResponse(
            {"error": "لم يتم العثور على صفحات صالحة للدمج.", "details": errors},
            status_code=400
        )

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'}
    )

# ------------ Compress PDF API (Ghostscript) ------------
@app.post("/api/compress-pdf")
async def compress_pdf(
    file: UploadFile = File(...),
    outfile: str = Form("compressed.pdf"),
    level: str = Form("medium"),     # low | medium | high
    dpi: str = Form("150"),          # "", "150", "120", "96", "72"
    grayscale: str = Form(None),     # "1" or None
):
    if not file:
        return JSONResponse({"error": "لم يتم رفع أي ملف."}, status_code=400)
    if not (file.filename or "").lower().endswith(".pdf"):
        return JSONResponse({"error": "الملف يجب أن يكون PDF."}, status_code=400)

    # تأكد من تواجد Ghostscript
    if shutil.which("gs") is None:
        return JSONResponse(
            {"error": "Ghostscript غير مثبت على الخادم. أضِف 'ghostscript' إلى apt.txt ثم أعد النشر."},
            status_code=500
        )

    pdfsettings = {
        "low": "/screen",     # أصغر حجم
        "medium": "/ebook",   # موصى به
        "high": "/printer"    # جودة أعلى
    }.get(level, "/ebook")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        dst = Path(tmp) / "out.pdf"
        src.write_bytes(await file.read())

        cmd = [
            "gs", "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={pdfsettings}",
            "-dNOPAUSE", "-dQUIET", "-dBATCH",
        ]

        # Downsampling
        if dpi:
            try:
                val = int(dpi)
                cmd += [
                    "-dColorImageDownsampleType=/Average",
                    f"-dColorImageResolution={val}",
                    "-dGrayImageDownsampleType=/Average",
                    f"-dGrayImageResolution={val}",
                    "-dMonoImageDownsampleType=/Subsample",
                    f"-dMonoImageResolution={val}",
                ]
            except Exception:
                pass

        # تدرّج رمادي اختياري
        if grayscale == "1":
            cmd += [
                "-sColorConversionStrategy=Gray",
                "-dProcessColorModel=/DeviceGray",
                "-dConvertCMYKImagesToRGB=true",
            ]

        cmd += ["-sOutputFile=" + str(dst), str(src)]

        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
            if not dst.exists():
                return JSONResponse({"error": f"فشل الضغط: {proc.stderr or proc.stdout}"}, status_code=500)
            data = dst.read_bytes()
        except Exception as e:
            return JSONResponse({"error": f"تعذر تشغيل محرك الضغط: {e}"}, status_code=500)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename=\"{outfile}\"'}
    )
