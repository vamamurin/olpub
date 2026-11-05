#!/usr/bin/env python3
"""Kiểm thử end-to-end olpub 1.1: publisher + collector + Nginx tĩnh (cấu hình VM 1007 thật)
trên dữ liệu Overleaf giả lập.

Chạy: python3 tests/e2e_test.py
Cần: nginx, qpdf, exiftool, poppler-utils; Python: reportlab, pikepdf, Pillow.
PYBIN=/usr/bin/python3.11 để chạy publisher/collector bằng Python của Debian 12."""
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zlib
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYBIN = os.environ.get("PYBIN", sys.executable)
PID = "65a1b2c3d4e5f60718293a4b"
PID_BAD = "65a1b2c3d4e5f60718293a4c"
PID_LINK = "65a1b2c3d4e5f60718293a4d"
PID_IMG = "65a1b2c3d4e5f60718293a4e"
UID = "aaaaaaaaaaaaaaaaaaaaaaaa"
UID2 = "bbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN = "t" * 48
PUB, INT, ADM = "http://127.0.0.1:18080", "http://127.0.0.1:18081", "http://127.0.0.1:18082"
ADMIN_H = {"X-Pub-User": "vamamurin", "X-Pub-Device": "CN=vamamurin"}
POST_H = dict(ADMIN_H, **{"Content-Type": "application/json", "X-OLPub": "1",
                          "Origin": "https://overleaf.tapama.cloud"})
fails = []
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def post(pid, body):
    return http("POST", f"{ADM}/_pub/api/project/{pid}", json.dumps(body).encode(), POST_H)


def status(pid):
    return json.loads(http("GET", f"{ADM}/_pub/api/project/{pid}", headers=ADMIN_H)[2])


def make_pdf(path, title, pages, author="Nguyen Van A", image=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor(author)
    c.setSubject("secret-subject")
    c.setCreator("pdfTeX /home/vamamurin/secret/path")
    for page in pages:
        y = 800
        for ln in page:
            c.drawString(72, y, ln)
            y -= 18
        if image:
            c.drawImage(image, 72, 300, width=200, height=150)
        c.showPage()
    c.save()


def make_exif_jpeg(path):
    from PIL import Image
    img = Image.new("RGB", (200, 150), (220, 20, 20))
    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneMaker"
    exif[0x0110] = "SecretModel-X"
    exif[0x8825] = {1: "N", 2: (10.0, 45.0, 30.0), 3: "E", 4: (106.0, 40.0, 15.0)}
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description>SecretXMPCreator</rdf:Description></rdf:RDF></x:xmpmeta>'
    img.save(path, "JPEG", exif=exif.tobytes(), xmp=xmp, quality=90)


def make_raw_jpeg_pdf(path, jpeg):
    """Nhúng JPEG NGUYÊN BẢN như pdfTeX: một ảnh /DCTDecode, một ảnh [/FlateDecode /DCTDecode]."""
    import zlib
    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    page = pdf.pages[0]
    im1 = pikepdf.Stream(pdf, jpeg)
    im1.update({"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Image, "/Width": 200, "/Height": 150,
                "/ColorSpace": pikepdf.Name.DeviceRGB, "/BitsPerComponent": 8, "/Filter": pikepdf.Name.DCTDecode})
    im2 = pikepdf.Stream(pdf, zlib.compress(jpeg))
    im2.update({"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Image, "/Width": 200, "/Height": 150,
                "/ColorSpace": pikepdf.Name.DeviceRGB, "/BitsPerComponent": 8,
                "/Filter": pikepdf.Array([pikepdf.Name.FlateDecode, pikepdf.Name.DCTDecode])})
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im1=im1, Im2=im2))
    page.Contents = pikepdf.Stream(pdf, b"q 200 0 0 150 72 300 cm /Im1 Do Q q 200 0 0 150 320 300 cm /Im2 Do Q")
    pdf.docinfo["/Title"] = "Co anh"
    pdf.save(path)


def make_js_pdf(path):
    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    pdf.save(path)


def build(data, pid, uid, build_id, pdf_src, files=None, age=10):
    out = os.path.join(data, "output", f"{pid}-{uid}", "generated-files", build_id)
    os.makedirs(out, exist_ok=True)
    shutil.copy(pdf_src, os.path.join(out, "output.pdf"))
    t = time.time() - age
    os.utime(os.path.join(out, "output.pdf"), (t, t))
    cdir = os.path.join(data, "compiles", f"{pid}-{uid}")
    os.makedirs(cdir, exist_ok=True)
    for name, content in (files or {}).items():
        p = os.path.join(cdir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return out


def collector_once(env):
    r = subprocess.run([PYBIN, os.path.join(ROOT, "collector", "olpub_collector.py")],
                       env=dict(env, OLPUB_ONCE="1"), capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def nginx_conf(tmp, www):
    s = open(os.path.join(ROOT, "nginx", "vm1007-olpub-static.conf")).read()
    s = s.replace("listen 8080 default_server;", "listen 127.0.0.1:18080 default_server;")
    s = s.replace("root /srv/olpub/www;", f"root {www};")
    s = s.replace("/var/log/nginx/", f"{tmp}/ng-")
    conf = f"""user nobody nogroup;
worker_processes 1;
pid {tmp}/nginx.pid;
error_log {tmp}/ng-error.log;
events {{ worker_connections 64; }}
http {{
  client_body_temp_path {tmp}/ngtmp; proxy_temp_path {tmp}/ngtmp; fastcgi_temp_path {tmp}/ngtmp;
  uwsgi_temp_path {tmp}/ngtmp; scgi_temp_path {tmp}/ngtmp;
{s}
}}
"""
    path = os.path.join(tmp, "nginx.conf")
    os.makedirs(os.path.join(tmp, "ngtmp"), exist_ok=True)
    with open(path, "w") as f:
        f.write(conf)
    return path


def start_publisher(env, tmp):
    p = subprocess.Popen([PYBIN, os.path.join(ROOT, "publisher", "olpub_publisher.py")], env=env,
                         stdout=open(os.path.join(tmp, "publisher.log"), "a"), stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            http("GET", f"{ADM}/_pub/api/list")
            return p
        except Exception:
            time.sleep(0.1)
    raise SystemExit("publisher không khởi động: " + open(os.path.join(tmp, "publisher.log")).read()[-2000:])


def main():
    tmp = tempfile.mkdtemp(prefix="olpub-e2e-")
    data = os.path.join(tmp, "ol", "data")
    www = os.path.join(tmp, "www")
    os.makedirs(os.path.join(data, "output"))
    os.makedirs(os.path.join(data, "compiles"))
    # Giống production: thư mục web thuộc group của Nginx, setgid, 2750; file 640
    os.chmod(tmp, 0o711)
    os.makedirs(www)
    shutil.chown(www, group="nogroup")
    os.chmod(www, 0o2750)
    tokf = os.path.join(tmp, "token")
    with open(tokf, "w") as f:
        f.write(TOKEN)
    secret = os.path.join(tmp, "host-secret.txt")
    with open(secret, "w") as f:
        f.write("HOST-SECRET-DO-NOT-LEAK\n")

    env = dict(os.environ, OLPUB_DATA_DIR=os.path.join(tmp, "pubdata"), OLPUB_WWW_DIR=www,
               OLPUB_INGEST_TOKEN_FILE=tokf, OLPUB_INTERNAL_BIND="127.0.0.1:18081",
               OLPUB_ADMIN_BIND="127.0.0.1:18082", OLPUB_OL_DATA=data, OLPUB_PUBLISHER_URL=INT,
               OLPUB_LOG_LEVEL="INFO")
    srv = start_publisher(env, tmp)
    nconf = nginx_conf(tmp, www)
    r = subprocess.run(["nginx", "-t", "-c", nconf], capture_output=True, text=True)
    check(r.returncode == 0, "cấu hình Nginx VM 1007 hợp lệ (nginx -t)" + ("" if r.returncode == 0 else r.stderr))
    subprocess.run(["nginx", "-c", nconf], check=True)
    time.sleep(0.3)
    try:
        print("[0] Không còn Python ở cổng public")
        out = subprocess.run(["ss", "-lntp"], capture_output=True, text=True).stdout
        line = next((ln for ln in out.splitlines() if ":18080 " in ln), "")
        check("nginx" in line and "python" not in line, "cổng 18080 do nginx nghe, không phải python")

        print("[1] Danh tính & CSRF ở cổng admin")
        check(http("GET", f"{ADM}/_pub/api/project/{PID}")[0] == 401, "không có X-Pub-User/X-Pub-Device -> 401")
        check(http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                   dict(ADMIN_H, **{"Content-Type": "application/json"}))[0] == 403, "thiếu X-OLPub -> 403 (CSRF)")
        check(http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                   dict(POST_H, Origin="https://evil.example"))[0] == 403, "Origin lạ -> 403")
        check(http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                   dict(POST_H, **{"Sec-Fetch-Site": "cross-site"}))[0] == 403, "Sec-Fetch-Site: cross-site -> 403")
        check(http("GET", f"{INT}/internal/targets", headers={"Authorization": "Bearer wrong"})[0] == 401,
              "internal sai token -> 401")

        print("[2] Trang công khai khi chưa có gì")
        st, h, b = http("GET", f"{PUB}/")
        check(st == 404 and b"read" not in b.lower() or st == 404, "trang gốc / -> 404, không liệt kê")
        check(http("POST", f"{PUB}/abcd", b"x")[0] == 405, "public chỉ GET/HEAD (POST -> 405)")
        check(http("GET", f"{PUB}/_pub/api/list", headers=ADMIN_H)[0] == 404, "public không có API admin")
        check(http("GET", f"{PUB}/olpub.sqlite3")[0] == 404 and http("GET", f"{PUB}/../pubdata/olpub.sqlite3")[0] in (400, 404),
              "không lộ CSDL")
        st, h, b = http("GET", f"{PUB}/robots.txt")
        check(st == 200 and b"User-agent: GPTBot\nDisallow: /" in b and b"User-agent: *\nAllow: /" in b,
              "robots.txt: chặn crawler huấn luyện, cho phép phần còn lại")
        check(http("GET", f"{PUB}/read.css")[0] == 200, "read.css có")

        print("[3] Publish, chưa có bản build")
        st, _, b = post(PID, {"action": "publish", "slug": "bai-bao-thu", "mode": "live"})
        s = json.loads(b)
        check(st == 200 and s["published"] and s["slug"] == "bai-bao-thu", "publish với tên link tùy chọn")
        check(s["waiting"] and not s["has_pdf"], "trạng thái chờ bản biên dịch")
        collector_once(env)
        check("Recompile" in (status(PID)["last_error"] or ""), "collector báo 'chưa thấy bản biên dịch'")
        check(http("GET", f"{PUB}/bai-bao-thu")[0] == 404, "chưa có PDF -> 404")

        print("[4] Bản build đầu tiên + mã nguồn nhiều file")
        pdf1 = os.path.join(tmp, "v1.pdf")
        make_pdf(pdf1, '<script>alert(1)</script> Bài & "thử"', [["Mo dau: phien ban MOT", "Dong thu hai"], ["Trang hai"]])
        main_tex = r"""\documentclass{article}
% TODO: bo doan nay truoc khi nop -- ghi chu noi bo
\begin{document}
Gia tri 50\% la dung. % comment cuoi dong: BI MAT 1
Xem \url{https://example.com/a%20b} va \verb|50%|.
\input{chapters/intro}
\iffalse
Doan van cu da bo: BI MAT 2
\ifx\a\b x \fi
\fi
\begin{comment}
BI MAT 3 trong moi truong comment
\end{comment}
\begin{verbatim}
giu nguyen % ca dau phan tram nay
\end{verbatim}
\ifdef{\foo}{a}{b} sau ifdef van con
\bibliography{refs}
\end{document}
"""
        files = {"main.tex": main_tex,
                 "chapters/intro.tex": "Phan gioi thieu. % BI MAT 4\n\\input{chapters/deep}\n",
                 "chapters/deep.tex": "Noi dung long sau.\n",
                 "output.bbl": "\\begin{thebibliography}{1}\\bibitem{a} Tac gia A.\\end{thebibliography}\n",
                 "output.fls": f"PWD /var/lib/overleaf/data/compiles/{PID}-{UID}\n"
                               "INPUT /usr/local/texlive/2025/texmf-dist/tex/latex/base/article.cls\n"
                               f"INPUT /var/lib/overleaf/data/compiles/{PID}-{UID}/main.tex\n"}
        build(data, PID, UID, "18f00000000-aaaaaaaaaaaaaaaa", pdf1, files)
        post(PID, {"action": "set_source", "enabled": True})
        collector_once(env)
        st, h, body = http("GET", f"{PUB}/bai-bao-thu.pdf")
        check(st == 200 and body.startswith(b"%PDF-"), ".pdf trả về PDF")
        check(h.get("content-type") == "application/pdf" and h.get("x-content-type-options") == "nosniff",
              "Content-Type application/pdf + nosniff")
        check(h.get("x-robots-tag") == "noindex", ".pdf luôn noindex")
        check(h.get("content-security-policy") == "frame-ancestors 'self'", "CSP của .pdf: chỉ cho nhúng cùng origin")
        check(h.get("content-disposition") == 'inline; filename="bai-bao-thu.pdf"', "Content-Disposition inline + tên file")
        meta = subprocess.run(["exiftool", "-a", "-G1", "-s", "-"], input=body, capture_output=True).stdout.decode()
        check("Nguyen Van A" not in meta and "secret" not in meta and "/home/" not in meta,
              "metadata Author/Subject/Creator đã bị xóa")
        check(b"Nguyen Van A" not in body and b"secret-subject" not in body, "không còn chuỗi metadata trong byte PDF")
        check("Bài" in meta, "Title được giữ lại")
        st, h, txt = http("GET", f"{PUB}/bai-bao-thu.txt")
        check(st == 200 and "phien ban MOT" in txt.decode() and "===== Trang 2 =====" in txt.decode(),
              ".txt có nội dung và đánh dấu trang")
        check(h.get("content-type") == "text/plain; charset=utf-8", ".txt là text/plain; charset=utf-8")
        check("sandbox" in h.get("content-security-policy", ""), ".txt có CSP sandbox")
        st, h, tex = http("GET", f"{PUB}/bai-bao-thu.tex")
        tex = tex.decode()
        check(st == 200 and h.get("content-type") == "text/plain; charset=utf-8", ".tex có, là text/plain")
        check(all(f"BI MAT {i}" not in tex for i in (1, 2, 3, 4)) and "TODO" not in tex,
              "đã xóa comment, \\iffalse, môi trường comment, comment trong file con")
        check("50\\%" in tex and "a%20b" in tex and "\\verb|50%|" in tex, "giữ \\%, % trong \\url và \\verb")
        check("giu nguyen % ca dau phan tram nay" in tex, "giữ nguyên verbatim")
        check("Phan gioi thieu." in tex and "Noi dung long sau." in tex, "đã gộp \\input lồng nhau")
        check("\\bibitem{a}" in tex and "\\bibliography{refs}" not in tex, "\\bibliography -> output.bbl")
        check("sau ifdef van con" in tex, "\\ifdef{...} không bị coi là \\if")
        st, h, page = http("GET", f"{PUB}/bai-bao-thu")
        page = page.decode()
        check(st == 200 and h.get("content-type") == "text/html; charset=utf-8", "trang xem là HTML utf-8")
        check("&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script" not in page,
              "tiêu đề chứa <script> bị escape; trang không có thẻ <script> nào")
        check("phien ban MOT" in page, "trang xem chứa văn bản (AI đọc được từ link chính)")
        check('<iframe src="/bai-bao-thu.pdf?v=' in page and "pdfjs" not in page, "nhúng PDF bằng trình xem của trình duyệt, không PDF.js")
        check('<meta name="robots" content="noindex">' in page and "x-robots-tag" not in h, "HTML: noindex qua thẻ meta")
        csp = h.get("content-security-policy", "")
        check("default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp,
              "CSP trang xem: default-src 'none', không script-src, không cho nhúng")
        etag = http("GET", f"{PUB}/bai-bao-thu.pdf")[1].get("etag")
        check(http("GET", f"{PUB}/bai-bao-thu.pdf", headers={"If-None-Match": etag})[0] == 304, "ETag -> 304")
        check(http("HEAD", f"{PUB}/bai-bao-thu.pdf")[0] == 200, "HEAD được phép")

        print("[5] Tự cập nhật khi Recompile (build mới của người dùng khác)")
        pdf2 = os.path.join(tmp, "v2.pdf")
        make_pdf(pdf2, "Bai thu", [["Mo dau: phien ban HAI"]])
        build(data, PID, UID2, "18f00000001-bbbbbbbbbbbbbbbb", pdf2, age=5)
        collector_once(env)
        check("phien ban HAI" in http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode(), "bản build mới nhất được publish")
        check(http("GET", f"{PUB}/bai-bao-thu.pdf")[1].get("etag") != etag, "ETag đổi theo nội dung")

        print("[6] Chốt phiên bản")
        post(PID, {"action": "set_mode", "mode": "pinned"})
        pdf3 = os.path.join(tmp, "v3.pdf")
        make_pdf(pdf3, "Bai thu", [["Mo dau: phien ban BA"]])
        build(data, PID, UID, "18f00000002-cccccccccccccccc", pdf3, age=3)
        collector_once(env)
        check("phien ban HAI" in http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode(), "đã chốt: không tự cập nhật")
        post(PID, {"action": "refresh"})
        collector_once(env)
        check("phien ban BA" in http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode(), "'Cập nhật bản chốt' lấy bản mới")

        print("[7] Đổi tên link, index, tắt mã nguồn")
        post(PID, {"action": "set_slug", "slug": "ten-moi"})
        check(http("GET", f"{PUB}/bai-bao-thu")[0] == 404 and http("GET", f"{PUB}/bai-bao-thu.pdf")[0] == 404,
              "tên cũ chết ngay")
        check(http("GET", f"{PUB}/ten-moi")[0] == 200 and http("GET", f"{PUB}/ten-moi.pdf")[0] == 200, "tên mới chạy")
        post(PID, {"action": "set_indexable", "enabled": True})
        check('<meta name="robots" content="all">' in http("GET", f"{PUB}/ten-moi")[2].decode(), "bật index -> meta robots all")
        post(PID, {"action": "set_source", "enabled": False})
        check(http("GET", f"{PUB}/ten-moi.tex")[0] == 404 and "ten-moi.tex" not in http("GET", f"{PUB}/ten-moi")[2].decode(),
              "tắt mã nguồn -> .tex biến mất khỏi site và trang xem")

        print("[8] PDF chứa JavaScript bị từ chối, bản cũ giữ nguyên")
        post(PID_BAD, {"action": "publish"})
        good = os.path.join(tmp, "good.pdf")
        make_pdf(good, "Ban tot", [["noi dung tot"]])
        build(data, PID_BAD, UID, "18f00000003-dddddddddddddddd", good)
        collector_once(env)
        slug_bad = status(PID_BAD)["slug"]
        jsf = os.path.join(tmp, "js.pdf")
        make_js_pdf(jsf)
        build(data, PID_BAD, UID, "18f00000004-eeeeeeeeeeeeeeee", jsf, age=5)
        collector_once(env)
        err = status(PID_BAD)["last_error"] or ""
        check("/JavaScript" in err or "/JS" in err, "lỗi ghi rõ thành phần bị chặn: " + err)
        check("noi dung tot" in http("GET", f"{PUB}/{slug_bad}.txt")[2].decode(), "bản tốt trước đó vẫn được phục vụ")

        print("[9] Ảnh JPEG có EXIF/GPS/XMP trong PDF")
        jpg = os.path.join(tmp, "photo.jpg")
        make_exif_jpeg(jpg)
        raw_jpg = open(jpg, "rb").read()
        check(b"SecretPhoneMaker" in raw_jpg and b"SecretXMPCreator" in raw_jpg, "(chuẩn bị) ảnh thử có EXIF + XMP")
        imgpdf = os.path.join(tmp, "img.pdf")
        make_raw_jpeg_pdf(imgpdf, raw_jpg)
        dec = subprocess.run(["qpdf", "--qdf", "--decode-level=generalized", "--object-streams=disable", imgpdf, "-"],
                             capture_output=True).stdout
        check(b"SecretPhoneMaker" in dec, "(chuẩn bị) PDF gốc mang EXIF của ảnh")
        post(PID_IMG, {"action": "publish", "slug": "co-anh"})
        build(data, PID_IMG, UID, "18f00000008-1111111111111111", imgpdf)
        collector_once(env)
        st, _, served = http("GET", f"{PUB}/co-anh.pdf")
        import pikepdf
        imgs = []
        with pikepdf.open(io.BytesIO(served)) as sp:
            for _, xo in sp.pages[0].Resources.XObject.items():
                raw, f = xo.read_raw_bytes(), xo.Filter
                if isinstance(f, pikepdf.Array) and str(f[0]) == "/FlateDecode":
                    raw = zlib.decompress(raw)  # bóc lớp Flate để đọc tận dữ liệu JPEG
                imgs.append((str(f), raw))
        leaked = [f for f, d in imgs if any(x in d for x in (b"SecretPhoneMaker", b"SecretModel", b"SecretXMPCreator", b"Exif\x00\x00"))]
        check(st == 200 and len(imgs) == 2 and not leaked,
              f"EXIF/GPS/XMP bị xóa trong CẢ ảnh DCT thuần và ảnh [Flate, DCT] (đọc tận dữ liệu ảnh; lọt: {leaked})")
        ppm = subprocess.run(["pdftoppm", "-r", "36", "-f", "1", "-l", "1", "-png", "-", os.path.join(tmp, "render")],
                             input=served, capture_output=True)
        from PIL import Image
        im = Image.open(os.path.join(tmp, "render-1.png")).convert("RGB")
        w, hh = im.size
        px = [im.getpixel((int(w * x), int(hh * 0.55))) for x in (0.2, 0.7)]
        check(ppm.returncode == 0 and all(p[0] > 180 and p[1] < 80 for p in px), f"cả hai ảnh vẫn hiển thị đúng ({px})")
        audit_log = open(os.path.join(tmp, "publisher.log")).read()
        check("img_meta_scrubbed=4" in audit_log, "audit ghi đúng số phân đoạn đã xóa (2 ảnh x EXIF+XMP = 4)")

        print("[10] Tấn công symlink từ container (collector chạy trên host)")
        post(PID_LINK, {"action": "publish"})
        post(PID_LINK, {"action": "set_source", "enabled": True})
        bdir = os.path.join(data, "output", f"{PID_LINK}-{UID}", "generated-files", "18f00000005-ffffffffffffffff")
        os.makedirs(bdir)
        os.symlink(secret, os.path.join(bdir, "output.pdf"))
        other = os.path.join(tmp, "elsewhere", "18f00000006-ffffffffffffffff")
        os.makedirs(other)
        shutil.copy(good, os.path.join(other, "output.pdf"))
        os.makedirs(os.path.join(data, "output", f"{PID_LINK}-{UID2}"))
        os.symlink(os.path.dirname(other), os.path.join(data, "output", f"{PID_LINK}-{UID2}", "generated-files"))
        collector_once(env)
        check(not status(PID_LINK)["has_pdf"], "không đi theo symlink output.pdf / generated-files")
        build(data, PID_LINK, UID, "18f00000007-ffffffffffffffff", good, {
            "main.tex": "\\documentclass{article}\\begin{document}\n\\input{leak}\n\\input{../../../../host-secret.txt}\n"
                        "\\input{sub/x}\n\\end{document}\n"})
        cdir = os.path.join(data, "compiles", f"{PID_LINK}-{UID}")
        os.symlink(secret, os.path.join(cdir, "leak.tex"))
        os.symlink(tmp, os.path.join(cdir, "sub"))
        with open(os.path.join(tmp, "x.tex"), "w") as f:
            f.write("HOST-SECRET-VIA-DIR\n")
        collector_once(env)
        tex = http("GET", f"{PUB}/{status(PID_LINK)['slug']}.tex")[2].decode()
        check("HOST-SECRET" not in tex and "bỏ qua" in tex, "\\input qua symlink và ../ không đọc được file host")

        print("[11] Nginx tĩnh: symlink và file lạ trong thư mục web")
        os.symlink(secret, os.path.join(www, "evil.txt"))
        check(http("GET", f"{PUB}/evil.txt")[0] in (403, 404), "symlink trong www không được phục vụ (disable_symlinks)")
        with open(os.path.join(www, ".tmp-abc"), "w") as f:
            f.write("x")
        check(http("GET", f"{PUB}/.tmp-abc")[0] == 404, "file ẩn/tạm không phục vụ")
        with open(os.path.join(www, "zzzz.html"), "w") as f:
            f.write("stray")
        srv.send_signal(signal.SIGTERM)
        srv.wait()
        srv = start_publisher(env, tmp)
        check(not os.path.exists(os.path.join(www, "zzzz.html")) and not os.path.lexists(os.path.join(www, "evil.txt"))
              and not os.path.exists(os.path.join(www, ".tmp-abc")), "khởi động lại: reconcile xóa file thừa/symlink")
        check(http("GET", f"{PUB}/ten-moi")[0] == 200 and http("GET", f"{PUB}/co-anh.pdf")[0] == 200,
              "khởi động lại: tài liệu đang publish vẫn còn")
        modes = {oct(os.stat(os.path.join(www, n)).st_mode & 0o777) for n in os.listdir(www)}
        check(modes == {"0o640"}, f"file trong www đều chmod 640 ({modes})")

        print("[12] Gỡ publish")
        post(PID, {"action": "unpublish"})
        check(all(http("GET", f"{PUB}/ten-moi{e}")[0] == 404 for e in ("", ".pdf", ".txt", ".tex")), "mọi dạng link chết ngay")
        check(not any(n.startswith("ten-moi.") for n in os.listdir(www)), "file đã bị xóa khỏi thư mục web")
        check(not os.path.exists(os.path.join(tmp, "pubdata", "files", PID, "doc.pdf")), "bản gốc đã bị xóa")
        check(json.loads(post(PID, {"action": "publish"})[2])["slug"] == "ten-moi", "publish lại giữ link cũ")
        print("\nlog publisher:", os.path.join(tmp, "publisher.log"))
    finally:
        srv.terminate()
        srv.wait()
        subprocess.run(["nginx", "-c", nconf, "-s", "stop"], capture_output=True)
    print(f"\n{'TẤT CẢ ĐẠT' if not fails else f'{len(fails)} LỖI'}")
    print("TMP", tmp)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
