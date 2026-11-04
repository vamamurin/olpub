# Runbook — Publish PDF từ Overleaf (olpub) · `read.tapama.cloud`

> **Phiên bản:** 1.0 — 04/11/2026 · Dùng để gộp vào tài liệu bàn giao bản 2.3
> **Phạm vi:** bọc Overleaf bằng mTLS + Authelia; thêm VM 1007 `host-publish`; collector trên VM 1006; vhost công khai `read.tapama.cloud` (Let's Encrypt).
> **Đã kiểm thử trong môi trường giả lập** (Python 3.11, nginx 1.24, Chromium): 41 kiểm thử chức năng/bảo mật của publisher + collector; Nginx thật với TLS, mTLS, `auth_request`, chèn nút theo nonce CSP; trình duyệt thật nạp nút dưới CSP `strict-dynamic` và PDF.js 6.5.438 hiển thị đủ trang, không lỗi CSP. **Chưa chạy trên hạ tầng thật** — các điểm phụ thuộc cấu hình riêng của bạn được đánh dấu ⚠️.

---

## 1. Kiến trúc

```
                 ┌──────── mTLS thiết bị ──► Authelia (group overleaf, two_factor) ─────────┐
3 thành viên ───►│ overleaf.tapama.cloud           → 10.0.30.56:80   VM 1006 Overleaf CE     │
                 │ overleaf.tapama.cloud/_pub/     → 10.0.30.57:8082 VM 1007 ADMIN API + nút  │
                 └───────────────────────────────────────────────────────────────────────────┘
Người xem / AI ─► read.tapama.cloud (Let's Encrypt) → 10.0.30.57:8080 VM 1007 PUBLIC (chỉ GET)

VM 1006: olpub-collector ──(chỉ đọc data/output, data/compiles)──► 10.0.30.57:8081 INTERNAL (Bearer token)
```

| Thành phần | Nơi chạy | Cổng | Ai được gọi tới |
|---|---|---|---|
| `olpub-publisher` PUBLIC | VM 1007 | 8080 | Nginx `10.0.10.12` |
| `olpub-publisher` ADMIN | VM 1007 | 8082 | Nginx `10.0.10.12` |
| `olpub-publisher` INTERNAL | VM 1007 | 8081 | VM 1006 `10.0.30.56` |
| `olpub-collector` | VM 1006 (host, ngoài container) | không mở cổng | — |

**Luồng sử dụng:** mở project trong Overleaf → nút **🌐 Publish** ở góc trái dưới → nhận link `https://read.tapama.cloud/<tên>` → mỗi lần Recompile, trang công khai tự cập nhật sau khoảng 5–10 giây. Có thể chốt phiên bản, kèm mã LaTeX đã xóa comment, đổi tên link, gỡ publish.

**Mỗi link có 4 dạng:**

| URL | Nội dung |
|---|---|
| `/<tên>` | Trang xem (PDF.js) + toàn văn bản trong thẻ `<details>` — AI đọc được ngay từ link chính |
| `/<tên>.pdf` | PDF đã làm sạch metadata |
| `/<tên>.txt` | Văn bản trích từ PDF, có đánh dấu `===== Trang N =====` |
| `/<tên>.tex` | Mã LaTeX đã gộp `\input` và xóa comment — chỉ khi chủ project bật |

**Vì sao thiết kế như vậy:**

- **Bản nháp không rời VM 1006.** Collector hỏi publisher danh sách project đang publish rồi mới đọc. Project không publish thì không bao giờ được gửi đi.
- **VM 1007 chỉ chứa nội dung đã công khai.** Nếu trang public bị chiếm, kẻ tấn công cũng chỉ lấy được thứ vốn đã công khai.
- **Coi dữ liệu Overleaf là thù địch.** Người dùng Overleaf CE chạy được lệnh trong container (không có Sandboxed Compiles), nên có thể tạo symlink trong thư mục data. Collector chạy trên host với quyền đọc vượt kiểm tra, vì vậy không đi theo symlink ở bất kỳ cấp nào (`openat` + `O_NOFOLLOW`), không cho `..` và chỉ đọc file thường. Các trường hợp này đều đã kiểm thử.
- **Danh tính do Nginx gắn.** `X-Pub-User` lấy từ Authelia, `X-Pub-Device` lấy từ `$ssl_client_s_dn`. Header cùng tên mà client tự gửi đều bị ghi đè (đã kiểm thử). Cổng ADMIN chỉ nhận kết nối từ Nginx.

---

## 2. Điều kiện tiên quyết

| # | Việc | Lý do |
|---|---|---|
| 2.1 | 🔴 **Backup Authelia** (cấu hình + CSDL người dùng/TOTP), mã hóa | Sau thay đổi này, Raspberry Pi hỏng thì **không ai vào được Overleaf**. Mục 8 của tài liệu bàn giao vẫn ghi chưa backup |
| 2.2 | Cấp chứng chỉ thiết bị cho **mọi thiết bị** 3 thành viên dùng Overleaf (kể cả điện thoại nếu cần) | Theo quy trình Device CA ở mục 7. Thiết bị chưa có chứng chỉ sẽ bị trả 400 |
| 2.3 | Snapshot: `qm snapshot 100 pre-olpub`, `qm snapshot 1006 pre-olpub` | Đường lùi |
| 2.4 | ⚠️ Kiểm tra Nginx có module sub_filter: `nginx -V 2>&1 \| grep -o http_sub_module` | Gói nginx của Debian có sẵn. Thiếu module thì nút không chèn được, nhưng trang dự phòng `/_pub/` vẫn dùng được |
| 2.5 | ⚠️ Kiểm tra `certbot` không ép CA nội bộ trong `cli.ini`: `grep -i server /etc/letsencrypt/cli.ini` | Certbot coi giá trị trong `cli.ini` như tham số dòng lệnh, **ghi đè** `server` trong file renewal của từng chứng chỉ. Nếu có dòng `server = https://ca.tapama.cloud...`, chứng chỉ Let's Encrypt sẽ gia hạn nhầm sang step-ca. Xem bước 6.2 |

---

## 3. Authelia

`configuration.yml`: đặt rule này **trước** mọi rule rộng hơn.

```yaml
access_control:
  rules:
    - domain: 'overleaf.tapama.cloud'
      subject: 'group:overleaf'
      policy: 'two_factor'
```

`users_database.yml`: thêm group `overleaf` cho 3 thành viên.

```yaml
users:
  <user>:
    groups:
      - overleaf      # giữ nguyên các group khác đang có
```

Khởi động lại Authelia, rồi kiểm tra log không có lỗi cấu hình.

> **Thời gian phiên:** phiên Authelia dùng chung cho mọi site `*.tapama.cloud`. Nới `inactivity` để soạn Overleaf đỡ bị đăng nhập lại thì cũng nới luôn cho pfSense, Proxmox, Wazuh. Đề xuất **giữ nguyên** và dùng ô "Remember me" (`remember_me`). Nếu đang soạn mà phiên hết hạn, Overleaf báo mất kết nối; đăng nhập lại thì nội dung vẫn còn, vì Overleaf lưu từng thao tác lên server.

---

## 4. VM 1007 `host-publish`

### 4.1 Tạo VM (trên `pve-control`)

```bash
qm clone 9000 1007 --name host-publish --full
qm set 1007 --memory 1024 --cores 1
qm resize 1007 scsi0 +8G                         # image gốc chỉ 2GB (mục 3.3)
qm config 1006 | grep ^net0                       # chép bridge + tag của VM 1006
qm set 1007 --net0 virtio,bridge=<bridge như VM 1006>,tag=30,firewall=1
qm set 1007 --ciuser sysadmin \
  --ipconfig0 ip=10.0.30.57/24,gw=10.0.30.1 \
  --nameserver 10.0.30.1 --searchdomain tapama.cloud \
  --sshkeys /root/.ssh/id_ed25519.pub
qm set 1007 --onboot 1
qm start 1007
```

Trong VM: làm theo chuẩn ở mục 3.4 (`qemu-guest-agent`, timezone, kiểm tra đồng bộ giờ, Wazuh agent).

### 4.2 Tường lửa

**pfSense** (đặt mô tả rõ ràng, gọi rule theo mô tả):

| Interface | Proto | Nguồn | Đích | Mô tả |
|---|---|---|---|---|
| VLAN 10 `opt1` | tcp | 10.0.10.12 | 10.0.30.57 : 8080, 8082 | `Nginx -> olpub (read + admin)` |
| VLAN 30 `opt3` | tcp/udp | 10.0.30.57 | pfSense(self) : 53 | `olpub -> DNS` |

Luồng VM 1006 → VM 1007 cổng 8081 nằm **cùng VLAN 30**, nên thuộc lớp Proxmox Firewall và không cần rule pfSense (mục 4.1).

**Proxmox Firewall** — `/etc/pve/firewall/1007.fw`:

```
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -source 10.0.10.12 -p tcp -dport 8080 # Nginx -> olpub PUBLIC (read.tapama.cloud)
IN ACCEPT -source 10.0.10.12 -p tcp -dport 8082 # Nginx -> olpub ADMIN (overleaf/_pub/)
IN ACCEPT -source 10.0.30.56 -p tcp -dport 8081 # collector VM 1006 -> ingest
IN ACCEPT -source 10.0.10.13 -p tcp -dport 22 # Guacamole SSH, như các tenant khác
```

Ngoài ra, unit systemd của publisher còn có `IPAddressAllow` riêng (lớp thứ ba): chỉ `10.0.10.12`, `10.0.30.56` và loopback được kết nối tới, và tiến trình không tự mở được kết nối ra ngoài.

### 4.3 DNS

| Nơi | Bản ghi |
|---|---|
| Unbound Host Override | `read.tapama.cloud → 10.0.10.12`. Thêm **trước** khi cấu hình Nginx (mục 4.8) |
| DNS công cộng | `read` → IP public, giống các site đang truy cập được từ ngoài. Bắt buộc để Let's Encrypt xác thực và người ngoài mở được link |

---

## 5. Cài đặt

Bộ file nằm trong `olpub-deploy.tar.gz`. Chép sang từng VM qua Guacamole hoặc SSH.

### 5.1 Publisher (VM 1007)

```bash
sudo apt update && sudo apt install -y python3 qpdf libimage-exiftool-perl poppler-utils unzip curl sqlite3
sudo useradd --system --home /var/lib/olpub --shell /usr/sbin/nologin olpub
sudo install -d -o olpub -g olpub -m 750 /var/lib/olpub
sudo install -d -m 755 /opt/olpub /opt/olpub/static
sudo install -m 644 publisher/olpub_publisher.py /opt/olpub/
sudo install -m 644 publisher/static/* /opt/olpub/static/

# PDF.js (bản dist chính thức, kèm viewer). Tách thư mục theo phiên bản, trỏ symlink.
V=6.5.438
curl -fsSLO https://github.com/mozilla/pdf.js/releases/download/v$V/pdfjs-$V-dist.zip
sudo unzip -q pdfjs-$V-dist.zip -d /opt/olpub/pdfjs-$V
sudo rm -f /opt/olpub/pdfjs-$V/web/compressed.tracemonkey-pldi-09.pdf
sudo ln -sfn /opt/olpub/pdfjs-$V /opt/olpub/pdfjs

# Token dùng chung với collector (chép sang VM 1006 ở bước 5.2)
sudo install -d -m 750 -o root -g olpub /etc/olpub
openssl rand -hex 32 | sudo tee /etc/olpub/ingest.token >/dev/null
sudo chown root:olpub /etc/olpub/ingest.token && sudo chmod 640 /etc/olpub/ingest.token
sudo install -m 640 -o root -g olpub publisher/publisher.env.example /etc/olpub/publisher.env

sudo install -m 644 publisher/olpub-publisher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now olpub-publisher
sudo journalctl -u olpub-publisher -n 20 --no-pager     # phải thấy 3 dòng "listening"
ss -lntp | grep -E ':(8080|8081|8082)'
```

### 5.2 Collector (VM 1006 — trên host, KHÔNG trong container)

```bash
# Xác định thư mục data trên host
cd <thư mục overleaf-toolkit>
grep OVERLEAF_DATA_PATH config/overleaf.rc || echo "mặc định: data/sharelatex"
# OLPUB_OL_DATA = <toolkit>/<OVERLEAF_DATA_PATH>/data  — phải có thư mục con output/ và compiles/
sudo ls <toolkit>/<OVERLEAF_DATA_PATH>/data/output | head

sudo useradd --system --no-create-home --shell /usr/sbin/nologin olpub-collector
sudo install -d -m 755 /opt/olpub
sudo install -m 644 collector/olpub_collector.py /opt/olpub/
sudo install -d -m 750 -o root -g olpub-collector /etc/olpub
# dán đúng nội dung /etc/olpub/ingest.token của VM 1007:
sudo install -m 640 -o root -g olpub-collector /dev/null /etc/olpub/ingest.token
sudoedit /etc/olpub/ingest.token
sudo install -m 640 -o root -g olpub-collector collector/collector.env.example /etc/olpub/collector.env
sudoedit /etc/olpub/collector.env                       # sửa OLPUB_OL_DATA

sudo install -m 644 collector/olpub-collector.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now olpub-collector
sudo journalctl -u olpub-collector -n 20 --no-pager
```

Collector chạy bằng user riêng, chỉ có `CAP_DAC_READ_SEARCH` (đọc được file của `www-data` trong container, không ghi được gì), `ProtectSystem=strict` và chỉ được kết nối tới `10.0.30.57`.

---

## 6. Nginx (VM 100)

### 6.1 Overleaf: mTLS + Authelia + nút Publish

```bash
cd <thư mục giải nén olpub-deploy>
sudo cp /etc/nginx/sites-available/overleaf.conf /etc/nginx/sites-available/overleaf.conf.pre-olpub   # đường lùi, KHÔNG symlink
sudo install -m 644 nginx/conf.d-olpub.conf /etc/nginx/conf.d/olpub.conf
```

Sửa `overleaf.conf` theo file mẫu `nginx/overleaf.conf`. Có ba điểm phải khớp với cấu hình hiện có của bạn:

1. **Authelia:** chép nguyên các dòng Authelia đang dùng trong `wazuh.conf`. `auth_request` phải áp dụng cho mọi `location` của site, nên cách gọn nhất là khai ở cấp `server`.
   ⚠️ Kiểm tra: `sudo nginx -T 2>/dev/null | grep -A3 -n "server_name wazuh" ; grep -n auth_request /etc/nginx/snippets/authelia.conf`
2. **mTLS:** `include snippets/mtls-device.conf;` + `ssl_verify_client on;` ở cấp `server`. **Sửa** dòng `access_log` sẵn có thành `... mtls;`, không thêm dòng thứ hai (mục 5.2).
3. **Ba `location`:** trang editor (chèn nút), `/_pub/` (API), `/` (như cũ). Regex của location editor **phải nằm trong dấu nháy kép** (`"^/project/[0-9a-f]{24}$"`), vì dấu `{` không có nháy làm `nginx -t` báo lỗi.

### 6.2 `read.tapama.cloud`: chứng chỉ Let's Encrypt

Đây là **ngoại lệ có chủ đích** so với mục 5.5 (mọi site khác dùng step-ca). Người ngoài và công cụ AI không tin `Tapama Root CA`.

```bash
# Đường ACME của default_server cổng 80 đã có sẵn cho mọi tên — xin chứng chỉ trước khi bật vhost
curl -s http://read.tapama.cloud/.well-known/acme-challenge/probe     # chạy TỪ BÊN NGOÀI (4G): phải ra probe-ok
sudo certbot certonly --webroot -w /var/www/acme -d read.tapama.cloud \
  --server https://acme-v02.api.letsencrypt.org/directory \
  --email <email quản trị> --agree-tos --no-eff-email
grep ^server /etc/letsencrypt/renewal/read.tapama.cloud.conf          # phải là acme-v02.api.letsencrypt.org
sudo certbot renew --dry-run --cert-name read.tapama.cloud            # với chứng chỉ LE, --dry-run là đúng (khác mục 6.11)
```

Nếu bước 2.5 phát hiện `server` trong `cli.ini`: chuyển dòng đó vào từng file `/etc/letsencrypt/renewal/<site>.conf` của các chứng chỉ step-ca (thường đã có sẵn), rồi xóa khỏi `cli.ini`. Sau đó chạy `sudo certbot renew --dry-run --cert-name read.tapama.cloud` và `sudo certbot renew --force-renewal --cert-name test.tapama.cloud` để xác nhận cả hai loại vẫn gia hạn đúng CA.

### 6.3 Bật cấu hình

```bash
sudo install -m 644 nginx/read.conf /etc/nginx/sites-available/read.conf
sudo ln -s ../sites-available/read.conf /etc/nginx/sites-enabled/read.conf
sudo nginx -t && sudo systemctl reload nginx
sudo nginx -T 2>/dev/null | grep -E "^\s*server_name|ssl_verify_client"   # overleaf phải có ssl_verify_client on
```

---

## 7. Kiểm chứng

| # | Kiểm tra | Kết quả đúng |
|---|---|---|
| 1 | `curl -s -o /dev/null -w "%{http_code}\n" https://overleaf.tapama.cloud/` từ máy **không** có chứng chỉ thiết bị | `400` |
| 2 | Trình duyệt có chứng chỉ, chưa đăng nhập Authelia | Chuyển sang `auth.tapama.cloud` |
| 3 | Mở một project | Nút **🌐 Publish** ở góc trái dưới. Không thấy nút: xem mục 9.2 |
| 4 | Bấm Publish, rồi Recompile | Trong ~10 giây panel hiện "Cập nhật lần cuối: …" |
| 5 | Mở link bằng điện thoại dùng **4G** (không cài root Tapama) | Mở được, không có cảnh báo chứng chỉ, PDF hiển thị |
| 6 | Dán link vào Claude/ChatGPT, yêu cầu tóm tắt | AI đọc được nội dung |
| 7 | `curl -X POST https://read.tapama.cloud/<tên>` / `curl https://read.tapama.cloud/_pub/api/list` | `403` / `404` |
| 8 | `curl -sI https://read.tapama.cloud/<tên>.pdf` | `Content-Type: application/pdf`, `X-Content-Type-Options: nosniff`, `X-Robots-Tag: noindex` |
| 9 | Tải `.pdf` về, `exiftool -a -G1 <file>.pdf` | Không còn Author/Creator/Producer/ngày tạo; chỉ còn Title |
| 10 | Từ VM 1006: `nc -zv -w 3 10.0.30.57 8080` | **Timeout** (VM 1006 chỉ được vào 8081) |
| 11 | Từ VM 1001: `nc -zv -w 3 10.0.30.57 8081` | **Timeout** |
| 12 | Từ VM 1006: `nc -zv -w 3 10.0.30.57 8081` | Thành công |
| 13 | Gỡ publish → mở lại link | `404` ngay lập tức |

Nhật ký thao tác (ai, thiết bị nào, publish hay gỡ project nào):

```bash
sudo sqlite3 /var/lib/olpub/olpub.sqlite3 \
  "SELECT datetime(ts,'unixepoch','+7 hours'), actor, device, project_id, action FROM audit ORDER BY id DESC LIMIT 20"
```

---

## 8. Vận hành

| Việc | Cách làm |
|---|---|
| Gỡ một tài liệu | Nút trong editor, hoặc trang `https://overleaf.tapama.cloud/_pub/` |
| **Tắt khẩn cấp toàn bộ trang công khai** | VM 1007: `sudo systemctl stop olpub-publisher` (người xem nhận 502). Không gỡ symlink `read.conf` để tắt: request `read` khi đó rơi vào server block 443 khác và nhận chứng chỉ sai tên |
| Cập nhật PDF.js | Tải bản mới vào `/opt/olpub/pdfjs-<V>`, đổi symlink `/opt/olpub/pdfjs`. Không cần restart. Trình duyệt cache tối đa 1 ngày. Theo dõi advisory của PDF.js (ví dụ CVE-2024-4367) |
| Đổi token ingest | Sinh token mới, ghi vào **cả hai** VM, rồi `systemctl restart olpub-publisher` và `olpub-collector` |
| Sau mỗi lần nâng cấp Overleaf | Mở một project, xác nhận nút vẫn hiện. Nếu không, dùng `/_pub/` và xem mục 9.2 |
| Backup | `/var/lib/olpub` (SQLite chứa danh sách link). File PDF tạo lại được bằng cách Recompile → "Cập nhật bản chốt". Gộp vào kế hoạch vzdump |
| Log | `journalctl -u olpub-publisher` / `-u olpub-collector`; Nginx: `read.access.log`, `overleaf.access.log` (có `dev_dn`) |

---

## 9. Sự cố thường gặp

### 9.1 Panel báo "Chờ bản biên dịch — hãy bấm Recompile"

Overleaf tự dọn các bản build cũ trong `data/output`. Chỉ cần Recompile một lần, collector sẽ lấy bản mới. Nếu vẫn chờ:

```bash
sudo journalctl -u olpub-collector -n 30 --no-pager
sudo find <OLPUB_OL_DATA>/output -maxdepth 1 -name '<projectId>-*'      # có thư mục không?
```

### 9.2 Không thấy nút Publish

1. `View source` trang editor, tìm `_pub/button.js`. Không có thì Nginx chưa chèn được: kiểm tra location editor, `sub_filter` và `Accept-Encoding ""`.
2. Có thẻ script nhưng `nonce=""`: map `$olpub_nonce` không bắt được nonce. Xem header CSP thật bằng `curl -sI ... | grep -i content-security`.
3. Console trình duyệt báo lỗi CSP: Overleaf có thể đã đổi định dạng CSP sau khi nâng cấp. Trong lúc chờ sửa, dùng trang `/_pub/`.

### 9.3 Panel báo "Lần lấy bản mới bị từ chối: PDF chứa thành phần bị chặn: …"

PDF có JavaScript, file đính kèm (gói `attachfile`, `embedfile`), `\href{run:...}` (Launch), media hoặc form gửi dữ liệu. Bản publish trước đó vẫn được giữ nguyên. Bỏ thành phần đó khỏi tài liệu rồi Recompile.

---

## 10. Giới hạn đã biết

- **Ai được publish:** mọi thành viên group `overleaf`, với bất kỳ project ID nào. Publisher không kiểm quyền theo từng project của Overleaf. Chấp nhận được với 3 người tin nhau; mọi thao tác đều được ghi vào bảng `audit`.
- **Project nhiều người biên dịch:** collector lấy bản build **mới nhất** theo thời gian, không phân biệt ai bấm Recompile.
- **Biên dịch có lỗi:** Overleaf vẫn tạo PDF (chạy `latexmk -f`), và PDF đó vẫn được publish. Dùng "Chốt phiên bản" khi tài liệu đang sửa dở.
- **Xóa comment trong `.tex`** xử lý được: `%` (giữ `\%`, `%` trong `\url`, `\href`, `\verb`), môi trường `comment`, khối `\iffalse…\fi`. **Không** xử lý được: macro comment tự định nghĩa, `\if0…\fi`, ghi chú dạng `\todo{}` hay `\note{}`. Hãy xem lại `/<tên>.tex` trước khi gửi link nếu bật tùy chọn này.
- **`.txt` với công thức toán** chỉ là chữ trích từ PDF, khó đọc. Tài liệu nhiều công thức nên bật `.tex` để AI đọc chính xác hơn.
- **`robots.txt` và `noindex`** chỉ là quy ước tự nguyện. Link đã publish nên được coi là công khai.

---

## 11. Cập nhật tài liệu bàn giao (bản 2.3)

| Mục | Thay đổi |
|---|---|
| 3.1 | Thêm `1007 host-publish 10.0.30.57 VLAN 30 — olpub publisher — 8080/8081/8082` |
| 4.3 | VLAN 10: `Nginx -> olpub (read + admin)`; VLAN 30: `olpub -> DNS` |
| 4.4 | Block VMID 1007 (4 rule ở 4.2) |
| 4.8 | Host Override `read` |
| 5.1 | `overleaf`: Authelia **Có** (group `overleaf`, two_factor), mTLS **on**. Ghi lý do: Overleaf CE không có Sandboxed Compiles, người dùng chạy được lệnh trong container, nên chỉ thành viên tin cậy có thiết bị đã đăng ký mới được chạm tới. Thêm dòng `read`: `read.conf`, `http://10.0.30.57:8080`, không Authelia, không mTLS |
| 5.5 | Ngoại lệ: `read` dùng **Let's Encrypt**, gia hạn qua certbot với `server` của LE |
| 5.6 | Kết quả đúng của vòng `curl`: `overleaf` → **400** (không chứng chỉ thiết bị); `read` → 404 ở `/` |
| 8 | Rủi ro mới: Authelia là SPOF cho cả Overleaf; nội dung đã publish là công khai |
| Phụ lục B | Thêm `10.0.30.57` |
