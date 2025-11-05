# -*- coding: utf-8 -*-
# FastAPI app: Images -> PDF فقط

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List
from zipfile import ZipFile, ZIP_DEFLATED

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from PIL import Image
import time

APP_TITLE = "تحويل الصور إلى PDF"
app = FastAPI(title=APP_TITLE)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["current_year"] = lambda: datetime.utcnow().year

def to_a4(img: Image.Image, dpi: int = 300, margin_mm: int = 8) -> Image.Image:
    a4_w, a4_h = int(8.27 * dpi), int(11.69 * dpi)
    bg = Image.new("RGB", (a4_w, a4_h), "white")
    margin_px = int((margin_mm / 25.4) * dpi)
    max_w, max_h = a4_w - 2 * margin_px, a4_h - 2 * margin_px
    if img.mode in ("RGBA", "LA", "P"): img = img.convert("RGB")
    elif img.mode == "L": img = img.convert("RGB")
    r = min(max_w / img.width, max_h / img.height)
    new_size = (max(1, int(img.width * r)), max(1, int(img.height * r)))
    img = img.resize(new_size, Image.LANCZOS)
    x = margin_px + (max_w - img.width) // 2
    y = margin_px + (max_h - img.height) // 2
    bg.paste(img, (x, y))
    return bg

@app.get("/healthz", response_class=PlainTextResponse)
def healthz(): return "ok"

# واجهة واحدة فقط: صور → PDF
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

    if order == "name":
        images.sort(key=lambda f: (f.filename or "").lower())
    elif order == "mtime":
        images.sort(key=lambda f: getattr(getattr(f, "spooled", None), "mtime", time.time()))

    pil_list, singles = [], []

    for f in images:
        if not (f.content_type or "").startswith("image/"):
            return JSONResponse({"error": f"الملف {f.filename} ليس صورة."}, status_code=400)
        data = await f.read()
        img = Image.open(BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"): img = img.convert("RGB")
        elif img.mode == "L": img = img.convert("RGB")
        if fit_a4 == "1": img = to_a4(img)
        if per_file == "1":
            buf = BytesIO(); img.save(buf, format="PDF"); buf.seek(0)
            name = (f.filename or "image").rsplit(".", 1)[0] + ".pdf"
            singles.append((name, buf.read()))
        else:
            pil_list.append(img)

    if per_file == "1":
        zip_bytes = BytesIO()
        with ZipFile(zip_bytes, "w", ZIP_DEFLATED) as zf:
            for name, blob in singles: zf.writestr(name, blob)
        zip_bytes.seek(0)
        return StreamingResponse(zip_bytes, media_type="application/zip",
                                 headers={"Content-Disposition": 'attachment; filename="images_pdf.zip"'})

    if not pil_list:
        return JSONResponse({"error": "لا توجد صور صالحة."}, status_code=400)

    pdf_bytes = BytesIO()
    first, rest = pil_list[0], pil_list[1:]
    first.save(pdf_bytes, format="PDF", save_all=True, append_images=rest)
    pdf_bytes.seek(0)
    if not outfile.lower().endswith(".pdf"): outfile += ".pdf"
    return StreamingResponse(pdf_bytes, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{outfile}"'})
