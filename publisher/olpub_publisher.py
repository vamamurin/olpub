#!/usr/bin/env python3
"""olpub-publisher — trang publish PDF cho Overleaf CE (VM 1007 host-publish).

Ba listener, tách cổng để Nginx không thể nhầm luồng:
  PUBLIC   :8080  read.tapama.cloud            — chỉ GET/HEAD, ai cũng xem được
  ADMIN    :8082  overleaf.tapama.cloud/_pub/  — sau mTLS + Authelia, Nginx gắn X-Pub-User / X-Pub-Device
  INTERNAL :8081  collector trên VM 1006       — Bearer token, nhận PDF/TeX đã biên dịch

Chỉ dùng thư viện chuẩn Python (>= 3.11) + qpdf, exiftool, poppler-utils.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import resource
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

# ---------------------------------------------------------------- cấu hình


def env(name, default):
    return os.environ.get(name, default)


DATA_DIR = env("OLPUB_DATA_DIR", "/var/lib/olpub")
APP_DIR = env("OLPUB_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(APP_DIR, "static")
PDFJS_DIR = env("OLPUB_PDFJS_DIR", "/opt/olpub/pdfjs")  # chứa build/ và web/ của bản dist
PUBLIC_BIND = env("OLPUB_PUBLIC_BIND", "0.0.0.0:8080")
ADMIN_BIND = env("OLPUB_ADMIN_BIND", "0.0.0.0:8082")
INTERNAL_BIND = env("OLPUB_INTERNAL_BIND", "0.0.0.0:8081")
PUBLIC_BASE_URL = env("OLPUB_PUBLIC_BASE_URL", "https://read.tapama.cloud").rstrip("/")
OVERLEAF_ORIGIN = env("OLPUB_OVERLEAF_ORIGIN", "https://overleaf.tapama.cloud").rstrip("/")
TOKEN_FILE = env("OLPUB_INGEST_TOKEN_FILE", "/etc/olpub/ingest.token")
MAX_PDF_BYTES = int(env("OLPUB_MAX_PDF_MB", "50")) * 1024 * 1024
MAX_TEX_BYTES = 5 * 1024 * 1024
MAX_TXT_IN_HTML = 2 * 1024 * 1024
REQUIRE_USER = env("OLPUB_REQUIRE_USER", "1") == "1"  # header từ Authelia
REQUIRE_DEVICE = env("OLPUB_REQUIRE_DEVICE", "1") == "1"  # header từ mTLS
BLOCK_TRAINING_CRAWLERS = env("OLPUB_BLOCK_TRAINING_CRAWLERS", "1") == "1"
TZ = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh, không DST

DB_PATH = os.path.join(DATA_DIR, "olpub.sqlite3")
FILES_DIR = os.path.join(DATA_DIR, "files")

# Tên PDF bị từ chối: hành động tự chạy, file đính kèm, form gửi dữ liệu đi
FORBIDDEN_PDF_NAMES = {
    "/JavaScript", "/JS", "/Launch", "/EmbeddedFile", "/EmbeddedFiles",
    "/RichMedia", "/XFA", "/SubmitForm", "/ImportData", "/GoToE", "/Sound", "/Movie",
}
PID_RE = re.compile(r"^[0-9a-f]{24}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}[a-z0-9]$")
RESERVED_SLUGS = {"static", "pdfjs", "robots", "favicon", "_pub", "internal", "admin", "api", "index"}
SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # bỏ i, l, o, 0, 1 cho dễ đọc

log = logging.getLogger("olpub")

# ---------------------------------------------------------------- CSDL

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(FILES_DIR, exist_ok=True)
    with _db_lock, db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
              project_id TEXT PRIMARY KEY,
              slug TEXT UNIQUE NOT NULL,
              published INTEGER NOT NULL DEFAULT 0,
              mode TEXT NOT NULL DEFAULT 'live',
              include_source INTEGER NOT NULL DEFAULT 0,
              indexable INTEGER NOT NULL DEFAULT 0,
              refresh_requested INTEGER NOT NULL DEFAULT 0,
              title TEXT,
              build_mtime REAL NOT NULL DEFAULT 0,
              pdf_sha TEXT,
              pdf_size INTEGER,
              has_tex INTEGER NOT NULL DEFAULT 0,
              updated_at REAL,
              created_by TEXT,
              created_at REAL,
              last_error TEXT,
              last_error_at REAL
            );
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL, actor TEXT, device TEXT,
              project_id TEXT, action TEXT, detail TEXT
            );
            """
        )


def audit(actor, device, pid, action, detail=""):
    with _db_lock, db() as c:
        c.execute(
            "INSERT INTO audit(ts, actor, device, project_id, action, detail) VALUES (?,?,?,?,?,?)",
            (time.time(), actor, device, pid, action, detail),
        )
    log.info("audit actor=%s device=%s project=%s action=%s %s", actor, device, pid, action, detail)


def get_pub(pid=None, slug=None):
    with db() as c:
        if pid:
            r = c.execute("SELECT * FROM publications WHERE project_id=?", (pid,)).fetchone()
        else:
            r = c.execute("SELECT * FROM publications WHERE slug=?", (slug,)).fetchone()
    return dict(r) if r else None


def update_pub(pid, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, db() as c:
        c.execute(f"UPDATE publications SET {keys} WHERE project_id=?", (*fields.values(), pid))


def new_slug():
    with db() as c:
        while True:
            s = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(8))
            if not c.execute("SELECT 1 FROM publications WHERE slug=?", (s,)).fetchone():
                return s


def files_dir(pid):
    assert PID_RE.match(pid)
    return os.path.join(FILES_DIR, pid)


def remove_files(pid, which=("doc.pdf", "doc.txt", "doc.tex")):
    d = files_dir(pid)
    for name in which:
        try:
            os.unlink(os.path.join(d, name))
        except FileNotFoundError:
            pass


def atomic_write(path, data: bytes):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------- làm sạch PDF


class RejectPDF(Exception):
    pass


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PDF_BYTES * 2, MAX_PDF_BYTES * 2))


def run(cmd, timeout=90, ok_codes=(0,)):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, preexec_fn=_limits)
    if p.returncode not in ok_codes:
        raise RejectPDF(f"{os.path.basename(cmd[0])} lỗi ({p.returncode}): "
                        f"{p.stderr.decode(errors='replace').strip()[:300]}")
    return p


def _walk_names(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_PDF_NAMES:
                found.add(k)
            _walk_names(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_names(v, found)
    elif isinstance(node, str) and node in FORBIDDEN_PDF_NAMES:
        found.add(node)


def sanitize_pdf(raw: bytes):
    """Trả về (pdf_sạch, title, text). Ném RejectPDF nếu file không chấp nhận được."""
    if not raw.startswith(b"%PDF-"):
        raise RejectPDF("không phải PDF (thiếu chữ ký %PDF-)")
    if len(raw) > MAX_PDF_BYTES:
        raise RejectPDF("PDF vượt giới hạn dung lượng")
    with tempfile.TemporaryDirectory(prefix="olpub-") as td:
        src = os.path.join(td, "in.pdf")
        with open(src, "wb") as f:
            f.write(raw)
        # PDF mã hóa: qpdf --is-encrypted trả 0 nếu có mã hóa, 2 nếu không
        if run(["qpdf", "--is-encrypted", src], ok_codes=(0, 2)).returncode == 0:
            raise RejectPDF("PDF bị mã hóa")
        # Cấu trúc object dạng JSON (không kèm dữ liệu stream) để tìm tên nguy hiểm
        js = run(["qpdf", "--json=2", "--json-key=qpdf", "--json-stream-data=none", src],
                 ok_codes=(0, 3))  # 3 = có cảnh báo nhưng đọc được
        found = set()
        _walk_names(json.loads(js.stdout), found)
        if found:
            raise RejectPDF("PDF chứa thành phần bị chặn: " + ", ".join(sorted(found)))
        # Lấy title trước khi xóa metadata
        info = run(["pdfinfo", src], ok_codes=(0,)).stdout.decode(errors="replace")
        m = re.search(r"^Title:\s*(.+)$", info, re.M)
        title = m.group(1).strip()[:200] if m else ""
        # Xóa toàn bộ metadata (Info + XMP), chỉ đặt lại Title.
        # exiftool sửa PDF theo kiểu incremental update (bản cũ vẫn khôi phục được),
        # nên BẮT BUỘC qpdf ghi lại file ngay sau đó để loại phần cũ.
        stripped = os.path.join(td, "stripped.pdf")
        cmd = ["exiftool", "-q", "-q", "-all=", "-o", stripped]
        if title:
            cmd.insert(-2, f"-Title={title}")
        run(cmd + [src])
        out = os.path.join(td, "out.pdf")
        run(["qpdf", "--linearize", "--object-streams=generate", stripped, out], ok_codes=(0, 3))
        run(["qpdf", "--check", out], ok_codes=(0, 3))
        # Văn bản thuần cho AI/đọc nhanh: thứ tự đọc mặc định (tốt cho bài 2 cột), đánh dấu trang
        run(["pdftotext", "-enc", "UTF-8", out, os.path.join(td, "out.txt")], ok_codes=(0,))
        with open(os.path.join(td, "out.txt"), "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
        pages = text.split("\f")
        if pages and not pages[-1].strip():
            pages = pages[:-1]
        text = "".join(f"\n\n===== Trang {i} =====\n\n{p.strip()}" for i, p in enumerate(pages, 1)).strip()
        if not title:
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not ln.startswith("=====")), "")
            title = first[:120]
        with open(out, "rb") as f:
            return f.read(), title, text


# ---------------------------------------------------------------- HTTP chung

ROBOTS_TRAINING = ["GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Google-Extended",
                   "Applebot-Extended", "meta-externalagent", "Bytespider", "cohere-training-data-crawler"]

PUBLIC_HTML_CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; frame-src 'self'; "
                   "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PDFJS_CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
             "img-src 'self' data: blob:; font-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
             "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
ADMIN_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
             "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

CTYPES = {
    ".html": "text/html; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif", ".ftl": "text/plain; charset=utf-8",
    ".json": "application/json", ".wasm": "application/wasm", ".bcmap": "application/octet-stream",
    ".pfb": "application/octet-stream", ".ttf": "font/ttf", ".icc": "application/octet-stream",
    ".map": "application/json",
}


def safe_join(root, rel):
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    if p != root and not p.startswith(root + os.sep):
        return None
    return p


def fmt_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M %d/%m/%Y") if ts else "—"


class Base(BaseHTTPRequestHandler):
    server_version = "olpub"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj, headers=None):
        h = {"Cache-Control": "no-store"}
        h.update(headers or {})
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", h)

    def read_body(self, limit):
        n = self.headers.get("Content-Length")
        if n is None or not n.isdigit():
            raise ValueError("thiếu Content-Length")
        n = int(n)
        if n > limit:
            raise ValueError("body quá lớn")
        return self.rfile.read(n)

    def path_only(self):
        return urlsplit(self.path).path

    def serve_file(self, path, headers, etag=None, mtime=None):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except (FileNotFoundError, IsADirectoryError):
            return self.send(404, "Không tìm thấy\n")
        ext = os.path.splitext(path)[1].lower()
        h = dict(headers)
        if etag:
            h["ETag"] = f'"{etag}"'
            if self.headers.get("If-None-Match") == f'"{etag}"':
                self.send_response(304)
                for k, v in h.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if mtime:
            h["Last-Modified"] = formatdate(mtime, usegmt=True)
        self.send(200, data, h.pop("Content-Type", CTYPES.get(ext, "application/octet-stream")), h)


# ---------------------------------------------------------------- PUBLIC :8080

VIEW_TMPL = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/static/read.css">
<link rel="alternate" type="application/pdf" href="{pdf}" title="PDF">
<link rel="alternate" type="text/plain" href="{txt}" title="Văn bản thuần">{tex_link}
</head><body>
<header><div class="t"><h1>{title}</h1><span class="u">Cập nhật {updated}</span></div>
<nav><a href="{pdf}" download>Tải PDF</a><a href="{txt}">Văn bản (.txt)</a>{tex_nav}</nav></header>
<main><iframe src="{viewer}" title="{title}"></iframe>
<noscript><p>Trình xem cần JavaScript. <a href="{pdf}">Mở file PDF trực tiếp</a>.</p></noscript></main>
<details><summary>Nội dung dạng văn bản</summary><pre>{text}</pre></details>
</body></html>
"""


class PublicHandler(Base):
    def do_POST(self):
        self.send(405, "Chỉ hỗ trợ GET\n", headers={"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path_only()
        if path == "/robots.txt":
            lines = []
            if BLOCK_TRAINING_CRAWLERS:
                lines.append("# Không dùng nội dung để huấn luyện mô hình")
                for ua in ROBOTS_TRAINING:
                    lines += [f"User-agent: {ua}", "Disallow: /", ""]
            lines += ["# Trình duyệt và công cụ AI tải link theo yêu cầu người dùng: cho phép",
                      "User-agent: *", "Allow: /", ""]
            return self.send(200, "\n".join(lines), headers={"Cache-Control": "public, max-age=3600"})
        if path.startswith("/static/"):
            p = safe_join(STATIC_DIR, path[len("/static/"):])
            if not p or not os.path.basename(p).startswith("read"):
                return self.send(404, "Không tìm thấy\n")
            return self.serve_file(p, {"Cache-Control": "public, max-age=3600"})
        if path.startswith("/pdfjs/"):
            p = safe_join(PDFJS_DIR, path[len("/pdfjs/"):])
            if not p:
                return self.send(404, "Không tìm thấy\n")
            return self.serve_file(p, {"Cache-Control": "public, max-age=86400",
                                       "Content-Security-Policy": PDFJS_CSP})
        m = re.match(r"^/([a-z0-9-]{4,64})(\.pdf|\.txt|\.tex)?$", path)
        if not m:
            return self.send(404, "Không tìm thấy\n")
        slug, ext = m.group(1), m.group(2) or ""
        pub = get_pub(slug=slug)
        if not pub or not pub["published"] or not pub["pdf_sha"]:
            return self.send(404, "Không tìm thấy hoặc chưa có bản publish\n")
        d = files_dir(pub["project_id"])
        common = {"Cache-Control": "no-cache",
                  "X-Robots-Tag": "all" if pub["indexable"] else "noindex"}
        if ext == ".pdf":
            h = dict(common, **{"Content-Type": "application/pdf",
                                "Content-Disposition": f'inline; filename="{slug}.pdf"',
                                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'"})
            return self.serve_file(os.path.join(d, "doc.pdf"), h, pub["pdf_sha"], pub["updated_at"])
        if ext == ".txt":
            return self.serve_file(os.path.join(d, "doc.txt"), dict(common, **{
                "Content-Type": "text/plain; charset=utf-8"}), pub["pdf_sha"] + "-t", pub["updated_at"])
        if ext == ".tex":
            if not (pub["include_source"] and pub["has_tex"]):
                return self.send(404, "Project này không publish mã nguồn\n")
            return self.serve_file(os.path.join(d, "doc.tex"), dict(common, **{
                "Content-Type": "text/plain; charset=utf-8"}), pub["pdf_sha"] + "-x", pub["updated_at"])
        # Trang xem
        try:
            with open(os.path.join(d, "doc.txt"), "rb") as f:
                text = f.read(MAX_TXT_IN_HTML).decode("utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        v = pub["pdf_sha"][:12]
        pdf_url = f"/{slug}.pdf"
        tex = pub["include_source"] and pub["has_tex"]
        title = html.escape(pub["title"] or slug)
        page = VIEW_TMPL.format(
            title=title,
            updated=fmt_time(pub["updated_at"]),
            pdf=pdf_url, txt=f"/{slug}.txt",
            tex_link=f'\n<link rel="alternate" type="text/x-tex" href="/{slug}.tex" title="LaTeX">' if tex else "",
            tex_nav=f'<a href="/{slug}.tex">LaTeX (.tex)</a>' if tex else "",
            viewer="/pdfjs/web/viewer.html?file=" + quote(f"{pdf_url}?v={v}", safe=""),
            text=html.escape(text),
        )
        h = dict(common, **{"Content-Security-Policy": PUBLIC_HTML_CSP})
        self.send(200, page, "text/html; charset=utf-8", h)


# ---------------------------------------------------------------- ADMIN :8082


def status_obj(pid):
    p = get_pub(pid=pid)
    if not p:
        return {"project_id": pid, "exists": False, "published": False}
    base = f"{PUBLIC_BASE_URL}/{p['slug']}"
    return {
        "project_id": pid, "exists": True, "published": bool(p["published"]),
        "slug": p["slug"], "url": base, "pdf_url": base + ".pdf", "txt_url": base + ".txt",
        "tex_url": base + ".tex" if p["include_source"] and p["has_tex"] else None,
        "mode": p["mode"], "include_source": bool(p["include_source"]),
        "indexable": bool(p["indexable"]), "has_pdf": bool(p["pdf_sha"]),
        "waiting": bool(p["published"] and (not p["pdf_sha"] or p["refresh_requested"])),
        "title": p["title"], "updated_at": p["updated_at"], "updated_text": fmt_time(p["updated_at"]),
        "last_error": p["last_error"], "created_by": p["created_by"],
    }


class AdminHandler(Base):
    def identity(self):
        user = (self.headers.get("X-Pub-User") or "").strip()
        device = (self.headers.get("X-Pub-Device") or "").strip()
        if (REQUIRE_USER and not user) or (REQUIRE_DEVICE and not device):
            return None
        return user or "-", device or "-"

    def do_GET(self):
        ident = self.identity()
        if not ident:
            return self.send(401, "Thiếu danh tính (Authelia/mTLS)\n")
        path = self.path_only()
        if path in ("/_pub/button.js", "/_pub/admin.js"):
            return self.serve_file(os.path.join(STATIC_DIR, path[len("/_pub/"):]),
                                   {"Cache-Control": "no-cache"}, etag=str(int(os.path.getmtime(
                                       os.path.join(STATIC_DIR, path[len("/_pub/"):])))))
        if path == "/_pub/admin.css":
            return self.serve_file(os.path.join(STATIC_DIR, "admin.css"), {"Cache-Control": "no-cache"})
        if path in ("/_pub", "/_pub/"):
            page = ("<!doctype html><html lang='vi'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<title>Publish — quản lý</title><link rel='stylesheet' href='/_pub/admin.css'></head>"
                    f"<body data-user='{html.escape(ident[0], quote=True)}'><div id='app'></div>"
                    "<script src='/_pub/admin.js'></script></body></html>")
            return self.send(200, page, "text/html; charset=utf-8",
                             {"Content-Security-Policy": ADMIN_CSP, "Cache-Control": "no-store"})
        if path == "/_pub/api/list":
            with db() as c:
                pids = [r[0] for r in c.execute(
                    "SELECT project_id FROM publications ORDER BY published DESC, updated_at DESC")]
            return self.send_json(200, {"user": ident[0], "items": [status_obj(p) for p in pids]})
        m = re.match(r"^/_pub/api/project/([0-9a-f]{24})$", path)
        if m:
            return self.send_json(200, status_obj(m.group(1)))
        self.send(404, "Không tìm thấy\n")

    def csrf_ok(self):
        if self.headers.get("X-OLPub") != "1":
            return False
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != OVERLEAF_ORIGIN:
            return False
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs and sfs not in ("same-origin", "none"):
            return False
        return True

    def do_POST(self):
        ident = self.identity()
        if not ident:
            return self.send(401, "Thiếu danh tính (Authelia/mTLS)\n")
        if not self.csrf_ok():
            return self.send_json(403, {"error": "Request bị chặn (CSRF)"})
        m = re.match(r"^/_pub/api/project/([0-9a-f]{24})$", self.path_only())
        if not m:
            return self.send(404, "Không tìm thấy\n")
        pid = m.group(1)
        try:
            req = json.loads(self.read_body(4096) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": f"Body không hợp lệ: {e}"})
        user, device = ident
        action = req.get("action")
        p = get_pub(pid=pid)
        try:
            if action == "publish":
                slug = (req.get("slug") or "").strip().lower()
                if slug:
                    self.check_slug(slug, pid)
                mode = req.get("mode") if req.get("mode") in ("live", "pinned") else "live"
                if not p:
                    with _db_lock, db() as c:
                        c.execute("INSERT INTO publications(project_id, slug, published, mode, refresh_requested,"
                                  " created_by, created_at) VALUES (?,?,1,?,1,?,?)",
                                  (pid, slug or new_slug(), mode, user, time.time()))
                else:
                    update_pub(pid, published=1, mode=mode, refresh_requested=1, last_error=None,
                               **({"slug": slug} if slug else {}))
            elif not p:
                return self.send_json(404, {"error": "Project chưa từng publish"})
            elif action == "unpublish":
                update_pub(pid, published=0, refresh_requested=0, pdf_sha=None, pdf_size=None,
                           has_tex=0, build_mtime=0, updated_at=None, last_error=None)
                remove_files(pid)
            elif action == "set_mode":
                mode = req.get("mode")
                if mode not in ("live", "pinned"):
                    raise ValueError("mode phải là live hoặc pinned")
                update_pub(pid, mode=mode, **({"refresh_requested": 1} if mode == "live" else {}))
            elif action == "refresh":
                update_pub(pid, refresh_requested=1, last_error=None)
            elif action == "set_source":
                on = bool(req.get("enabled"))
                update_pub(pid, include_source=int(on), **({"refresh_requested": 1} if on else {"has_tex": 0}))
                if not on:
                    remove_files(pid, ("doc.tex",))
            elif action == "set_indexable":
                update_pub(pid, indexable=int(bool(req.get("enabled"))))
            elif action == "set_slug":
                slug = (req.get("slug") or "").strip().lower()
                self.check_slug(slug, pid)
                update_pub(pid, slug=slug)
            else:
                return self.send_json(400, {"error": "action không hợp lệ"})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        audit(user, device, pid, action, json.dumps({k: v for k, v in req.items() if k != "action"},
                                                    ensure_ascii=False))
        self.send_json(200, status_obj(pid))

    @staticmethod
    def check_slug(slug, pid):
        if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            raise ValueError("Tên link: 4–64 ký tự a-z, 0-9, dấu gạch ngang; không bắt đầu/kết thúc bằng '-'")
        other = get_pub(slug=slug)
        if other and other["project_id"] != pid:
            raise ValueError("Tên link đã được dùng")


# ---------------------------------------------------------------- INTERNAL :8081


def load_token():
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    if len(t) < 32:
        raise SystemExit(f"Token trong {TOKEN_FILE} quá ngắn (>= 32 ký tự)")
    return t


class InternalHandler(Base):
    token = None

    def authed(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {self.token}".encode())

    def do_GET(self):
        if not self.authed():
            return self.send(401, "unauthorized\n")
        if self.path_only() == "/internal/targets":
            with db() as c:
                rows = c.execute("SELECT project_id, mode, include_source, build_mtime, refresh_requested "
                                 "FROM publications WHERE published=1 AND (mode='live' OR refresh_requested=1)")
                items = [{"project_id": r[0], "mode": r[1], "include_source": bool(r[2]),
                          "build_mtime": r[3], "refresh": bool(r[4])} for r in rows]
            return self.send_json(200, {"targets": items})
        self.send(404, "not found\n")

    def do_PUT(self):
        if not self.authed():
            return self.send(401, "unauthorized\n")
        m = re.match(r"^/internal/(pdf|tex|error)/([0-9a-f]{24})$", self.path_only())
        if not m:
            return self.send(404, "not found\n")
        kind, pid = m.groups()
        p = get_pub(pid=pid)
        if not p or not p["published"]:
            return self.send_json(409, {"error": "project không ở trạng thái publish"})
        try:
            body = self.read_body(MAX_PDF_BYTES if kind == "pdf" else MAX_TEX_BYTES)
        except ValueError as e:
            return self.send_json(413, {"error": str(e)})
        if kind == "error":
            update_pub(pid, last_error=body.decode(errors="replace")[:500], last_error_at=time.time())
            return self.send_json(200, {"ok": True})
        if kind == "tex":
            if not p["include_source"]:
                return self.send_json(409, {"error": "project không bật publish mã nguồn"})
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                return self.send_json(400, {"error": "TeX phải là UTF-8"})
            # ghi tạm; chỉ đưa vào sử dụng khi PDF cùng bản build được nhận
            atomic_write(os.path.join(files_dir(pid), "doc.tex.pending"), body)
            return self.send_json(200, {"ok": True})
        # kind == pdf
        try:
            build_mtime = float(self.headers.get("X-Build-Mtime", "0"))
        except ValueError:
            build_mtime = 0.0
        try:
            pdf, title, text = sanitize_pdf(body)
        except (RejectPDF, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            msg = str(e) if isinstance(e, RejectPDF) else f"xử lý PDF thất bại: {e.__class__.__name__}"
            # không publish bản lỗi; bản cũ (nếu có) vẫn giữ nguyên
            update_pub(pid, last_error=msg, last_error_at=time.time(), build_mtime=build_mtime,
                       refresh_requested=0)
            log.warning("reject project=%s: %s", pid, msg)
            return self.send_json(422, {"error": msg})
        sha = hashlib.sha256(pdf).hexdigest()
        d = files_dir(pid)
        atomic_write(os.path.join(d, "doc.txt"), text.encode("utf-8"))
        atomic_write(os.path.join(d, "doc.pdf"), pdf)
        pending = os.path.join(d, "doc.tex.pending")
        has_tex = 0
        if p["include_source"] and os.path.exists(pending):
            os.replace(pending, os.path.join(d, "doc.tex"))
            has_tex = 1
        elif p["include_source"] and os.path.exists(os.path.join(d, "doc.tex")):
            has_tex = 1
        update_pub(pid, pdf_sha=sha, pdf_size=len(pdf), title=title or p["title"], build_mtime=build_mtime,
                   updated_at=time.time(), refresh_requested=0, last_error=None, has_tex=has_tex)
        audit("collector", "-", pid, "ingest", f"sha={sha[:12]} size={len(pdf)} tex={has_tex}")
        self.send_json(200, {"ok": True, "sha": sha})


# ---------------------------------------------------------------- main


def serve(bind, handler):
    host, port = bind.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, name=f"{handler.__name__}:{port}", daemon=True)
    t.start()
    log.info("listening %s on %s", handler.__name__, bind)
    return srv


def main():
    logging.basicConfig(level=env("OLPUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for tool in ("qpdf", "exiftool", "pdfinfo", "pdftotext"):
        if not shutil.which(tool):
            raise SystemExit(f"Thiếu công cụ: {tool}")
    if not os.path.exists(os.path.join(PDFJS_DIR, "web", "viewer.html")):
        log.warning("Chưa có PDF.js tại %s — trang xem sẽ trống, link .pdf/.txt vẫn chạy", PDFJS_DIR)
    InternalHandler.token = load_token()
    init_db()
    serve(PUBLIC_BIND, PublicHandler)
    serve(ADMIN_BIND, AdminHandler)
    serve(INTERNAL_BIND, InternalHandler)
    threading.Event().wait()


if __name__ == "__main__":
    main()
