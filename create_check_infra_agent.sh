#!/usr/bin/env bash
# Agent Builder のスキル(service-infra-lookup)とエージェント(check_infra)を作成する
# 既に存在する場合は更新するので、まっさらなクラスタでも既存クラスタでも実行できる
# 接続設定は generate_basic.py と同じ: /home/elastic/.k8s-vm-env を source して読み込む
#   KIBANA_URL                 https://<host>
#   ELASTICSEARCH_AUTH_BASE64  base64 認証情報
#   ELASTICSEARCH_AUTH_SCHEME  任意。"Basic"(既定) または "ApiKey"
# 使い方: [SKILL_ID=service-infra-lookup] [AGENT_ID=check_infra] ./create_check_infra_agent.sh
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
# usage: upsert <skills|agents> <id>   (本文 JSON は標準入力、id なし)
upsert() {
  local kind="$1" id="$2" body status
  body=$(cat)
  status=$(kb -o /dev/null -w '%{http_code}' "${BASE}/${kind}/${id}")
  if [ "${status}" = "200" ]; then
    echo "== update ${kind}: ${id}"
    printf '%s' "${body}" | kb -X PUT "${BASE}/${kind}/${id}" -d @-
  else
    echo "== create ${kind}: ${id}"
    printf '%s' "${body}" | ID="${id}" python3 -c 'import sys,json,os; d=json.load(sys.stdin); d["id"]=os.environ["ID"]; print(json.dumps(d))' \
      | kb -X POST "${BASE}/${kind}" -d @-
  fi
  echo
}

# ---------------------------------------------------------------
# 1. スキル
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
4. **特定した各ホスト名について、時間窓を変えて `get_hosts` を2回呼ぶ**: `get_hosts` は指定した窓の集計値(平均)を1つ返すだけで、既定は直近1時間である。長い窓だけを使うと、今まさに起きているスパイクが平均に埋もれて見えなくなる。必ず次の2つを取得して比較すること。
   - **直近(現在の状態)**: `get_hosts(kqlFilter: 'host.name: "<name>"', start: "now-5m", end: "now")` — 現在の状態を表す。最優先で報告する値。
   - **ベースライン(平常時との比較)**: `get_hosts(kqlFilter: 'host.name: "<name>"', start: "now-1h", end: "now")` — 比較用。24時間のような長い窓は使わない。
   - 直近の窓でデータが空の場合は `now-15m` に広げて再取得し、使った窓を明記する。
   - クラウドプロバイダ、IP、OS はどちらの窓からも取れる。CPU、メモリ、ディスク、ネットワーク、負荷は、直近値とベースライン値の両方を併記し、直近値がベースラインより明らかに高い項目を指摘する(診断はせず、事実のみ)。
   - アラートの時刻が分かっている場合は、アラート発生時刻を含む短い窓(例: アラート時刻の前後5〜10分)も使う。
5. **予期しないホストの妥当性を確認する**: ホスト名が調査対象のサービスに明確に属していない場合(例:共有インフラ)、`get_services(kqlFilter: 'host.name: "<name>"')`で照合し、そのホスト上で他に何が動いているかを確認し、無関係であれば除外する。
6. **コンポーネントごとにまとめる**: サービス/モジュール名 → ホスト名 → クラウドプロバイダ → IP → 基本的なヘルス状態。識別できるホストがないコンポーネント(例:ブラウザ上で実行されるフロントエンド)については、ホストをでっち上げずにその旨を明示すること。

## 補足

- ヘルスメトリクスは「平均」だけで判断しない。長い窓の平均は現在の異常を隠すため、直近の短い窓の値を必ず併記する。

- トポロジーのdirection "both"は重要である — 依存先(下流)と呼び出し元(上流)の両方が、ユーザーが意図する「関連モジュール」に含まれる。
- 「get_index_infoの無関係な結果に偶然出てきたホスト」と「そのサービスを実際に動かしているホスト」を混同しないこと — ホストをサービスに紐付ける前に、必ずget_servicesまたはget_trace_metricsで照合すること。
- DB/キャッシュなど`service.name`を持たない依存先については、`service.name`ベースのフィルタが効かないことを前提に、ホスト名のワイルドカード検索を標準の代替手段として扱うこと。
SKILL_CONTENT_EOF
)

SKILL_NAME="${SKILL_ID}" SKILL_DESCRIPTION="${SKILL_DESCRIPTION}" SKILL_TOOLS="${SKILL_TOOLS}" SKILL_CONTENT="${SKILL_CONTENT}" \
python3 -c 'import json,os; e=os.environ; print(json.dumps({"name":e["SKILL_NAME"],"description":e["SKILL_DESCRIPTION"],"content":e["SKILL_CONTENT"],"tool_ids":json.loads(e["SKILL_TOOLS"])}))' \
  | upsert skills "${SKILL_ID}"

# ---------------------------------------------------------------
# 2. エージェント
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
      "platform.core.execute_workflow"
    ]}],
    "skill_ids": [os.environ["SKILL_ID"]],
    "enable_elastic_capabilities": False,
    "workflow_ids": [],
    "plugin_ids": []
  }
}))' | upsert agents "${AGENT_ID}"
