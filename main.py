from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from typing import List
import io
import zipfile
from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os

app = FastAPI()

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
SIZES = [(1280, 900), (2000, 3000)]
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

# More workers — each (image × size) pair runs as its own task
_executor = ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2))


def to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


def resize_crop_to_bytes(contents: bytes, filename: str, target_w: int, target_h: int) -> tuple[str, bytes]:
    img = Image.open(io.BytesIO(contents))
    # Draft mode: tell JPEG decoder to subsample during decode — free speedup on large photos
    img.draft("RGB", (target_w * 2, target_h * 2))
    img = to_rgb(img)
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.BOX)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    stem = Path(filename).stem
    return (f"resized-images/{target_w}x{target_h}/{stem}_{target_w}x{target_h}.jpg", out.getvalue())


@app.get("/")
async def index():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/resize")
async def resize_images(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    async def read_upload(file: UploadFile):
        if file.content_type not in ALLOWED_TYPES:
            return {"filename": file.filename, "error": "unsupported format"}
        contents = await file.read()
        if len(contents) > MAX_FILE_SIZE:
            return {"filename": file.filename, "error": "exceeds 50MB limit"}
        return {"filename": file.filename, "contents": contents}

    uploads = await asyncio.gather(*[read_upload(f) for f in files])

    valid = [u for u in uploads if "contents" in u]
    errors = [f"{u['filename']}: {u['error']}" for u in uploads if "error" in u]

    if not valid:
        raise HTTPException(
            status_code=400,
            detail="No images could be processed. " + " | ".join(errors),
        )

    # Fan out every (image × size) as its own parallel task
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(_executor, resize_crop_to_bytes, u["contents"], u["filename"], w, h)
        for u in valid
        for w, h in SIZES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    zip_buffer = io.BytesIO()
    processed_files = set()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
        for r in results:
            if isinstance(r, Exception):
                errors.append(str(r))
            else:
                path, data = r
                zf.writestr(path, data)
                processed_files.add(path.split("/")[2])  # extract original filename

    if not processed_files:
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
