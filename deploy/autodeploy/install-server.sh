#!/bin/bash
set -euo pipefail
umask 077

[[ "$(id -u)" == 0 ]] || { echo 'Run installer with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
public_key_file=${1:?Pass the dedicated deployment public key file}
test -f /opt/calendar-bot/.env
test -f /opt/calendar-bot/data/planner.db
test -x /usr/bin/docker
test -x /usr/bin/sudo
test -x /usr/sbin/visudo
ssh-keygen -l -f "$public_key_file" > /dev/null
grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$' "$public_key_file"
[[ "$(wc -l < "$public_key_file")" == 1 ]]

if ! id calendar-deploy > /dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/calendar-deploy --shell /bin/bash calendar-deploy
fi
[[ "$(id -u calendar-deploy)" != 0 ]]
for group in $(id -nG calendar-deploy); do
    [[ "$group" != sudo && "$group" != docker ]] || { echo 'Deployment user has excessive privileges.' >&2; exit 1; }
done

install -d -m 755 -o root -g root /var/lib/calendar-deploy /var/lib/calendar-deploy/.ssh /usr/local/libexec
install -d -m 700 -o root -g root /etc/calendar-bot /var/lib/calendar-bot-deploy
exec 9>/var/lib/calendar-bot-deploy/deploy.lock
flock -n 9 || { echo 'An active deployment must finish before installing server scripts.' >&2; exit 1; }
install -m 755 "$source_dir/deploy-server.py" /usr/local/sbin/calendar-bot-deploy
install -m 755 "$source_dir/ssh-command.sh" /usr/local/libexec/calendar-bot-ssh
install -m 600 "$source_dir/compose.server.yml" /etc/calendar-bot/compose.yml

printf 'calendar-deploy ALL=(root) NOPASSWD: /usr/local/sbin/calendar-bot-deploy *\n' > /etc/calendar-bot/sudoers.pending
chmod 440 /etc/calendar-bot/sudoers.pending
visudo -cf /etc/calendar-bot/sudoers.pending
install -m 440 /etc/calendar-bot/sudoers.pending /etc/sudoers.d/calendar-bot-deploy

# A public key is readable by sshd under the login UID, but only root may replace it.
printf 'restrict,command="/usr/local/libexec/calendar-bot-ssh" %s\n' "$(cat "$public_key_file")" > /var/lib/calendar-deploy/.ssh/authorized_keys
chmod 644 /var/lib/calendar-deploy/.ssh/authorized_keys
chown root:root /var/lib/calendar-deploy/.ssh/authorized_keys

CALENDAR_IMAGE=calendar-bot:production docker compose --project-name calendar-bot \
    --project-directory /opt/calendar-bot -f /etc/calendar-bot/compose.yml config --quiet
echo 'Deployment entry point installed. The running bot, nginx and other projects were not restarted.'
