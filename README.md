# olpub — publish PDF từ Overleaf CE

Xem `RUNBOOK.md` để triển khai.

| Thư mục | Nội dung | Triển khai tới |
|---|---|---|
| `publisher/` | `olpub_publisher.py`, `static/`, unit systemd, env mẫu | VM 1007 `host-publish` |
| `collector/` | `olpub_collector.py`, unit systemd, env mẫu | VM 1006 (host, ngoài container) |
| `nginx/` | `conf.d-olpub.conf`, `overleaf.conf` (mẫu), `read.conf` | VM 100 `nginx-prod-01` |
| `tests/` | `e2e_test.py` — chạy thử trên máy bất kỳ có qpdf, exiftool, poppler-utils, reportlab, pikepdf | — |

Chỉ dùng thư viện chuẩn Python ≥ 3.11 (Debian 12), cộng với `qpdf`, `libimage-exiftool-perl`, `poppler-utils`.
