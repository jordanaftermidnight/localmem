#!/usr/bin/env bash
# LOCALMEM Ubuntu Server Setup
# Run AFTER Claude Code is installed. Handles everything else:
# Python 3.14, uv, LOCALMEM install, data dirs, systemd service.
#
# Usage:
#   chmod +x deploy/setup-ubuntu.sh
#   sudo ./deploy/setup-ubuntu.sh [--no-service] [--install-dir /opt/localmem]
#
# Prerequisites (do these manually first):
#   sudo apt update && sudo apt install -y curl git
#   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
#   sudo apt install -y nodejs && sudo npm install -g @anthropic-ai/claude-code

set -euo pipefail

# --- Defaults ---
INSTALL_DIR="/opt/localmem"
DATA_DIR="/var/lib/localmem"
CONFIG_DIR="/etc/localmem"
LOG_DIR="/var/log/localmem"
SERVICE_USER="localmem"
PYTHON_VERSION="3.14"
INSTALL_SERVICE=true
ENABLE_AUTH=false
QDRANT_MODE="local"
QDRANT_URL=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-service)    INSTALL_SERVICE=false; shift ;;
        --install-dir)   INSTALL_DIR="$2"; shift 2 ;;
        --data-dir)      DATA_DIR="$2"; shift 2 ;;
        --python)        PYTHON_VERSION="$2"; shift 2 ;;
        --auth)          ENABLE_AUTH=true; shift ;;
        --qdrant-server) QDRANT_MODE="server"; QDRANT_URL="$2"; shift 2 ;;
        -h|--help)
            cat <<HELP
Usage: sudo $0 [options]

  --no-service            Skip systemd unit install
  --install-dir DIR       Install dir (default: /opt/localmem)
  --data-dir DIR          Data dir (default: /var/lib/localmem)
  --python VERSION        Python version (default: 3.14)
  --auth                  Generate API key, enable dashboard auth
  --qdrant-server URL     Use remote Qdrant at URL (default: local mode)
HELP
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# --- Checks ---
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run with sudo"
    exit 1
fi

echo "=== LOCALMEM Ubuntu Setup ==="
echo "  Install dir:  $INSTALL_DIR"
echo "  Data dir:     $DATA_DIR"
echo "  Config dir:   $CONFIG_DIR"
echo "  Python:       $PYTHON_VERSION"
echo "  Systemd:      $INSTALL_SERVICE"
echo ""

# --- Detect Ubuntu version ---
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    UBUNTU_VERSION="${VERSION_ID:-unknown}"
    echo "Detected: $PRETTY_NAME"
else
    echo "WARNING: Cannot detect OS version, proceeding anyway"
    UBUNTU_VERSION="unknown"
fi

# --- 1. System packages ---
echo ""
echo "--- [1/6] System packages ---"
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential \
    libsqlite3-dev \
    libffi-dev \
    libssl-dev \
    curl \
    git \
    software-properties-common

# --- 2. Python ---
echo ""
echo "--- [2/6] Python $PYTHON_VERSION ---"

# Check if already installed
if command -v "python${PYTHON_VERSION}" &>/dev/null; then
    echo "python${PYTHON_VERSION} already installed: $(python${PYTHON_VERSION} --version)"
else
    # Ubuntu 26.04 ships 3.14 natively; older versions need deadsnakes
    if dpkg -l "python${PYTHON_VERSION}" &>/dev/null 2>&1; then
        echo "Available from system repos"
    else
        echo "Adding deadsnakes PPA..."
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -qq
    fi
    apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" \
        "python${PYTHON_VERSION}-venv" \
        "python${PYTHON_VERSION}-dev"
fi

PYTHON_BIN="python${PYTHON_VERSION}"
echo "Using: $($PYTHON_BIN --version)"

# --- 3. uv (fast Python package manager) ---
echo ""
echo "--- [3/6] uv ---"
if command -v uv &>/dev/null; then
    echo "uv already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin by default — for root that's /root/.local/bin
    export PATH="/root/.local/bin:$PATH"
    echo "Installed: $(uv --version)"
fi

# --- 4. Service user & directories ---
echo ""
echo "--- [4/6] User & directories ---"

if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$DATA_DIR" "$SERVICE_USER"
    echo "Created user: $SERVICE_USER"
else
    echo "User $SERVICE_USER already exists"
fi

mkdir -p "$INSTALL_DIR" "$DATA_DIR"/{qdrant,manifests} "$CONFIG_DIR" "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"

# --- 5. Install LOCALMEM ---
echo ""
echo "--- [5/6] LOCALMEM ---"

# Create venv in install dir
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    $PYTHON_BIN -m venv "$INSTALL_DIR/.venv"
    echo "Created venv at $INSTALL_DIR/.venv"
fi

VENV_PIP="$INSTALL_DIR/.venv/bin/pip"
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"

# Install from project source (editable if project dir exists, otherwise from current dir)
if [[ -f "$PROJECT_DIR/pyproject.toml" ]]; then
    echo "Installing from source: $PROJECT_DIR"
    $VENV_PIP install --upgrade pip
    $VENV_PIP install "$PROJECT_DIR"
else
    echo "ERROR: pyproject.toml not found at $PROJECT_DIR"
    echo "       Run this script from the localmem repo, or pass --install-dir"
    exit 1
fi

# Generate API key + prune env file when --auth is requested
if [[ "$ENABLE_AUTH" == true ]]; then
    if [[ ! -f "$CONFIG_DIR/prune.env" ]]; then
        # Pure-stdlib token: 48 hex chars from /dev/urandom
        API_KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
        umask 0177
        cat > "$CONFIG_DIR/prune.env" <<ENV
# Generated by setup-ubuntu.sh — read by localmem-prune systemd unit and
# interpolated into localmem.yaml via \${LOCALMEM_API_KEY} substitution.
LOCALMEM_API_KEY=$API_KEY
ENV
        chown root:"$SERVICE_USER" "$CONFIG_DIR/prune.env"
        chmod 0640 "$CONFIG_DIR/prune.env"
        echo "API key written to $CONFIG_DIR/prune.env (mode 0640)"
    else
        echo "$CONFIG_DIR/prune.env already exists (keeping existing key)"
    fi
fi

# Deploy config
if [[ ! -f "$CONFIG_DIR/localmem.yaml" ]]; then
    cat > "$CONFIG_DIR/localmem.yaml" <<YAML
server:
  host: "127.0.0.1"
  port: 8781
  transport: "sse"

storage:
  base_path: "$DATA_DIR"
  qdrant_mode: "$QDRANT_MODE"
  qdrant_path: "$DATA_DIR/qdrant"
  qdrant_url: "$QDRANT_URL"
  sqlite_path: "$DATA_DIR/localmem.db"
  graph_path: "$DATA_DIR/graph.json"

embedding:
  model: "all-MiniLM-L6-v2"
  sparse_model: "Qdrant/bm25"
  device: "cpu"

loading:
  l1_top_k: 15
  l1_max_tokens: 120
  decay_rate_per_day: 0.01

graph:
  persistence_debounce_seconds: 5
  max_community_size: 50

concurrency:
  write_timeout_seconds: 30

logging:
  level: "INFO"
  format: "json"
  file: "$LOG_DIR/localmem.log"
  max_bytes: 10000000
  backup_count: 3

dashboard:
  enabled: true
  host: "127.0.0.1"
  port: 8782
  auth_enabled: $ENABLE_AUTH
  api_key: "\${LOCALMEM_API_KEY}"
YAML
    chmod 0640 "$CONFIG_DIR/localmem.yaml"
    echo "Config written to $CONFIG_DIR/localmem.yaml"
else
    echo "Config already exists at $CONFIG_DIR/localmem.yaml (skipping)"
fi

# Copy manifests
if [[ -d "$PROJECT_DIR/manifests" ]]; then
    cp -n "$PROJECT_DIR/manifests/"*.yaml "$DATA_DIR/manifests/" 2>/dev/null || true
    echo "Manifests deployed to $DATA_DIR/manifests/"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

# --- 6. Systemd service ---
echo ""
echo "--- [6/6] Systemd service ---"

if [[ "$INSTALL_SERVICE" == true ]]; then
    cp "$SCRIPT_DIR/localmem.service" /etc/systemd/system/localmem.service

    # Patch paths into the unit file
    sed -i "s|__INSTALL_DIR__|$INSTALL_DIR|g" /etc/systemd/system/localmem.service
    sed -i "s|__CONFIG_DIR__|$CONFIG_DIR|g" /etc/systemd/system/localmem.service
    sed -i "s|__DATA_DIR__|$DATA_DIR|g" /etc/systemd/system/localmem.service
    sed -i "s|__LOG_DIR__|$LOG_DIR|g" /etc/systemd/system/localmem.service
    sed -i "s|__SERVICE_USER__|$SERVICE_USER|g" /etc/systemd/system/localmem.service

    systemctl daemon-reload
    systemctl enable localmem.service
    echo "Service installed and enabled"
    echo ""
    echo "Start with:   sudo systemctl start localmem"
    echo "Status:       sudo systemctl status localmem"
    echo "Logs:         sudo journalctl -u localmem -f"
else
    echo "Skipped (--no-service)"
    echo ""
    echo "Manual run:"
    echo "  $INSTALL_DIR/.venv/bin/localmem-serve $CONFIG_DIR/localmem.yaml"
fi

# --- Done ---
echo ""
echo "=== Setup complete ==="
echo ""
echo "localmem v0.1.2"
echo "  Binary:  $INSTALL_DIR/.venv/bin/localmem-serve"
echo "  Config:  $CONFIG_DIR/localmem.yaml"
echo "  Data:    $DATA_DIR/"
echo "  Logs:    $LOG_DIR/ + journalctl -u localmem"
echo "  Qdrant:  $QDRANT_MODE${QDRANT_URL:+ ($QDRANT_URL)}"
if [[ "$ENABLE_AUTH" == true ]]; then
    echo "  Auth:    enabled (key in $CONFIG_DIR/prune.env)"
fi
echo ""
echo "Quick test:"
echo "  curl -s http://127.0.0.1:8781/sse"
echo ""
