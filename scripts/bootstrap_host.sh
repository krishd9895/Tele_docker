#!/usr/bin/env bash
#
# bootstrap_host.sh — one-time setup for running Tele_docker on ANY WSL machine.
#
# Goal: get the bot running "at any cost" with the minimum manual steps.
# This script checks for each prerequisite and only installs what's missing,
# so it's safe to re-run on a machine that already has some of this set up.
#
# Usage (from the repo root, on the WSL host — NOT inside a container):
#   chmod +x scripts/bootstrap_host.sh
#   sudo ./scripts/bootstrap_host.sh
#
# What it does, in order:
#   1. Installs git, curl, openssh-server if missing
#   2. Installs Docker Engine + the docker compose plugin if missing
#   3. Starts/enables sshd with password authentication
#   4. Adds the invoking (non-root) user to the docker group
#   5. Creates .env from .env.example if it doesn't exist yet (does NOT
#      overwrite an existing .env)
#   6. Brings the bot up with `docker compose up -d --build`
#
# Anything this script can't determine automatically (your Telegram bot
# token, allowed user id, SSH password) is left as a clearly marked TODO in
# .env — the script prints exactly what you still need to fill in.
#
set -uo pipefail  # deliberately NOT -e: one missing/failed step should not
                   # abort the whole bootstrap; we report and keep going.

log()  { echo -e "\n\033[1;34m[bootstrap]\033[0m $*"; }
ok()   { echo -e "\033[1;32m  ✓ $*\033[0m"; }
warn() { echo -e "\033[1;33m  ! $*\033[0m"; }
fail() { echo -e "\033[1;31m  ✗ $*\033[0m"; }

STEP_FAILURES=0

# Resolve the "real" (non-root) user even when invoked via sudo, so we know
# whose account to add to the docker group and where the repo actually is.
REAL_USER="${SUDO_USER:-$(whoami)}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    fail "This script needs root privileges (it installs system packages and configures sshd)."
    echo "  Re-run with: sudo ./scripts/bootstrap_host.sh"
    exit 1
fi

command_exists() { command -v "$1" >/dev/null 2>&1; }

detect_pkg_manager() {
    if command_exists apt-get; then echo "apt"; 
    elif command_exists dnf; then echo "dnf";
    elif command_exists yum; then echo "yum";
    elif command_exists apk; then echo "apk";
    elif command_exists pacman; then echo "pacman";
    else echo "unknown"; fi
}

PKG_MGR="$(detect_pkg_manager)"
log "Detected package manager: $PKG_MGR"

pkg_install() {
    case "$PKG_MGR" in
        apt)     apt-get update -qq && apt-get install -y -qq "$@" ;;
        dnf)     dnf install -y -q "$@" ;;
        yum)     yum install -y -q "$@" ;;
        apk)     apk add --no-cache "$@" ;;
        pacman)  pacman -Sy --noconfirm "$@" ;;
        *)       return 1 ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────
# 1. git, curl, openssh-server
# ─────────────────────────────────────────────────────────────────────────
log "Checking git / curl / openssh-server..."
MISSING_BASICS=()
command_exists git  || MISSING_BASICS+=("git")
command_exists curl || MISSING_BASICS+=("curl")
if command_exists sshd || [ -x /usr/sbin/sshd ]; then
    :
else
    case "$PKG_MGR" in
        apk) MISSING_BASICS+=("openssh") ;;
        *)   MISSING_BASICS+=("openssh-server") ;;
    esac
fi

if [ "${#MISSING_BASICS[@]}" -gt 0 ]; then
    log "Installing: ${MISSING_BASICS[*]}"
    if pkg_install "${MISSING_BASICS[@]}"; then
        ok "Installed ${MISSING_BASICS[*]}"
    else
        fail "Could not install ${MISSING_BASICS[*]} automatically — install manually for package manager '$PKG_MGR'."
        STEP_FAILURES=$((STEP_FAILURES + 1))
    fi
else
    ok "git, curl, openssh-server already present"
fi

# ─────────────────────────────────────────────────────────────────────────
# 2. Docker Engine + compose plugin
# ─────────────────────────────────────────────────────────────────────────
log "Checking Docker..."
if command_exists docker; then
    ok "Docker already installed ($(docker --version 2>/dev/null))"
else
    log "Docker not found — installing via get.docker.com convenience script..."
    if curl -fsSL https://get.docker.com | sh; then
        ok "Docker installed"
    else
        fail "Docker install script failed — install Docker manually, then re-run this script."
        STEP_FAILURES=$((STEP_FAILURES + 1))
    fi
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin already present"
else
    log "docker compose plugin missing — attempting install..."
    case "$PKG_MGR" in
        apt) pkg_install docker-compose-plugin || warn "Could not install docker-compose-plugin via apt; the get.docker.com script above usually includes it." ;;
        *)   warn "Please install the docker compose plugin manually for package manager '$PKG_MGR'." ;;
    esac
fi

# Make sure the Docker daemon is actually running (WSL doesn't start services
# on boot by default).
if command_exists dockerd; then
    if ! docker info >/dev/null 2>&1; then
        log "Starting Docker daemon..."
        service docker start >/dev/null 2>&1 || (dockerd >/tmp/dockerd.log 2>&1 &)
        sleep 3
        if docker info >/dev/null 2>&1; then
            ok "Docker daemon is running"
        else
            fail "Docker daemon did not start — check 'sudo service docker start' manually, or that Docker Desktop's WSL integration is enabled for this distro."
            STEP_FAILURES=$((STEP_FAILURES + 1))
        fi
    else
        ok "Docker daemon already running"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 3. sshd — start it and make sure password auth is on
# ─────────────────────────────────────────────────────────────────────────
log "Configuring SSH server..."
SSHD_CONFIG="/etc/ssh/sshd_config"
if [ -f "$SSHD_CONFIG" ]; then
    if grep -qE '^\s*PasswordAuthentication\s+no' "$SSHD_CONFIG"; then
        sed -i 's/^\s*PasswordAuthentication\s\+no/PasswordAuthentication yes/' "$SSHD_CONFIG"
        ok "Enabled PasswordAuthentication in sshd_config"
    elif grep -qE '^\s*PasswordAuthentication\s+yes' "$SSHD_CONFIG"; then
        ok "PasswordAuthentication already enabled"
    else
        echo "PasswordAuthentication yes" >> "$SSHD_CONFIG"
        ok "Added PasswordAuthentication yes to sshd_config"
    fi
fi

service ssh start >/dev/null 2>&1 || service sshd start >/dev/null 2>&1 || /usr/sbin/sshd >/dev/null 2>&1
if pgrep -x sshd >/dev/null 2>&1; then
    ok "sshd is running"
else
    fail "sshd did not start — run 'sudo service ssh start' manually and check /var/log/auth.log."
    STEP_FAILURES=$((STEP_FAILURES + 1))
fi

# ─────────────────────────────────────────────────────────────────────────
# 4. docker group membership for the real (non-root) user
# ─────────────────────────────────────────────────────────────────────────
if [ "$REAL_USER" != "root" ]; then
    log "Ensuring '$REAL_USER' is in the docker group..."
    if id -nG "$REAL_USER" 2>/dev/null | grep -qw docker; then
        ok "'$REAL_USER' is already in the docker group"
    else
        if usermod -aG docker "$REAL_USER" 2>/dev/null; then
            ok "Added '$REAL_USER' to the docker group (log out/in, or run 'newgrp docker', for it to take effect in existing shells)"
        else
            warn "Could not add '$REAL_USER' to the docker group automatically."
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 5. .env — create from template if missing, never overwrite
# ─────────────────────────────────────────────────────────────────────────
log "Checking .env..."
ENV_FILE="$REPO_DIR/.env"
ENV_EXAMPLE="$REPO_DIR/.env.example"
if [ -f "$ENV_FILE" ]; then
    ok ".env already exists — leaving it untouched"
else
    if [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        # Pre-fill what we can safely infer so the bot has the best chance
        # of starting even if the user forgets to edit it.
        sed -i "s/^HOST_SSH_USER=.*/HOST_SSH_USER=${REAL_USER}/" "$ENV_FILE"
        sed -i "s#^BROWSER_ROOT=.*#BROWSER_ROOT=/home/${REAL_USER}#" "$ENV_FILE"
        sed -i "s#^GIT_SCAN_PATHS=.*#GIT_SCAN_PATHS=/home/${REAL_USER}#" "$ENV_FILE"
        chown "$REAL_USER":"$REAL_USER" "$ENV_FILE" 2>/dev/null
        warn "Created .env from .env.example — you STILL need to fill in:"
        echo "      TELEGRAM_BOT_TOKEN   (from @BotFather)"
        echo "      ALLOWED_USER_ID      (from @userinfobot)"
        echo "      HOST_SSH_PASSWORD    (the login password for '${REAL_USER}')"
        echo "  Edit $ENV_FILE, then re-run: sudo docker compose up -d --build"
        STEP_FAILURES=$((STEP_FAILURES + 1))  # don't auto-launch with a half-filled .env
    else
        fail ".env.example not found — cannot create .env automatically."
        STEP_FAILURES=$((STEP_FAILURES + 1))
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# 6. Bring the bot up (only if .env looks usable)
# ─────────────────────────────────────────────────────────────────────────
if [ -f "$ENV_FILE" ] && ! grep -q "your_wsl_password_here\|123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh" "$ENV_FILE"; then
    log "Starting the bot with docker compose..."
    (cd "$REPO_DIR" && docker compose up -d --build) && ok "Bot containers started (docker compose ps to check status)" \
        || { fail "docker compose up failed — see output above."; STEP_FAILURES=$((STEP_FAILURES + 1)); }
else
    warn "Skipping auto-start: .env still has placeholder values. Fill it in, then run:"
    echo "      sudo docker compose up -d --build"
fi

echo
if [ "$STEP_FAILURES" -eq 0 ]; then
    log "Bootstrap complete — no failures detected."
else
    log "Bootstrap finished with $STEP_FAILURES step(s) needing your attention (see '!'/'✗' lines above)."
fi
