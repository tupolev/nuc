# 🧠 Local LLM Stack (Ollama + Adapter + Prometheus + Grafana)

A fully local, production-style LLM serving stack with:

* OpenAI-compatible API
* Priority-based scheduler
* Observability (Prometheus + Grafana)
* SQLite-based API key management

---

# 🚀 Architecture

```
Client → Adapter (FastAPI)
        → Scheduler (priority + queues)
        → Ollama (LLM)

Metrics → Prometheus → Grafana
```

---

# 🧩 Components

* **Ollama** → runs LLM models locally
* **Adapter (FastAPI)** → OpenAI-compatible API + scheduler
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
* Streaming (SSE)
* Request cancellation
* Queue timeout
* Prometheus metrics (p95 latency)
* Grafana dashboards (auto-provisioned)
* No hardcoded secrets

---

# 📁 Project Structure

```
llm-stack/
├── docker-compose.yml
├── adapter/
│   ├── app.py
│   └── Dockerfile
├── data/
│   └── auth.db
├── prometheus/
│   └── prometheus.yml
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── datasource.yml
│       └── dashboards/
│           ├── dashboard.yml
│           └── llm.json
```

---

# 🚀 Quick Start

```bash
docker compose up -d
```

---

# 🌐 Endpoints

* API → http://localhost:4000
* Prometheus → http://localhost:9090
* Grafana → http://localhost:3001

---

# 🔐 API Keys (SQLite)

## 1. Create database

```bash
mkdir -p data
sqlite3 data/auth.db
```

## 2. Create table

```sql
CREATE TABLE api_keys (
  key TEXT PRIMARY KEY,
  priority TEXT
);
```

## 3. Insert keys

```sql
INSERT INTO api_keys VALUES ('maravillas123', 'medium');
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

## Chat

```bash
curl http://localhost:4000/v1/chat/completions \
-H "Authorization: Bearer maravillas123" \
-H "Content-Type: application/json" \
-d '{
  "messages":[{"role":"user","content":"Hello"}]
}'
```

---

## Priority Override

```bash
-H "X-Priority: high"
```

---

## Embeddings

```bash
curl http://localhost:4000/v1/embeddings \
-H "Authorization: Bearer batch123" \
-H "Content-Type: application/json" \
-d '{
  "input": "hello world"
}'
```

---

# 📊 Metrics

## JSON endpoint

```bash
curl http://localhost:4000/metrics
```

Includes:

* wait_p95
* latency_p95
* tokens_streamed
* queue sizes

---

## Prometheus endpoint

```bash
curl http://localhost:4000/metrics/prometheus
```

Example metrics:

```
llm_requests_total
llm_tokens_streamed_total
llm_wait_p95
llm_latency_p95
```

---

# 📈 Grafana Provisioning (Step-by-Step)

## 1. Datasource

Create:

`grafana/provisioning/datasources/datasource.yml`

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
```

---

## 2. Dashboard provider

Create:

`grafana/provisioning/dashboards/dashboard.yml`

```yaml
apiVersion: 1

providers:
  - name: default
    folder: ""
    type: file
    options:
      path: /etc/grafana/provisioning/dashboards
```

---

## 3. Dashboard definition

Create:

`grafana/provisioning/dashboards/llm.json`

```json
{
  "title": "LLM Metrics",
  "panels": [
    {
      "type": "graph",
      "title": "Requests Total",
      "targets": [
        { "expr": "llm_requests_total" }
      ]
    },
    {
      "type": "graph",
      "title": "Tokens Streamed",
      "targets": [
        { "expr": "llm_tokens_streamed_total" }
      ]
    }
  ]
}
```

---

## 4. Restart Grafana

```bash
docker compose restart grafana
```

---

# 🧪 Concurrency Test

```bash
(
curl ... low &
curl ... low &
curl ... low &

sleep 3

curl ... high &

wait
)
```

---

# 🧠 Scheduler Behavior

* Non-preemptive (like OpenAI / vLLM)
* Priority applied at queue level
* FIFO within same priority
* Chat and embeddings isolated

---

# 🐧 Run on Linux Startup (systemd)

## 1. Create service file

```bash
sudo nano /etc/systemd/system/llm-stack.service
```

---

## 2. Add content

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

## 3. Enable service

```bash
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable llm-stack
sudo systemctl start llm-stack
```

---

## 4. Check status

```bash
systemctl status llm-stack
```

---

# ⚠️ Current Limitations

* No rate limiting per API key
* No fairness / anti-starvation
* Single-node Ollama
* Basic dashboards only

---

# 🚀 Possible Improvements

* Rate limiting per API key
* Multi-node Ollama (load balancing)
* Advanced dashboards
* Admin API for key management
* Usage tracking per user

---

# 🏁 Status

```
✔ Production-ready local LLM stack
✔ Priority scheduler
✔ Observability included
✔ Fully reproducible
```

---

# 🧑‍💻 Notes

This stack is designed for:

* Local development
* Private AI infrastructure
* Controlled LLM serving environments
