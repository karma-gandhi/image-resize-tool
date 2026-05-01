from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from typing import List
import io
import zipfile
from pathlib import Path

app = FastAPI()

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
SIZES = [(1280, 900), (2000, 3000)]
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


def to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


def resize_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/resize")
async def resize_images(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    zip_buffer = io.BytesIO()
    errors = []
    processed = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            try:
                if file.content_type not in ALLOWED_TYPES:
                    errors.append(f"{file.filename}: unsupported format")
                    continue

                contents = await file.read()

                if len(contents) > MAX_FILE_SIZE:
                    errors.append(f"{file.filename}: exceeds 50MB limit")
                    continue

                img = Image.open(io.BytesIO(contents))
                img = to_rgb(img)
                stem = Path(file.filename).stem

                for w, h in SIZES:
                    resized = resize_crop(img, w, h)
                    out = io.BytesIO()
                    resized.save(out, format="JPEG", quality=90, optimize=True)
                    out.seek(0)
                    zf.writestr(f"resized-images/{stem}_{w}x{h}.jpg", out.read())

                processed += 1

            except Exception as e:
                errors.append(f"{file.filename}: {str(e)}")

    if processed == 0:
        raise HTTPException(
            status_code=400,
            detail="No images could be processed. " + " | ".join(errors),
        )

    zip_buffer.seek(0)

    headers = {"Content-Disposition": "attachment; filename=resized-images.zip"}
    if errors:
        headers["X-Processing-Errors"] = " | ".join(errors[:5])

    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


app.mount("/static", StaticFiles(directory="static"), name="static")
