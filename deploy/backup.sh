#!/usr/bin/env bash
#
# Back up a team deployment. Safe to run while the server is serving.
#
#   sudo -u pi deploy/backup.sh [DEST]
#
# Three things are worth backing up, and they need three different treatments:
#
#   pi.db        accounts, quotas, usage and the audit log. Copied with `sqlite3 .backup`,
#                because the database runs in WAL mode: copying the file alone can produce a
#                database that is missing the newest transactions, or a torn one.
#   agents/      the JSONL transcripts, one tree per user. Small, so they are archived.
#   workspaces/  the users' actual work. Potentially large, so it is mirrored with rsync
#                (delta transfer, hard-linkable snapshots) instead of re-archived nightly.
#
# Retention is applied to the snapshots this script creates; the workspace mirror is a
# single rolling copy, because keeping N full copies of a large tree is how backups get
# switched off.

set -euo pipefail

DATA_ROOT="${PI_DATA_ROOT:-/var/lib/pi}"
DEST="${1:-/var/backups/pi}"
KEEP_DAYS="${PI_BACKUP_KEEP_DAYS:-14}"

if [[ ! -d "$DATA_ROOT" ]]; then
	echo "backup: PI_DATA_ROOT does not exist: $DATA_ROOT" >&2
	exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot="$DEST/snapshots/$stamp"
mkdir -p "$snapshot"

# --- database -------------------------------------------------------------------------
if [[ -f "$DATA_ROOT/pi.db" ]]; then
	if ! command -v sqlite3 >/dev/null 2>&1; then
		echo "backup: sqlite3 is required (apt install sqlite3)" >&2
		exit 1
	fi
	sqlite3 "$DATA_ROOT/pi.db" ".backup '$snapshot/pi.db'"
	echo "backup: pi.db -> $snapshot/pi.db"
else
	echo "backup: no pi.db at $DATA_ROOT (single-mode deployment?)" >&2
fi

# --- transcripts ----------------------------------------------------------------------
if [[ -d "$DATA_ROOT/agents" ]]; then
	tar -C "$DATA_ROOT" -czf "$snapshot/agents.tar.gz" agents
	echo "backup: agents/ -> $snapshot/agents.tar.gz"
fi

# --- workspaces -----------------------------------------------------------------------
if [[ -d "$DATA_ROOT/workspaces" ]]; then
	mkdir -p "$DEST/workspaces"
	rsync -a --delete --numeric-ids \
		"$DATA_ROOT/workspaces/" "$DEST/workspaces/"
	echo "backup: workspaces/ mirrored -> $DEST/workspaces/"
fi

# --- retention ------------------------------------------------------------------------
if [[ -d "$DEST/snapshots" ]]; then
	find "$DEST/snapshots" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -print -exec rm -rf {} +
fi

echo "backup: done at $stamp (snapshots kept ${KEEP_DAYS}d in $DEST/snapshots)"
