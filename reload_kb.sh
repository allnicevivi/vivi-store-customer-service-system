#!/usr/bin/env bash
# 強制重新上傳並覆蓋 FAQ + Policy 兩個 collection
set -euo pipefail

BASE_URL="${KNOWLEDGE_API_URL:-http://localhost:8080/kb}"
FAQ_FILE="${FAQ_FILE:-$(dirname "$0")/backend/knowledge_base/ecommerce_faq.md}"
POLICY_FILE="${POLICY_FILE:-$(dirname "$0")/backend/knowledge_base/ecommerce_policy.md}"

echo "==> Reloading FAQ: $FAQ_FILE"
curl -sf -X POST "$BASE_URL/reload" \
  -F "file=@$FAQ_FILE" | python3 -m json.tool

echo ""
echo "==> Reloading Policy: $POLICY_FILE"
curl -sf -X POST "$BASE_URL/policy/reload" \
  -F "file=@$POLICY_FILE" | python3 -m json.tool

echo ""
echo "==> Status after reload:"
curl -sf "$BASE_URL/status" | python3 -m json.tool
