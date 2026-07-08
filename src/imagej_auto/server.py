from __future__ import annotations

import cgi
import json
import shutil
import socket
import tempfile
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .grouping import parse_group_names, parse_order, parse_replicates_per_group
from .models import PipelineOptions
from .pipeline import run_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs"


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ImageJAutomationHandler(BaseHTTPRequestHandler):
    server_version = "ImageJAutomation/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
            html = html.replace("__DEFAULT_OUTPUT__", str(DEFAULT_OUTPUT))
            _text_response(self, 200, html)
            return
        if parsed.path == "/api/health":
            _json_response(self, 200, {"ok": True, "default_output": str(DEFAULT_OUTPUT)})
            return
        _text_response(self, 404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            _json_response(self, 404, {"ok": False, "error": "Not found"})
            return
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            if "pptx" not in form or not getattr(form["pptx"], "filename", ""):
                raise ValueError("请先上传 PPTX 文件。")

            output_dir = form.getfirst("output_dir", str(DEFAULT_OUTPUT)).strip()
            if not output_dir:
                raise ValueError("请填写输出路径。")
            threshold_method = form.getfirst("threshold_method", "Otsu").strip() or "Otsu"
            min_threshold = int(form.getfirst("min_threshold", "8"))
            order = parse_order(form.getfirst("order", "red,green,merge"))
            group_names = parse_group_names(form.getfirst("group_names", ""))
            replicates_per_group = parse_replicates_per_group(form.getfirst("replicates_per_group", "3"))
            roi_per_replicate = max(1, int(form.getfirst("roi_per_replicate", "3")))
            expected_trend = form.getfirst("expected_trend", "none").strip() or "none"
            threshold_scope = form.getfirst("threshold_scope", "fixed").strip() or "fixed"
            red_fixed_threshold = int(form.getfirst("red_fixed_threshold", "80"))
            green_fixed_threshold = int(form.getfirst("green_fixed_threshold", "80"))
            use_min_threshold = form.getfirst("use_min_threshold", "") == "1"
            background_subtraction = form.getfirst("background_subtraction", "") == "1"
            show_threshold_preview = form.getfirst("show_threshold_preview", "") == "1"
            dead_percent_mode = form.getfirst("dead_percent_mode", "intensity_ratio").strip() or "intensity_ratio"
            fiji_path = form.getfirst("fiji_path", "").strip()

            logs: list[str] = []
            with tempfile.TemporaryDirectory(prefix="imagej_upload_") as tmp:
                ppt_item = form["pptx"]
                upload_path = Path(tmp) / Path(ppt_item.filename).name
                with upload_path.open("wb") as f:
                    shutil.copyfileobj(ppt_item.file, f)

                options = PipelineOptions(
                    threshold_method=threshold_method,
                    min_threshold=min_threshold,
                    order=order,
                    group_names=group_names,
                    replicates_per_group=replicates_per_group,
                    roi_per_replicate=roi_per_replicate,
                    expected_trend=expected_trend,
                    threshold_scope=threshold_scope,
                    red_fixed_threshold=red_fixed_threshold,
                    green_fixed_threshold=green_fixed_threshold,
                    use_min_threshold=use_min_threshold,
                    background_subtraction=background_subtraction,
                    show_threshold_preview=show_threshold_preview,
                    dead_percent_mode=dead_percent_mode,
                    fiji_path=fiji_path,
                )
                result = run_pipeline(upload_path, output_dir, options, log=logs.append)

            _json_response(self, 200, {"ok": True, "logs": logs, "result": result})
        except Exception as exc:
            _json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                },
            )

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def find_available_port(start: int = 8765, attempts: int = 40) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有找到可用端口。")


def run_server(open_browser: bool = True) -> None:
    port = find_available_port()
    url = f"http://127.0.0.1:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), ImageJAutomationHandler)
    print(f"ImageJ 自动化检测已启动: {url}")
    print("关闭这个终端窗口即可停止服务。")
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()
