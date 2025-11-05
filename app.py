# -*- coding: utf-8 -*-
# FastAPI app: Images -> PDF فقط

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from PIL import Image, ImageFile
import time

# يسمح بقراءة صور ناقصة التحمّيل بدل ما ترمي خطأ
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Pillow 10 غيّر الثوابت؛ نوحّد LANCZOS
try:
    from PIL.Image import Resampling
    LANCZOS = Resampling.LANCZOS
except Exception:
    LANCZOS = Image.LANCZOS  # للنسخ الأقدم

APP_TITLE = "تحويل الصور إلى PDF"
app = FastAPI(title=APP_TITLE)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year

# حدّ أقصى منطقي لعدد الصور (اختياري)
MAX_IMAGES = 300

def to_a4(img: Image.Image, dpi: int = 300, margin_mm: int = 8) -> Image.Image:
    a4_w, a4_h = int(8.27 * dpi), int(11.69 * dpi)
    bg = Image.new("RGB", (a4_w, a4_h), "white")
    margin_px = int((margin_mm / 25.4) * dpi)
    max_w, max_h = a4_w - 2 * margin_px, a4_h - 2 * margin_px

    # توحيد الألوان
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

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

# صفحة واحدة: صور → PDF
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("convert_images.html", {"request": request, "title": APP_TITLE})

# API: صور → PDF (دمج أو ملف لكل صورة + خيار A4)
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

    # ترتيب حسب اختيار المستخدم
    if order == "name":
        images.sort(key=lambda f: (f.filename or "").lower())
    elif order == "mtime":
        # غالبًا غير متاح من المتصفح؛ نحاول بدون كسر التنفيذ
        images.sort(key=lambda f: getattr(getattr(f, "spooled", None), "mtime", time.time()))
    elif order == "as_is":
        pass  # اتركها كما جاءت
    else:
        order = "name"
        images.sort(key=lambda f: (f.filename or "").lower())

    merged_pages: List[Image.Image] = []
    singles: List[Tuple[str, bytes]] = []

    for f in images:
        if not (f.content_type or "").startswith("image/"):
            return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)

        try:
            data = await f.read()
            img = Image.open(BytesIO(data))
            # توحيد الألوان قبل أي معالجة
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
                # Pillow يمكنه حفظ مجموعة الصور مباشرة كصفحات PDF
                merged_pages.append(img)
        except Exception as e:
            return JSONResponse({"error": f"فشل قراءة {f.filename}: {e}"}, status_code=400)

    # ملف PDF لكل صورة → ZIP
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

    # دمج في PDF واحد
    if not merged_pages:
        return JSONResponse({"error": "لا توجد صور صالحة."}, status_code=400)

    pdf_bytes = BytesIO()
    first, rest = merged_pages[0], merged_pages[1:]
    first.save(pdf_bytes, format="PDF", save_all=True, append_images=rest)
    pdf_bytes.seek(0)

    # Free memory
    first.close()
    for p in rest:
        p.close()

    if not outfile.lower().endswith(".pdf"):
        outfile += ".pdf"

    return StreamingResponse(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{outfile}"'}
    )
