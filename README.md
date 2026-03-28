# 🧠 Local LLM Stack (Ollama + Adapter + Prometheus + Grafana)

A fully local, production-style LLM serving stack with:

* OpenAI-compatible API
* Priority-based scheduler
* Observability (Prometheus + Grafana)
* SQLite-based API key management
* TLS via Caddy (local domain: `nuc`)
* Automatic model warmup

---

# 🚀 Architecture

```
Client → Caddy (TLS)
       → Adapter (FastAPI)
       → Scheduler (priority + queues)
       → Ollama (LLM)

Metrics → Prometheus → Grafana
```

---

# ⚡ One-Command Installation (Ubuntu)

Install everything on a clean Ubuntu machine:

```bash
curl -fsSL https://raw.githubusercontent.com/tupolev/nuc/main/install.sh | bash
```

---

## 🧠 What the installer does

The script will:

1. Update the system
2. Install dependencies (git, curl, sqlite3…)
3. Install Docker + Compose
4. Clone repo into:

```
/opt/llm-stack
```

5. Create SQLite DB + API keys
6. Build and start containers
7. Configure systemd services:

   * `llm-stack.service`
   * `ollama-warmup.service`
8. Enable auto-start on boot
9. Warm up Ollama models

---

# 🌐 Endpoints

| Service      | URL                                                  |
| ------------ | ---------------------------------------------------- |
| OpenWebUI    | [http://localhost:8080](http://localhost:8080)       |
| API (direct) | [http://localhost:4000/v1](http://localhost:4000/v1) |
| API (TLS)    | [https://nuc/v1](https://nuc/v1)                     |
| Prometheus   | [http://localhost:9090](http://localhost:9090)       |
| Grafana      | [http://localhost:3001](http://localhost:3001)       |

---

# 🧩 Components

* **Ollama** → runs LLM models locally
* **Adapter (FastAPI)** → OpenAI-compatible API + scheduler
* **Caddy** → TLS + reverse proxy
* **Prometheus** → metrics scraping
* **Grafana** → dashboards
* **SQLite (`auth.db`)** → API keys + priorities

---

# ⚙️ Features

* Priority queue (high / medium / low)
* Separate queues:

  * chat
  * embeddings
* Concurrency control
* Streaming (SSE, OpenAI-compatible)
* Request cancellation
* Queue timeout
* Prometheus metrics (p95 latency)
* Grafana dashboards
* Automatic model warmup
* Fully local (no external APIs)

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
├── Caddyfile
```

---

# 🔐 API Keys (SQLite)

## Default keys

| Key      | Priority |
| -------- | -------- |
| admin123 | high     |
| test123  | medium   |
| batch123 | low      |

---

## Manual setup (optional)

```bash
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

## Chat

```bash
curl https://nuc/v1/chat/completions \
-H "Authorization: Bearer test123" \
-H "Content-Type: application/json" \
-d '{
  "model":"qwen2.5-coder:7b",
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
curl https://nuc/v1/embeddings \
-H "Authorization: Bearer batch123" \
-H "Content-Type: application/json" \
-d '{
  "input": "hello world"
}'
```

---

# 📊 Metrics

## JSON

```bash
curl http://localhost:4000/metrics
```

---

## Prometheus

```bash
curl http://localhost:4000/metrics/prometheus
```

---

## Prometheus target

```
http://adapter:4000/metrics/prometheus
```

---

# 📈 Grafana

## Access

```
http://localhost:3001
```

---

## Important note

Use **rate() / increase()** for counters:

Example:

```
rate(llm_requests_total[1m])
```

---

# 🔥 Ollama Warmup

Automatically executed on boot.

## Wrapper

```
/usr/local/bin/warm-ollama-wrapper.sh
```

### Behavior

1. Waits for Ollama readiness
2. Executes:

```
/opt/llm-stack/warm-ollama.sh
```

---

## Benefits

* Faster first token
* No cold start
* Embeddings preloaded

---

# 🐧 systemd Services

## LLM stack

```bash
systemctl status llm-stack
```

---

## Warmup

```bash
systemctl status ollama-warmup
```

---

## Restart

```bash
sudo systemctl restart llm-stack
```

---

## Logs

```bash
journalctl -u llm-stack -f
journalctl -u ollama-warmup -f
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

* Non-preemptive
* Priority-based queue
* FIFO within same priority
* Chat & embeddings isolated

---

# ⚠️ Limitations

* No per-key rate limiting
* No anti-starvation
* Single-node Ollama

---

# 🚀 Possible Improvements

* Rate limiting per API key
* Multi-node Ollama
* Better dashboards
* Admin UI for keys
* Usage tracking

---

# 🧼 Uninstall

```bash
sudo systemctl stop llm-stack
sudo systemctl disable llm-stack
sudo rm -rf /opt/llm-stack
```

---

# 🏁 Status

```
✔ Fully local LLM stack
✔ OpenAI-compatible API
✔ TLS reverse proxy
✔ Observability (Prometheus + Grafana)
✔ Auto-start on boot
✔ Warmed models
✔ Production-ready
```

---

# 🧑‍💻 Notes

Designed for:

* Local development
* Private AI infra
* Self-hosted LLM serving
* High-control environments

---

Si quieres el siguiente salto natural ahora mismo sería:

👉 convertir esto en un **installer tipo producto (CLI + config + upgrades)** o
👉 añadir **multi-model routing + fallback automático** 😏
