#!/usr/bin/env bash
# 過去対応事例インデックス(past-incidents)の作成とサンプルデータ投入
# 接続設定は generate_basic.py と同じ: /home/elastic/.k8s-vm-env を source して読み込む
#   ELASTICSEARCH_URL          https://<host>:443
#   ELASTICSEARCH_AUTH_BASE64  base64 認証情報
#   ELASTICSEARCH_AUTH_SCHEME  任意。"Basic"(既定) または "ApiKey"
# 使い方: [RECREATE=1] [INDEX=past-incidents] ./create_past_incidents.sh
set -euo pipefail

ENV_FILE="/home/elastic/.k8s-vm-env"
# 呼び出し元の環境変数は参照しない(generate_basic.py と同じ挙動)
unset ELASTICSEARCH_URL ELASTICSEARCH_AUTH_BASE64 ELASTICSEARCH_AUTH_SCHEME
if [ -f "${ENV_FILE}" ]; then
  set -a; . "${ENV_FILE}"; set +a
fi
: "${ELASTICSEARCH_URL:?ELASTICSEARCH_URL is not set (${ENV_FILE})}"
: "${ELASTICSEARCH_AUTH_BASE64:?ELASTICSEARCH_AUTH_BASE64 is not set (${ENV_FILE})}"
AUTH_SCHEME="${ELASTICSEARCH_AUTH_SCHEME:-Basic}"
INDEX="${INDEX:-past-incidents}"

es() { curl -sS -H "Authorization: ${AUTH_SCHEME} ${ELASTICSEARCH_AUTH_BASE64}" -H "Content-Type: application/json" "$@"; }

if [ "${RECREATE:-0}" = "1" ]; then
  echo "== delete index: ${INDEX}"
  es -X DELETE "${ELASTICSEARCH_URL}/${INDEX}"; echo
fi

echo "== create index: ${INDEX}"
es -X PUT "${ELASTICSEARCH_URL}/${INDEX}" -d '{
  "mappings": {
    "properties": {
      "incident_id":        { "type": "keyword" },
      "title":              { "type": "text" },
      "occurred_at":        { "type": "date" },
      "resolved_at":        { "type": "date" },
      "severity":           { "type": "keyword" },
      "service_name":       { "type": "keyword" },
      "affected_hosts":     { "type": "keyword" },
      "symptom":            { "type": "text", "copy_to": "semantic_summary" },
      "root_cause":         { "type": "text", "copy_to": "semantic_summary" },
      "root_cause_category":{ "type": "keyword" },
      "evidence":           { "type": "text", "copy_to": "semantic_summary" },
      "resolution":         { "type": "text" },
      "prevention":         { "type": "text" },
      "resolved_by":        { "type": "keyword" },
      "tags":               { "type": "keyword" },
      "remediation_available":   { "type": "boolean" },
      "remediation_workflow":    { "type": "keyword" },
      "remediation_action":      { "type": "keyword" },
      "remediation_command":     { "type": "keyword" },
      "remediation_endpoint":    { "type": "keyword" },
      "approval_required":       { "type": "boolean" },
      "remediation_description": { "type": "text" },
      "semantic_summary":   { "type": "semantic_text", "inference_id": ".multilingual-e5-small-elasticsearch" }
    }
  }
}'
echo

echo "== bulk insert sample incidents"
es -X POST "${ELASTICSEARCH_URL}/${INDEX}/_bulk?refresh=wait_for" --data-binary @- <<'EOF'
{"index":{"_id":"INC-0001"}}
{"incident_id":"INC-0001","title":"test-card-service レイテンシ上昇 (Redis ホスト CPU 飽和)","occurred_at":"2026-10-20T03:10:00Z","resolved_at":"2026-10-20T03:40:00Z","severity":"high","service_name":"test-card-service","affected_hosts":["sample-otel-demo-redis"],"symptom":"test-card-service の平均トランザクション時間が約5.7msから数百msへ急上昇。エラー率は0%で性能劣化のみ。Redis SETEX スパンが処理時間の約90%を占有。","root_cause":"Redis ホスト上でバックアップスクリプト (/opt/backup/nightly_backup.sh: tar | gzip -9) が業務時間帯に実行され、CPU競合により Redis の SETEX コマンド処理が遅延した。","root_cause_category":"cpu-saturation","evidence":"Redisホストの CPU 使用率が約68%まで上昇。プロセス一覧で tar / gzip -9 (親: nightly_backup.sh) が CPU を占有。syslog に 'CRON (root) CMD (/opt/backup/nightly_backup.sh)' と 'starting full backup' を検出。スパイク時間帯と一致。","resolution":"Redis ホスト上のバックアッププロセス (nightly_backup.sh / tar / gzip) を kill。Redis SETEX 遅延と test-card-service のレイテンシが基準値に復帰したことを確認。","prevention":"Redis ホストの CPU 使用率アラート(50%超)を追加。バックアップ処理を Redis ホストから分離(レプリカ側で実行)し、実行時間帯とCPU優先度(nice/ionice)を制限。","resolved_by":"sre-team","tags":["redis","cpu","backup","latency","apm"],"remediation_available":true,"remediation_workflow":"remediation-with-approval","remediation_action":"stop_cpu_load","remediation_command":"python3 cpu_load.py stop","remediation_endpoint":"POST /remediate_service","approval_required":true,"remediation_description":"承認システム(server.py)に承認依頼(POST /approval_requests)を登録し、承認後に POST /remediate_service で CPU 負荷プロセスを停止する。workflow: remediation-with-approval"}
{"index":{"_id":"INC-0002"}}
{"incident_id":"INC-0002","title":"java-status-api-frontend JVM ヒープ枯渇によるエラー率上昇","occurred_at":"2026-10-27T14:05:00Z","resolved_at":"2026-10-27T14:50:00Z","severity":"critical","service_name":"java-status-api-frontend-aws","affected_hosts":["frontend-host-1"],"symptom":"HTTP 500 エラー率が急上昇し、レスポンスが断続的にタイムアウト。","root_cause":"リリース後のキャッシュ実装不具合によるメモリリークで JVM ヒープが枯渇し、Full GC が連続発生した。","root_cause_category":"memory-leak","evidence":"JVM ヒープ使用率が95%超で張り付き、GC 時間が急増。OutOfMemoryError ログを確認。","resolution":"直前リリースをロールバックし、アプリケーションを再起動。","prevention":"リリース前の負荷試験にヒープ使用量の確認を追加。ヒープ使用率アラートを設定。","resolved_by":"dev-team","tags":["jvm","memory","gc","rollback"],"remediation_available":false,"approval_required":false,"remediation_description":"自動修復手段なし。担当チームによる手動対応が必要。"}
{"index":{"_id":"INC-0003"}}
{"incident_id":"INC-0003","title":"Redis ディスク容量逼迫による書き込み遅延","occurred_at":"2026-11-03T09:20:00Z","resolved_at":"2026-11-03T10:30:00Z","severity":"medium","service_name":"test-card-service","affected_hosts":["sample-otel-demo-redis"],"symptom":"Redis への書き込みレイテンシが増加し、test-card-service の応答時間が悪化。","root_cause":"Redis の AOF/ログがディスクを圧迫し、fsync 待ちが増加した。","root_cause_category":"disk-pressure","evidence":"ホストのディスク使用率が92%。iowait が上昇。","resolution":"不要ログの削除と AOF rewrite の実行、ディスク拡張。","prevention":"ディスク使用率アラートとログローテーションの見直し。","resolved_by":"infra-team","tags":["redis","disk","iowait","latency"],"remediation_available":false,"approval_required":false,"remediation_description":"自動修復手段なし。担当チームによる手動対応が必要。"}
EOF
echo
