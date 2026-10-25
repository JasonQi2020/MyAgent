# Deploying agent-demo to the Tencent Cloud VM

Design: `design/deployment.md`. The VM is shared with Hermes and future agents. agent-demo lives
only in `/srv/agents/agent-demo`, runs as the unprivileged user `agent-demo` (no sudo, no login),
listens on `127.0.0.1:8101` only, and has its own systemd unit with memory/CPU limits.

All commands run **on the laptop**, from the `agent-demo/` folder:

| Command | What it does |
|---|---|
| `deploy/deploy.sh bootstrap` | once: copies `uv` to `/usr/local/bin`, creates the `agent-demo` user, `/srv/agents/` and `PORTS.md`, the journald limit and logrotate |
| `deploy/deploy.sh code` | builds the web UI, copies the code (never `.env`, `agent.properties`, `state/`, `data/`), runs `uv sync` from the Tencent PyPI mirror, installs the units, restarts the service |
| `deploy/deploy.sh code --dry-run` | lists what would be copied |
| `deploy/deploy.sh config` | builds the VM's `.env`, `agent.properties` and `state/` from the laptop's (`make_vm_env.py`), copies them over SSH (mode 600), shreds the temporary copies, restarts. Prints key **names** only |
| `deploy/deploy.sh check` | WeCom long-connection reachability from the VM (dummy credentials) |
| `deploy/deploy.sh status` | service state and the last log lines |

Settings: `AGENT_VM` (default `ubuntu@49.233.173.2`), `DEPLOY_SSH_OPTS` (extra ssh options).

## The one rule: one consumer per bot

The Feishu app and the WeCom robot now run **on the VM only**. On the laptop, keep
`IM_CHANNELS=telegram` (Telegram is blocked on the VM). If the laptop also ran `feishu` or `wecom`,
the two machines would split Feishu events and take the WeCom robot from each other (the journal
then shows `disconnected_event`).

## On the VM

```sh
ssh ubuntu@49.233.173.2
sudo journalctl -u agent-demo -f                                   # logs
sudo systemctl restart agent-demo                                  # restart
sudo -u agent-demo /srv/agents/agent-demo/deploy/agent-demo-cli    # the CLI (no IM channels)
sudo -u agent-demo nano /srv/agents/agent-demo/.env                # change one setting, then restart
```

Web UI from the laptop: `ssh -L 8101:127.0.0.1:8101 ubuntu@49.233.173.2`, then http://127.0.0.1:8101.

Backups: `agent-demo-backup.timer` archives `state/ data/ .env agent.properties` daily at 04:30
(VM time) into `/srv/agents/agent-demo/backups/` and keeps 7.

## Removal or moving back to the laptop

```sh
sudo systemctl disable --now agent-demo agent-demo-backup.timer
sudo rm /etc/systemd/system/agent-demo*.{service,timer} /etc/logrotate.d/agent-demo
sudo userdel agent-demo && sudo rm -rf /srv/agents/agent-demo
```

Then put `feishu,wecom` back into the laptop's `IM_CHANNELS`.
