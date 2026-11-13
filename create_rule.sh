
#!/bin/bash
set -Eeuo pipefail

# ============================================================
# Configuration
# ============================================================

ENV_FILE="/home/elastic/.k8s-vm-env"
RULE_NAME="Elasticsearch query rule"
MAX_RETRIES=5
RETRY_INTERVAL=5

LOG_FILE="/home/elastic/create_observability_rule.log"

# ============================================================
# Logging
# ============================================================

exec > >(tee -a "$LOG_FILE") 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ============================================================
# Load environment
# ============================================================

log "[INFO] Loading environment: $ENV_FILE"

if [[ ! -r "$ENV_FILE" ]]; then
    log "[ERROR] Environment file not found or unreadable."
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${KIBANA_URL:?KIBANA_URL is not defined}"
: "${ELASTICSEARCH_AUTH_BASE64:?ELASTICSEARCH_AUTH_BASE64 is not defined}"

KIBANA_URL="${KIBANA_URL%/}"
AUTH_BASE64=$(printf '%s' "$ELASTICSEARCH_AUTH_BASE64" | tr -d '\r\n')

RULE_API="${KIBANA_URL}/api/alerting/rule"
FIND_API="${KIBANA_URL}/api/alerting/rules/_find"

log "[INFO] Kibana URL: $KIBANA_URL"

# ============================================================
# Dependencies
# ============================================================

for cmd in curl jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log "[ERROR] Required command not found: $cmd"
        exit 1
    fi
done

# ============================================================
# Temporary files
# ============================================================

RESPONSE_FILE=$(mktemp)
trap 'rm -f "$RESPONSE_FILE"' EXIT

# ============================================================
# HTTP headers
# ============================================================

HEADERS=(
    -H "Authorization: Basic ${AUTH_BASE64}"
    -H "kbn-xsrf: true"
    -H "Content-Type: application/json"
)

# ============================================================
# Rule payload
# ============================================================

PAYLOAD=$(cat <<'JSON'
{
  "name": "Elasticsearch query rule",
  "rule_type_id": ".es-query",
  "consumer": "alerts",
  "enabled": false,
  "tags": [],
  "schedule": {
    "interval": "1m"
  },
  "actions": [
    {
      "id": "system-connector-.workflows",
      "group": "query matched",
      "params": {
        "subActionParams": {
          "workflowId": "alert-infra-check-and-root-cause-investigation-with-check-infra"",
          "summaryMode": true,
          "alertStates": {
            "new": true,
            "ongoing": false,
            "recovered": false
          }
        },
        "subAction": "run"
      }
    }
  ],
  "params": {
    "searchType": "esqlQuery",
    "timeWindowSize": 50,
    "timeWindowUnit": "m",
    "threshold": [0],
    "thresholdComparator": ">",
    "size": 100,
    "esqlQuery": {
      "esql": "FROM traces-generic.otel-default\n| WHERE service.name == \"test-card-service\" AND processor.event == \"transaction\" AND @timestamp > NOW() - 1h\n| STATS avg_duration = AVG(transaction.duration.us) BY bucket = BUCKET(@timestamp, 1 minute)\n| CHANGE_POINT avg_duration ON bucket\n| WHERE type IS NOT NULL\n| KEEP bucket, avg_duration, type, pvalue"
    },
    "aggType": "count",
    "groupBy": "all",
    "termSize": 5,
    "sourceFields": [],
    "timeField": "@timestamp",
    "excludeHitsFromPreviousRun": true
  },
  "alert_delay": {
    "active": 1
  }
}
JSON
)

# ============================================================
# Check existing rule
# ============================================================

check_existing_rule() {

    log "[INFO] Checking existing rule..."

    local status
    local curl_rc=0

    status=$(curl -sS \
        --connect-timeout 10 \
        --max-time 30 \
        --retry 2 \
        --retry-all-errors \
        "${HEADERS[@]}" \
        --get \
        --data-urlencode "search=${RULE_NAME}" \
        --data-urlencode "search_fields=name" \
        --data-urlencode "per_page=100" \
        -o "$RESPONSE_FILE" \
        -w '%{http_code}' \
        "$FIND_API") || curl_rc=$?

    if [[ "$curl_rc" -ne 0 || "$status" != "200" ]]; then
        log "[WARN] Rule search failed. HTTP=$status curl=$curl_rc"
        return 2
    fi

    if jq -e --arg name "$RULE_NAME" \
        '.data[]? | select(.name == $name)' \
        "$RESPONSE_FILE" >/dev/null; then

        log "[INFO] Rule already exists. Skipping creation."
        jq --arg name "$RULE_NAME" \
            '.data[] | select(.name == $name) | {id, name, enabled}' \
            "$RESPONSE_FILE"

        return 0
    fi

    return 1
}

# ============================================================
# Create rule with retry
# ============================================================

for attempt in $(seq 1 "$MAX_RETRIES"); do

    log "[INFO] Attempt $attempt/$MAX_RETRIES"

    # Check before every POST to avoid duplicate creation
    existing_result=0
    check_existing_rule || existing_result=$?

    case "$existing_result" in
        0)
            log "[SUCCESS] Rule is already configured."
            exit 0
            ;;
        1)
            ;;
        *)
            log "[WARN] Cannot verify existing rule."
            ;;
    esac

    log "[INFO] Creating alert rule..."

    curl_rc=0

    HTTP_STATUS=$(curl -sS \
        --connect-timeout 10 \
        --max-time 60 \
        "${HEADERS[@]}" \
        -X POST \
        -d "$PAYLOAD" \
        -o "$RESPONSE_FILE" \
        -w '%{http_code}' \
        "$RULE_API") || curl_rc=$?

    if [[ "$curl_rc" -eq 0 && "$HTTP_STATUS" =~ ^2[0-9][0-9]$ ]]; then

        log "[SUCCESS] Alert rule created successfully."

        jq '{id, name, enabled, rule_type_id, schedule}' \
            "$RESPONSE_FILE"

        exit 0
    fi

    log "[WARN] Creation failed. HTTP=$HTTP_STATUS curl=$curl_rc"
    cat "$RESPONSE_FILE"
    echo

    # Do not retry permanent client errors
    if [[ "$curl_rc" -eq 0 &&
          "$HTTP_STATUS" =~ ^4[0-9][0-9]$ &&
          "$HTTP_STATUS" != "429" ]]; then

        log "[ERROR] Permanent HTTP error. Stopping."
        exit 1
    fi

    if [[ "$attempt" -lt "$MAX_RETRIES" ]]; then
        WAIT=$((RETRY_INTERVAL * attempt))
        log "[INFO] Retrying in ${WAIT} seconds..."
        sleep "$WAIT"
    fi

done

log "[ERROR] Failed to create alert rule after $MAX_RETRIES attempts."
exit 1
