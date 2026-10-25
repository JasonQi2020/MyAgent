#!/usr/bin/env bash
# Deploy agent-demo to the shared VM (design/deployment.md §9). Run on the laptop:
#
#   deploy/deploy.sh bootstrap        once: uv, the agent-demo user, /srv/agents, units, limits
#   deploy/deploy.sh code [--dry-run] build the web UI, copy the code, uv sync, restart
#   deploy/deploy.sh config           copy .env/agent.properties/state for the VM (secrets never printed)
#   deploy/deploy.sh check            WeCom reachability from the VM, as the agent-demo user
#   deploy/deploy.sh status           service state and recent log lines
#
# Settings: AGENT_VM (default ubuntu@49.233.173.2), DEPLOY_SSH_OPTS (extra ssh options).
# Only /srv/agents/agent-demo, its units and /srv/agents/PORTS.md are ever touched on the VM.
set -euo pipefail

VM="${AGENT_VM:-ubuntu@49.233.173.2}"
SSH_OPTS="${DEPLOY_SSH_OPTS:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/srv/agents/agent-demo
STAGING="${DEPLOY_STAGING:-/home/ubuntu/agent-demo-staging}"  # the SSH user's folder, outside /srv
UV_ENV="UV_CACHE_DIR=$DEST/.cache/uv HOME=$DEST UV_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/ UV_PYTHON_DOWNLOADS=never"

# shellcheck disable=SC2086
vm() { ssh $SSH_OPTS "$VM" "$@"; }
# shellcheck disable=SC2086
vm_copy() { scp -q $SSH_OPTS "$@"; }
export RSYNC_RSH="ssh $SSH_OPTS"

bootstrap() {
    local uv_bin
    uv_bin="$(command -v uv)"
    echo "== uv: copying the laptop's $("$uv_bin" --version) to /usr/local/bin (GitHub downloads time out from the VM)"
    vm_copy "$uv_bin" "$VM:/tmp/uv.agent-demo"
    vm "sudo install -m 755 -o root -g root /tmp/uv.agent-demo /usr/local/bin/uv && rm -f /tmp/uv.agent-demo && uv --version"

    echo "== user, folders, registry, journald limit, logrotate"
    vm 'sudo bash -s' <<'EOF'
set -euo pipefail
id agent-demo >/dev/null 2>&1 || useradd --system --home-dir /srv/agents/agent-demo --shell /usr/sbin/nologin agent-demo
install -d -m 755 -o root -g root /srv/agents
install -d -m 750 -o agent-demo -g agent-demo /srv/agents/agent-demo
for d in state data logs backups .cache; do install -d -m 700 -o agent-demo -g agent-demo /srv/agents/agent-demo/$d; done
if [ ! -f /srv/agents/PORTS.md ]; then cat > /srv/agents/PORTS.md <<'PORTS'
# Agents on this VM (design: agent-demo/design/deployment.md §4)

Every agent: its own Linux user, folder /srv/agents/<name>, systemd unit, port on 127.0.0.1, bot accounts, .env.

| Agent | User | Service(s) | Port (127.0.0.1) | Bots |
|---|---|---|---|---|
| hermes | ubuntu | hermes-dashboard.service, hermes-gateway.service (user) | 9119 | its own (including its own Feishu app) |
| agent-demo | agent-demo | agent-demo.service, agent-demo-backup.timer | 8101 | Feishu app "AgentDemoBot", WeCom smart robot |
| (next) | agent-x | agent-x.service | 8102 | its own |
PORTS
chmod 644 /srv/agents/PORTS.md; fi
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/agents.conf
systemctl restart systemd-journald
cat > /etc/logrotate.d/agent-demo <<'ROT'
/srv/agents/agent-demo/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
    su agent-demo agent-demo
}
ROT
EOF
    echo "== done. Next: deploy/deploy.sh code, then deploy/deploy.sh config"
}

code() {
    local dry=()
    [ "${1:-}" = "--dry-run" ] && dry=(--dry-run --itemize-changes)
    if [ ${#dry[@]} -eq 0 ]; then
        echo "== building the web UI"
        (cd "$ROOT/web" && npm run build --silent)
    fi
    echo "== copying the code to the VM's staging folder"
    # Only code: .env, agent.properties, state/, data/, logs/, backups/ are never part of a deploy.
    (cd "$ROOT" && rsync -a --delete --relative "${dry[@]}" \
        --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.claude-sandbox/' \
        --exclude '.env' --exclude 'agent.properties' --exclude 'node_modules/' \
        ./backend ./web/dist ./design ./deploy ./mcp_servers.json ./README.md "$VM:$STAGING/")
    [ ${#dry[@]} -gt 0 ] && return 0

    echo "== installing into $DEST, uv sync, units"
    vm "sudo bash -s" <<EOF
set -euo pipefail
cd $STAGING
rsync -a --delete --relative --chown=agent-demo:agent-demo --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= \
    --exclude '.venv/' ./backend ./web/dist ./design ./deploy ./mcp_servers.json ./README.md $DEST/
chmod 700 $DEST/deploy/*.sh $DEST/deploy/agent-demo-cli
cd $DEST/backend
sudo -u agent-demo env $UV_ENV /usr/local/bin/uv sync --frozen --no-dev --quiet
if [ ! -f $DEST/data/demo.db ]; then sudo -u agent-demo env $UV_ENV /usr/local/bin/uv run --frozen --no-sync python scripts/seed_db.py; fi
for unit in agent-demo.service agent-demo-backup.service agent-demo-backup.timer; do
    install -m 644 $DEST/deploy/\$unit /etc/systemd/system/\$unit
done
systemctl daemon-reload
systemctl enable --quiet agent-demo-backup.timer && systemctl start agent-demo-backup.timer
if systemctl is-enabled --quiet agent-demo.service; then systemctl restart agent-demo.service; echo "restarted agent-demo"; fi
EOF
    echo "== done"
}

config() {
    local tmp
    tmp="$(mktemp -d)"
    trap 'find "$tmp" -type f -exec shred -u {} + 2>/dev/null; rm -rf "$tmp"' RETURN
    python3 "$ROOT/deploy/make_vm_env.py" "$tmp/vm"
    (cd "$tmp/vm" && tar -czf "$tmp/config.tgz" .env agent.properties state)
    chmod 600 "$tmp/config.tgz"
    echo "== copying over SSH and installing (mode 600, owner agent-demo)"
    vm 'umask 077; cat > /tmp/agent-demo-config.tgz' < "$tmp/config.tgz"
    vm "sudo bash -s" <<EOF
set -euo pipefail
umask 077
tmp=\$(mktemp -d)
tar -xzf /tmp/agent-demo-config.tgz -C \$tmp
shred -u /tmp/agent-demo-config.tgz
install -m 600 -o agent-demo -g agent-demo \$tmp/.env $DEST/.env
install -m 600 -o agent-demo -g agent-demo \$tmp/agent.properties $DEST/agent.properties
for f in \$tmp/state/*; do [ -e "\$f" ] && install -m 600 -o agent-demo -g agent-demo "\$f" $DEST/state/; done
find \$tmp -type f -exec shred -u {} +
rm -rf \$tmp
if systemctl is-enabled --quiet agent-demo.service; then systemctl restart agent-demo.service; echo "restarted agent-demo"; fi
EOF
    echo "== done"
}

check() {
    vm "sudo -u agent-demo env $UV_ENV /usr/local/bin/uv run --directory $DEST/backend --frozen --no-sync python $DEST/deploy/check_wecom.py"
}

status() {
    vm "systemctl --no-pager status agent-demo.service | head -12; sudo journalctl -u agent-demo -n 25 --no-pager"
}

case "${1:-}" in
    bootstrap) bootstrap ;;
    code) code "${2:-}" ;;
    config) config ;;
    check) check ;;
    status) status ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
