#!/bin/bash
set -euo pipefail

# This key cannot execute arbitrary shell commands, SCP, or SFTP.
if [[ "${SSH_ORIGINAL_COMMAND:-}" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
    exec /usr/bin/sudo -n /usr/local/sbin/calendar-bot-deploy "${BASH_REMATCH[1]}"
fi
echo 'Only deploy <40-character commit SHA> is permitted.' >&2
exit 1
