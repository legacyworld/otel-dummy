#!/usr/bin/env bash
# Agent Builder のスキル(rca-lite)と、そのスキルだけを持つエージェント(rca_agent)を作成する
# check_infra の結果(ホストのbaselineと±10分の1分バケット)を前提にした、簡素なRCA用
# 既に存在する場合は更新するので、まっさらなクラスタでも既存クラスタでも実行できる
# 接続設定は generate_basic.py と同じ: /home/elastic/.k8s-vm-env を source して読み込む
#   KIBANA_URL                 https://<host>
#   ELASTICSEARCH_AUTH_BASE64  base64 認証情報
#   ELASTICSEARCH_AUTH_SCHEME  任意。"Basic"(既定) または "ApiKey"
# 使い方: [SKILL_ID=rca-lite] [AGENT_ID=rca_agent] ./create_rca_agent.sh
# 前提: スキルが参照する observability.* ツールが Kibana(Observability)で利用可能であること
set -euo pipefail

ENV_FILE="/home/elastic/.k8s-vm-env"
# 呼び出し元の環境変数は参照しない(generate_basic.py と同じ挙動)
unset KIBANA_URL ELASTICSEARCH_AUTH_BASE64 ELASTICSEARCH_AUTH_SCHEME
if [ -f "${ENV_FILE}" ]; then
  set -a; . "${ENV_FILE}"; set +a
fi
: "${KIBANA_URL:?KIBANA_URL is not set (${ENV_FILE})}"
: "${ELASTICSEARCH_AUTH_BASE64:?ELASTICSEARCH_AUTH_BASE64 is not set (${ENV_FILE})}"
AUTH_SCHEME="${ELASTICSEARCH_AUTH_SCHEME:-Basic}"
AUTH_BASE64=$(printf '%s' "${ELASTICSEARCH_AUTH_BASE64}" | tr -d '\r\n')
SKILL_ID="${SKILL_ID:-service-infra-lookup}"
AGENT_ID="${AGENT_ID:-check_infra}"
export SKILL_ID AGENT_ID
BASE="${KIBANA_URL%/}/api/agent_builder"

kb() { curl -sS -H "Authorization: ${AUTH_SCHEME} ${AUTH_BASE64}" -H "kbn-xsrf: true" -H "Content-Type: application/json" "$@"; }

# 存在すれば PUT(id なし)、なければ POST(id 付き)で作成する
# usage: upsert <tools|skills|agents> <id>   (本文 JSON は標準入力、id なし)
upsert() {
  local kind="$1" id="$2" body status
  body=$(cat)
  status=$(kb -o /dev/null -w '%{http_code}' "${BASE}/${kind}/${id}")
  if [ "${status}" = "200" ]; then
    echo "== update ${kind}: ${id}"
    printf '%s' "${body}" | python3 -c 'import sys,json; d=json.load(sys.stdin); d.pop("type",None); print(json.dumps(d))' \
      | kb -X PUT "${BASE}/${kind}/${id}" -d @-
  else
    echo "== create ${kind}: ${id}"
    printf '%s' "${body}" | ID="${id}" python3 -c 'import sys,json,os; d=json.load(sys.stdin); d["id"]=os.environ["ID"]; print(json.dumps(d))' \
      | kb -X POST "${BASE}/${kind}" -d @-
  fi
  echo
}

SKILL_ID="${SKILL_ID:-rca-lite}"
AGENT_ID="${AGENT_ID:-rca_agent}"
export SKILL_ID AGENT_ID

# ---------------------------------------------------------------
# 1. スキル
# ---------------------------------------------------------------
SKILL_DESCRIPTION='アラートの根本原因分析(RCA)用。入力に check_infra のインフラ情報(ホストごとの24時間baselineとアラート前後10分の1分バケット)が含まれている前提で、ホスト上の実行履歴(OSログ)とアプリ側(サービス状態・依存関係・ログ/トレース)を最小限確認して原因仮説を確度つきで報告する。'
SKILL_TOOLS='["observability.get_services", "observability.get_service_topology", "observability.get_logs", "observability.get_log_groups", "observability.get_traces"]'
SKILL_CONTENT=$(cat <<'SKILL_CONTENT_EOF'
## Observability RCA Skill (lite)

入力に「インフラ情報 (check_infra の結果)」が含まれる。ここには、関連ホストごとの
24時間平均(baseline_24h)と、アラート時刻前後10分の1分バケット(cpu / mem / load)が既に入っている。
ホストのメトリクスを自分で取り直さないこと。

## 手順

1. **アラートを読む**: `kibana.alert.start`(時間の基準)、`service.name` または `host.name`、`kibana.alert.reason`。
2. **インフラ情報を読む**: 各ホストについて、1分バケットが baseline_24h から外れ始めた時刻を探す。
   アラート開始より前に外れ始めていれば、そのホストが原因候補。
3. **ホスト上の実行履歴を必ず確認する**(エラーが無くても省略しない): 手順2で外れ始めたホストごとに、
   `get_logs` で `host.name` を絞り、外れ始めた時刻の **-10分〜+10分** を見る。
   アプリのログだけでなく、OSのログ(`/var/log/syslog` 等、`log.file.path` や `data_stream.dataset` が system 系)も対象にする。
   - 見るもの: スケジューラ(CRON など)による起動、スクリプト/コマンドの開始・終了、
     ファイルのコピー・圧縮・転送・書き出しといった重い処理を示す行(`tar`, `gzip`, `rsync`, `dump` などのコマンド名やパス)。
   - 「平常時には出ない行」を探す: 同じホストの前日同時刻、または直前の1時間と比べて、新しく現れた行や、
     外れ始めた時刻とほぼ同時に出た行を拾う。件数が多ければ `get_log_groups` で群に分けてから確認する。
   - 開始行と終了行の両方を探す。実行時間が外れた期間と一致するかを確認する。
   - 該当する行があれば、メッセージ全文・時刻・ホストをそのまま引用する。無ければ「該当なし」と明記する。
4. **アプリ側を確認する(必要最小限)**: 以下のツールだけを使う。時間窓は `kibana.alert.start - 15分` 〜 now。
   - `get_services` — 対象サービスの error rate / latency
   - `get_service_topology`(direction: both、24時間) — 依存先・呼び出し元
   - `get_log_groups` または `get_traces` — エラーの中身。エラーが主要シグナルのときだけ
5. **仮説を立てる**: 根拠が異なる複数の仮説があれば、すべて挙げる。

## 判断ルール

- 根拠は、ツールの出力かインフラ情報にある値だけ。推測で書かない。
- 呼び出し側が失敗しているという観測は、呼び出し先の内部状態の証拠ではない。
  「停止」と書かず「到達不能」「エラー」「テレメトリなし」のように、確認できた深さで書く。
- 依存先が正常で、対象だけが悪いなら対象が原因。依存先が悪いなら依存先が原因で、対象は症状。

## 出力

- 原因(複数あれば確度つきで全部): 主張 / 根拠(値・時刻・trace ID) / 確度(Critical, High, Medium, Speculative)
- ホスト上で見つかった実行履歴(手順3)と、メトリクスが外れた期間との時刻の対応
- 原因から症状までの伝播経路を一文で
- 影響(件数・率・開始時刻・継続時間)
- 推奨対応(仮説が複数ならそれぞれに対して)
SKILL_CONTENT_EOF
)

SKILL_NAME="${SKILL_ID}" SKILL_DESCRIPTION="${SKILL_DESCRIPTION}" SKILL_TOOLS="${SKILL_TOOLS}" SKILL_CONTENT="${SKILL_CONTENT}" \
python3 -c 'import json,os; e=os.environ; print(json.dumps({"name":e["SKILL_NAME"],"description":e["SKILL_DESCRIPTION"],"content":e["SKILL_CONTENT"],"tool_ids":json.loads(e["SKILL_TOOLS"])}))' \
  | upsert skills "${SKILL_ID}"

# ---------------------------------------------------------------
# 2. エージェント(このスキルだけを持つ)
# ---------------------------------------------------------------
python3 -c '
import json, os
print(json.dumps({
  "name": "rca_agent",
  "description": "check_infra の結果を踏まえて根本原因分析を行うエージェント",
  "configuration": {
    "instructions": "入力に含まれるインフラ情報を前提に、スキル rca-lite の手順に従って根本原因を分析する。",
    "tools": [{"tool_ids": ["platform.core.search"]}],
    "skill_ids": [os.environ["SKILL_ID"]],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${AGENT_ID}"
