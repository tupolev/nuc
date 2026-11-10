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
# 3. Verify systemd
# =========================
echo "⚙️ Verifying systemd..."
if ! command -v systemctl &> /dev/null; then
    echo "❌ systemctl is required, but was not found." >&2
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
# 7. Build + start stack
# =========================
echo "🐳 Starting Docker stack..."
docker compose up -d --build --wait --remove-orphans

# =========================
# 8. Create systemd service (stack)
# =========================
echo "⚙️ Creating systemd service..."

cat <<EOF > /etc/systemd/system/llm-stack.service
[Unit]
Description=LLM Stack (Docker Compose)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$STACK_DIR
ExecStart=/usr/bin/docker compose up -d --wait --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

# =========================
# 9. Enable services
# =========================
echo "🔁 Enabling services..."

systemctl daemon-reexec
systemctl daemon-reload

systemctl enable llm-stack

systemctl start llm-stack

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
