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
from fastapi.staticfiles import StaticFiles  # ✅ مهم للـ /static

from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageFile, ExifTags
import img2pdf

ImageFile.LOAD_TRUNCATED_IMAGES = True

APP_TITLE = "PDF Web — أدوات بسيطة (Mobile-Ready)"
app = FastAPI(title=APP_TITLE)

# ------------ I18N (Backend messages) ------------
SUPPORTED_LANGS = {"ar", "en", "tr"}
DEFAULT_LANG = "ar"


def normalize_lang(lang: str | None) -> str:
    lang = (lang or "").lower()
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


ERROR_MESSAGES = {
    "no_images": {
        "ar": "لم تُرسل أي صور.",
        "en": "No images were uploaded.",
        "tr": "Herhangi bir resim yüklenmedi.",
    },
    "too_many_images": {
        "ar": "عدد الصور كبير ({count}). الحد الأقصى {max}.",
        "en": "Too many images ({count}). Maximum allowed is {max}.",
        "tr": "Çok fazla resim yüklendi ({count}). İzin verilen en fazla sayı: {max}.",
    },
    "not_image": {
        "ar": "الملف {name} ليس صورة.",
        "en": "File {name} is not an image.",
        "tr": "{name} bir resim dosyası değil.",
    },
    "image_process_failed": {
        "ar": "فشل معالجة {name}: {error}",
        "en": "Failed to process {name}: {error}",
        "tr": "{name} işlenemedi: {error}",
    },
    "pdf_creation_failed": {
        "ar": "فشل إنشاء PDF: {error}",
        "en": "Failed to create PDF: {error}",
        "tr": "PDF oluşturulamadı: {error}",
    },
    "no_files": {
        "ar": "لم تُرسل أي ملفات.",
        "en": "No files were uploaded.",
        "tr": "Herhangi bir dosya yüklenmedi.",
    },
    "not_pdf": {
        "ar": "{name}: ليس PDF",
        "en": "{name}: is not a PDF file",
        "tr": "{name}: bir PDF dosyası değil",
    },
    "password_protected": {
        "ar": "{name}: ملف محمي بكلمة مرور",
        "en": "{name}: file is password-protected",
        "tr": "{name}: parola korumalı bir dosya",
    },
    "read_error": {
        "ar": "{name}: خطأ القراءة ({error})",
        "en": "{name}: read error ({error})",
        "tr": "{name}: okuma hatası ({error})",
    },
    "no_valid_pages": {
        "ar": "لم يتم العثور على صفحات صالحة للدمج.",
        "en": "No valid pages found to merge.",
        "tr": "Birleştirilecek geçerli sayfa bulunamadı.",
    },
    "no_file_uploaded": {
        "ar": "لم يتم رفع أي ملف.",
        "en": "No file was uploaded.",
        "tr": "Herhangi bir dosya yüklenmedi.",
    },
    "must_be_pdf": {
        "ar": "الملف يجب أن يكون PDF.",
        "en": "The uploaded file must be a PDF.",
        "tr": "Yüklenen dosya PDF olmalıdır.",
    },
    "gs_missing": {
        "ar": "Ghostscript غير مثبت على الخادم. أضِف 'ghostscript' إلى apt.txt ثم أعد النشر.",
        "en": "Ghostscript is not installed on the server. Add 'ghostscript' to apt.txt and redeploy.",
        "tr": "Sunucuda Ghostscript yüklü değil. 'ghostscript'i apt.txt dosyasına ekleyip yeniden deploy edin.",
    },
    "compress_failed": {
        "ar": "فشل الضغط: {detail}",
        "en": "Compression failed: {detail}",
        "tr": "Sıkıştırma başarısız: {detail}",
    },
    "compress_engine_failed": {
        "ar": "تعذر تشغيل محرك الضغط: {error}",
        "en": "Failed to run compression engine: {error}",
        "tr": "Sıkıştırma motoru çalıştırılamadı: {error}",
    },
}


def msg(key: str, lang: str, **kwargs) -> str:
    lang = normalize_lang(lang)
    template = ERROR_MESSAGES.get(key, {}).get(lang) or ERROR_MESSAGES.get(key, {}).get(DEFAULT_LANG) or key
    try:
        return template.format(**kwargs)
    except Exception:
        return template


# ------------ Templates ------------
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year
templates.env.globals["site_name"] = "PDF Web"

# ------------ Static files (for locales, assets) ✅ ------------
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
            w = box_w
            h = w / img_aspect
        else:
            h = box_h
            w = h * img_aspect
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
def home(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse(
        "convert_images.html",
        {"request": request, "title": "صور إلى PDF", "active": "img2pdf", "lang": lang},
    )


@app.get("/merge/pdf", response_class=HTMLResponse)
def merge_pdf_page(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse(
        "merge_pdf.html",
        {"request": request, "title": "دمج ملفات PDF", "active": "merge", "lang": lang},
    )


# صفحة ضغط PDF
@app.get("/compress/pdf", response_class=HTMLResponse)
def compress_pdf_page(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse(
        "compress_pdf.html",
        {"request": request, "title": "ضغط PDF", "active": "compress", "lang": lang},
    )


# صفحات قانونية/معلومات
@app.get("/about", response_class=HTMLResponse)
def about(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse("about.html", {"request": request, "title": "من نحن", "lang": lang})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse(
        "privacy.html", {"request": request, "title": "سياسة الخصوصية", "lang": lang}
    )


@app.get("/cookies", response_class=HTMLResponse)
def cookies(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse(
        "cookies.html", {"request": request, "title": "سياسة ملفات الارتباط", "lang": lang}
    )


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request, lang: str = "ar"):
    lang = normalize_lang(lang)
    return templates.TemplateResponse("contact.html", {"request": request, "title": "اتصل بنا", "lang": lang})


# ------------ APIs ------------
@app.post("/api/images-to-pdf")
async def images_to_pdf(
    images: List[UploadFile] = File(...),
    outfile: str = Form("images.pdf"),
    order: str = Form("name"),       # name | mtime | as_is
    per_file: str = Form(None),      # "1" => PDF per image (zipped)
    style: str = Form("full_bleed"), # full_bleed | a4_margins
    compress: str = Form(None),      # "1" to recompress non-JPEG for smaller PDFs
    lang: str = Form("ar"),
):
    lang = normalize_lang(lang)

    if not images:
        return JSONResponse({"error": msg("no_images", lang)}, status_code=400)
    if len(images) > MAX_IMAGES:
        return JSONResponse(
            {"error": msg("too_many_images", lang, count=len(images), max=MAX_IMAGES)},
            status_code=400,
        )

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
                return JSONResponse(
                    {"error": msg("not_image", lang, name=f.filename)},
                    status_code=400,
                )
            try:
                data = await f.read()
                with Image.open(BytesIO(data)) as img:
                    img = _auto_orient(img)
                    if compress == "1" and (img.format or "").upper() != "JPEG":
                        data_bytes = _compress_to_jpeg_bytes(img)
                    else:
                        data_bytes = data if (img.format or "").upper() == "JPEG" else _compress_to_jpeg_bytes(img)
                pdf_bytes = (
                    img2pdf.convert(data_bytes)
                    if style == "full_bleed"
                    else img2pdf.convert(data_bytes, layout_fun=layout_fun)
                )
                name = (f.filename or "image").rsplit(".", 1)[0] + ".pdf"
                singles.append((name, pdf_bytes))
            except Exception as e:
                return JSONResponse(
                    {"error": msg("image_process_failed", lang, name=f.filename, error=e)},
                    status_code=400,
                )

        zip_bytes = BytesIO()
        with ZipFile(zip_bytes, "w", ZIP_DEFLATED) as zf:
            for name, blob in singles:
                zf.writestr(name, blob)
        zip_bytes.seek(0)
        return StreamingResponse(
            zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="images_pdf.zip"'},
        )

    try:
        img_streams: List[bytes] = []
        for f in images:
            if not (f.content_type or "").startswith("image/"):
                return JSONResponse(
                    {"error": msg("not_image", lang, name=f.filename)},
                    status_code=400,
                )
            data = await f.read()
            with Image.open(BytesIO(data)) as img:
                img = _auto_orient(img)
                if compress == "1" and (img.format or "").upper() != "JPEG":
                    data_bytes = _compress_to_jpeg_bytes(img)
                else:
                    data_bytes = data if (img.format or "").upper() == "JPEG" else _compress_to_jpeg_bytes(img)
            img_streams.append(data_bytes)

        pdf_data = (
            img2pdf.convert(img_streams)
            if style == "full_bleed"
            else img2pdf.convert(img_streams, layout_fun=layout_fun)
        )
        pdf_writer_bytes.write(pdf_data)
    except Exception as e:
        return JSONResponse(
            {"error": msg("pdf_creation_failed", lang, error=e)},
            status_code=500,
        )

    pdf_writer_bytes.seek(0)
    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        pdf_writer_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'},
    )


@app.post("/api/merge-pdf")
async def merge_pdf(
    files: List[UploadFile] = File(...),
    outfile: str = Form("merged.pdf"),
    order: str = Form("name"),   # name | as_is
    lang: str = Form("ar"),
):
    lang = normalize_lang(lang)

    if not files:
        return JSONResponse({"error": msg("no_files", lang)}, status_code=400)

    if order == "name":
        files.sort(key=lambda f: (f.filename or "").lower())

    writer = PdfWriter()
    total_pages = 0
    errors = []

    for f in files:
        name = f.filename or "file.pdf"
        if not (name.lower().endswith(".pdf") or (f.content_type or "").endswith("pdf")):
            errors.append(msg("not_pdf", lang, name=name))
            continue
        data = await f.read()
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")  # محاولة كلمة مرور فارغة
                except Exception:
                    errors.append(msg("password_protected", lang, name=name))
                    continue
            for p in reader.pages:
                writer.add_page(p)
                total_pages += 1
        except Exception as e:
            errors.append(msg("read_error", lang, name=name, error=e))

    if total_pages == 0:
        return JSONResponse(
            {"error": msg("no_valid_pages", lang), "details": errors},
            status_code=400,
        )

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'},
    )


# ------------ Compress PDF API (Ghostscript) ------------
@app.post("/api/compress-pdf")
async def compress_pdf(
    file: UploadFile = File(...),
    outfile: str = Form("compressed.pdf"),
    level: str = Form("medium"),     # low | medium | high
    dpi: str = Form("150"),          # "", "150", "120", "96", "72"
    grayscale: str = Form(None),     # "1" or None
    lang: str = Form("ar"),
):
    lang = normalize_lang(lang)

    if not file:
        return JSONResponse({"error": msg("no_file_uploaded", lang)}, status_code=400)
    if not (file.filename or "").lower().endswith(".pdf"):
        return JSONResponse({"error": msg("must_be_pdf", lang)}, status_code=400)

    # تأكد من تواجد Ghostscript
    if shutil.which("gs") is None:
        return JSONResponse(
            {"error": msg("gs_missing", lang)},
            status_code=500,
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
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
            )
            if not dst.exists():
                detail = proc.stderr or proc.stdout or ""
                return JSONResponse(
                    {"error": msg("compress_failed", lang, detail=detail)},
                    status_code=500,
                )
            data = dst.read_bytes()
        except Exception as e:
            return JSONResponse(
                {"error": msg("compress_engine_failed", lang, error=e)},
                status_code=500,
            )

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename=\"{outfile}\"'}
    )
