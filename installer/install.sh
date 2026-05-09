#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Installing Local LLM Stack..."

REPO_URL="https://github.com/tupolev/nuc.git"
INSTALL_DIR="/opt/llm-stack"
STACK_DIR="${INSTALL_DIR}/llm-stack"

# =========================
# 1. System update
# =========================
echo "📦 Updating system..."
apt update && apt upgrade -y

# =========================
# 2. Install dependencies
# =========================
echo "🔧 Installing dependencies..."
apt install -y \
    git \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    sqlite3 \
    sudo

# =========================
# 3. Verify Ollama
# =========================
echo "🦙 Verifying Ollama..."
if ! command -v systemctl &> /dev/null; then
    echo "❌ systemctl is required, but was not found." >&2
    exit 1
fi

if ! systemctl cat ollama.service >/dev/null 2>&1; then
    echo "❌ Ollama is not installed or ollama.service is unavailable. Install Ollama before running this installer." >&2
    exit 1
fi

# =========================
# 4. Install Docker
# =========================
if ! command -v docker &> /dev/null; then
    echo "🐳 Installing Docker..."

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) \
      signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list

    apt update

    apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable docker
systemctl start docker

# =========================
# 5. Clone repo
# =========================
echo "📂 Cloning repository..."
rm -rf "$INSTALL_DIR"
git clone "$REPO_URL" "$INSTALL_DIR"

cd "$STACK_DIR"

# =========================
# 6. Create data + DB
# =========================
echo "🗄️ Setting up database..."
mkdir -p data

if [ ! -f .env ]; then
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
fi

sqlite3 data/auth.db <<EOF
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL DEFAULT 'medium'
);
EOF

# =========================
# 7. Install Ollama tuning
# =========================
echo "⚙️ Applying Ollama tuning..."
chmod +x "${STACK_DIR}/ollama/install-ollama-tuning.sh"
"${STACK_DIR}/ollama/install-ollama-tuning.sh"

# =========================
# 8. Build + start stack
# =========================
echo "🐳 Starting Docker stack..."
docker compose up -d --build

# =========================
# 9. Create systemd service (stack)
# =========================
echo "⚙️ Creating systemd service..."

cat <<EOF > /etc/systemd/system/llm-stack.service
[Unit]
Description=LLM Stack (Docker Compose)
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=$STACK_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
Restart=always
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

# =========================
# 10. Warmup script wrapper
# =========================
echo "🔥 Setting up Ollama warmup..."

cat <<'EOF' > /usr/local/bin/warm-ollama-wrapper.sh
#!/usr/bin/env bash

set -euo pipefail

MODELS=(
  "qwen2.5-coder:14b"
  "qwen2.5-coder:7b"
  "llama3-groq-tool-use:8b"
  "llama3.1:8b"
  "qwen3-coder"
  "deepseek-coder-v2:16b"
)

echo "Waiting for Ollama..."

until curl -s http://localhost:11434/api/tags > /dev/null; do
  sleep 2
done

echo "Ollama ready. Warming up..."

for model in "${MODELS[@]}"; do
  bash /opt/llm-stack/warm-ollama.sh "$model"
done

curl http://localhost:11434/api/embeddings \
  -d '{"model":"all-minilm","prompt":"warmup"}'
EOF

chmod +x /usr/local/bin/warm-ollama-wrapper.sh

# =========================
# 11. Warmup systemd service
# =========================
cat <<EOF > /etc/systemd/system/ollama-warmup.service
[Unit]
Description=Warm up Ollama models
After=llm-stack.service
Requires=llm-stack.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/warm-ollama-wrapper.sh

[Install]
WantedBy=multi-user.target
EOF

# =========================
# 12. Enable services
# =========================
echo "🔁 Enabling services..."

systemctl daemon-reexec
systemctl daemon-reload

systemctl enable llm-stack
systemctl enable ollama-warmup

systemctl start llm-stack
systemctl start ollama-warmup

# =========================
# DONE
# =========================
echo ""
echo "✅ Installation complete!"
echo ""
echo "🌐 OpenWebUI: http://localhost:8080"
echo "📡 API: http://localhost:4000/v1"
echo "📊 Grafana: http://localhost:3001"
echo ""
