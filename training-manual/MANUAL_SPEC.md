# ハンズオントレーニング マニュアル作成用 仕様書

> **この文書の目的**: ハンズオントレーニング用マニュアルを AI に作らせるための「唯一の情報源」。
> 元プロジェクト(`otel-dummy`)のソースコードは読まなくてよい。**この文書に書かれた仕様・動作・表示内容だけからマニュアルを書くこと。**
> 作成日: 2026-11-13 / 元リポジトリ: `otel-dummy`(HEAD `eceb996` + 作業ツリーの未コミット変更を反映)

---

## 0. AI への指示(最初に必ず読む)

1. 親フォルダ(`../`)のソースコード・スクリプトを開かない。情報が足りなければ推測せず、§14「未確定事項」に従って質問するか `【要確認】` と書く。
2. 出力先は `training-manual/output/`。この仕様書(`MANUAL_SPEC.md`)は変更しない。
3. 本書の値(ホスト名、サービス名、ID、画面文言、メッセージ)は**そのまま**使う。言い換え・翻訳をしない(画面の文言は英語のまま)。
4. 受講者が「実際に見える画面」と「実行するコマンド」を中心に書く。内部実装の説明は最小限にする。
5. 手順ごとに「期待される結果」を書く。時間がかかる処理には所要時間の目安を書く(§11)。
6. マニュアルの言語は日本語。
7. 構成案は §13 を出発点にしてよいが、受講者像・所要時間が決まっていれば §14 の回答を優先する。

---

## 1. トレーニングで体験させること(ストーリー)

> **「Redis サーバの CPU が夜間バックアップの暴走で飽和し、それを使うアプリが遅くなる。この障害を、Elastic Observability + AI エージェントが自動で検知 → インフラ特定 → 根本原因分析 → 過去事例照合 → 人間の承認 → 自動修復までつなぐ。」**

受講者が体験する流れ(エンドツーエンド):

```
ダミーデータ生成(OTel形式)
   │  Redis ホストの CPU 上昇 → アプリのレイテンシ上昇
   ▼
アラートルール(ES|QL + CHANGE_POINT)が検知
   ▼
Workflow「Alert Infra Check and Root Cause Investigation」が起動
   ├─ ① check_infra エージェント : 関連ホストと CPU/メモリ/負荷を収集
   ├─ ② rca_agent エージェント   : 根本原因分析(RCA)
   ├─ ③ past-incidents を意味検索 : 同一事象の自動修復可能な過去事例を探す
   ├─ ④ 承認依頼を IT Service Portal に登録 → Workflow は承認待ちで停止
   │       人間が /approval 画面で RCA と Infra Check を確認 → Approve / Reject
   └─ ⑤ 承認されたら /remediate_service で修復を実行
   ▼
CPU 負荷が下がり、レイテンシが平常に復帰
```

## 2. 登場する環境・コンポーネント

| 区分 | 名前 | 内容 |
|---|---|---|
| 基盤 | Elasticsearch / Kibana(Serverless Observability) | データ投入先。Kibana の Alerts / Workflows / Agent Builder を使う |
| 接続情報 | `/home/elastic/.k8s-vm-env` | 演習 VM 上のファイル。`KIBANA_URL` / `ELASTICSEARCH_URL` / `ELASTICSEARCH_AUTH_BASE64` / `ELASTICSEARCH_AUTH_SCHEME`(任意)を定義。**スクリプトが自動で読む。受講者が値を入力する必要はない** |
| 演習 VM | `host-1` | 承認ポータルが `host-1:3000` で動く。スクリプトは `/home/elastic/workshop-scripts` に配置 |
| ダミーデータ生成 | `generate_basic.py` | `generate.py` と同じ機能で、上記 env ファイルから接続情報を読む版。演習ではこちらを起動 |
| 負荷操作 | `cpu_load.py` | 実行中の生成プログラムに対し CPU 負荷を手動で上げ下げ |
| 承認ポータル | `server.py`(FastAPI、ポート 3000) | 画面名「IT Service Portal」。ServiceNow 風の承認画面と修復 API |
| 初期設定 | `create_agents.sh` / `create_rule.sh` / `create_past_incidents.sh` | エージェント・アラートルール・過去事例を作る(§5〜§8) |

ダミー環境上の「見かけ上のシステム」:

| 論理名 | 値 |
|---|---|
| アプリ VM(ホスト名) | `sample-otel-demo-app`(IP 10.146.0.37) |
| Redis VM(ホスト名) | `sample-otel-demo-redis`(IP 10.146.0.29) |
| APIサービス(Spring Boot/Tomcat) | `test-card-service`(`GET /api/status` → Redis `SETEX`) |
| フロント(ブラウザ計測 OTel webjs) | `java-status-api-frontend-aws` |
| Redis | `redis-cache`(Redis 7.2.4、ポート 6379) |

依存関係: フロント → `test-card-service` → Redis(`SETEX` と `PING`)。Redis は `sample-otel-demo-redis` 上で動く。

## 3. ダミーデータ(OTel形式)の仕様

### 3.1 起動と動作

- 起動: `python3 generate_basic.py`(ローカル検証用の `generate.py` は `.env` を使う別版。**演習では generate_basic.py を使う**【要確認: 演習で受講者が起動するのか、事前に起動済みか】)。
- 起動すると: ①自分が書くデータストリームだけ削除 → ②直近 **115 分**の履歴を投入 → ③以降 **10 秒ごと**に現在時刻のデータを投入し続ける(Ctrl-C で停止)。
- `--dry-run`: クラスタに触れず、CPU と遅延の対応表と件数だけ表示。`--no-live`: 履歴のみ。`--no-reset`: 削除しない。`--interval`: ライブ間隔秒(既定 10)。
- エラー時は終了コード 1。

### 3.2 生成されるデータストリーム

| データストリーム | 内容 |
|---|---|
| `traces-generic.otel-default` | フロント → `GET /api/status` → `StatusController.runRedisSet` → Redis `SETEX`、および `PING` |
| `logs-generic.otel-default` | アプリログ(起動ログ、`API Hit`)、フロントの navigation timing イベント |
| `metrics-hostmetricsreceiver.otel-default` | 2VM の cpu / load / memory / disk / filesystem / network / processes とプロセス別メトリクス。負荷中は `nightly_backup.sh` / `tar` / `gzip` のプロセスが現れる |
| `metrics-generic.otel-default` | JVM / OTel SDK / `http.server.request.duration` / `db.client.operation.duration` |
| `metrics-{transaction,service_transaction,service_summary,service_destination}.{1m,10m,60m}.otel-default` | APM 集計メトリクス(レイテンシ・スループット) |
| `logs-system.syslog-default` / `logs-system.auth-default` | Filebeat 形式の syslog / auth |

### 3.3 平常時の値

- トラフィック: 30 リクエスト/分。Redis `PING` は 36 秒間隔。
- 平常時 CPU: app 1.5%、redis 1.5%。
- `test-card-service` の平均トランザクション時間: **約 5.7 ms**(過去事例 INC-0001 の記述より)。エラー率 0%。

### 3.4 CPU 負荷とレイテンシの関係(核心)

Redis ホストの CPU% が上がると、Redis `SETEX` の平均レイテンシ(ms)が下表の折れ線補間で上がる(対数正規分布でばらつく)。それに比例してアプリのトランザクション時間も上がる。**エラーは発生せず、性能劣化のみ。**

| Redis CPU% | 0 | 5 | 10 | 15 | 25 | 35 | 55 | 80 | 100 |
|---|---|---|---|---|---|---|---|---|---|
| SETEX 平均レイテンシ ms | 0.8 | 1.2 | 8 | 16 | 90 | 115 | 160 | 260 | 400 |

### 3.5 負荷中に現れるもの(RCA の手がかり)

負荷フェーズ中、Redis ホストに次が現れる(受講者が RCA 結果で確認すべき証拠):

- **メトリクス**: CPU 使用率・load average の上昇。プロセス一覧に `nightly_backup.sh`(`/bin/bash /opt/backup/nightly_backup.sh`、root)、`tar`、`gzip` が出現。
- **syslog(`/var/log/syslog`、`logs-system.syslog-default`)**:
  - `CRON` : `(root) CMD (/opt/backup/nightly_backup.sh)`
  - `nightly_backup.sh` : `[<pid>] starting full backup: tar -cf - /var/lib/redis /var/log | gzip -9 > /backup/full-YYYYMMDD.tar.gz`
  - `nightly_backup.sh` : `[<pid>] backup completed in <秒>s`(負荷フェーズ終了時)
  - CRON の `pam_unix(cron:session): session opened/closed for user root`(平常ノイズ)

### 3.6 負荷シナリオ

- **履歴部分(自動)**: `scenario.json` の `cpu_load`。現在の既定は **Redis を履歴ウィンドウの 35〜55 分目に CPU 30%(ramp 30 秒)**。コマンドで `--load redis:40:60:70:30` のように上書き可(形式 `host(app|redis):開始分:終了分:CPU%[:ramp秒]`、複数可)。
- **ライブ部分(手動)**: 別ターミナルで `cpu_load.py`(実行ディレクトリは `/home/elastic/workshop-scripts`)。

```bash
python3 cpu_load.py start redis 80              # redis を CPU 80% に(30 秒かけて上昇、stop まで継続)
python3 cpu_load.py start app 50 --duration 10  # app を 50% で 10 分間
python3 cpu_load.py stop [redis|app|all]        # 30 秒かけて平常値に戻す(既定 all)
python3 cpu_load.py status                      # 現在・直近の手動負荷を表示
```

- 反映は次のバッチ(最大 interval 秒 + 1 秒)から。既に負荷中の host に `start` すると、ramp なしで即時に CPU% が変更される。CPU% は 1〜99。
- 状態は `control.json` に保存され、生成プログラム起動時にリセットされる。

### 3.7 制約・注意(受講者が詰まりやすい点)

- **APM 集計メトリクスの 10m / 60m は区間終了後に投入**(1m も最大約 1 分遅れ)。APM 画面で直近が空に見えることがある。
- **メトリクスは約 2 時間より古い時刻を黙って捨てる**。Kibana で 3 時間表示にすると先頭が空に見える。履歴は 115 分前後。
- 未対応: エラー/非 200 リクエスト、スキャナ由来のノイズ、`exemplars-generic.otel`。
- `metric_types.json`(メトリクスの型定義)は削除しない。

## 4. ダミー CMDB(参考・現行ワークフローでは未使用)

> 注意: 作業ツリー上で `cmdb.py` は削除済み(Git の HEAD にはある)。**現行のワークフロー/エージェントは CMDB を参照しない。** マニュアルで扱うかは §14 で確認。扱う場合は以下を使う。

投入先: `cmdb-ci`(9 CI)、`cmdb-relationships`(11 関係)。`host.name` / `service.name` はテレメトリと同値なので、アラートから所有者・重要度・依存先を引ける。

| ci_id | クラス | 主な属性 |
|---|---|---|
| `business_service:card-status` | business_service | カードステータス照会 / criticality high / owner `payments-platform` / SLA 可用性 99.9%、p95 300ms |
| `application:test-card-service` | application | Java/OpenJDK 21、port 8080、owner `payments-platform` |
| `application:java-status-api-frontend-aws` | application | webjs、criticality medium、owner `web-frontend` |
| `server:sample-otel-demo-app` | server | 10.146.0.37、e2-medium(2vCPU/4GB)、backup_policy `daily`、owner `infra-team` |
| `server:sample-otel-demo-redis` | server | 10.146.0.29、e2-medium、**backup_policy `none`**、owner `infra-team` |
| `database:redis-cache` | database | Redis 7.2.4、6379、persistence none |
| `network:vpc-default` | vpc_network | 10.146.0.0/20 |
| `network:fw-allow-redis` | firewall_rule | app→redis 6379/tcp |
| `network:fw-allow-http` | firewall_rule | 0.0.0.0/0 → app 8080 |

関係(source → target): 業務サービス `depends_on` フロント/API、フロント `depends_on` API、API `depends_on` redis-cache、API `runs_on` app サーバ、redis-cache `runs_on` redis サーバ、app `connected_to` redis、両サーバ `member_of` VPC、FW が各サーバを `protects`。

※ 注目点: Redis サーバの `backup_policy` は `none` なのに夜間バックアップが動いている=「本来動いてはいけないものが動いている」という教材上のポイントになりうる。

## 5. アラートルール(`create_rule.sh`)

- 名前: **`Elasticsearch query rule`**(種別 `.es-query`、ES|QL)。実行間隔 1 分。
- **作成時は無効(`enabled: false`)**。受講者が Kibana で有効化する必要がある。同名ルールが既にあれば作成をスキップ(冪等)。ログ `/home/elastic/create_observability_rule.log`。
- 検知クエリ(ES|QL):

```esql
FROM traces-generic.otel-default
| WHERE service.name == "test-card-service" AND processor.event == "transaction" AND @timestamp > NOW() - 1h
| STATS avg_duration = AVG(transaction.duration.us) BY bucket = BUCKET(@timestamp, 1 minute)
| CHANGE_POINT avg_duration ON bucket
| WHERE type IS NOT NULL
| KEEP bucket, avg_duration, type, pvalue
```

- 意味: 直近 1 時間の 1 分平均トランザクション時間に**変化点(CHANGE_POINT)**があれば 1 件以上ヒット=アラート(閾値 `> 0`、時間窓 50 分)。
- アクション: 新規(`new`)アラート時のみ、Workflow コネクタで **workflowId `alert-infra-check-and-root-cause-investigation`** を実行(`summaryMode: true`。ongoing/recovered では実行しない)。`alert_delay` は 1。
- 前提: Workflow 側に同 ID の Workflow が登録済みであること(Workflow の登録方法は §14 で確認)。

## 6. Agent Builder(`create_agents.sh`)

実行すると、次を作成(既存なら更新)。**冪等**。前提: Kibana で `observability.*` ツールが利用可能。

### 6.1 ES|QL ツール(2 つ。データは `metrics-hostmetricsreceiver.otel-default`)

| ツール ID | 入力 | 返すもの |
|---|---|---|
| `infra_host_baseline_24h` | `host`(host.name) | 直近 24 時間平均の cpu(=1−idle)・mem・load1(1 行) |
| `infra_host_cpu_mem_load_1m` | `host`、`start`、`end`(ISO8601 UTC) | 1 分バケットの cpu・mem・load1 を時刻昇順 |

### 6.2 エージェント `check_infra`(スキル `service-infra-lookup`)

- 役割: アラート直後に「対象サービスと関連モジュール(依存先・呼び出し元)がどのホストで動くか」を特定し、各ホストの**ベースライン(24h 平均)とアラート時刻 T の前後 10 分(T−10m〜T+10m、1 分バケット 21 行)**を取得する。診断・考察はしない。
- 手順: `get_service_topology`(direction both, 24h)で関連モジュール特定 → `get_trace_metrics`(groupBy host.name)等でホスト特定 → 上記 2 ツールを各ホストに実行。Redis のように `service.name` を持たない依存先は、`host.name: *redis*` のワイルドカード検索で探す。ブラウザ上のフロントはホストなし(`host: none`)。
- **出力形式(厳守・前置きなし)**:

```
<サービス/モジュール名> → <ホスト名> (<クラウドプロバイダ>, <IP>, <OS>)
  baseline_24h: cpu=<値> mem=<値> load=<値>
  HH:MM cpu=<値> mem=<値> load=<値>   ← 1分バケット、T-10m〜T+10m の21行
```
  ホスト不明は `<名前> → host: none` の 1 行。

### 6.3 エージェント `rca_agent`(スキル `rca-lite`)

- 役割: check_infra の結果を前提に RCA を行う。ホストのメトリクスは取り直さない。
- 手順の要点:
  1. アラートから `kibana.alert.start`、`service.name`/`host.name`、`kibana.alert.reason` を読む。
  2. インフラ情報で、baseline_24h から**外れ始めた時刻**を探す。アラート開始より前に外れていればそのホストが原因候補。
  3. 外れ始めたホストの**ログ(−10〜+10 分)を必ず確認**。アプリログに加え OS ログ(`/var/log/syslog`)も。CRON 起動、スクリプトの開始/終了、`tar` / `gzip` 等の重い処理を探し、**開始行と終了行**の両方を見て時間帯の一致を確認。メッセージ全文・時刻・ホストを引用。無ければ「該当なし」。
  4. アプリ側は最小限: `get_services`、`get_service_topology`、必要時 `get_log_groups`/`get_traces`。
  5. 根拠の異なる仮説はすべて挙げる。
- 判断ルール: 根拠はツール出力とインフラ情報のみ(推測禁止)/「停止」ではなく「到達不能」等、確認できた深さで書く/依存先が悪ければ依存先が原因で対象は症状。
- **出力項目**: 原因(主張・根拠・確度 Critical/High/Medium/Speculative)、ホスト上の実行履歴とメトリクス逸脱期間の時刻対応、伝播経路を一文、影響(件数・率・開始時刻・継続時間)、推奨対応。

### 6.4 期待される RCA の結論(正解)

> Redis ホスト `sample-otel-demo-redis` 上で CRON が `/opt/backup/nightly_backup.sh`(`tar | gzip -9`)を起動し、CPU が飽和 → Redis `SETEX` が遅延 → `test-card-service` のトランザクション時間が上昇(エラー率は 0%、性能劣化のみ)。

エージェント出力は LLM 生成のため**文面は毎回変わる**。マニュアルでは「この結論に相当する内容が含まれていれば正解」と書き、文面の一致を求めないこと。

## 7. Workflow(`RCA_with_check_infra.yml`)

名前: **Alert Infra Check and Root Cause Investigation**(トリガー: `alert`)。ID は `alert-infra-check-and-root-cause-investigation`。

| # | ステップ名 | 種別 | 内容 | 実行条件 |
|---|---|---|---|---|
| 1 | `check_infra` | ai.agent(`check_infra`) | アラート情報を渡し、関連ホストの 24h 平均と T±10 分の 1 分メトリクスを出力させる | 常に |
| 2 | `investigate_root_cause` | ai.agent(`rca_agent`) | 1 の出力とアラート情報から RCA | 常に |
| 3 | `search_past_incidents` | elasticsearch.search | インデックス `past-incidents` を、RCA 結果をクエリに `semantic_summary` で意味検索。`size 1`、`min_score 0.85`、`remediation_available = true` のみ | 常に |
| 4 | `no_past_incident` | console | メッセージ「過去に同一事象の自動修復可能な対応実績は見つかりませんでした。手動で対応してください。」を出力して終了 | ヒット 0 件 |
| 5 | `register_approval` | http | `POST http://host-1:3000/approval_requests` に `execution_id`、`service_name`、`description`、`action`、`rca_result`、`infra_check` を送り、承認画面に登録 | ヒット ≥ 1 |
| 6 | `approval` | waitForInput | 承認待ちで停止(タイムアウト 24h)。入力: `approved`(boolean、必須)、`comments`(文字列) | ヒット ≥ 1 |
| 7 | `execute_remediation` | http | `POST http://host-1:3000/remediate_service`(body: `service_name`) | `approved == true` |

- 6 の待機メッセージ(Workflow 実行画面に表示):

```
## Remediation Approval Required

過去事例 INC-0001 と同一の事象を検知しました。

**Target service:** test-card-service

**Command:**
`pkill -f nightly_backup.sh; pkill -x tar; pkill -x gzip`

Open "Approvals" at http://host-1:3000/approval to approve or reject.
```
- 5 の `description` は `過去事例 INC-0001 と同一事象のため修復を実行: test-card-service レイテンシ上昇 (Redis ホスト CPU 飽和)`。
- **Reject した場合**: 7 は実行されない(修復されない。CPU 負荷は残る)。
- 旧版 `RCA.yml`(check_infra なし、RCA のみ)も存在するが、**演習では `RCA_with_check_infra.yml` を使う**【要確認】。

## 8. 過去事例インデックス `past-incidents`(`create_past_incidents.sh`)

- 実行: `[RECREATE=1] [INDEX=past-incidents] ./create_past_incidents.sh`。`RECREATE=1` で削除して作り直し。
- `symptom` / `root_cause` / `evidence` が `semantic_summary`(`semantic_text`、推論 `.multilingual-e5-small-elasticsearch`)にコピーされ、意味検索の対象になる。
- サンプル 3 件:

| ID | タイトル | 重大度 | 自動修復 |
|---|---|---|---|
| **INC-0001** | test-card-service レイテンシ上昇 (Redis ホスト CPU 飽和) | high | **あり**(workflow `remediation-with-approval`、コマンド `pkill -f nightly_backup.sh; pkill -x tar; pkill -x gzip`、エンドポイント `POST /remediate_service`、承認必須) |
| INC-0002 | java-status-api-frontend JVM ヒープ枯渇によるエラー率上昇 | critical | なし(手動対応) |
| INC-0003 | Redis ディスク容量逼迫による書き込み遅延 | medium | なし(手動対応) |

- INC-0001 の原因/証拠: バックアップスクリプト(`tar | gzip -9`)が業務時間帯に実行 / Redis ホスト CPU 約 68% / syslog に `CRON (root) CMD (/opt/backup/nightly_backup.sh)` と `starting full backup`。対処: プロセス kill。再発防止: CPU 50% 超アラート追加、バックアップを Redis ホストから分離、nice/ionice で制限。
- ポイント: 修復可能なのは INC-0001 だけ。INC-0003 は症状(Redis 書き込み遅延)が似るが `remediation_available=false` なので、フィルタにより自動修復の候補にならない。
- 【リスク】意味検索のスコアが 0.85 に届かない場合、承認依頼が登録されず `no_past_incident` になる。実機で確認済みか §14 で確認。

## 9. 承認ポータル(`server.py`、`host-1:3000`)— 画面仕様

画面名 **IT Service Portal**(ServiceNow 風)。全ページ共通レイアウト:

- ヘッダ(濃緑): 左に丸アイコン + `IT Service Portal`、ナビ `Home` / `Approvals`、右端に `System Administrator`。
- 左サイドバー: **Self-Service** → `Dashboard` / **Process** → `My Approvals`、`Incidents`、`Change Requests`、`Problems`(後 3 つはダミーリンク)/ **Configuration** → `CMDB`、`Service Catalog`(ダミー)。

### 9.1 `/`(Home)— 見出し `Service Operations Workspace`

- カード 4 枚(固定値): `12 Open incidents` / `3 Pending approvals` / `5 Changes this week` / `99.2% SLA compliance`。
- **Recent incidents** 表(固定):

| Number | Short description | Priority | State |
|---|---|---|---|
| INC0010231 | Redis VM high CPU utilization | 1 - Critical | In Progress |
| INC0010230 | Application response time degraded | 2 - High | New |
| INC0010227 | Disk usage warning on batch node | 3 - Moderate | On Hold |
| INC0010219 | Certificate renewal completed | 4 - Low | Resolved |

- **Pending approvals** 表(固定): `APR0001001` / `Remediate high CPU on redis` / リンク `Open`(→ `/approval`)。
- ※ これらはデモ用の固定表示で、Workflow の実行結果とは連動しない。

### 9.2 `/approval`(承認画面)— 見出し `Approval Request`

- 表示対象: URL の `?execution_id=...` で指定された依頼。指定なしなら、状態が `Requested` の**最新**の依頼。
- 依頼が無い場合: パネル `No pending approvals`、本文 `There are no approval requests waiting.`
- 依頼がある場合、次の順に表示:
  1. パネル(見出しは依頼 ID 例 `APR0001001`)内のフォーム:

| ラベル | 値(INC-0001 一致時の例) |
|---|---|
| Request | `APR0001001`(最初の依頼。以降 `APR0001002`…の連番。サーバ再起動で 1001 から振り直し) |
| Short description | `過去事例 INC-0001 と同一事象のため修復を実行: test-card-service レイテンシ上昇 (Redis ホスト CPU 飽和)` |
| Configuration item | `test-card-service` |
| Requested action | `pkill -f nightly_backup.sh; pkill -x tar; pkill -x gzip` |
| State | `Requested`(承認/却下後は `Approved` / `Rejected`) |

  ボタン: **Approve**(緑塗り)/ **Reject**(赤枠)。
  2. パネル **Root Cause Analysis (RCA)** — `investigate_root_cause` の出力全文(整形済みテキスト、最大高さ 420px でスクロール)。
  3. パネル **Infrastructure Check** — `check_infra` の出力全文(同上)。
  - RCA / Infra Check は、Workflow から渡された場合のみ表示される。
- ボタン押下の挙動: 両ボタンが無効化 → 結果を画面下に表示。
  - 成功(緑枠): Approve → `Approved. Result sent to workflow.` / Reject → `Rejected. Result sent to workflow.`
  - 失敗(赤枠、ボタン再有効化): `Failed to send result: <エラー>` または `Error: <例外>`
- 内部動作: Kibana の Workflow Resume API に `approved`(true/false)と `comments`(自動文言)を送り、停止中の Workflow を再開する。
- 依頼はサーバのメモリ上のみ保持。**サーバ再起動で消える**(その場合 Workflow 側から再実行が必要)。

### 9.3 その他の API(受講者が直接叩くことは通常ない)

| エンドポイント | 動作 |
|---|---|
| `POST /approval_requests` | Workflow が承認依頼を登録(`success: true` と登録内容を返す) |
| `POST /approval` | 承認/却下を受け Workflow を再開 |
| `POST /remediate_service` | 修復を実行(§10) |
| `POST /scale_service` | スケールアップのモック(2〜4 台 → +2 台、`estimated_time: 45s`)。**現行 Workflow では未使用** |
| `GET /health` | `{"status":"healthy","uptime":<秒>}` |

## 10. 修復(`/remediate_service`)の実際の挙動

- 画面・ログ・メッセージには「`pkill -f nightly_backup.sh; pkill -x tar; pkill -x gzip` でバックアップを停止」と表示されるが、**実際に実行されるのは `python3 cpu_load.py stop`**(`/home/elastic/workshop-scripts` で非同期実行、ログは `cpu_load.log`)。ダミーデータの CPU 負荷を 30 秒かけて平常値に戻す。実プロセスは kill しない(ダミー環境のため)。
- レスポンス例: `success: true`、`status: remediation_triggered`、`remediation_id: rem-<pid>`、`service: test-card-service`、`action: kill_backup_processes`、`message: Backup processes (nightly_backup.sh / tar / gzip) stopped for test-card-service`。
- 反映: stop の ramp(約 30 秒)+ 生成プログラムの次バッチ(約 10 秒)の後、Redis CPU・Redis SETEX・`test-card-service` のレイテンシが下がる。メトリクスは Kibana の自動更新間隔にも依存。
- マニュアルでは、この「表示コマンドと実処理の違い」を**受講者に隠さず、ダミー環境の仕様として明記**すること。

## 11. 想定タイムライン(目安。【要確認】は実機計測が必要)

| 時点 | 出来事 |
|---|---|
| T0 | `generate_basic.py` 起動、履歴 115 分を投入。以後 10 秒ごとに投入 |
| T0+数分 | Kibana の APM / Hosts / Discover でデータ確認 |
| 任意 | `cpu_load.py start redis 80` で Redis CPU 上昇(30 秒で到達)→ レイテンシが約 260 ms 付近へ |
| +1〜数分 | CHANGE_POINT によりルール `Elasticsearch query rule` がアラート(ルールが有効の場合)【要確認: 実測の遅延】 |
| +数分 | Workflow 実行: check_infra → RCA(LLM 処理のため数分かかりうる)【要確認】 |
| 同上 | 承認画面に依頼が出現。Workflow は `waitForInput` で停止 |
| 受講者操作 | RCA / Infra Check を読んで Approve |
| +1 分弱 | 修復実行、CPU・レイテンシが平常へ |

## 12. 受講者がハンズオンで確認する観点(学習目標案)

1. OTel データ(traces / metrics / logs)が APM・Infrastructure・Discover でどう見えるか。
2. 「エラー無し・レイテンシだけ悪化」の障害を、トレース(Redis SETEX スパンが処理時間の大半)から特定する。
3. 変化点検知(ES|QL `CHANGE_POINT`)でアラート化する仕組み。
4. アラート → Workflow → AI エージェントの連携。スキル/ツール/エージェントの役割分担(check_infra は事実収集のみ、rca_agent が分析)。
5. RCA の根拠(メトリクスの逸脱開始時刻とログ `CRON` / `starting full backup` の一致)の読み方。
6. 意味検索による過去事例のマッチングと、「修復可能な事例のみ」に絞る設計。
7. Human-in-the-loop(承認)を挟む自動修復の設計。承認前に RCA と Infra Check を人が確認できる意義。
8. 修復後の復帰確認。

## 13. マニュアル構成案(出発点)

1. はじめに(ゴール、ストーリー、全体図 §1)
2. 環境と前提(§2)
3. 演習 1: データ投入と確認(§3)
4. 演習 2: 障害の発生(`cpu_load.py start redis 80`)と症状の確認(APM / Hosts)
5. 演習 3: アラートルールの有効化と検知(§5)
6. 演習 4: Workflow・エージェントの動作確認(§6, §7)— RCA と Infra Check の読み方
7. 演習 5: 承認と修復(§9, §10)
8. 演習 6: 復帰確認と振り返り(§12)
9. 付録: トラブルシュート、コマンド一覧、用語集、リセット手順

各演習に「目的 / 手順(番号付き)/ 期待される結果 / 確認ポイント / うまくいかない時」を入れる。画面キャプチャが必要な箇所は `【スクリーンショット: 内容】` と書いて挿入位置を示す。

## 14. 未確定事項(AI はここを推測で埋めず、質問または `【要確認】` で残すこと)

- [ ] 受講者像(SRE / 開発者 / 営業 SE 等)、前提知識、全体の所要時間、1 演習の目安時間
- [ ] 環境の提供形態(Instruqt 等のラボか、各自の環境か)。受講者が見る端末/ブラウザのタブ構成
- [ ] セットアップ(`create_agents.sh` / `create_rule.sh` / `create_past_incidents.sh` / Workflow 登録 / データ生成起動)は**事前に講師が実施済み**か、**受講者が実施**か
- [ ] Workflow(`alert-infra-check-and-root-cause-investigation`)の Kibana への登録方法(UI 貼り付け等)
- [ ] 障害発生のさせ方: 履歴の自動シナリオに任せるか、`cpu_load.py start redis <値>` を受講者が実行するか。推奨 CPU 値
- [ ] アラートルールを受講者が有効化するか(作成時は無効)
- [ ] 意味検索が 0.85 以上で INC-0001 にヒットすることの実機確認結果
- [ ] §11 の各所要時間の実測値
- [ ] CMDB(§4)を演習に含めるか
- [ ] マニュアルの出力形式(Markdown / Word / スライド等)と、スクリーンショットの有無
- [ ] 演習後のリセット手順(再演習時に必要な操作)

## 15. 用語集

| 用語 | 説明 |
|---|---|
| OTel | OpenTelemetry。トレース・メトリクス・ログの標準形式 |
| APM | アプリケーションの性能監視(トランザクション時間、依存先) |
| ES\|QL | Elasticsearch のクエリ言語。`CHANGE_POINT` で時系列の変化点を検出 |
| Agent Builder | Kibana 上で AI エージェント・スキル・ツールを定義する機能 |
| スキル | エージェントに与える手順書(ツールの使い方と判断ルール) |
| Workflow | Kibana のワークフロー。アラートをトリガーに複数ステップを実行 |
| waitForInput | Workflow を一時停止し、人間の入力(承認)で再開するステップ |
| 意味検索(semantic) | 文章の意味の近さで検索。`semantic_text` フィールドを使用 |
| RCA | Root Cause Analysis。根本原因分析 |
| CMDB | 構成管理データベース。システム構成要素(CI)と依存関係の台帳 |
