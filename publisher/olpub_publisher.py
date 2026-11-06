#!/usr/bin/env python3
"""olpub-publisher 1.1 — publish PDF cho Overleaf CE (VM 1007 host-publish).

Từ bản 1.1, Python KHÔNG còn trả lời người xem. Publisher sinh sẵn file tĩnh vào
OLPUB_WWW_DIR; Nginx trên chính VM 1007 phục vụ thư mục đó ở cổng 8080.

Hai listener Python còn lại, đều không mở cho người lạ:
  ADMIN    :8082  overleaf.tapama.cloud/_pub/button.js + /_pub/api/ — sau mTLS + Authelia;
                  Nginx gắn X-Pub-User / X-Pub-Device và ghim mã băm SRI của button.js
  INTERNAL :8081  collector trên VM 1006       — Bearer token, nhận PDF/TeX đã biên dịch

Chỉ dùng thư viện chuẩn Python (>= 3.11) + qpdf, exiftool, poppler-utils.
"""
import base64
import binascii
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
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ---------------------------------------------------------------- cấu hình


def env(name, default):
    return os.environ.get(name, default)


DATA_DIR = env("OLPUB_DATA_DIR", "/var/lib/olpub")
WWW_DIR = env("OLPUB_WWW_DIR", "/srv/olpub/www")
APP_DIR = env("OLPUB_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(APP_DIR, "static")
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
FILES_DIR = os.path.join(DATA_DIR, "files")  # bản gốc đã làm sạch, theo project_id

# Tên PDF bị từ chối: hành động tự chạy, file đính kèm, form gửi dữ liệu đi
FORBIDDEN_PDF_NAMES = {
    "/JavaScript", "/JS", "/Launch", "/EmbeddedFile", "/EmbeddedFiles",
    "/RichMedia", "/XFA", "/SubmitForm", "/ImportData", "/GoToE", "/Sound", "/Movie",
}
PID_RE = re.compile(r"^[0-9a-f]{24}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}[a-z0-9]$")
WWW_FILE_RE = re.compile(r"^([a-z0-9][a-z0-9-]{2,62}[a-z0-9])\.(html|pdf|txt|tex)$")
RESERVED_SLUGS = {"static", "pdfjs", "robots", "favicon", "_pub", "internal", "admin", "api", "index", "read"}
SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # bỏ i, l, o, 0, 1 cho dễ đọc

log = logging.getLogger("olpub")

# ---------------------------------------------------------------- CSDL

_db_lock = threading.Lock()
_render_lock = threading.RLock()


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


def atomic_write(path, data: bytes, mode=0o640):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_bytes(path, limit=None):
    with open(path, "rb") as f:
        return f.read(limit) if limit else f.read()


# ---------------------------------------------------------------- làm sạch PDF


class RejectPDF(Exception):
    pass


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))


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


# Phân đoạn JPEG chứa metadata: APP1 (EXIF — có thể có GPS, hãng/đời máy, thời điểm chụp;
# XMP) và APP13 (Photoshop/IPTC). KHÔNG đụng APP0 (JFIF), APP2 (ICC — màu), APP14 (Adobe — màu).
_JPEG_META = (
    (0xE1, (b"Exif\x00\x00", b"http://ns.adobe.com/xap/1.0/\x00", b"http://ns.adobe.com/xmp/extension/\x00")),
    (0xED, (b"Photoshop 3.0\x00",)),
)


def scrub_jpeg_metadata(buf: bytearray) -> int:
    """Xóa trắng (ghi 0x00) phần dữ liệu của các phân đoạn metadata trong mọi ảnh JPEG
    nằm trong buf. Giữ nguyên độ dài nên không làm lệch bảng xref của PDF; bộ giải mã
    JPEG bỏ qua phân đoạn APPn không nhận ra. Trả về số phân đoạn đã xóa."""
    count, i, n = 0, 0, len(buf)
    while True:
        i = buf.find(b"\xff\xd8\xff", i)
        if i < 0:
            return count
        j = i + 2
        while j + 4 <= n and buf[j] == 0xFF:
            marker = buf[j + 1]
            if marker == 0xFF:  # byte đệm
                j += 1
                continue
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # marker không có độ dài
                j += 2
                continue
            if marker in (0xDA, 0xD9):  # SOS: phía sau là dữ liệu ảnh nén
                break
            seglen = (buf[j + 2] << 8) | buf[j + 3]
            if seglen < 2 or j + 2 + seglen > n:
                break
            start, end = j + 4, j + 2 + seglen
            for m, ids in _JPEG_META:
                if marker == m and any(buf[start:start + len(x)] == x for x in ids):
                    buf[start:end] = bytes(end - start)
                    count += 1
            j = end
        i += 3


def _undo_filter(name, data):
    """Giải một lớp nén chung. Trả None nếu không hỗ trợ (giữ nguyên ảnh đó)."""
    if name == "/FlateDecode":
        return zlib.decompress(data)
    if name == "/ASCIIHexDecode":
        return binascii.unhexlify(re.sub(rb"\s", b"", data).rstrip(b">"))
    if name == "/ASCII85Decode":
        d = re.sub(rb"\s", b"", data)
        d = d[2:] if d.startswith(b"<~") else d
        return base64.a85decode(d[:-2] if d.endswith(b"~>") else d)
    return None


def scrub_layered_jpegs(path, td):
    """Ảnh JPEG bọc thêm lớp nén (vd. [/FlateDecode /DCTDecode], thường gặp khi chèn một PDF
    khác bằng \\includegraphics): qpdf không bóc được ở mức 'generalized'. Bóc bằng Python,
    xóa metadata, ghi lại dạng /DCTDecode thuần qua qpdf --update-from-json.
    Trả về (đường_dẫn_file_mới, số_phân_đoạn_đã_xóa)."""
    js = json.loads(run(["qpdf", "--json=2", "--json-key=qpdf", "--json-stream-data=none", path],
                        ok_codes=(0, 3)).stdout)
    upd, count = {}, 0
    for key, obj in js["qpdf"][1].items():
        st = obj.get("stream") if isinstance(obj, dict) else None
        if not st or st["dict"].get("/Subtype") != "/Image":
            continue
        filters = st["dict"].get("/Filter")
        if not (isinstance(filters, list) and len(filters) > 1 and filters[-1] == "/DCTDecode"):
            continue
        parms = st["dict"].get("/DecodeParms")
        if parms and any(x not in (None, "null") for x in (parms if isinstance(parms, list) else [parms])[:-1]):
            continue  # có tham số giải nén (predictor...) ở lớp ngoài: hiếm, bỏ qua
        m = re.match(r"^obj:(\d+) (\d+) R$", key)
        if not m:
            continue
        data = run(["qpdf", f"--show-object={m.group(1)},{m.group(2)}", "--raw-stream-data", path]).stdout
        try:
            for f in filters[:-1]:
                data = _undo_filter(f, data)
                if data is None:
                    break
        except (zlib.error, binascii.Error, ValueError):
            data = None
        if data is None:
            log.warning("bỏ qua ảnh %s: không bóc được lớp nén %s", key, filters)
            continue
        buf = bytearray(data)
        n = scrub_jpeg_metadata(buf)
        if not n:
            continue
        d = {k: v for k, v in st["dict"].items() if k not in ("/Length", "/DecodeParms")}
        d["/Filter"] = "/DCTDecode"
        upd[key] = {"stream": {"dict": d, "data": base64.b64encode(bytes(buf)).decode()}}
        count += n
    if not upd:
        return path, 0
    uj, out = os.path.join(td, "upd.json"), os.path.join(td, "layered.pdf")
    with open(uj, "w") as f:
        json.dump({"qpdf": [{"jsonversion": 2}, upd]}, f)
    run(["qpdf", path, f"--update-from-json={uj}", out], ok_codes=(0, 3))
    return out, count


def sanitize_pdf(raw: bytes):
    """Trả về (pdf_sạch, title, text, số_phân_đoạn_metadata_ảnh_đã_xóa)."""
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
        js = run(["qpdf", "--json=2", "--json-key=qpdf", "--json-stream-data=none", src], ok_codes=(0, 3))
        found = set()
        _walk_names(json.loads(js.stdout), found)
        if found:
            raise RejectPDF("PDF chứa thành phần bị chặn: " + ", ".join(sorted(found)))
        # Giải nén các lớp nén chung (Flate, ASCII85...) để lộ dữ liệu JPEG gốc, rồi xóa EXIF/XMP/IPTC
        # trong ảnh. pdfTeX chép nguyên file JPEG vào PDF, kể cả GPS của ảnh chụp bằng điện thoại.
        qdf = os.path.join(td, "qdf.pdf")
        run(["qpdf", "--qdf", "--object-streams=disable", "--decode-level=generalized",
             "--compress-streams=n", src, qdf], ok_codes=(0, 3))
        buf = bytearray(read_bytes(qdf))
        scrubbed = scrub_jpeg_metadata(buf)
        if scrubbed:
            with open(qdf, "wb") as f:
                f.write(buf)
        del buf
        qdf, n2 = scrub_layered_jpegs(qdf, td)
        scrubbed += n2
        # Lấy title trước khi xóa metadata
        info = run(["pdfinfo", qdf]).stdout.decode(errors="replace")
        m = re.search(r"^Title:\s*(.+)$", info, re.M)
        title = m.group(1).strip()[:200] if m else ""
        # Xóa metadata của PDF (Info + XMP), chỉ đặt lại Title.
        # exiftool sửa PDF theo kiểu incremental update (bản cũ vẫn khôi phục được),
        # nên BẮT BUỘC qpdf ghi lại file ngay sau đó để loại phần cũ.
        stripped = os.path.join(td, "stripped.pdf")
        cmd = ["exiftool", "-q", "-q", "-all="] + ([f"-Title={title}"] if title else []) + ["-o", stripped, qdf]
        run(cmd)
        out = os.path.join(td, "out.pdf")
        run(["qpdf", "--linearize", "--object-streams=generate", "--compress-streams=y", stripped, out],
            ok_codes=(0, 3))
        run(["qpdf", "--check", out], ok_codes=(0, 3))
        # Văn bản thuần: thứ tự đọc mặc định (tốt cho bài 2 cột), đánh dấu trang
        run(["pdftotext", "-enc", "UTF-8", out, os.path.join(td, "out.txt")])
        text = read_bytes(os.path.join(td, "out.txt")).decode("utf-8", errors="replace")
        pages = text.split("\f")
        if pages and not pages[-1].strip():
            pages = pages[:-1]
        text = "".join(f"\n\n===== Trang {i} =====\n\n{p.strip()}" for i, p in enumerate(pages, 1)).strip()
        if not title:
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not ln.startswith("=====")), "")
            title = first[:120]
        return read_bytes(out), title, text, scrubbed


# ---------------------------------------------------------------- sinh trang tĩnh

ROBOTS_TRAINING = ["GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Google-Extended",
                   "Applebot-Extended", "meta-externalagent", "Bytespider", "cohere-training-data-crawler"]

# Trang xem: không có JavaScript. CSP do Nginx VM 1007 gắn (default-src 'none', không script-src).
VIEW_TMPL = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="{robots}">
<title>{title}</title>
<link rel="stylesheet" href="/read.css">
<link rel="alternate" type="application/pdf" href="/{slug}.pdf" title="PDF">
<link rel="alternate" type="text/plain" href="/{slug}.txt" title="Văn bản thuần">{tex_link}
</head><body>
<header><div class="t"><h1>{title}</h1><span class="u">Cập nhật {updated}</span></div>
<nav><a href="/{slug}.pdf" download="{slug}.pdf">Tải PDF</a><a href="/{slug}.txt">Văn bản (.txt)</a>{tex_nav}</nav></header>
<p class="m">Điện thoại không hiện PDF? Bấm <a href="/{slug}.pdf">mở PDF</a> hoặc Tải PDF.</p>
<main><iframe src="/{slug}.pdf?v={v}" title="{title}"></iframe></main>
<details><summary>Nội dung dạng văn bản</summary><pre>{text}</pre></details>
</body></html>
"""


def fmt_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M %d/%m/%Y") if ts else "—"


def www_path(name):
    return os.path.join(WWW_DIR, name)


# PDF của tài liệu được phép index nằm trong thư mục con này; Nginx tĩnh tìm ở thư mục
# thường trước (gắn X-Robots-Tag: noindex), không thấy mới tìm ở đây (không gắn header).
INDEXED_DIR = os.path.join(WWW_DIR, "indexed")


def indexed_path(name):
    return os.path.join(INDEXED_DIR, name)


def unrender(slug):
    with _render_lock:
        for path in [www_path(f"{slug}.{e}") for e in ("html", "pdf", "txt", "tex")] + [indexed_path(f"{slug}.pdf")]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def render(pid):
    """Ghi bộ file tĩnh của một tài liệu vào WWW_DIR. PDF/TXT/TEX ghi trước, HTML ghi sau cùng
    để trang không bao giờ trỏ tới file chưa có."""
    with _render_lock:
        p = get_pub(pid=pid)
        if not p or not p["published"] or not p["pdf_sha"]:
            return
        slug, d = p["slug"], files_dir(pid)
        try:
            pdf = read_bytes(os.path.join(d, "doc.pdf"))
            txt = read_bytes(os.path.join(d, "doc.txt"))
        except FileNotFoundError:
            log.warning("render %s: thiếu file gốc, bỏ qua", pid)
            return
        # PDF ghi vào đúng một chỗ theo lựa chọn index, rồi xóa bản ở chỗ còn lại
        keep, drop = ((indexed_path(f"{slug}.pdf"), www_path(f"{slug}.pdf")) if p["indexable"]
                      else (www_path(f"{slug}.pdf"), indexed_path(f"{slug}.pdf")))
        atomic_write(keep, pdf)
        try:
            os.unlink(drop)
        except FileNotFoundError:
            pass
        atomic_write(www_path(f"{slug}.txt"), txt)
        tex = bool(p["include_source"] and p["has_tex"] and os.path.exists(os.path.join(d, "doc.tex")))
        if tex:
            atomic_write(www_path(f"{slug}.tex"), read_bytes(os.path.join(d, "doc.tex")))
        else:
            try:
                os.unlink(www_path(f"{slug}.tex"))
            except FileNotFoundError:
                pass
        title = html.escape(p["title"] or slug)
        page = VIEW_TMPL.format(
            robots="all" if p["indexable"] else "noindex",
            title=title, slug=slug, v=p["pdf_sha"][:12],
            updated=fmt_time(p["updated_at"]),
            tex_link=f'\n<link rel="alternate" type="text/x-tex" href="/{slug}.tex" title="LaTeX">' if tex else "",
            tex_nav=f'<a href="/{slug}.tex">LaTeX (.tex)</a>' if tex else "",
            text=html.escape(txt[:MAX_TXT_IN_HTML].decode("utf-8", errors="replace")),
        )
        atomic_write(www_path(f"{slug}.html"), page.encode("utf-8"))


def reconcile():
    """Đồng bộ WWW_DIR với CSDL khi khởi động: sinh lại tài liệu đang publish, xóa file thừa."""
    with _render_lock:
        os.makedirs(WWW_DIR, exist_ok=True)
        os.makedirs(INDEXED_DIR, exist_ok=True)   # kế thừa group + setgid của thư mục cha
        lines = []
        if BLOCK_TRAINING_CRAWLERS:
            lines.append("# Không dùng nội dung để huấn luyện mô hình")
            for ua in ROBOTS_TRAINING:
                lines += [f"User-agent: {ua}", "Disallow: /", ""]
        lines += ["# Trình duyệt và công cụ AI tải link theo yêu cầu người dùng: cho phép",
                  "User-agent: *", "Allow: /", ""]
        atomic_write(www_path("robots.txt"), "\n".join(lines).encode())
        atomic_write(www_path("read.css"), read_bytes(os.path.join(STATIC_DIR, "read.css")))
        with db() as c:
            live = {r[0]: r[1] for r in c.execute(
                "SELECT slug, project_id FROM publications WHERE published=1 AND pdf_sha IS NOT NULL")}
        for name in os.listdir(WWW_DIR):
            path = www_path(name)
            if name in ("robots.txt", "read.css"):
                continue
            m = WWW_FILE_RE.match(name)
            if name.startswith(".tmp-") or not m or m.group(1) not in live:
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
                    log.info("reconcile: xóa file thừa %s", name)
        for name in os.listdir(INDEXED_DIR):
            path = indexed_path(name)
            m = WWW_FILE_RE.match(name)
            if not (m and m.group(2) == "pdf" and m.group(1) in live):
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
                    log.info("reconcile: xóa file thừa indexed/%s", name)
        for slug, pid in live.items():
            render(pid)     # render tự xóa bản PDF ở chỗ không đúng với lựa chọn index
        log.info("reconcile: %d tài liệu đang publish", len(live))


# ---------------------------------------------------------------- HTTP chung



class Base(BaseHTTPRequestHandler):
    server_version = "olpub"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60  # giây chờ tối đa cho mỗi thao tác đọc/ghi socket

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

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                  {"Cache-Control": "no-store"})

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
        if path == "/_pub/button.js":
            # Nginx VM 100 ghim mã băm SRI của file này; trả nguyên byte, không biến đổi
            return self.send(200, read_bytes(os.path.join(STATIC_DIR, "button.js")),
                             "text/javascript; charset=utf-8", {"Cache-Control": "no-cache"})
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
            with _render_lock:
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
                        if slug and slug != p["slug"]:
                            unrender(p["slug"])
                        update_pub(pid, published=1, mode=mode, refresh_requested=1, last_error=None,
                                   **({"slug": slug} if slug else {}))
                        render(pid)
                elif not p:
                    return self.send_json(404, {"error": "Project chưa từng publish"})
                elif action == "unpublish":
                    unrender(p["slug"])
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
                    render(pid)
                elif action == "set_indexable":
                    update_pub(pid, indexable=int(bool(req.get("enabled"))))
                    render(pid)
                elif action == "set_slug":
                    slug = (req.get("slug") or "").strip().lower()
                    self.check_slug(slug, pid)
                    if slug != p["slug"]:
                        unrender(p["slug"])
                        update_pub(pid, slug=slug)
                        render(pid)
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
            pdf, title, text, scrubbed = sanitize_pdf(body)
        except (RejectPDF, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            msg = str(e) if isinstance(e, RejectPDF) else f"xử lý PDF thất bại: {e.__class__.__name__}"
            # không publish bản lỗi; bản cũ (nếu có) vẫn giữ nguyên
            update_pub(pid, last_error=msg, last_error_at=time.time(), build_mtime=build_mtime,
                       refresh_requested=0)
            log.warning("reject project=%s: %s", pid, msg)
            return self.send_json(422, {"error": msg})
        sha = hashlib.sha256(pdf).hexdigest()
        d = files_dir(pid)
        with _render_lock:
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
            render(pid)
        audit("collector", "-", pid, "ingest",
              f"sha={sha[:12]} size={len(pdf)} tex={has_tex} img_meta_scrubbed={scrubbed}")
        self.send_json(200, {"ok": True, "sha": sha, "img_meta_scrubbed": scrubbed})


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
    InternalHandler.token = load_token()
    init_db()
    reconcile()
    serve(ADMIN_BIND, AdminHandler)
    serve(INTERNAL_BIND, InternalHandler)
    threading.Event().wait()


if __name__ == "__main__":
    main()
