# UR Vacancy Monitor / UR 房源监控 / UR空室モニター

[中文](#中文) · [日本語](#日本語) · [English](#english)

Lightweight, self-hosted monitor for vacancy changes on UR rental property pages. Built with Python's standard library, SQLite, SMTP, and Docker.

Docker Hub: [`zhanggqdocker/ur-monitor`](https://hub.docker.com/r/zhanggqdocker/ur-monitor)

> This independent project is not affiliated with UR都市機構. UR availability may not be real-time; always confirm on the official detail page or with a UR office.

---

## 中文

### 功能

- 定时检查一个或多个 UR 物件页面，默认每 10 分钟一次
- 新房源、重新上架或在架房源资料变化时发送邮件
- 仅下架不发邮件，但完整保存下架履历
- 通知包含变化房源、当前全部房源及详情链接
- SMTP 暂时失败时将邮件保存在 SQLite，下一轮自动重试
- SQLite 保存当前状态、每次检查和每次上下架/资料变化
- 提供网页面板、`/healthz` 健康检查和 JSON 历史接口
- 支持租金、户型筛选以及日志轮转
- 只使用 Python 标准库；镜像支持 `linux/amd64` 和 `linux/arm64`

### 使用 Docker Hub 镜像部署（NAS 推荐）

保存 [`docker-compose.nas.yml`](docker-compose.nas.yml) 和 [`.env.example`](.env.example)，然后：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写邮件配置，并使用：

```dotenv
DOCKER_IMAGE=zhanggqdocker/ur-monitor:latest
```

```bash
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
docker compose -f docker-compose.nas.yml logs -f
```

打开 `http://NAS-IP:8080`。数据库和日志位于 named volume `ur-monitor-data`，更新容器不会丢失。

### 从源码运行

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f ur-monitor
```

源码部署的数据保存在 `./data`。

### 邮件配置

项目支持标准 SMTP。Resend 示例：

```dotenv
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_SSL=false
SMTP_USER=resend
SMTP_PASSWORD=re_your_api_key
EMAIL_FROM=UR Monitor <notify@your-verified-domain.example>
EMAIL_TO=you@example.com
```

也可以使用 Gmail、Outlook 或 NAS 自带 SMTP。多个收件人用英文逗号分隔。不要提交真实的 `.env`；它已包含在 `.gitignore` 和 `.dockerignore` 中。

### 检查、筛选和首次通知

```dotenv
UR_URLS=https://www.ur-net.go.jp/chintai/kanto/saitama/50_4090.html
CHECK_INTERVAL_SECONDS=600
NOTIFY_ON_FIRST_RUN=false
MIN_RENT=
MAX_RENT=100000
LAYOUTS=1DK,1LDK
```

多个 URL 和户型用英文逗号分隔。检查间隔最少 300 秒。默认首次运行只建立基准；设为 `NOTIFY_ON_FIRST_RUN=true` 可在首次检查时发送当前房源。

### 测试与接口

```bash
docker compose run --rm ur-monitor python app.py --test-email
docker compose run --rm ur-monitor python app.py --check-once
curl http://127.0.0.1:8080/healthz
```

- 当前面板：`http://HOST:8080/`
- 变化履历：`http://HOST:8080/api/events`
- 检查履历：`http://HOST:8080/api/checks`

事件类型为 `baseline`、`added`、`removed` 和 `updated`。日志默认写入 `/data/ur-monitor.log`，达到 5 MB 后轮转，并保留 3 个备份。

---

## 日本語

### 主な機能

- 1件または複数のUR賃貸住宅ページを定期確認（既定は10分間隔）
- 新規掲載、再掲載、掲載中の部屋情報変更をメール通知
- 掲載終了だけの場合は通知せず、履歴には保存
- 通知には変更対象、現在の全空室、各詳細ページへのリンクを掲載
- SMTP送信に失敗したメールはSQLiteに保存し、次回に自動再送
- 現在状態、全チェック、掲載・終了・情報変更の履歴をSQLiteに保存
- Webダッシュボード、`/healthz`、JSON履歴APIを提供
- 家賃・間取りフィルター、ログローテーション対応
- Python標準ライブラリのみ使用。イメージは `linux/amd64` と `linux/arm64` に対応

### Docker HubからNASへ導入（推奨）

[`docker-compose.nas.yml`](docker-compose.nas.yml) と [`.env.example`](.env.example) を保存し、次を実行します。

```bash
cp .env.example .env
```

`.env` のメール設定を入力し、`DOCKER_IMAGE=zhanggqdocker/ur-monitor:latest` を指定します。

```bash
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
docker compose -f docker-compose.nas.yml logs -f
```

`http://NAS-IP:8080` を開いてください。データベースとログは named volume `ur-monitor-data` に保存され、コンテナ更新後も保持されます。

### ソースから起動

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f ur-monitor
```

標準SMTPに対応し、Resend、Gmail、Outlook、NASのSMTPなどを利用できます。設定例は中国語セクションと [`.env.example`](.env.example) を参照してください。複数宛先はカンマ区切りです。

`UR_URLS` は複数指定可能です。`CHECK_INTERVAL_SECONDS` の既定値は600秒、最小値は300秒です。`MIN_RENT`、`MAX_RENT`、`LAYOUTS` は任意です。初回から通知する場合は `NOTIFY_ON_FIRST_RUN=true` を設定します。

### テストとAPI

```bash
docker compose run --rm ur-monitor python app.py --test-email
docker compose run --rm ur-monitor python app.py --check-once
curl http://127.0.0.1:8080/healthz
```

- ダッシュボード：`http://HOST:8080/`
- 変更履歴：`http://HOST:8080/api/events`
- チェック履歴：`http://HOST:8080/api/checks`

イベント種別は `baseline`、`added`、`removed`、`updated` です。実際の空室状況はUR公式ページまたは営業窓口で必ず確認してください。

---

## English

### Features

- Polls one or more UR rental property pages; the default interval is 10 minutes
- Emails on newly listed, relisted, or modified active rooms
- Records removals in history without sending removal-only notifications
- Includes changed rooms, the complete current inventory, and detail links in email
- Persists failed SMTP messages in SQLite and retries them on the next cycle
- Stores current state, every check, and every listing transition in SQLite
- Provides a web dashboard, `/healthz`, and JSON history APIs
- Supports rent/layout filters and rotating logs
- Uses only the Python standard library; images support `linux/amd64` and `linux/arm64`

### Deploy from Docker Hub (recommended for NAS)

Save [`docker-compose.nas.yml`](docker-compose.nas.yml) and [`.env.example`](.env.example), then run:

```bash
cp .env.example .env
```

Fill in the SMTP settings and set `DOCKER_IMAGE=zhanggqdocker/ur-monitor:latest`.

```bash
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
docker compose -f docker-compose.nas.yml logs -f
```

Open `http://NAS-IP:8080`. The database and logs are stored in the `ur-monitor-data` named volume and survive container updates.

### Run from source

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f ur-monitor
```

Source deployments persist data under `./data`. Any standard SMTP provider can be used; see the Resend example above and [`.env.example`](.env.example). Separate multiple recipients with commas.

`UR_URLS` accepts comma-separated URLs. `CHECK_INTERVAL_SECONDS` defaults to 600 seconds and is clamped to a minimum of 300 seconds. Filters are optional. Set `NOTIFY_ON_FIRST_RUN=true` to email the initial baseline. Never commit the real `.env`.

### Tests and endpoints

```bash
docker compose run --rm ur-monitor python app.py --test-email
docker compose run --rm ur-monitor python app.py --check-once
curl http://127.0.0.1:8080/healthz
```

- Dashboard: `http://HOST:8080/`
- Change history: `http://HOST:8080/api/events`
- Check history: `http://HOST:8080/api/checks`

Event types are `baseline`, `added`, `removed`, and `updated`. Logs default to `/data/ur-monitor.log`, rotate at 5 MB, and retain three backups.

## License

MIT
