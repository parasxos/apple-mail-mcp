#!/usr/bin/env bash
#
# lxplus_mail_master.sh — headlessly (re)establish the SSH ControlMaster
# socket that email_mcp/sender.py pipes `sendmail` through.
#
# Reads CERN_PASSWORD from ~/.secrets/cern_secrets.sh and generates a fresh
# TOTP via the cernvironment helper, so it needs no interactive input. Safe
# to call repeatedly — if a master is already alive it is a no-op.
#
# Overridable via the same env vars the MCP uses:
#   EMAIL_MCP_SEND_HOST   (default lxplus.cern.ch)
#   EMAIL_MCP_SEND_USER   (default pmoschov)
#   EMAIL_MCP_SSH_SOCKET  (default ~/.ssh/sock-lxplus-mail)
set -euo pipefail

HOST="${EMAIL_MCP_SEND_HOST:-lxplus.cern.ch}"
USER_="${EMAIL_MCP_SEND_USER:-pmoschov}"
SOCK="${EMAIL_MCP_SSH_SOCKET:-$HOME/.ssh/sock-lxplus-mail}"
SOCK="${SOCK/#\~/$HOME}"
SECRETS="${CERN_SECRETS_FILE:-$HOME/.secrets/cern_secrets.sh}"
TOTP_GEN="${CERN_TOTP_GEN:-$HOME/cernvironment/tools/generate_totp.py}"

# Already alive? Nothing to do.
if ssh -O check -o ControlPath="$SOCK" "$USER_@$HOST" >/dev/null 2>&1; then
    echo "master already alive: $SOCK"
    exit 0
fi

[ -f "$SECRETS" ] && source "$SECRETS"
: "${CERN_PASSWORD:?CERN_PASSWORD not set (checked $SECRETS)}"

TOTP="$(python3 "$TOTP_GEN")"

# expect drives password + 2nd-factor; -fN backgrounds ssh after auth so the
# ControlPersist master keeps the forward alive.
expect <<EXP
set timeout 60
spawn ssh -o StrictHostKeyChecking=no -o ControlMaster=yes \
    -o ControlPath=$SOCK -o ControlPersist=4h -fN $USER_@$HOST
expect {
    -re "(?i)password:" { send "$CERN_PASSWORD\r"; exp_continue }
    "2nd factor"        { send "$TOTP\r"; exp_continue }
    eof
}
EXP

if ssh -O check -o ControlPath="$SOCK" "$USER_@$HOST" >/dev/null 2>&1; then
    echo "master established: $SOCK"
    exit 0
fi
echo "failed to establish master: $SOCK" >&2
exit 1
