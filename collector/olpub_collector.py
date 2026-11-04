#!/usr/bin/env python3
"""olpub-collector — chạy trên VM 1006 (host-overleaf-opensrc), NGOÀI container Overleaf.

Định kỳ hỏi publisher (VM 1007) danh sách project đang publish, tìm bản biên dịch mới nhất
trong thư mục data của Overleaf rồi đẩy PDF (và mã LaTeX đã xóa comment nếu được bật) sang.

An toàn:
- Chỉ đọc. Không mở cổng nào. Chỉ kết nối tới publisher (systemd IPAddressAllow).
- Thư mục data do container ghi; người dùng Overleaf CE chạy được lệnh trong container,
  nên coi mọi file/thư mục ở đó là THÙ ĐỊCH: không đi theo symlink ở bất kỳ cấp nào
  (openat + O_NOFOLLOW), chỉ đọc file thường, giới hạn kích thước.
"""
import json
import logging
import os
import re
import stat
import time
import urllib.error
import urllib.request

DATA_ROOT = os.environ.get("OLPUB_OL_DATA", "/srv/overleaf-toolkit/data/sharelatex/data")
PUBLISHER = os.environ.get("OLPUB_PUBLISHER_URL", "http://10.0.30.57:8081").rstrip("/")
TOKEN_FILE = os.environ.get("OLPUB_INGEST_TOKEN_FILE", "/etc/olpub/ingest.token")
INTERVAL = float(os.environ.get("OLPUB_INTERVAL", "5"))
SETTLE_SECONDS = 2.0  # bỏ qua bản build vừa ghi < 2 giây
MAX_PDF = int(os.environ.get("OLPUB_MAX_PDF_MB", "50")) * 1024 * 1024
MAX_TEX_FILE = 2 * 1024 * 1024
MAX_TEX_TOTAL = 5 * 1024 * 1024
CONTAINER_COMPILES = "/var/lib/overleaf/data/compiles/"

PID_RE = re.compile(r"^[0-9a-f]{24}$")
NAME_RE = re.compile(r"^([0-9a-f]{24})(?:-[0-9a-f]{24})?$")
BUILD_RE = re.compile(r"^[0-9a-f-]{8,64}$")
log = logging.getLogger("olpub-collector")

# ---------------------------------------------------------------- đọc file an toàn

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def open_dir(parts, base_fd=None):
    """Mở thư mục theo từng cấp, từ chối symlink ở mọi cấp. Trả về fd."""
    fd = os.open(DATA_ROOT, O_DIR) if base_fd is None else os.dup(base_fd)
    try:
        for p in parts:
            if p in ("", ".", "..") or "/" in p:
                raise PermissionError(f"thành phần đường dẫn không hợp lệ: {p!r}")
            nfd = os.open(p, O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(dir_fd, name, limit):
    """Đọc file thường trong dir_fd; từ chối symlink, thiết bị, FIFO, file quá lớn."""
    if "/" in name or name in ("", ".", ".."):
        raise PermissionError(f"tên file không hợp lệ: {name!r}")
    fd = os.open(name, O_FILE, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"{name} không phải file thường")
        if st.st_size > limit:
            raise ValueError(f"{name} vượt giới hạn {limit} byte")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            total += len(b)
            if total > limit:
                raise ValueError(f"{name} vượt giới hạn {limit} byte")
            chunks.append(b)
        return b"".join(chunks), st.st_mtime
    finally:
        os.close(fd)


def list_dir(fd):
    """Liệt kê mục con là THƯ MỤC thật (không phải symlink)."""
    out = []
    with os.scandir(fd) as it:
        for e in it:
            if e.is_dir(follow_symlinks=False):
                out.append(e.name)
    return out


# ---------------------------------------------------------------- tìm bản build


def newest_build(pid):
    """Trả về (tên compile dir '<pid>-<uid>', build_id, mtime) mới nhất, hoặc None.
    Bản build nằm ở data/output/<pid>-<uid>/generated-files/<buildId>/output.pdf
    (OutputCacheManager chép vào đây sau khi biên dịch xong)."""
    best = None
    try:
        out_fd = open_dir(["output"])
    except FileNotFoundError:
        return None
    try:
        for name in list_dir(out_fd):
            m = NAME_RE.match(name)
            if not m or m.group(1) != pid:
                continue
            try:
                gen_fd = open_dir([name, "generated-files"], out_fd)
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                continue
            try:
                for b in list_dir(gen_fd):
                    if not BUILD_RE.match(b):
                        continue
                    try:
                        bfd = os.open(b, O_DIR, dir_fd=gen_fd)
                    except OSError:
                        continue
                    try:
                        st = os.stat("output.pdf", dir_fd=bfd, follow_symlinks=False)
                    except OSError:
                        continue
                    finally:
                        os.close(bfd)
                    if stat.S_ISREG(st.st_mode) and (best is None or st.st_mtime > best[2]):
                        best = (name, b, st.st_mtime)
            finally:
                os.close(gen_fd)
    finally:
        os.close(out_fd)
    return best


def read_build_pdf(name, build_id):
    fd = open_dir(["output", name, "generated-files", build_id])
    try:
        return read_file(fd, "output.pdf", MAX_PDF)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- mã LaTeX: gộp file + xóa comment

VERBATIM_ENVS = ("verbatim", "verbatim*", "lstlisting", "minted", "Verbatim", "comment")
BEGIN_RE = re.compile(r"\\begin\{(" + "|".join(re.escape(e) for e in VERBATIM_ENVS) + r")\}")
INPUT_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]+)\}")
BIB_RE = re.compile(r"\\bibliography\s*\{[^}]*\}")
VERB_RE = re.compile(r"\\verb\*?([^a-zA-Z\s*])")
URL_RE = re.compile(r"\\(?:url|href|path)\s*\{")


def strip_line_comment(line):
    """Cắt từ ký tự % bắt đầu comment. Bỏ qua \\%, và % nằm trong \\verb|..|, \\url{..}, \\href{..}."""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            m = VERB_RE.match(line, i)
            if m:
                j = line.find(m.group(1), m.end())
                i = n if j < 0 else j + 1
                continue
            m = URL_RE.match(line, i)
            if m:
                depth, j = 1, m.end()
                while j < n and depth:
                    depth += {"{": 1, "}": -1}.get(line[j], 0)
                    j += 1
                i = j
                continue
            i += 2  # ký tự bị escape, kể cả \% và \\
            continue
        if c == "%":
            return line[:i], True
        i += 1
    return line, False


def strip_comments(text):
    out, env_end, skipping = [], None, False
    for line in text.splitlines():
        if env_end:  # đang trong môi trường verbatim (giữ nguyên) hoặc comment (bỏ)
            if not skipping:
                out.append(line)
            if env_end in line:
                env_end = None
            continue
        code, had = strip_line_comment(line)
        m = BEGIN_RE.search(code)
        if m:
            end = "\\end{" + m.group(1) + "}"
            if m.group(1) == "comment":
                if code[:m.start()].strip():
                    out.append(code[:m.start()].rstrip())
                if end not in code[m.end():]:
                    env_end, skipping = end, True
                continue
            out.append(code.rstrip() if had else line)
            if end not in code[m.end():]:
                env_end, skipping = end, False
            continue
        if had and not code.strip():
            continue  # dòng chỉ có comment: bỏ hẳn
        out.append(code.rstrip() if had else line)
    return strip_iffalse("\n".join(out) + "\n")


COND_RE = re.compile(r"\\(iffalse|if[a-zA-Z@]*|fi)(?![a-zA-Z@])")


def strip_iffalse(text):
    """Bỏ các khối \\iffalse ... \\fi (cách 'comment' nhiều dòng phổ biến).
    Lệnh dạng \\ifXxx{...} (etoolbox, ifthen) không có \\fi nên không tính.
    Nếu không tìm được \\fi khớp thì giữ nguyên văn bản (an toàn)."""
    out, pos = [], 0
    while True:
        m = re.search(r"\\iffalse(?![a-zA-Z@])", text[pos:])
        if not m:
            out.append(text[pos:])
            return "".join(out)
        start = pos + m.start()
        depth, i, end = 1, pos + m.end(), None
        for t in COND_RE.finditer(text, i):
            tok = t.group(1)
            if tok == "fi":
                depth -= 1
                if depth == 0:
                    end = t.end()
                    break
            else:
                if text[t.end():t.end() + 64].lstrip().startswith("{"):
                    continue
                depth += 1
        if end is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:start])
        pos = end


def find_main(cfd, names):
    """File .tex gốc: lấy từ output.fls (dòng INPUT đầu tiên là .tex trong compile dir)."""
    try:
        fls, _ = read_file(cfd, "output.fls", 4 * 1024 * 1024)
        for line in fls.decode(errors="replace").splitlines():
            if not line.startswith("INPUT "):
                continue
            p = line[6:].strip()
            if p.startswith(CONTAINER_COMPILES):
                p = p[len(CONTAINER_COMPILES):].split("/", 1)[-1]
            p = p[2:] if p.startswith("./") else p
            if p.endswith(".tex") and not p.startswith("/"):
                return p
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        pass
    if "main.tex" in names:
        return "main.tex"
    for n in sorted(names):
        if n.endswith(".tex"):
            try:
                if b"\\documentclass" in read_file(cfd, n, MAX_TEX_FILE)[0]:
                    return n
            except (PermissionError, ValueError, OSError):
                continue
    return None


def read_rel(cfd, rel, limit):
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise PermissionError(f"đường dẫn ngoài project: {rel}")
    dfd = open_dir(parts[:-1], cfd) if len(parts) > 1 else os.dup(cfd)
    try:
        return read_file(dfd, parts[-1], limit)[0]
    finally:
        os.close(dfd)


def flatten_source(compile_name):
    """Gộp file .tex gốc và các \\input/\\include thành một file, đã xóa comment."""
    cfd = open_dir(["compiles", compile_name])
    try:
        with os.scandir(cfd) as it:
            names = [e.name for e in it if e.is_file(follow_symlinks=False)]
        main = find_main(cfd, names)
        if not main:
            return None
        total = [0]
        seen = set()

        def load(rel, depth):
            if depth > 20 or rel in seen:
                return f"% [olpub] bỏ qua {rel}: lồng quá sâu hoặc lặp\n"
            seen.add(rel)
            names_ = [rel] if rel.endswith(".tex") else [rel + ".tex", rel]
            bases = [os.path.dirname(main), ""] if os.path.dirname(main) else [""]
            cand = [os.path.join(b, n) if b else n for b in bases for n in names_]
            for c in cand:
                try:
                    raw = read_rel(cfd, c, MAX_TEX_FILE)
                    break
                except FileNotFoundError:
                    continue
                except (PermissionError, ValueError, OSError) as e:
                    return f"% [olpub] bỏ qua {c}: {e}\n"
            else:
                return f"% [olpub] không tìm thấy {rel}\n"
            total[0] += len(raw)
            if total[0] > MAX_TEX_TOTAL:
                raise ValueError("mã nguồn vượt 5MB")
            body = strip_comments(raw.decode("utf-8", errors="replace"))

            def repl(m):
                inner = load(m.group(2).strip(), depth + 1)
                return f"% ===== [olpub] bắt đầu {m.group(2).strip()} =====\n{inner}% ===== [olpub] hết {m.group(2).strip()} =====\n"
            return INPUT_RE.sub(repl, body)

        seen.discard(main)
        text = load(main, 0)
        # \bibliography{...} -> nội dung output.bbl (danh mục tham khảo đã định dạng)
        try:
            bbl, _ = read_file(cfd, "output.bbl", MAX_TEX_FILE)
            text = BIB_RE.sub(lambda m: "% [olpub] output.bbl\n" + bbl.decode("utf-8", errors="replace"), text, count=1)
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            pass
        return f"% Mã nguồn từ {main} — đã gộp \\input và xóa comment (olpub)\n" + text
    finally:
        os.close(cfd)


# ---------------------------------------------------------------- HTTP tới publisher


def request(method, path, token, data=None, headers=None):
    req = urllib.request.Request(PUBLISHER + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def report_error(token, pid, msg):
    try:
        request("PUT", f"/internal/error/{pid}", token, msg.encode(), {"Content-Type": "text/plain; charset=utf-8"})
    except Exception as e:  # noqa: BLE001
        log.warning("không báo lỗi được cho %s: %s", pid, e)


def cycle(token, last_error):
    targets = request("GET", "/internal/targets", token)["targets"]
    for t in targets:
        pid = t["project_id"]
        if not PID_RE.match(pid):
            continue
        b = newest_build(pid)
        if not b:
            if last_error.get(pid) != "nobuild":
                report_error(token, pid, "Chưa thấy bản biên dịch trên server — hãy bấm Recompile trong Overleaf.")
                last_error[pid] = "nobuild"
            continue
        name, build_id, mtime = b
        if not t["refresh"] and mtime <= t["build_mtime"]:
            continue
        if time.time() - mtime < SETTLE_SECONDS:
            continue
        try:
            pdf, _ = read_build_pdf(name, build_id)
        except (PermissionError, ValueError, OSError) as e:
            report_error(token, pid, f"Không đọc được output.pdf: {e}")
            continue
        if t["include_source"]:
            try:
                tex = flatten_source(name)
                if tex:
                    request("PUT", f"/internal/tex/{pid}", token, tex.encode("utf-8"),
                            {"Content-Type": "text/plain; charset=utf-8"})
            except (PermissionError, ValueError, OSError) as e:
                log.warning("bỏ qua mã nguồn %s: %s", pid, e)
        try:
            r = request("PUT", f"/internal/pdf/{pid}", token, pdf,
                        {"Content-Type": "application/pdf", "X-Build-Mtime": repr(mtime)})
            log.info("đã đẩy %s build=%s sha=%s", pid, build_id, r.get("sha", "")[:12])
            last_error.pop(pid, None)
        except urllib.error.HTTPError as e:
            log.warning("publisher từ chối %s: %s %s", pid, e.code, e.read()[:300])


def main():
    logging.basicConfig(level=os.environ.get("OLPUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    if not os.path.isdir(os.path.join(DATA_ROOT, "output")):
        log.warning("Chưa thấy %s/output — kiểm tra OLPUB_OL_DATA", DATA_ROOT)
    last_error = {}
    once = os.environ.get("OLPUB_ONCE") == "1"
    while True:
        try:
            cycle(token, last_error)
        except Exception as e:  # noqa: BLE001 — collector không được chết vì một lỗi mạng
            log.warning("chu kỳ lỗi: %s", e)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
