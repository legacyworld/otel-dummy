#!/usr/bin/env bash
# Agent Builder に次をまとめて作成する(既に存在する場合は更新。まっさらなクラスタでも既存クラスタでも実行できる)
#   - ES|QL ツール infra_host_baseline_24h / infra_host_cpu_mem_load_1m
#   - スキル service-infra-lookup + エージェント check_infra   (アラート直後にホストのメトリクスを集める)
#   - スキル rca-lite + エージェント rca_agent                  (check_infra の結果を踏まえてRCAを行う)
#   - ES|QL ツール infra_host_log_inventory / infra_host_process_logs / infra_host_top_processes + エージェント check_process_log
#                                                              (CPU上位プロセスをメトリクスで特定し、そのログで裏付けを取る)
# 接続設定は generate_basic.py と同じ: /home/elastic/.k8s-vm-env を source して読み込む
#   KIBANA_URL                 https://<host>
#   ELASTICSEARCH_AUTH_BASE64  base64 認証情報
#   ELASTICSEARCH_AUTH_SCHEME  任意。"Basic"(既定) または "ApiKey"
# 使い方: [INFRA_SKILL_ID=..] [INFRA_AGENT_ID=..] [RCA_SKILL_ID=..] [RCA_AGENT_ID=..] [PROCESS_LOG_AGENT_ID=..] ./create_agents.sh
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
INFRA_SKILL_ID="${INFRA_SKILL_ID:-service-infra-lookup}"
INFRA_AGENT_ID="${INFRA_AGENT_ID:-check_infra}"
RCA_SKILL_ID="${RCA_SKILL_ID:-rca-lite}"
RCA_AGENT_ID="${RCA_AGENT_ID:-rca_agent}"
PROCESS_LOG_AGENT_ID="${PROCESS_LOG_AGENT_ID:-check_process_log}"
export INFRA_SKILL_ID INFRA_AGENT_ID RCA_SKILL_ID RCA_AGENT_ID PROCESS_LOG_AGENT_ID
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

# ---------------------------------------------------------------
# 0. ES|QL ツール(ホストのメトリクス取得用。スキル/エージェントから直接呼ばせる)
# ---------------------------------------------------------------
TOOLS_TSV=$(mktemp)
python3 - <<'TOOLS_PY' > "${TOOLS_TSV}"
import json
IDX = "metrics-hostmetricsreceiver.otel-default"
WIN = 'AND @timestamp >= TO_DATETIME(?start) AND @timestamp < TO_DATETIME(?end)'
P_HOST = {"type": "string", "description": "host.name の値"}
P_START = {"type": "string", "description": "取得範囲の開始(ISO8601 UTC 例: 2026-11-13T00:22:00.000Z)。アラート時刻の10分前"}
P_END = {"type": "string", "description": "取得範囲の終了(ISO8601 UTC)。アラート時刻の10分後"}
Q_START = {"type": "string", "description": "取得範囲の開始(ISO8601 UTC 例: 2026-11-13T00:22:00.000Z)"}
Q_END = {"type": "string", "description": "取得範囲の終了(ISO8601 UTC)"}
tools = [
 ("infra_host_cpu_mem_load_1m",
  "ホストのCPU使用率(1-idle)・メモリ使用率・load 1m を1分バケットで返す。start/end はアラート時刻の前後10分。",
  f'''FROM {IDX}
| WHERE host.name == ?host {WIN}
| STATS idle = AVG(metrics.system.cpu.utilization) WHERE state == "idle",
        mem = AVG(metrics.system.memory.utilization),
        load1 = AVG(`metrics.system.cpu.load_average.1m`)
        BY minute = BUCKET(@timestamp, 1 minute)
| EVAL cpu = 1 - idle
| SORT minute''',
  {"host": P_HOST, "start": P_START, "end": P_END}),
 ("infra_host_baseline_24h",
  "ホストの直近24時間平均(ベースライン): CPU使用率(1-idle)・メモリ使用率・load 1m を返す。",
  f'''FROM {IDX}
| WHERE host.name == ?host AND @timestamp >= NOW() - 24 hours
| STATS idle = AVG(metrics.system.cpu.utilization) WHERE state == "idle",
        mem = AVG(metrics.system.memory.utilization),
        load1 = AVG(`metrics.system.cpu.load_average.1m`)
| EVAL cpu = 1 - idle''',
  {"host": P_HOST}),
 ("infra_host_log_inventory",
  "ホストのsyslogを process.name ごとに棚卸しする。検索語なしで、窓内(start〜end)に出ているログの発生元を広く洗い出す。"
  "n_win は窓内のログ件数、n_base は base_start〜end 全体の件数、win_ratio=n_win/n_base(1に近いほどその発生元のログが窓内に集中している=普段は出ない・単発のジョブ)。"
  "first_win/last_win は窓内で最初/最後に出た時刻。start/end はアラート時刻の60分前〜10分後、base_start はその24時間前を推奨。",
  '''FROM logs-system.syslog-default
| WHERE host.name == ?host AND @timestamp >= TO_DATETIME(?base_start) AND @timestamp < TO_DATETIME(?end)
| EVAL in_win = @timestamp >= TO_DATETIME(?start)
| EVAL wt = CASE(in_win, @timestamp, NULL)
| STATS n_base = COUNT(*), n_win = COUNT(wt), first_win = MIN(wt), last_win = MAX(wt) BY process.name
| WHERE n_win > 0
| EVAL win_ratio = ROUND(n_win * 1.0 / n_base, 3)
| SORT win_ratio DESC, first_win
| LIMIT 20''',
  {"host": P_HOST, "start": Q_START, "end": Q_END,
   "base_start": {"type": "string", "description": "比較用の基準期間の開始(ISO8601 UTC。start の24時間前を推奨)"}}),
 ("infra_host_top_processes",
  "ホスト上でCPU時間を多く使ったプロセスの上位を返す(ログで見つけたジョブの実行を、メトリクス側で裏付けるために使う。CPUが低いジョブもあるため、これだけを入口にしない)(プロセスのCPU累積時間の増加分から算出)。"
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
  {"host": P_HOST, "start": Q_START, "end": Q_END}),
 ("infra_host_process_logs",
  "ホストのsyslogから、keyword(大文字小文字を区別しない、単語単位の一致)がプロセス名と一致するか、メッセージ中に単語として含まれるログを時刻順に返す。"
  "プロセスの起動・終了・実行内容を示すログの確認に使う。keyword には棚卸し(infra_host_log_inventory)やメトリクスの結果に出てきたプロセス名・コマンドの語を渡す。"
  "start/end はログを探す時間範囲(アラート時刻の60分前〜10分後を推奨)。",
  '''FROM logs-system.syslog-default
| WHERE host.name == ?host AND @timestamp >= TO_DATETIME(?start) AND @timestamp < TO_DATETIME(?end)
| EVAL kw = TO_LOWER(?keyword), words = CONCAT(" ", REPLACE(TO_LOWER(message), "[^a-z0-9_.]", " "), " ")
| WHERE TO_LOWER(process.name) == kw OR CONTAINS(words, CONCAT(" ", kw, " "))
| SORT @timestamp
| KEEP @timestamp, process.name, message
| LIMIT 20''',
  {"host": P_HOST, "start": Q_START, "end": Q_END,
   "keyword": {"type": "string", "description": "検索語(プロセス名・コマンド名など)。例: メトリクスで見つけたプロセスの proc / cmd の語"}}),
]
for id_, desc, q, params in tools:
    print(id_ + "\t" + json.dumps({"type": "esql", "description": desc, "tags": ["check_infra"],
          "configuration": {"query": q, "params": params}}))
TOOLS_PY
while IFS=$'\t' read -r id body; do
  printf '%s' "${body}" | upsert tools "${id}"
done < "${TOOLS_TSV}"
rm -f "${TOOLS_TSV}"

# ---------------------------------------------------------------
# 1. スキル(service-infra-lookup: check_infra 用)
# ---------------------------------------------------------------
SKILL_DESCRIPTION='サービス(およびデータベースやキャッシュ、上流/下流の呼び出し元などの関連・依存モジュール)がどのインフラ、ホスト、サーバー上で動作しているかをユーザーが尋ねた場合に使用する。「Xはどこで動いている?」「Xのインフラは何?」「Xとその関連モジュールが動作しているインフラ」といった表現、またはサービスの依存関係トポロジーを具体的なホスト/クラウドプロバイダにマッピングする依頼でトリガーする。サービスが不健全な理由を診断する用途には使わない(その場合は調査系スキルを使うこと)— これは読み取り専用のインフラ調査スキルである。'
SKILL_TOOLS='["observability.get_service_topology", "observability.get_trace_metrics", "observability.get_hosts", "observability.get_services", "observability.get_index_info"]'
SKILL_CONTENT=$(cat <<'SKILL_CONTENT_EOF'
## 使用するタイミング

ユーザーがサービスとその関連モジュール(依存先、呼び出し元、データベース、キャッシュなど)が物理的/仮想的に**どこで**動作しているかを知りたい場合、すなわちサービスをホスト・クラウドプロバイダ・IPにマッピングしたい場合にこのスキルを使用する。これは読み取り専用の調査であり、インシデント調査ではない。

ユーザーが「なぜ」何かが遅い/エラーになっているかを診断したい場合はこのスキルを使わないこと — それはヘルス調査であり、インフラ調査ではない。

## 使用ツール

- `observability.get_service_topology` — サービスの直接的な依存先・呼び出し元を調べる(direction: "both")。これが出発点であり、どのサービス/リソース(DB、キャッシュ、上流フロントエンドなど)が「関連モジュール」であるかを教えてくれる。
- `observability.get_trace_metrics`(groupBy: "host.name") — トポロジーだけではホストが分からない場合に、特定サービスのトランザクションが実際に実行されたホストを特定する。
- `observability.get_hosts` — 上記で見つけたホスト名について、インフラのメタデータ(ホスト名、クラウドプロバイダ、IP、OS)と基本的なヘルスメトリクスを取得する。ホスト名の一部しか分からない場合は `host.name: *keyword*` のようなワイルドカード指定の `kqlFilter` でも検索できる。
- `observability.get_services` — 関連ノード(例えば予期せず出てきたホスト名)が本当に既知のサービスに対応しているかを確認し、無関係なホストを誤って紐付けないためのフォールバックとして使う。
- `observability.get_index_info`(operation: get-field-values) — trace_metrics/topologyが直接host.nameを返さない場合(例:独自のAPMエージェントを持たないDB/キャッシュの宛先)に、kqlFilterでそのservice.nameに絞ってhost.name / cloud.providerの値を調べる。

## ワークフロー

1. **対象サービス名を特定する** ユーザーのリクエストから対象サービス名を特定する(曖昧な場合は質問するか、`observability.get_services`で存在を確認する)。
2. **トポロジーを取得する** `get_service_topology(serviceName, direction: "both", start: now-24h, end: now)`を呼ぶ。トポロジーは変化が遅いため、短時間の調査であっても24時間のウィンドウが適切である。これにより「関連モジュール」の集合(下流の依存先、例:redis、データベースと、上流の呼び出し元である他のサービス)が得られる。トポロジーの結果で依存先が `service.name` ではなく `span.destination.service.resource`(例:"redis")として表現されている場合、それはAPMで独立計装されていない非サービス型の依存先(DB/キャッシュ)であることを意味する。
3. **対象サービスおよび各関連モジュールについて、そのホストを特定する**:
   - まず`get_trace_metrics(kqlFilter='service.name: "X"', groupBy: "host.name")`を使い、サービスのトランザクションが実行されたホスト名を取得する。これは対象が独自の`service.name`を持つAPMサービスである場合にのみ有効。
   - これが空の場合、`get_index_info(operation: "get-field-values", fields: ["host.name","cloud.provider"], kqlFilter: 'service.name: "X"')`をログ/メトリクス/トレースのインデックスに絞って使うフォールバックを行う。
   - **重要 — 非計装の依存先(DB/キャッシュ)の場合:** topologyで`span.destination.service.resource`(例:"redis")として表現される依存先は`service.name`の値を持たない。そのため`service.name: "redis"`のようなフィルタでget_trace_metrics/get_index_infoを呼んでも一致せず、空の結果や無関係な全ホスト一覧が返ることがある — これを「ホストが見つからない」と判断してはいけない。代わりに`get_hosts(kqlFilter: 'host.name: *<リソース名>*')`(例:`host.name: *redis*`)のようにホスト名にリソース名を含むワイルドカード検索を行うこと。このワイルドカード検索でも見つからない場合に初めて「ホストを特定できない」と報告する。
   - 注意:ブラウザ/webjsエージェント(フロントエンド)は通常サーバー側のホストを持たない — 推測せずにその旨を明示的に報告すること。
4. **特定した各ホスト名について、次の2つの専用ツールを直ちに呼ぶ**(後段のエージェントに渡すため、これ以外の指標・考察は取得も出力もしない)。**インデックスの探索や ES|QL の自作、`get_hosts` / `list_indices` / `get_index_mapping` は使わない。**
   - `infra_host_baseline_24h(host)` — ベースライン(24時間平均): cpu(=1-idle)・mem・load1。
   - `infra_host_cpu_mem_load_1m(host, start, end)` — 1分バケットの cpu・mem・load1。
   - `start` / `end` はアラート発生時刻 T の `T-10m` / `T+10m`(ISO8601 UTC、例 `2026-11-13T00:22:00.000Z`)。T が与えられていない場合は T = 現在時刻とする。
   - ホスト名が `host.name` の値と一致せず空の結果になった場合のみ、ホスト名を確認し直す。
5. **予期しないホストの妥当性を確認する**: ホスト名が調査対象のサービスに明確に属していない場合(例:共有インフラ)、`get_services(kqlFilter: 'host.name: "<name>"')`で照合し、そのホスト上で他に何が動いているかを確認し、無関係であれば除外する。
6. **コンポーネントごとに、下記「出力形式」のとおり出力する**。識別できるホストがないコンポーネント(例:ブラウザ上で実行されるフロントエンド)は、ホストをでっち上げず `host: none` とする。

## 補足

- **出力形式(厳守)**: この出力は次のエージェントにそのまま渡される。前置き・説明・考察・原因の推測・まとめ文は一切書かず、下記の形式の事実のみを簡潔に出力する。
  ```
  <サービス/モジュール名> → <ホスト名> (<クラウドプロバイダ>, <IP>, <OS>)
    baseline_24h: cpu=<値> mem=<値> load=<値>
    HH:MM cpu=<値> mem=<値> load=<値>   ← 1分バケット、T-10m〜T+10m の21行
  ```
  ホストが特定できないコンポーネントは `<名前> → host: none` の1行のみ。

- トポロジーのdirection "both"は重要である — 依存先(下流)と呼び出し元(上流)の両方が、ユーザーが意図する「関連モジュール」に含まれる。
- 「get_index_infoの無関係な結果に偶然出てきたホスト」と「そのサービスを実際に動かしているホスト」を混同しないこと — ホストをサービスに紐付ける前に、必ずget_servicesまたはget_trace_metricsで照合すること。
- DB/キャッシュなど`service.name`を持たない依存先については、`service.name`ベースのフィルタが効かないことを前提に、ホスト名のワイルドカード検索を標準の代替手段として扱うこと。
SKILL_CONTENT_EOF
)

SKILL_NAME="${INFRA_SKILL_ID}" SKILL_DESCRIPTION="${SKILL_DESCRIPTION}" SKILL_TOOLS="${SKILL_TOOLS}" SKILL_CONTENT="${SKILL_CONTENT}" \
python3 -c 'import json,os; e=os.environ; print(json.dumps({"name":e["SKILL_NAME"],"description":e["SKILL_DESCRIPTION"],"content":e["SKILL_CONTENT"],"tool_ids":json.loads(e["SKILL_TOOLS"])}))' \
  | upsert skills "${INFRA_SKILL_ID}"

# ---------------------------------------------------------------
# 2. エージェント(check_infra)
# ---------------------------------------------------------------
python3 -c '
import json, os
print(json.dumps({
  "name": "check_infra",
  "description": "インフラのチェックを行うエージェント",
  "configuration": {
    "instructions": "",
    "tools": [{"tool_ids": [
      "platform.core.search",
      "platform.core.list_indices",
      "platform.core.get_index_mapping",
      "platform.core.generate_esql",
      "platform.core.execute_esql",
      "platform.core.get_document_by_id",
      "platform.core.get_workflow_execution_status",
      "platform.core.resume_workflow_execution",
      "platform.core.list_workflow_executions",
      "platform.core.generate_workflow",
      "platform.core.execute_workflow",
      "infra_host_cpu_mem_load_1m",
      "infra_host_baseline_24h"
    ]}],
    "skill_ids": [os.environ["INFRA_SKILL_ID"]],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${INFRA_AGENT_ID}"

# ---------------------------------------------------------------
# 3. スキル(rca-lite: rca_agent 用)
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

SKILL_NAME="${RCA_SKILL_ID}" SKILL_DESCRIPTION="${SKILL_DESCRIPTION}" SKILL_TOOLS="${SKILL_TOOLS}" SKILL_CONTENT="${SKILL_CONTENT}" \
python3 -c 'import json,os; e=os.environ; print(json.dumps({"name":e["SKILL_NAME"],"description":e["SKILL_DESCRIPTION"],"content":e["SKILL_CONTENT"],"tool_ids":json.loads(e["SKILL_TOOLS"])}))' \
  | upsert skills "${RCA_SKILL_ID}"

# ---------------------------------------------------------------
# 4. エージェント(rca_agent: rca-lite だけを持つ)
# ---------------------------------------------------------------
python3 -c '
import json, os
print(json.dumps({
  "name": "rca_agent",
  "description": "check_infra の結果を踏まえて根本原因分析を行うエージェント",
  "configuration": {
    "instructions": "入力に含まれるインフラ情報を前提に、スキル rca-lite の手順に従って根本原因を分析する。",
    "tools": [{"tool_ids": ["platform.core.search"]}],
    "skill_ids": [os.environ["RCA_SKILL_ID"]],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${RCA_AGENT_ID}"

# ---------------------------------------------------------------
# 5. エージェント(check_process_log: check_infra が特定したホストのログ全体を棚卸しし、メトリクスで裏付ける。ログのキーワードは指示に書かない)
# ---------------------------------------------------------------
INSTRUCTIONS=$(cat <<'INSTR_EOF'
あなたは、アラート発生時刻の近辺でホスト上で何が動いていたかを、ログ全体から広く洗い出し、メトリクスで裏付ける調査エージェントです。
入力には check_infra の結果(対象ホスト名)とアラート発生時刻 T が含まれます。ホストは入力のものをそのまま使い、再調査しないでください。
CPU が高いプロセスだけを調べるのは不十分です。CPU をほとんど使わないジョブもあるため、入口はログ全体の棚卸しにします。

手順(この順に実行する):
1. infra_host_log_inventory を各対象ホストについて、start=T-60m, end=T+10m, base_start=T-24h-60m(ISO8601 UTC)で呼び、窓内にログを出した発生元(process.name)を一覧する。
2. 一覧から調査候補を選ぶ。選ぶ基準は「窓内にログが集中している(win_ratio が高い、n_base が小さい)」「窓内で最初に出た時刻(first_win)が T の近く」。
   常時ログを出し続けている発生元(件数が多く win_ratio が低いもの)は、窓内に新しく始まったものがなければ候補にしない。
   特定の発生元の名前を前提にせず、棚卸しの結果から判断すること。
3. 各候補の process.name をキーに infra_host_process_logs を start=T-60m, end=T+10m で呼び、ログの中身(何を実行したか、開始・終了を示す文言)を確認する。
   ログの message に別のプロセス名・コマンド名が出てくれば、その語でも検索して、関連するログを集める。必ず結果に出てきた語だけを使い、推測で語を作らない。
4. 候補が実行していたことをメトリクスで裏付ける。infra_host_top_processes を start=T-10m, end=T+10m で呼び、ログの内容やコマンドと対応するプロセス(proc / cmd / ppid)が窓内でどれだけ CPU を使い、いつから動いていたか(first_seen)を確認する。
   対応するプロセスが見つからない、または CPU が小さい場合も、その事実をそのまま書く。
5. ログとメトリクスの時刻を照合する。
   - ログの開始時刻と first_seen が近いか
   - 終了を示すログがあるか。あれば T より前か後か(後なら、アラート時点でも実行中だった)

出力形式(厳守。前置き・考察・原因の推測・対処の提案は書かない。事実のみ):
```
<ホスト名>
  candidate: <process.name> n_win=<値> win_ratio=<値> first_win=<時刻>     ← 候補ごとに1行
  log: <時刻> <process.name> <message>     ← 候補の該当ログを時刻順にすべて
  metrics: <proc> pid=<pid> cpu_cores=<値> first_seen=<時刻> last_seen=<時刻> cmd=<cmd>   ← 対応するプロセスがなければ none
  timeline: <ログと first_seen の関係、終了ログの有無とTとの前後関係を1〜2行>
```
窓内に候補とすべきログがなかった場合は `candidate: none` とし、見つかったふりをしない。
INSTR_EOF
)

INSTRUCTIONS="${INSTRUCTIONS}" python3 -c '
import json, os
print(json.dumps({
  "name": "check_process_log",
  "description": "アラート近辺のログ全体から窓内に集中して出た発生元を洗い出し、その実行内容をログとメトリクスで裏付けるエージェント",
  "configuration": {
    "instructions": os.environ["INSTRUCTIONS"],
    "tools": [{"tool_ids": ["infra_host_log_inventory", "infra_host_process_logs", "infra_host_top_processes"]}],
    "skill_ids": [],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${PROCESS_LOG_AGENT_ID}"
