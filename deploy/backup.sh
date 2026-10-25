#!/bin/sh
# Daily archive of what a deploy does not bring back: state, data and config. Keeps the last 7.
set -eu
ROOT=/srv/agents/agent-demo
cd "$ROOT"
mkdir -p backups
tar -czf "backups/agent-demo-$(date +%Y%m%d-%H%M%S).tar.gz" --ignore-failed-read state data .env agent.properties
ls -1t backups/agent-demo-*.tar.gz | tail -n +8 | xargs -r rm -f
