#!/usr/bin/env sh
# Renders (or lints) the chart with throwaway secrets generated at run time.
#
# The upstream chart refuses to render without an encryption key, salt and a
# handful of passwords. Committing plausible-looking values for those would put
# credential-shaped literals in the repository — noise for secret scanners and
# a bad habit — so they are generated here instead.
#
# Usage:
#   ci/render.sh                 # helm template, output to stdout
#   ci/render.sh lint            # helm lint
#   ci/render.sh template --set foo=bar
set -eu

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VALUES="$CHART_DIR/ci/test-values.yaml"

CMD="template"
if [ "$#" -gt 0 ]; then
  case "$1" in
    template | lint)
      CMD="$1"
      shift
      ;;
    -*)
      # Anything else is a helm flag — leave it in "$@" for pass-through.
      ;;
    *)
      echo "usage: $0 [template|lint] [helm flags...]" >&2
      exit 2
      ;;
  esac
fi

# 32 random bytes as hex — the encryption key must be exactly that shape.
KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"

set -- \
  -f "$VALUES" \
  --set "langfuse.langfuse.encryptionKey.value=$KEY" \
  --set langfuse.langfuse.salt.value=render-test \
  --set langfuse.langfuse.nextauth.secret.value=render-test \
  --set langfuse.postgresql.auth.password=render-test \
  --set langfuse.clickhouse.auth.password=render-test \
  --set langfuse.redis.auth.password=render-test \
  --set langfuse.s3.auth.secretAccessKey=render-test \
  "$@"

case "$CMD" in
  lint) exec helm lint "$CHART_DIR" "$@" ;;
  *) exec helm template test "$CHART_DIR" "$@" ;;
esac
