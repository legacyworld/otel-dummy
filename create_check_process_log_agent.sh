#!/usr/bin/env bash
# Agent Builder の ES|QL ツール(infra_host_top_processes / infra_host_process_logs)と
# エージェント(check_process_log)を作成する。既に存在する場合は更新する。
#
# 目的: check_infra が特定したホストについて、アラート近辺で CPU を使っていたプロセスを
#       メトリクスから特定し、そのプロセスに関するログ(起動・終了など)で裏付けを取る。
#       指示文にはログのキーワードを書かない(キーワードはメトリクスから得る)。
#
# 接続設定: このスクリプトと同じディレクトリの .env を読み込む
#   KIBANA_URL / ELASTIC_API_KEY
# 使い方: [AGENT_ID=check_process_log] ./create_check_process_log_agent.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset KIBANA_URL ELASTIC_API_KEY
set -a; . "${DIR}/.env"; set +a
: "${KIBANA_URL:?KIBANA_URL is not set (.env)}"
: "${ELASTIC_API_KEY:?ELASTIC_API_KEY is not set (.env)}"
AGENT_ID="${AGENT_ID:-check_process_log}"
export AGENT_ID
BASE="${KIBANA_URL%/}/api/agent_builder"

kb() { curl -sS -H "Authorization: ApiKey ${ELASTIC_API_KEY}" -H "kbn-xsrf: true" -H "Content-Type: application/json" "$@"; }

# 存在すれば PUT(id なし)、なければ POST(id 付き)で作成する
# usage: upsert <tools|agents> <id>   (本文 JSON は標準入力、id なし)
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

# ---------------------------------------------------------------
# 1. ES|QL ツール
# ---------------------------------------------------------------
TOOLS_TSV=$(mktemp)
python3 - <<'TOOLS_PY' > "${TOOLS_TSV}"
import json
P_HOST = {"type": "string", "description": "host.name の値(check_infra が特定したホスト)"}
P_START = {"type": "string", "description": "取得範囲の開始(ISO8601 UTC 例: 2026-11-13T00:22:00.000Z)"}
P_END = {"type": "string", "description": "取得範囲の終了(ISO8601 UTC)"}
tools = [
 ("infra_host_top_processes",
  "ホスト上でCPU時間を多く使ったプロセスの上位を返す(プロセスのCPU累積時間の増加分から算出)。"
  "cpu_cores は窓内の平均CPUコア使用数、first_seen/last_seen は窓内でそのプロセスが観測された最初/最後の時刻"
  "(窓の途中で起動したプロセスは first_seen が窓の開始より後になる)。start/end はアラート時刻の前後10分。",
  '''FROM metrics-hostmetricsreceiver.otel-default
| WHERE host.name == ?host AND metrics.process.cpu.time IS NOT NULL
        AND @timestamp >= TO_DATETIME(?start) AND @timestamp < TO_DATETIME(?end)
| EVAL ct = metrics.process.cpu.time::DOUBLE
| STATS t0 = MIN(@timestamp), t1 = MAX(@timestamp), mn = MIN(ct), mx = MAX(ct)
        BY proc = process.executable.name, pid = resource.attributes.process.pid,
           ppid = resource.attributes.process.parent_pid, cmd = resource.attributes.process.command_line, state
| STATS cpu_sec = SUM(mx - mn), first_seen = MIN(t0), last_seen = MAX(t1) BY proc, pid, ppid, cmd
| EVAL secs = DATE_DIFF("seconds", first_seen, last_seen)
| EVAL cpu_cores = ROUND(cpu_sec / secs, 3)
| WHERE secs > 0
| SORT cpu_sec DESC
| KEEP proc, pid, ppid, cmd, cpu_cores, first_seen, last_seen
| LIMIT 5''',
  {"host": P_HOST, "start": P_START, "end": P_END}),
 ("infra_host_process_logs",
  "ホストのsyslogから、keyword(大文字小文字を区別しない、単語単位の一致)がプロセス名と一致するか、メッセージ中に単語として含まれるログを時刻順に返す。"
  "プロセスの起動・終了・実行内容を示すログの裏付け確認に使う。keyword にはメトリクスで見つけたプロセス名やコマンドの語を渡す。"
  "start/end はログを探す時間範囲(アラート時刻の60分前〜10分後を推奨)。",
  '''FROM logs-system.syslog-default
| WHERE host.name == ?host AND @timestamp >= TO_DATETIME(?start) AND @timestamp < TO_DATETIME(?end)
| EVAL kw = TO_LOWER(?keyword), words = CONCAT(" ", REPLACE(TO_LOWER(message), "[^a-z0-9_.]", " "), " ")
| WHERE TO_LOWER(process.name) == kw OR CONTAINS(words, CONCAT(" ", kw, " "))
| SORT @timestamp
| KEEP @timestamp, process.name, message
| LIMIT 20''',
  {"host": P_HOST, "start": P_START, "end": P_END,
   "keyword": {"type": "string", "description": "検索語(プロセス名・コマンド名など)。例: メトリクスで見つけたプロセスの proc / cmd の語"}}),
]
for id_, desc, q, params in tools:
    print(id_ + "\t" + json.dumps({"type": "esql", "description": desc, "tags": ["check_process_log"],
          "configuration": {"query": q, "params": params}}))
TOOLS_PY
while IFS=$'\t' read -r id body; do
  printf '%s' "${body}" | upsert tools "${id}"
done < "${TOOLS_TSV}"
rm -f "${TOOLS_TSV}"

# ---------------------------------------------------------------
# 2. エージェント(ログのキーワードは指示に書かない)
# ---------------------------------------------------------------
INSTRUCTIONS=$(cat <<'INSTR_EOF'
あなたは、アラート発生時刻の近辺でホスト上で何が動いていたかを、メトリクスとログで裏付ける調査エージェントです。
入力には check_infra の結果(対象ホスト名)とアラート発生時刻 T が含まれます。ホストは入力のものをそのまま使い、再調査しないでください。

手順(この順に実行する):
1. infra_host_top_processes を、各対象ホストについて start=T-10m, end=T+10m(ISO8601 UTC)で呼び、CPU を多く使ったプロセスを確認する。
   CPU 使用が明らかに突出しているプロセス(cpu_cores が他と比べて大きいもの)だけを調査対象にする。突出したものがなければ「突出したプロセスなし」と報告して終了する。
2. 調査対象のプロセスについて、proc / cmd / ppid から検索語を決め、infra_host_process_logs を start=T-60m, end=T+10m で呼ぶ。
   1回で見つからなければ、cmd に含まれる別の語でも試す。ただし推測だけで検索語を作らず、必ずメトリクスやログの結果に出てきた語を使う。
   ログが見つかったら、そのログの process.name(ジョブ名・スクリプト名など)をキーにもう一度検索し、同じジョブの起動・終了などの前後のログも集める。
3. ログとメトリクスの時刻を照合する。
   - ログの起動時刻と first_seen、CPU 上昇の開始が近いか
   - 終了を示すログがあるか。あれば T より前か後か(後なら、アラート時点でも実行中だった)

出力形式(厳守。前置き・考察・原因の推測・対処の提案は書かない。事実のみ):
```
<ホスト名>
  top_process: <proc> pid=<pid> cpu_cores=<値> first_seen=<時刻> last_seen=<時刻> cmd=<cmd>
  log: <時刻> <process.name> <message>     ← 該当ログを時刻順にすべて
  timeline: <ログと first_seen の関係、終了ログの有無とTとの前後関係を1〜2行>
```
該当ログが見つからなかった場合は `log: none` とし、見つかったふりをしない。
INSTR_EOF
)

INSTRUCTIONS="${INSTRUCTIONS}" python3 -c '
import json, os
print(json.dumps({
  "name": "check_process_log",
  "description": "アラート近辺でCPUを使っていたプロセスをメトリクスで特定し、そのプロセスの起動・終了ログで裏付けを取るエージェント",
  "configuration": {
    "instructions": os.environ["INSTRUCTIONS"],
    "tools": [{"tool_ids": ["infra_host_top_processes", "infra_host_process_logs"]}],
    "skill_ids": [],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${AGENT_ID}"
