from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}


def natural_key(value: str | Path) -> list[int | str]:
    text = Path(value).name if isinstance(value, Path) else str(value)
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def extract_ppt_images(pptx_path: str | Path, output_dir: str | Path) -> list[Path]:
    pptx = Path(pptx_path)
    out = Path(output_dir)
    if not pptx.exists():
        raise FileNotFoundError(f"PPT 文件不存在: {pptx}")
    if pptx.suffix.lower() != ".pptx":
        raise ValueError("请上传 .pptx 文件")

    out.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    with zipfile.ZipFile(pptx) as zf:
        media_names = [
            name
            for name in zf.namelist()
            if name.startswith("ppt/media/")
            and Path(name).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
            and not name.endswith("/")
        ]
        media_names.sort(key=natural_key)
        for name in media_names:
            target = out / Path(name).name
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            copied.append(target)

    if not copied:
        raise ValueError("PPT 中没有找到可处理的图片。请确认图片在 PPT 内，而不是外部链接。")
    return copied
