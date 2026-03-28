# 🧠 Local LLM Stack (Ollama + Adapter + Caddy + Prometheus + Grafana)

A fully local, production-style LLM serving stack with:

* OpenAI-compatible API (✅ streaming + SSE fixed)
* Priority-based scheduler
* Reverse proxy with TLS (Caddy)
* Observability (Prometheus + Grafana)
* SQLite-based API key management
* Automatic Ollama warmup on boot

---

# 🚀 Architecture

```
Client → Caddy (TLS + routing)
        → Adapter (FastAPI, OpenAI-compatible)
        → Scheduler (priority + queues)
        → Ollama (LLM)

Metrics → Prometheus → Grafana
```

---

# 🧩 Components

* **Ollama** → runs LLM models locally
* **Adapter (FastAPI)** → OpenAI-compatible API + scheduler
* **Caddy** → reverse proxy + HTTPS (mkcert or real certs)
* **Prometheus** → metrics scraping
* **Grafana** → visualization dashboards
* **SQLite (`auth.db`)** → API keys + priorities

---

# ⚙️ Features

* Priority queue (high / medium / low)
* Separate queues:

  * chat
  * embeddings
* Concurrency control
* ✅ OpenAI-compatible streaming (SSE fixed)
* Request cancellation
* Queue timeout
* Prometheus metrics (p95 latency)
* Grafana dashboards (auto-provisioned)
* TLS via Caddy
* Works with OpenWebUI / SDKs / curl

---

# 📁 Project Structure

```
llm-stack/
├── docker-compose.yml
├── Caddyfile
├── adapter/
│   ├── app.py
│   └── Dockerfile
├── data/
│   └── auth.db
├── certs/
│   └── (mkcert or real certs)
├── prometheus/
│   └── prometheus.yml
├── grafana/
│   └── provisioning/
```

---

# 🚀 Quick Start

```bash
docker compose up -d --build
```

---

# 🌐 Endpoints

### Local

* API → http://localhost:4000
* OpenWebUI → http://localhost:8080
* Prometheus → http://localhost:9090
* Grafana → http://localhost:3001

### Via Caddy (recommended)

* API → https://nuc/v1/*
* UI → https://nuc

---

# 🔐 API Keys (SQLite)

```bash
mkdir -p data
sqlite3 data/auth.db
```

```sql
CREATE TABLE api_keys (
  key TEXT PRIMARY KEY,
  priority TEXT
);

INSERT INTO api_keys VALUES ('test123', 'medium');
INSERT INTO api_keys VALUES ('admin123', 'high');
INSERT INTO api_keys VALUES ('batch123', 'low');
```

---

# 🧠 Priority Levels

| Level  | Value | Use case         |
| ------ | ----- | ---------------- |
| high   | 0     | UI / interactive |
| medium | 1     | normal usage     |
| low    | 2     | batch jobs       |

---

# 📡 API Usage

## Chat (OpenAI-compatible)

```bash
curl https://nuc/v1/chat/completions \
-H "Authorization: Bearer test123" \
-H "Content-Type: application/json" \
-d '{
  "model": "qwen2.5-coder:7b",
  "messages":[{"role":"user","content":"Hello"}]
}'
```

---

## Embeddings

```bash
curl https://nuc/v1/embeddings \
-H "Authorization: Bearer batch123" \
-H "Content-Type: application/json" \
-d '{"input":"hello world"}'
```

---

# 🔥 Caddy Configuration

`Caddyfile`:

```caddy
nuc {

    tls /certs/fullchain.pem /certs/nuc-key.pem

    handle /v1/* {
        reverse_proxy adapter:4000 {
            transport http {
                versions 1.1
            }
        }
    }

    handle /metrics {
        reverse_proxy adapter:4000
    }

    handle {
        reverse_proxy open-webui:8080
    }
}
```

---

# 🔐 TLS Certificates (mkcert)

```bash
mkcert -install
mkcert nuc
```

Move files to:

```
certs/
```

---

# 📊 Metrics

```bash
curl http://localhost:4000/metrics
curl http://localhost:4000/metrics/prometheus
```

---

# 🐧 Systemd Services (FULL SETUP)

## 1️⃣ LLM Stack (Docker Compose)

```bash
sudo nano /etc/systemd/system/llm-stack.service
```

```ini
[Unit]
Description=LLM Stack (Docker Compose)
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/path/to/llm-stack
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
Restart=always
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

---

## 2️⃣ Ollama Warmup Script

Script (`warm-ollama.sh` already exists)

---

## 3️⃣ Wrapper Script

```bash
nano /usr/local/bin/warm-ollama-wrapper.sh
```

```bash
#!/bin/bash

echo "Waiting for Ollama..."

until curl -s http://localhost:11434/api/tags > /dev/null; do
  sleep 2
done

echo "Ollama ready. Warming up..."

/path/to/warm-ollama.sh
```

```bash
chmod +x /usr/local/bin/warm-ollama-wrapper.sh
```

---

## 4️⃣ systemd Warmup Service

```bash
sudo nano /etc/systemd/system/ollama-warmup.service
```

```ini
[Unit]
Description=Warm up Ollama models
After=network.target docker.service llm-stack.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/warm-ollama-wrapper.sh

[Install]
WantedBy=multi-user.target
```

---

## 5️⃣ Enable Everything

```bash
sudo systemctl daemon-reexec
sudo systemctl daemon-reload

sudo systemctl enable llm-stack
sudo systemctl enable ollama-warmup

sudo systemctl start llm-stack
sudo systemctl start ollama-warmup
```

---

## 6️⃣ Debug

```bash
journalctl -u ollama-warmup.service -f
docker logs llm-stack-adapter-1 -f
```

---

# 🧪 Testing

```bash
curl https://nuc/v1/chat/completions ...
```

---

# 🏁 Status

```
✔ Fully working OpenAI-compatible local API
✔ Streaming + WebUI working
✔ TLS + reverse proxy
✔ Observability stack
✔ Auto warmup on boot
```
