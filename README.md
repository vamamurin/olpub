# olpub 1.1 — publish PDF từ Overleaf CE

| Tài liệu | Dùng khi |
|---|---|
| `RUNBOOK.md` | Runbook gốc 1.0: triển khai mới từ đầu (sau đó áp `UPGRADE-1.1.md`) |
| `UPGRADE-1.1.md` | Runbook sửa đổi: nâng cấp hệ thống đã dựng theo 1.0 lên 1.1, kèm quay lui |

| Thư mục | Nội dung | Triển khai tới |
|---|---|---|
| `publisher/` | `olpub_publisher.py`, `static/`, unit systemd, env mẫu | VM 1007 `host-publish` |
| `proxmox/` | `1007.fw` — Proxmox Firewall VM 1007 (chặn đường ra Internet) | `pve-control` |
| `collector/` | `olpub_collector.py`, unit systemd, env mẫu | VM 1006 (host, ngoài container) |
| `nginx/` | `conf.d-olpub.conf`, `overleaf.conf` (mẫu), `read.conf`, `00-default-ssl.conf` | VM 100 `nginx-prod-01` |
| `nginx/vm1007-*` | Nginx tĩnh + drop-in systemd | VM 1007 |
| `tests/` | `e2e_test.py` — chạy thử trên máy bất kỳ có nginx, qpdf, exiftool, poppler-utils, reportlab, pikepdf, Pillow | — |

Chỉ dùng thư viện chuẩn Python ≥ 3.11 (Debian 12), cộng với `qpdf`, `libimage-exiftool-perl`, `poppler-utils`; VM 1007 thêm `nginx`.
Mã nguồn và cấu hình trong gói này là bản **1.1**.
