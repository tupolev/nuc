#!/usr/bin/env bash

set -e

echo "🚀 Installing Local LLM Stack..."

REPO_URL="https://github.com/tupolev/nuc.git"
INSTALL_DIR="/opt/llm-stack"
SERVICE_NAME="llm-stack"

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
    sqlite3

# =========================
# 3. Install Docker
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
# 4. Clone repo
# =========================
echo "📂 Cloning repository..."
rm -rf $INSTALL_DIR
git clone $REPO_URL $INSTALL_DIR

cd $INSTALL_DIR/llm-stack

# =========================
# 5. Create data + DB
# =========================
echo "🗄️ Setting up database..."
mkdir -p data

sqlite3 data/auth.db <<EOF
CREATE TABLE IF NOT EXISTS api_keys (
  key TEXT PRIMARY KEY,
  priority TEXT
);

INSERT OR IGNORE INTO api_keys VALUES ('admin123', 'high');
INSERT OR IGNORE INTO api_keys VALUES ('test123', 'medium');
INSERT OR IGNORE INTO api_keys VALUES ('batch123', 'low');
EOF

# =========================
# 6. Build + start stack
# =========================
echo "🐳 Starting Docker stack..."
docker compose up -d --build

# =========================
# 7. Create systemd service (stack)
# =========================
echo "⚙️ Creating systemd service..."

cat <<EOF > /etc/systemd/system/llm-stack.service
[Unit]
Description=LLM Stack (Docker Compose)
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=$INSTALL_DIR/llm-stack
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
Restart=always
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

# =========================
# 8. Warmup script wrapper
# =========================
echo "🔥 Setting up Ollama warmup..."

cat <<'EOF' > /usr/local/bin/warm-ollama-wrapper.sh
#!/usr/bin/env bash

echo "Waiting for Ollama..."

until curl -s http://localhost:11434/api/tags > /dev/null; do
  sleep 2
done

echo "Ollama ready. Warming up..."

bash /opt/llm-stack/warm-ollama.sh
EOF

chmod +x /usr/local/bin/warm-ollama-wrapper.sh

# =========================
# 9. Warmup systemd service
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
# 10. Enable services
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
