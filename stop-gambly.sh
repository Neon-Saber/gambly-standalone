#!/bin/bash
# Stop the gambly-bot systemd service with a reason that shows up in the
# bot status embed - usage:
#   ./stop-gambly.sh "deploying update"
# With no argument it just stops the service with no reason set (the bot
# will show "no reason given").
set -e
BOT_DIR="$HOME/gambly-standalone"

if [ -n "$1" ]; then
    echo "$1" > "$BOT_DIR/stop_reason.txt"
    echo "reason set: $1"
fi

sudo systemctl stop gambly-bot
echo "gambly-bot stopped"
