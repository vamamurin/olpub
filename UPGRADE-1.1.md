# Runbook sửa đổi — Nâng cấp olpub 1.0 → 1.1 · `read.tapama.cloud`

> **Phiên bản:** 1.1 — 05/11/2026
> **Áp dụng cho:** hệ thống đã dựng theo **Runbook olpub 1.0** (`claude/runbook-olpub-publish.md`). Runbook 1.0 **giữ nguyên** làm tài liệu gốc; tài liệu này chỉ ghi phần thay đổi. Mục nào không nhắc tới ở đây thì vẫn theo bản 1.0.
> **Thời gian gián đoạn:** trang công khai khoảng 1 phút. Overleaf và collector không bị gián đoạn.
>
> **Đã kiểm thử trong môi trường giả lập** (Python 3.11, nginx 1.24, Chromium 153):
> - 69 kiểm thử chức năng/bảo mật chạy qua đúng cấu hình Nginx tĩnh của VM 1007.
> - Chuỗi đầy đủ Nginx VM 100 → Nginx VM 1007, có `ssl_reject_handshake`.
> - Trình duyệt thật hiển thị PDF bằng trình xem có sẵn.
> - **Nâng cấp thật trên CSDL do bản 1.0 tạo ra:** tài liệu đã publish được tự sinh lại thành trang tĩnh.
>
> **Chưa chạy trên hạ tầng thật.** Các điểm phụ thuộc cấu hình riêng của bạn được đánh dấu ⚠️.

---

## 1. Thay đổi gì và vì sao

| # | Thay đổi | Lý do |
|---|---|---|
| 1 | **Người xem chỉ còn gặp Nginx phục vụ file tĩnh.** Publisher sinh sẵn `<tên>.html/.pdf/.txt/.tex` vào `/srv/olpub/www`; Nginx trên VM 1007 phục vụ cổng 8080. Python không còn nghe 8080 | Ở bản 1.0, mọi request của người lạ đi qua code Python (`http.server`, regex, SQLite). Bỏ hẳn phần mềm đó khỏi đường mà người lạ chạm tới; phía Internet chỉ còn Nginx |
| 2 | **Bỏ PDF.js**, dùng trình xem PDF có sẵn của trình duyệt. Trang xem **không còn dòng JavaScript nào**; CSP `default-src 'none'` không có `script-src` | Đóng đường XSS qua PDF.js (kiểu CVE-2024-4367). Trình xem của trình duyệt chạy trong sandbox và tự cập nhật |
| 3 | **Tự động xóa EXIF/GPS/XMP/IPTC của ảnh JPEG nhúng trong PDF**, kể cả ảnh bọc thêm lớp nén (`[/FlateDecode /DCTDecode]`, gặp khi chèn PDF khác bằng `\includegraphics`) | pdfTeX chép nguyên file JPEG vào PDF, kể cả tọa độ GPS và đời máy của ảnh chụp bằng điện thoại. Bản 1.0 chỉ xóa metadata của PDF, không xóa metadata của ảnh |
| 4 | **VM 1007 không đi Internet được** (Proxmox FW `policy_out: DROP`), chỉ mở DNS, NTP, Wazuh | Kể cả khi bị chiếm root, máy cũng không gọi về C2 được |
| 5 | VM 100: `ssl_reject_handshake` cho SNI lạ hoặc IP trần; `limit_conn` + `limit_rate` cho `read` | Không lộ chứng chỉ của site khác; một bot tải liên tục PDF lớn không làm nghẽn đường mạng chung |
| 6 | Trang gốc `/` trả 404 trống. Tùy chọn "cho phép index" chuyển sang thẻ `<meta name="robots">` của trang xem; `.pdf/.txt/.tex` luôn `noindex` | Cấu hình Nginx tĩnh đơn giản, không cần `if` |

**Các mục của runbook 1.0 bị thay thế bởi tài liệu này:** 1 (bảng thành phần: cổng 8080), 4.2 (Proxmox FW VM 1007, thêm NTP), 5.1 (Nginx tĩnh thay PDF.js), 6.3 (thêm `00-default-ssl.conf`), 7 (thêm kiểm chứng), 8 (vận hành), 10 (giới hạn). Các mục 2, 3, 4.1, 4.3, 5.2, 6.1, 6.2, 9.2, 9.3 **không đổi**.

**Kiến trúc sau nâng cấp:**

```
Người xem / AI ─► read.tapama.cloud (Let's Encrypt, VM 100) → 10.0.30.57:8080 VM 1007 NGINX TĨNH
                                                                    │ đọc file
                                                          /srv/olpub/www  ◄── publisher ghi sẵn
overleaf.tapama.cloud/_pub/ (mTLS + Authelia) ───────────► 10.0.30.57:8082 Python ADMIN   (không đổi)
VM 1006 collector (token) ───────────────────────────────► 10.0.30.57:8081 Python INTERNAL (không đổi)
```

| Cổng VM 1007 | Bản 1.0 | Bản 1.1 |
|---|---|---|
| 8080 | Python | **Nginx (file tĩnh)** |
| 8081 | Python | Python (không đổi) |
| 8082 | Python | Python (không đổi) |

---

## 2. Chuẩn bị

| # | Việc | Lệnh / Ghi chú |
|---|---|---|
| 2.1 | Snapshot VM 1007 | Trên `pve-control`: `qm snapshot 1007 pre-olpub-1-1` |
| 2.2 | Sao lưu cấu hình sẽ bị thay | VM 100: `sudo cp /etc/nginx/sites-available/read.conf /root/read.conf.pre-1.1 && sudo cp /etc/nginx/conf.d/olpub.conf /root/olpub.conf.pre-1.1` · `pve-control`: `cp /etc/pve/firewall/1007.fw /root/1007.fw.pre-1.1` |
| 2.3 | ⚠️ Không có server block 443 nào khác khai `default_server` | VM 100: `grep -Rn "default_server" /etc/nginx/sites-enabled/` — chỉ được thấy `00-default-redirect.conf` (cổng 80) |
| 2.4 | ⚠️ pfSense có dịch vụ NTP nghe trên VLAN 30 | Services → NTP → Interfaces: chọn `opt3` nếu chưa có |
| 2.5 | Nên làm trước: bản 1.0 chạy được trọn vòng (đã có `ingest` trong log publisher) | Nếu collector chưa đẩy được PDF, xem bảng 9.1 ở cuối tài liệu này. Nâng cấp không sửa lỗi đó, nhưng có vòng chạy đúng thì dễ kiểm chứng hơn |

Giải nén `olpub-deploy-1.1.tar.gz` và chép thư mục `olpub/` sang VM 1007 và VM 100.

---

## 3. Các bước nâng cấp

### 3.1 VM 1007

```bash
cd <thư mục olpub 1.1>

# Cài nginx TRƯỚC khi chặn đường ra (bước 3.3)
sudo apt update && sudo apt install -y nginx

# Giữ bản 1.0 để quay lui nhanh
sudo cp /opt/olpub/olpub_publisher.py /opt/olpub/olpub_publisher.py.1.0
sudo cp /etc/olpub/publisher.env /etc/olpub/publisher.env.1.0
sudo cp /etc/systemd/system/olpub-publisher.service /root/olpub-publisher.service.1.0

sudo systemctl stop olpub-publisher

# Thư mục web: olpub ghi, group www-data (Nginx) đọc; setgid để file mới kế thừa group
sudo install -d -o root  -g root     -m 755  /srv/olpub
sudo install -d -o olpub -g www-data -m 2750 /srv/olpub/www

sudo install -m 644 publisher/olpub_publisher.py /opt/olpub/
sudo install -m 644 publisher/static/* /opt/olpub/static/
sudo install -m 644 publisher/olpub-publisher.service /etc/systemd/system/

# env: thêm OLPUB_WWW_DIR, bỏ OLPUB_PUBLIC_BIND và OLPUB_PDFJS_DIR
sudo sed -i '/^OLPUB_PUBLIC_BIND=/d; /^OLPUB_PDFJS_DIR=/d' /etc/olpub/publisher.env
grep -q '^OLPUB_WWW_DIR=' /etc/olpub/publisher.env || echo 'OLPUB_WWW_DIR=/srv/olpub/www' | sudo tee -a /etc/olpub/publisher.env

sudo systemctl daemon-reload && sudo systemctl start olpub-publisher
sudo journalctl -u olpub-publisher -n 10 --no-pager
#   phải thấy: "reconcile: N tài liệu đang publish", "listening AdminHandler ... 8082", "listening InternalHandler ... 8081"
ls -la /srv/olpub/www/                     # <tên>.html/.pdf/.txt, read.css, robots.txt — group www-data, quyền 640

# Nginx tĩnh
sudo rm -f /etc/nginx/sites-enabled/default
sudo install -m 644 nginx/vm1007-olpub-static.conf /etc/nginx/sites-available/olpub-static
sudo ln -sf ../sites-available/olpub-static /etc/nginx/sites-enabled/olpub-static
sudo install -d /etc/systemd/system/nginx.service.d
sudo install -m 644 nginx/vm1007-nginx-dropin.conf /etc/systemd/system/nginx.service.d/olpub.conf
sudo nginx -t && sudo systemctl daemon-reload && sudo systemctl enable --now nginx && sudo systemctl restart nginx

ss -lntp | grep -E ':(8080|8081|8082)'     # 8080 = nginx; 8081, 8082 = python3
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/robots.txt    # 200
```

> **Không xóa PDF.js lúc này.** Giữ `/opt/olpub/pdfjs*` cho tới khi qua bước dọn dẹp (mục 6), để quay lui bằng tay vẫn được.

### 3.2 VM 100

```bash
cd <thư mục olpub 1.1>
sudo install -m 644 nginx/conf.d-olpub.conf /etc/nginx/conf.d/olpub.conf      # thêm limit_conn_zone + map băng thông PDF
sudo install -m 644 nginx/read.conf /etc/nginx/sites-available/read.conf      # bỏ location /pdfjs/, thêm limit_conn/limit_rate
sudo install -m 644 nginx/00-default-ssl.conf /etc/nginx/sites-available/00-default-ssl.conf
sudo ln -sf ../sites-available/00-default-ssl.conf /etc/nginx/sites-enabled/00-default-ssl.conf
sudo nginx -t && sudo systemctl reload nginx
```

⚠️ Nếu các site có nghe IPv6 (`listen [::]:443 ssl`), thêm `listen [::]:443 ssl default_server;` vào `00-default-ssl.conf`.

`overleaf.conf` **không đổi**.

### 3.3 Tường lửa và NTP

**pfSense:** thêm một rule.

| Interface | Proto | Nguồn | Đích | Mô tả |
|---|---|---|---|---|
| VLAN 30 `opt3` | udp | 10.0.30.57 | pfSense(self) : 123 | `olpub -> NTP` |

**VM 1007:** trỏ NTP về pfSense.

```bash
sudo sed -i 's/^#\?NTP=.*/NTP=10.0.30.1/' /etc/systemd/timesyncd.conf
sudo systemctl restart systemd-timesyncd && timedatectl status     # System clock synchronized: yes
```

**Proxmox Firewall** (`pve-control`): chép `proxmox/1007.fw` đè lên `/etc/pve/firewall/1007.fw`.

| Chiều | Cho phép | Còn lại |
|---|---|---|
| IN | Nginx `10.0.10.12` → 8080, 8082; VM 1006 → 8081; Guacamole → 22 (như 1.0) | DROP |
| OUT | DNS + NTP tới `10.0.30.1`; Wazuh `10.0.20.10:1514-1515`; trả lời các kết nối IN (Proxmox FW có trạng thái) | **DROP** (1.0 là ACCEPT) |

Dòng `|OUT ACCEPT -p tcp -dport 80,443` là rule **đang tắt**: dấu `|` ở đầu dòng là cú pháp tắt rule của Proxmox. Chỉ bật khi cập nhật gói (mục 5).

> Ghi chú cho mục 4.4 của tài liệu bàn giao: điểm "Ký tự `|` thừa ở đầu một dòng SSH của một block tenant" nhiều khả năng **không phải lỗi cú pháp**, mà là một rule đang tắt. ⚠️ Xem trong GUI (Firewall của VM đó, cột Enable) rule đó có cần bật không.

---

## 4. Kiểm chứng

Các bước 1–6 và 10–13 ở mục 7 của runbook 1.0 vẫn áp dụng. Thêm:

| # | Kiểm tra | Kết quả đúng |
|---|---|---|
| U1 | Mở một link đã publish **trước** khi nâng cấp | Vẫn chạy, cùng link, PDF hiển thị bằng trình xem của trình duyệt |
| U2 | Recompile một project đang publish | Trang cập nhật sau ~10 giây; log publisher có `ingest … img_meta_scrubbed=` |
| U3 | `curl -i https://read.tapama.cloud/` | `404`, không liệt kê gì |
| U4 | `curl -sI https://read.tapama.cloud/<tên>` | `Content-Security-Policy: default-src 'none'…`, **không** có `script-src` |
| U5 | `curl -s https://read.tapama.cloud/<tên> \| grep -c "<script"` | `0` |
| U6 | `curl -sI https://read.tapama.cloud/<tên>.pdf` | `Content-Type: application/pdf`, `X-Robots-Tag: noindex`, `Content-Security-Policy: frame-ancestors 'self'` |
| U7 | `curl -s -o /dev/null -w "%{http_code}\n" https://read.tapama.cloud/pdfjs/web/viewer.html` | `404` (PDF.js không còn được phục vụ) |
| U8 | `openssl s_client -connect <IP public>:443 -servername khongco.tapama.cloud </dev/null` | `alert unrecognized name`, **không** có chứng chỉ nào |
| U9 | Trên VM 1007: `ss -lntp \| grep 8080` | Chỉ có `nginx` |
| U10 | Trên VM 1007: `curl -m 5 -sI https://deb.debian.org` | **Timeout** |
| U11 | Trên VM 1007: `timedatectl status` | `System clock synchronized: yes` |
| U12 | Publish tài liệu có ảnh chụp điện thoại, tải `.pdf`, `pdfimages -j <file>.pdf img && exiftool img-*.jpg \| grep -iE "gps\|make\|model"` | Không có dòng nào |
| U13 | Từ VM 1006: `nc -zv -w 3 10.0.30.57 8081` | Thành công (collector vẫn đẩy được) |
| U14 | Wazuh dashboard: agent VM 1007 | Vẫn `Active` sau khi chặn đường ra |

---

## 5. Vận hành sau nâng cấp (thay mục 8 của runbook 1.0)

| Việc | Cách làm |
|---|---|
| Gỡ một tài liệu | Như cũ: nút trong editor, hoặc `https://overleaf.tapama.cloud/_pub/` |
| **Tắt khẩn cấp trang công khai** | VM 1007: `sudo systemctl stop nginx` (người xem nhận 502). Không gỡ symlink `read.conf` ở VM 100: request `read` khi đó rơi vào `00-default-ssl` và bị từ chối TLS, khó chẩn đoán |
| **Cập nhật gói VM 1007** | Proxmox GUI → VM 1007 → Firewall → bật rule `apt update - CHI BAT TAM` → `sudo apt update && sudo apt upgrade` → **tắt rule lại**. `unattended-upgrades` không chạy được vì không có Internet, nên hãy cập nhật thủ công theo cảnh báo lỗ hổng của Wazuh |
| Cập nhật PDF.js | **Không còn cần** |
| Đổi token ingest | Như cũ |
| Backup | `/var/lib/olpub` (CSDL + bản gốc đã làm sạch). `/srv/olpub/www` **không cần backup**: publisher sinh lại mỗi khi khởi động |
| Log | `journalctl -u olpub-publisher` / `-u olpub-collector`; VM 1007: `/var/log/nginx/olpub-static.access.log` và `.error.log`; VM 100: `read.access.log` |

> CSDL tên là `olpub.sqlite3`. Gõ nhầm tên (ví dụ `olpub.db`) thì `sqlite3` lặng lẽ tạo một file rỗng mới. Nếu lỡ tạo, xóa nó: `sudo rm /var/lib/olpub/olpub.db`.

---

## 6. Dọn dẹp (sau khoảng 1 tuần chạy ổn)

```bash
# VM 1007
sudo rm -rf /opt/olpub/pdfjs /opt/olpub/pdfjs-*
sudo rm -f /opt/olpub/olpub_publisher.py.1.0 /etc/olpub/publisher.env.1.0 /root/olpub-publisher.service.1.0
# pve-control
qm delsnapshot 1007 pre-olpub-1-1
```

---

## 7. Quay lui về 1.0

**Cách nhanh (nếu chưa qua bước dọn dẹp):**

```bash
# pve-control — LƯU Ý: mất các thao tác publish/gỡ thực hiện SAU khi chụp snapshot
qm rollback 1007 pre-olpub-1-1 && qm start 1007
cp /root/1007.fw.pre-1.1 /etc/pve/firewall/1007.fw

# VM 100
sudo rm -f /etc/nginx/sites-enabled/00-default-ssl.conf
sudo cp /root/read.conf.pre-1.1 /etc/nginx/sites-available/read.conf
sudo cp /root/olpub.conf.pre-1.1 /etc/nginx/conf.d/olpub.conf
sudo nginx -t && sudo systemctl reload nginx
```

**Cách giữ CSDL hiện tại** (CSDL hai bản cùng cấu trúc):

```bash
# VM 1007
sudo systemctl disable --now nginx
sudo cp /opt/olpub/olpub_publisher.py.1.0 /opt/olpub/olpub_publisher.py
sudo cp /etc/olpub/publisher.env.1.0 /etc/olpub/publisher.env
sudo cp /root/olpub-publisher.service.1.0 /etc/systemd/system/olpub-publisher.service
sudo systemctl daemon-reload && sudo systemctl restart olpub-publisher
```

Sau đó làm phần VM 100 như cách nhanh. Proxmox FW có thể giữ `policy_out: DROP`, vì bản 1.0 cũng không cần Internet khi đã có sẵn PDF.js.

---

## 8. Giới hạn mới / thay đổi so với 1.0

- **Trình xem trên điện thoại:** Safari iOS thường chỉ hiện trang đầu của PDF nhúng; Chrome Android tùy phiên bản có thể tải file về thay vì hiển thị. Trên màn hình nhỏ, trang có dòng nhắc "mở PDF / Tải PDF". Mới thử bằng giả lập, chưa thử trên máy thật.
- **Xóa metadata ảnh** áp dụng cho JPEG (EXIF, XMP, IPTC), kể cả JPEG bọc Flate, ASCII85, ASCIIHex. Ảnh bọc LZW hoặc có tham số giải nén lạ thì được bỏ qua và ghi log. Ảnh PNG: pdfTeX không chép các chunk metadata vào PDF. Hãy kiểm chứng bằng bước U12 cho tài liệu có ảnh chụp.
- **"Cho phép index"** giờ chỉ áp dụng cho trang `/<tên>` (qua thẻ meta). `.pdf/.txt/.tex` luôn `noindex`, để công cụ tìm kiếm chỉ trỏ về trang chính.
- **VM 1007 cập nhật gói thủ công** (mục 5).

---

## 9.1 Chẩn đoán: trang 404 dù đã publish (bổ sung cho mục 9.1 của runbook 1.0)

Publisher chỉ sinh trang khi đã nhận được PDF từ collector.

```bash
# VM 1007: đã nhận PDF chưa? Phải có dòng actor=collector action=ingest
sudo journalctl -u olpub-publisher --no-pager | grep ingest | tail -3
sudo sqlite3 /var/lib/olpub/olpub.sqlite3 "SELECT slug, published, pdf_sha IS NOT NULL, last_error FROM publications"
ls -la /srv/olpub/www/

# VM 1006
sudo journalctl -u olpub-collector -n 30 --no-pager
sudo find <OLPUB_OL_DATA>/output -maxdepth 1 -name '<projectId>-*'
nc -zv -w 3 10.0.30.57 8081
```

| Dấu hiệu | Nguyên nhân | Cách sửa |
|---|---|---|
| `Chưa thấy bản biên dịch…` | Sai `OLPUB_OL_DATA` (thiếu `/data` ở cuối, hoặc toolkit dùng `data/overleaf`), hoặc chưa Recompile sau khi publish | Sửa env, restart collector, Recompile |
| Collector báo `timed out` / `refused` | Proxmox FW VM 1007 chưa mở 8081 cho `10.0.30.56` | Mục 3.3 |
| `HTTP Error 401` | Token hai bên không khớp (thường do thừa dòng khi dán) | Chép lại token |
| `Permission denied` | `ProtectHome` chặn khi toolkit nằm trong `/home` | Báo lại để chỉnh unit |
| Có `ingest` nhưng Nginx VM 1007 trả 403 | Thư mục web sai quyền | `sudo chown olpub:www-data /srv/olpub/www && sudo chmod 2750 /srv/olpub/www && sudo systemctl restart olpub-publisher` |

---

## 10. Cập nhật tài liệu bàn giao (bản 2.3) — bổ sung cho mục 11 của runbook 1.0

| Mục | Thay đổi thêm |
|---|---|
| 3.1 | VM 1007: `Nginx tĩnh 8080, Python 8081/8082` (thay "olpub publisher 8080/8081/8082") |
| 4.3 | VLAN 30: thêm `olpub -> NTP` |
| 4.4 | Block VMID 1007: `policy_out: DROP` — VM tenant đầu tiên siết chiều ra, đúng ghi chú `policy_out` của mục 4.4. Sửa ghi chú về ký tự `\|` (rule đang tắt, không phải lỗi) |
| 5.1 | Thêm `00-default-ssl.conf` (`ssl_reject_handshake`). Dòng `read` → `http://10.0.30.57:8080` là **Nginx tĩnh** |
| 8 | Thêm rủi ro: VM 1007 cập nhật gói thủ công vì không có Internet |
