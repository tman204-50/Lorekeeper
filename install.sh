#!/usr/bin/env bash
# Lorekeeper 1-command installer
#
#   curl -fsSL https://raw.githubusercontent.com/tman204-50/Lorekeeper/main/install.sh | bash
#
# Installs:
#   1. Node service  -> ~/.local/share/lorekeeper (git clone + npm install)
#   2. systemd unit  -> lorekeeper.service (user or system)
#   3. Hermes plugin -> $HERMES_HOME/plugins/lorekeeper/
#   4. Activates     -> memory.provider = lorekeeper
#
# Requirements: node >= 22, npm, git, python3, hermes CLI on PATH.
# Optional:     ollama with nomic-embed-text (falls back to openai embedder).

set -euo pipefail

# --- resolve HERMES_HOME -----------------------------------------------------
if [ -n "${HERMES_HOME:-}" ]; then
  HERMES_HOME="${HERMES_HOME}"
elif [ -d "$HOME/.hermes" ]; then
  HERMES_HOME="$HOME/.hermes"
else
  echo "!! Cannot find Hermes home (expected ~/.hermes or \$HERMES_HOME)." >&2
  echo "   Install Hermes Agent first: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" >&2
  exit 1
fi

REPO_URL="${LOREKEEPER_REPO_URL:-https://github.com/tman204-50/Lorekeeper.git}"
REPO_REF="${LOREKEEPER_REPO_REF:-main}"
INSTALL_DIR="${LOREKEEPER_INSTALL_DIR:-$HOME/.local/share/lorekeeper}"
DATA_DIR="${LOREKEEPER_DATA_DIR:-$HERMES_HOME/lorekeeper}"
PORT="${LOREKEEPER_PORT:-18777}"
SERVICE_NAME="lorekeeper"

log()  { printf '\033[1;32m[lorekeeper]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[lorekeeper]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[lorekeeper]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

# --- prereqs -----------------------------------------------------------------
command -v node >/dev/null 2>&1 || die "node not found (need >= 22)"
command -v npm  >/dev/null 2>&1 || die "npm not found"
command -v git  >/dev/null 2>&1 || die "git not found"
command -v hermes >/dev/null 2>&1 || die "hermes CLI not found on PATH"
NODE_MAJOR=$(node -e "console.log(process.version.split('.')[0].replace('v',''))" 2>/dev/null || echo 0)
if [ "$NODE_MAJOR" -lt 22 ]; then
  die "node >= 22 required (found $(node --version))"
fi

log "Hermes home : $HERMES_HOME"
log "Install dir : $INSTALL_DIR"
log "Data dir    : $DATA_DIR"

# --- 1. clone + npm install --------------------------------------------------
if [ ! -d "$INSTALL_DIR/.git" ]; then
  log "Cloning $REPO_URL @ $REPO_REF"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
else
  log "Repo exists at $INSTALL_DIR — pulling latest"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF" 2>/dev/null || true
  git -C "$INSTALL_DIR" checkout -q "$REPO_REF" 2>/dev/null || true
  git -C "$INSTALL_DIR" pull -q --ff-only 2>/dev/null || true
fi

log "npm install (LanceDB native deps — may take a minute)"
(cd "$INSTALL_DIR" && npm install --no-audit --no-fund >/dev/null 2>&1) || die "npm install failed"

# --- 2. data dir + token -----------------------------------------------------
mkdir -p "$DATA_DIR"

# --- 3. systemd unit ---------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  UNIT="$UNIT_DIR/lorekeeper.service"
  cat > "$UNIT" <<EOF
[Unit]
Description=Lorekeeper memory service (LanceDB long-term memory for Hermes)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$(command -v node) server/index.js
Environment=HOME=$HOME
Environment=LOREKEEPER_PORT=$PORT
Environment=LOREKEEPER_DB_PATH=$DATA_DIR/lancedb
Environment=LOREKEEPER_GRAPH_PATH=$DATA_DIR/graph.db
Environment=LOREKEEPER_TOKEN_PATH=$DATA_DIR/token
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME" >/dev/null 2>&1 || warn "systemd enable failed (will try direct start)"
  sleep 2
  if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    log "Service running via systemd (user unit)"
  else
    warn "systemd unit inactive — starting directly instead"
    (cd "$INSTALL_DIR" && nohup node server/index.js >/dev/null 2>&1 &)
  fi
else
  warn "No systemd — starting service directly"
  (cd "$INSTALL_DIR" && nohup node server/index.js >/dev/null 2>&1 &)
fi

# --- 4. wait for health ------------------------------------------------------
TOKEN_FILE="$DATA_DIR/token"
log "Waiting for service on 127.0.0.1:$PORT ..."
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
TOKEN=""
if [ -f "$TOKEN_FILE" ]; then TOKEN=$(cat "$TOKEN_FILE"); fi
if [ -z "$TOKEN" ]; then
  warn "Service healthy but no token file yet"
fi

# --- 5. install Hermes plugin -------------------------------------------------
PLUGIN_DIR="$HERMES_HOME/plugins/lorekeeper"
log "Installing Hermes provider -> $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp "$INSTALL_DIR/provider/__init__.py" "$PLUGIN_DIR/"
cp "$INSTALL_DIR/provider/_client.py" "$PLUGIN_DIR/"
cp "$INSTALL_DIR/provider/plugin.yaml" "$PLUGIN_DIR/"

# --- 5b. install usage skill -------------------------------------------------
SKILL_DIR="$HERMES_HOME/skills/lorekeeper-usage"
if [ -d "$INSTALL_DIR/skills/lorekeeper-usage" ]; then
  log "Installing usage skill -> $SKILL_DIR"
  mkdir -p "$SKILL_DIR"
  cp "$INSTALL_DIR/skills/lorekeeper-usage/SKILL.md" "$SKILL_DIR/"
else
  warn "No skills/lorekeeper-usage in repo — skipping skill install"
fi

# --- 6. configure Hermes ------------------------------------------------------
log "Activating memory.provider = lorekeeper"
hermes config set memory.provider lorekeeper --force >/dev/null 2>&1 || \
  warn "Could not set memory.provider automatically — set it with: hermes config set memory.provider lorekeeper"

# --- 7. write lorekeeper.json -------------------------------------------------
if [ ! -f "$HERMES_HOME/lorekeeper.json" ]; then
  cat > "$HERMES_HOME/lorekeeper.json" <<EOF
{
  "host": "http://127.0.0.1:$PORT",
  "token": "$TOKEN"
}
EOF
  chmod 600 "$HERMES_HOME/lorekeeper.json"
  log "Wrote $HERMES_HOME/lorekeeper.json"
fi

log ""
log "Done. Lorekeeper is installed and active."
log "  Service : http://127.0.0.1:$PORT (token: $TOKEN_FILE)"
log "  Plugin  : $PLUGIN_DIR"
log "  Restart Hermes (new session) to load the memory provider."
log ""
log "Manage:"
log "  hermes memory status"
log "  systemctl --user status lorekeeper"
log ""
log "Next session will have lorekeeper_* tools available."