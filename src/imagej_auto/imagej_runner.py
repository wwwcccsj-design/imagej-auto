from __future__ import annotations

import os
import subprocess
from pathlib import Path


FIJI_CANDIDATES = [
    "/usr/local/bin/fiji",
    "/usr/local/bin/imagej",
    "/Applications/Fiji/Fiji.app/Contents/MacOS/fiji-macos",
]


def find_fiji(user_path: str = "") -> str:
    candidates = []
    if user_path.strip():
        candidates.append(user_path.strip())
    if os.environ.get("FIJI_PATH"):
        candidates.append(os.environ["FIJI_PATH"])
    candidates.extend(FIJI_CANDIDATES)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError("没有找到 Fiji/ImageJ。请确认 /usr/local/bin/fiji 或 /Applications/Fiji/Fiji.app 存在。")


def run_imagej_macro(
    macro_path: str | Path,
    extracted_images_dir: str | Path,
    run_dir: str | Path,
    fiji_path: str = "",
    timeout_seconds: int = 1800,
) -> subprocess.CompletedProcess[str]:
    executable = find_fiji(fiji_path)
    macro = Path(macro_path).expanduser().resolve()
    images = Path(extracted_images_dir).expanduser().resolve()
    work = Path(run_dir).expanduser().resolve()
    command = [executable, "--headless", "-macro", str(macro), str(images)]
    completed = subprocess.run(
        command,
        cwd=work,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    log_text = [
        "COMMAND:",
        " ".join(command),
        "",
        "STDOUT:",
        completed.stdout,
        "",
        "STDERR:",
        completed.stderr,
        "",
        f"RETURN_CODE: {completed.returncode}",
    ]
    (work / "imagej_run.log").write_text("\n".join(log_text), encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"Fiji/ImageJ 运行失败，返回码 {completed.returncode}。请查看 {work / 'imagej_run.log'}")
    return completed
