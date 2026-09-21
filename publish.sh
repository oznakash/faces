#!/bin/sh
# Publish this machine's index to a hosted Faces server.
#   FACES_ADMIN_TOKEN=... ./publish.sh https://faces.cloud-claude.com
# Bundles data/faces.db + data/crops (never the models, never source photos) and
# uploads it; the server swaps it in atomically and keeps the previous index as data.prev.
set -e
SERVER="${1:?usage: publish.sh https://server}"
: "${FACES_ADMIN_TOKEN:?set FACES_ADMIN_TOKEN}"
cd "$(dirname "$0")/data"
# checkpoint the WAL so faces.db alone is complete
python3 -c "import sqlite3; c=sqlite3.connect('faces.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
BUNDLE="$(mktemp -t faces-bundle).tar.gz"
tar -czf "$BUNDLE" faces.db crops
echo "bundle: $(du -h "$BUNDLE" | cut -f1) -> $SERVER"
curl -sS --fail-with-body -X POST "$SERVER/api/admin/import" \
  -H "Authorization: Bearer $FACES_ADMIN_TOKEN" \
  -F "bundle=@$BUNDLE;type=application/gzip" \
  --max-time 1800 --progress-bar
echo
rm -f "$BUNDLE"
