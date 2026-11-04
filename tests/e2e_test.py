#!/usr/bin/env python3
"""Kiểm thử end-to-end olpub: publisher + collector trên dữ liệu Overleaf giả lập.
Chạy: python3 tests/e2e_test.py  (cần qpdf, exiftool, poppler-utils, reportlab, pikepdf)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PID = "65a1b2c3d4e5f60718293a4b"
PID_BAD = "65a1b2c3d4e5f60718293a4c"
PID_LINK = "65a1b2c3d4e5f60718293a4d"
UID = "aaaaaaaaaaaaaaaaaaaaaaaa"
UID2 = "bbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN = "t" * 48
PUB, INT, ADM = "http://127.0.0.1:18080", "http://127.0.0.1:18081", "http://127.0.0.1:18082"
ADMIN_H = {"X-Pub-User": "vamamurin", "X-Pub-Device": "CN=vamamurin"}
POST_H = dict(ADMIN_H, **{"Content-Type": "application/json", "X-OLPub": "1",
                          "Origin": "https://overleaf.tapama.cloud"})
fails = []


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
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def make_pdf(path, title, body_lines, author="Nguyen Van A"):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor(author)
    c.setSubject("secret-subject")
    c.setCreator("pdfTeX /home/vamamurin/secret/path")
    for page in body_lines:
        y = 800
        for ln in page:
            c.drawString(72, y, ln)
            y -= 18
        c.showPage()
    c.save()


def make_js_pdf(path):
    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    pdf.save(path)


def build(data, pid, uid, build_id, pdf_src, files=None):
    out = os.path.join(data, "output", f"{pid}-{uid}", "generated-files", build_id)
    os.makedirs(out, exist_ok=True)
    shutil.copy(pdf_src, os.path.join(out, "output.pdf"))
    t = time.time() - 10
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
    r = subprocess.run([os.environ.get("PYBIN", sys.executable), os.path.join(ROOT, "collector", "olpub_collector.py")],
                       env=dict(env, OLPUB_ONCE="1"), capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def main():
    tmp = tempfile.mkdtemp(prefix="olpub-e2e-")
    data = os.path.join(tmp, "ol", "data")
    os.makedirs(os.path.join(data, "output"))
    os.makedirs(os.path.join(data, "compiles"))
    tokf = os.path.join(tmp, "token")
    with open(tokf, "w") as f:
        f.write(TOKEN)
    secret = os.path.join(tmp, "host-secret.txt")
    with open(secret, "w") as f:
        f.write("HOST-SECRET-DO-NOT-LEAK\n")

    env = dict(os.environ, OLPUB_DATA_DIR=os.path.join(tmp, "pubdata"), OLPUB_INGEST_TOKEN_FILE=tokf,
               OLPUB_PUBLIC_BIND="127.0.0.1:18080", OLPUB_INTERNAL_BIND="127.0.0.1:18081",
               OLPUB_ADMIN_BIND="127.0.0.1:18082", OLPUB_PDFJS_DIR=os.environ.get("PDFJS_DIR", "/tmp/pdfjs/dist"),
               OLPUB_OL_DATA=data, OLPUB_PUBLISHER_URL=INT, OLPUB_LOG_LEVEL="INFO")
    srv = subprocess.Popen([os.environ.get("PYBIN", sys.executable), os.path.join(ROOT, "publisher", "olpub_publisher.py")], env=env,
                           stdout=open(os.path.join(tmp, "publisher.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                http("GET", PUB + "/robots.txt")
                break
            except Exception:
                time.sleep(0.1)

        print("[1] Danh tính & CSRF ở cổng admin")
        st, _, _ = http("GET", f"{ADM}/_pub/api/project/{PID}")
        check(st == 401, "không có X-Pub-User/X-Pub-Device -> 401")
        st, _, _ = http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                        dict(ADMIN_H, **{"Content-Type": "application/json"}))
        check(st == 403, "thiếu header X-OLPub -> 403 (CSRF)")
        st, _, _ = http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                        dict(POST_H, Origin="https://evil.example"))
        check(st == 403, "Origin lạ -> 403")
        st, _, _ = http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}',
                        dict(POST_H, **{"Sec-Fetch-Site": "cross-site"}))
        check(st == 403, "Sec-Fetch-Site: cross-site -> 403")
        st, _, _ = http("GET", f"{INT}/internal/targets", headers={"Authorization": "Bearer wrong"})
        check(st == 401, "internal sai token -> 401")
        st, _, _ = http("POST", f"{PUB}/x", b"")
        check(st == 405, "cổng public chỉ GET")
        st, _, _ = http("GET", f"{PUB}/_pub/api/list", headers=ADMIN_H)
        check(st == 404, "cổng public không phục vụ API admin")

        print("[2] Publish project, chưa có bản build")
        st, _, b = http("POST", f"{ADM}/_pub/api/project/{PID}", json.dumps(
            {"action": "publish", "slug": "bai-bao-thu", "mode": "live"}).encode(), POST_H)
        s = json.loads(b)
        check(st == 200 and s["published"] and s["slug"] == "bai-bao-thu", "publish với tên link tùy chọn")
        check(s["waiting"] and not s["has_pdf"], "trạng thái chờ bản biên dịch")
        out = collector_once(env)
        s = json.loads(http("GET", f"{ADM}/_pub/api/project/{PID}", headers=ADMIN_H)[2])
        check("Recompile" in (s["last_error"] or ""), "collector báo 'chưa thấy bản biên dịch'")
        st, _, _ = http("GET", f"{PUB}/bai-bao-thu")
        check(st == 404, "chưa có PDF -> trang public 404")

        print("[3] Bản build đầu tiên + mã nguồn nhiều file")
        pdf1 = os.path.join(tmp, "v1.pdf")
        make_pdf(pdf1, "Bài báo thử nghiệm", [["Mo dau: phien ban MOT", "Dong thu hai"], ["Trang hai"]])
        main_tex = r"""\documentclass{article}
% TODO: bo doan nay truoc khi nop -- ghi chu noi bo
\usepackage{hyperref}
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
        st, _, b = http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"set_source","enabled":true}', POST_H)
        out = collector_once(env)
        st, h, body = http("GET", f"{PUB}/bai-bao-thu.pdf")
        check(st == 200 and body.startswith(b"%PDF-"), ".pdf trả về PDF")
        check(h.get("Content-Type") == "application/pdf" and h.get("X-Content-Type-Options") == "nosniff",
              "Content-Type đúng + nosniff")
        check(h.get("X-Robots-Tag") == "noindex", "mặc định noindex")
        meta = subprocess.run(["exiftool", "-a", "-G1", "-s", "-"], input=body, capture_output=True).stdout.decode()
        check("Nguyen Van A" not in meta and "secret" not in meta and "/home/" not in meta,
              "metadata Author/Subject/Creator đã bị xóa (kể cả bản cũ trong incremental update)")
        check(b"Nguyen Van A" not in body and b"secret-subject" not in body, "không còn chuỗi metadata trong byte PDF")
        check("Bài báo thử nghiệm" in meta, "Title được giữ lại")
        st, _, txt = http("GET", f"{PUB}/bai-bao-thu.txt")
        check(st == 200 and "phien ban MOT" in txt.decode() and "===== Trang 2 =====" in txt.decode(),
              ".txt có nội dung và đánh dấu trang")
        st, _, tex = http("GET", f"{PUB}/bai-bao-thu.tex")
        tex = tex.decode()
        check(st == 200, ".tex có khi bật mã nguồn")
        check(all(f"BI MAT {i}" not in tex for i in (1, 2, 3, 4)) and "TODO" not in tex,
              "đã xóa comment, \\iffalse, môi trường comment, comment trong file con")
        check("50\\%" in tex and "a%20b" in tex and "\\verb|50%|" in tex, "giữ \\%, % trong \\url và \\verb")
        check("giu nguyen % ca dau phan tram nay" in tex, "giữ nguyên nội dung verbatim")
        check("Phan gioi thieu." in tex and "Noi dung long sau." in tex, "đã gộp \\input lồng nhau")
        check("\\bibitem{a}" in tex and "\\bibliography{refs}" not in tex, "đã thay \\bibliography bằng output.bbl")
        check("sau ifdef van con" in tex, "\\ifdef{...} của etoolbox không bị coi là \\if")
        st, h, page = http("GET", f"{PUB}/bai-bao-thu")
        page = page.decode()
        check(st == 200 and "Bài báo thử nghiệm" in page and "phien ban MOT" in page,
              "trang xem có tiêu đề + văn bản (AI đọc được ngay từ link chính)")
        check('rel="alternate" type="application/pdf"' in page, "trang có <link rel=alternate> tới PDF")
        check("default-src 'none'" in h.get("Content-Security-Policy", ""), "trang xem có CSP chặt")
        etag = http("GET", f"{PUB}/bai-bao-thu.pdf")[1].get("ETag")
        st, _, _ = http("GET", f"{PUB}/bai-bao-thu.pdf", headers={"If-None-Match": etag})
        check(st == 304, "ETag -> 304 khi không đổi")

        print("[4] Tự cập nhật khi Recompile (build mới của người dùng khác)")
        pdf2 = os.path.join(tmp, "v2.pdf")
        make_pdf(pdf2, "Bài báo thử nghiệm", [["Mo dau: phien ban HAI"]])
        b2 = build(data, PID, UID2, "18f00000001-bbbbbbbbbbbbbbbb", pdf2)
        t = time.time() - 5
        os.utime(os.path.join(b2, "output.pdf"), (t, t))
        collector_once(env)
        txt = http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode()
        check("phien ban HAI" in txt, "bản build mới nhất (theo mtime) được publish")
        check(http("GET", f"{PUB}/bai-bao-thu.pdf")[1].get("ETag") != etag, "ETag đổi theo nội dung")

        print("[5] Chốt phiên bản")
        http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"set_mode","mode":"pinned"}', POST_H)
        pdf3 = os.path.join(tmp, "v3.pdf")
        make_pdf(pdf3, "Bài báo thử nghiệm", [["Mo dau: phien ban BA"]])
        b3 = build(data, PID, UID, "18f00000002-cccccccccccccccc", pdf3)
        t = time.time() - 3
        os.utime(os.path.join(b3, "output.pdf"), (t, t))
        collector_once(env)
        check("phien ban HAI" in http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode(), "đã chốt: không tự cập nhật")
        http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"refresh"}', POST_H)
        collector_once(env)
        check("phien ban BA" in http("GET", f"{PUB}/bai-bao-thu.txt")[2].decode(), "'Cập nhật bản chốt' lấy bản mới")

        print("[6] PDF chứa JavaScript bị từ chối, bản cũ giữ nguyên")
        http("POST", f"{ADM}/_pub/api/project/{PID_BAD}", b'{"action":"publish"}', POST_H)
        good = os.path.join(tmp, "good.pdf")
        make_pdf(good, "Ban tot", [["noi dung tot"]])
        build(data, PID_BAD, UID, "18f00000003-dddddddddddddddd", good)
        collector_once(env)
        s = json.loads(http("GET", f"{ADM}/_pub/api/project/{PID_BAD}", headers=ADMIN_H)[2])
        jsf = os.path.join(tmp, "js.pdf")
        make_js_pdf(jsf)
        b5 = build(data, PID_BAD, UID, "18f00000004-eeeeeeeeeeeeeeee", jsf)
        collector_once(env)
        s2 = json.loads(http("GET", f"{ADM}/_pub/api/project/{PID_BAD}", headers=ADMIN_H)[2])
        check("/JavaScript" in (s2["last_error"] or "") or "/JS" in (s2["last_error"] or ""),
              "lỗi ghi rõ thành phần bị chặn: " + str(s2["last_error"]))
        check("noi dung tot" in http("GET", f"{PUB}/{s['slug']}.txt")[2].decode(), "bản tốt trước đó vẫn được phục vụ")

        print("[7] Tấn công symlink từ container (collector chạy trên host)")
        http("POST", f"{ADM}/_pub/api/project/{PID_LINK}", b'{"action":"publish"}', POST_H)
        http("POST", f"{ADM}/_pub/api/project/{PID_LINK}", b'{"action":"set_source","enabled":true}', POST_H)
        # (a) output.pdf là symlink tới file trên host
        bdir = os.path.join(data, "output", f"{PID_LINK}-{UID}", "generated-files", "18f00000005-ffffffffffffffff")
        os.makedirs(bdir)
        os.symlink(secret, os.path.join(bdir, "output.pdf"))
        # (b) generated-files là symlink tới thư mục khác chứa PDF hợp lệ
        other = os.path.join(tmp, "elsewhere", "18f00000006-ffffffffffffffff")
        os.makedirs(other)
        shutil.copy(good, os.path.join(other, "output.pdf"))
        os.makedirs(os.path.join(data, "output", f"{PID_LINK}-{UID2}"))
        os.symlink(os.path.dirname(other), os.path.join(data, "output", f"{PID_LINK}-{UID2}", "generated-files"))
        out = collector_once(env)
        s = json.loads(http("GET", f"{ADM}/_pub/api/project/{PID_LINK}", headers=ADMIN_H)[2])
        check(not s["has_pdf"], "không đi theo symlink output.pdf / generated-files")
        # (c) build thật nhưng \input trỏ symlink ra ngoài + đường dẫn ../
        good_b = build(data, PID_LINK, UID, "18f00000007-ffffffffffffffff", good, {
            "main.tex": "\\documentclass{article}\\begin{document}\n\\input{leak}\n\\input{../../../../host-secret.txt}\n"
                        "\\input{sub/x}\n\\end{document}\n"})
        cdir = os.path.join(data, "compiles", f"{PID_LINK}-{UID}")
        os.symlink(secret, os.path.join(cdir, "leak.tex"))
        os.symlink(tmp, os.path.join(cdir, "sub"))
        with open(os.path.join(tmp, "x.tex"), "w") as f:
            f.write("HOST-SECRET-VIA-DIR\n")
        out = collector_once(env)
        tex = http("GET", f"{PUB}/{s['slug']}.tex")[2].decode()
        check("HOST-SECRET" not in tex, "\\input qua symlink file/thư mục và ../ không đọc được file host")
        check("bỏ qua" in tex, "ghi chú [olpub] bỏ qua thay vì lỗi im lặng")

        print("[8] Gỡ publish")
        http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"unpublish"}', POST_H)
        check(http("GET", f"{PUB}/bai-bao-thu.pdf")[0] == 404, "link chết ngay sau khi gỡ")
        check(not os.path.exists(os.path.join(tmp, "pubdata", "files", PID, "doc.pdf")), "file đã bị xóa khỏi đĩa")
        st, _, b = http("POST", f"{ADM}/_pub/api/project/{PID}", b'{"action":"publish"}', POST_H)
        check(json.loads(b)["slug"] == "bai-bao-thu", "publish lại giữ nguyên link cũ")

        print("[9] robots.txt")
        r = http("GET", f"{PUB}/robots.txt")[2].decode()
        check("User-agent: GPTBot\nDisallow: /" in r and "User-agent: *\nAllow: /" in r,
              "chặn crawler huấn luyện, cho phép phần còn lại")
        print("\nlog publisher:", os.path.join(tmp, "publisher.log"))
    finally:
        srv.terminate()
        srv.wait()
    print(f"\n{'TẤT CẢ ĐẠT' if not fails else f'{len(fails)} LỖI'}")
    print("TMP", tmp)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
