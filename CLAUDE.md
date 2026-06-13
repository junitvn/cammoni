# moni-bot

Telegram bot ghi chép thu chi gia đình, lưu vào Google Sheets, phân loại bằng Gemini AI.

## Quy tắc làm việc

Sau mỗi lần sửa code, **luôn commit và push lên main** ngay mà không cần người dùng nhắc.

## Deploy

> **Lưu ý quan trọng**: Nếu `git push` thất bại do mạng bị chặn (Connection closed, Could not read from remote repository), **KHÔNG** copy file thủ công lên server. Chỉ báo cho người dùng biết để họ tự xử lý (đổi mạng, VPN, v.v.).

- **Oracle instance**: `ubuntu@155.248.181.32` (SSH key: `~/.ssh/oracle_bot`)
- **Deploy**: `ssh -i ~/.ssh/oracle_bot ubuntu@155.248.181.32 "cd moni-bot && git pull && docker compose up --build -d"`
- **Logs**: `docker logs moni-bot --tail 30`
- **Restart**: `docker compose restart`

## Stack

- Python 3.11, python-telegram-bot 21.10
- Google Sheets API (httpx trực tiếp, không dùng gspread runtime)
- Gemini AI (google-generativeai) với fallback chain: `gemini-3.1-flash-lite → gemini-3.5-flash → gemini-2.5-flash-lite`
- Matplotlib cho biểu đồ, Docker + docker-compose để deploy

## Cấu trúc file

| File | Vai trò |
|------|---------|
| `bot.py` | Entry point, toàn bộ Telegram handlers, routing |
| `sheets.py` | Google Sheets CRUD qua REST API (httpx async) |
| `parser.py` | Parse text message → transaction; `normalize_vn()`, `parse_amount_search()` |
| `classifier.py` | Phân loại category: tier-0 Config sheet, tier-1 keyword, tier-2 Gemini |
| `gemini_utils.py` | `generate_with_fallback()` — Gemini với model fallback khi quota 429 |
| `voice.py` | Transcribe voice → intent (record / search / budget / category_filter) |
| `stats.py` | Tính thống kê, format text, kiểm tra ngân sách |
| `charts.py` | Vẽ pie chart + bar chart bằng matplotlib, trả bytes PNG |
| `editor.py` | ConversationHandler `/edit` và `/search`: paging, 4-button actions |
| `budget.py` | ConversationHandler `/budget`: đặt/xem ngân sách |
| `users.py` | Cache tên user `{user_id: name}`, dùng chung giữa các module |
| `config/users.yaml` | Whitelist user IDs + tên hiển thị (nguồn chính để quản lý quyền) |
| `config/categories.yaml` | Không dùng runtime; seed categories trong `sheets.py._CATEGORIES_SEED` |

## Google Sheets schema

Sheet **Transactions** — cột A→J:
`id | timestamp | user | type | amount | category | description | auto_classified | user_name | excluded`

- `type`: `chi` hoặc `thu`
- `amount`: VND (số tiền × 1000)
- `auto_classified`: `Y`/`N`
- `excluded`: `Y` = không tính vào ngân sách/thống kê

Sheet **Budget**: `scope | limit_vnd | period` (scope = `chung` hoặc category key)
Sheet **Categories**: `key | name | emoji | income | keywords` (seed khi khởi động lần đầu)
Sheet **Users**: `user_id | name`
Sheet **Config**: `description | category` (learned mappings từ recat)

## Categories

`an_ngoai` 🍜 | `di_cho` 🛒 | `bat_buoc` 📌 | `y_te` 🏥 | `phuong_tien` 🚗 | `dau_tu` 📈 | `khac` 📦 | `luong` 💼 | `thu_khac` 💵

## Các tính năng chính

**Ghi chép**
- Text: `50 cơm`, `cơm 50`, `15. 50 cơm` (ngày 15), `15/6 50 cơm`, `15-6 50 cơm`
- Batch: `cơm 50, grab 30, điện 200`
- Voice: tự động nhận dạng intent (ghi / tìm / ngân sách / lọc category)
- Sau khi ghi: 4 nút — ✏️ Phân loại, 📅 Ngày, 🗑️ Xóa, 🚫 Không tính

**Tìm kiếm** (`/search`)
- Từ khoá: `/search cơm`, `/search an ngoai` (không dấu OK)
- Khoảng giá: `/search 200` (200k-299k), `/search <200`, `/search >50`, `/search 50-200`
- Category: `/search ăn ngoài`, `/search y te`
- Voice: nói "tìm cơm", "dưới 200k", "tìm khoảng 50"

**Thống kê** — `/month`, `/week`, `/today`, `/range`
- Nút **Danh sách**: xem text list → **✏️ Sửa** → chọn số thứ tự → edit item

**Ngân sách** — `/budget`, voice: "đặt ngân sách ăn ngoài 3 triệu"

## Quản lý user

Thêm user: sửa `config/users.yaml`, commit + deploy.
```yaml
users:
  1024403012: "Lâm"
  8778261847: "Tên"
```
Nếu set `ALLOWED_USERS` trong `.env` thì yaml bị bỏ qua.

## Env vars (`.env`)

```
BOT_TOKEN=...
GEMINI_API_KEY=...
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS_FILE=credentials.json
ALLOWED_USERS=1024403012,8778261847   # override users.yaml
USE_AI_FALLBACK=true                   # false = tắt Gemini classify
```
