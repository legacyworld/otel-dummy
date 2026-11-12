# dummy-data-generator

「Redis VM の CPU 負荷が上がると、Redis を使うアプリの処理が遅くなる」ダミーデータ(OTel形式)を生成して Elasticsearch(Serverless Observability)に投入します。標準ライブラリのみ(インストール不要、Python 3.8+)。

## 構成

| ファイル | 内容 |
|---|---|
| `generate.py` | 本体(生成 → 削除 → 履歴投入 → **以降リアルタイムで投入し続ける**) |
| `cpu_load.py` | 実行中の `generate.py` に対し、CPU 負荷を手動で上げ下げする(`control.json` 経由) |
| `cmdb.py` | ダミー CMDB(CI と依存関係)を `cmdb-ci` / `cmdb-relationships` に投入 |
| `scenario.json` | トラフィック量・平常時 CPU・CPU 負荷シナリオ・CPU→レイテンシ対応表 |
| `metric_types.json` | メトリクス名ごとの ES フィールド型(元クラスタのマッピングから抽出)。**削除しないこと** |
| `.env` / `.env.example` | 接続情報(`.env` は git 管理外) |

## 使い方

```bash
cp .env.example .env            # ELASTIC_URL / ELASTIC_API_KEY を設定
python3 generate.py --dry-run   # クラスタに触れず、CPU と遅延の対応表・件数だけ確認
python3 generate.py             # 既存データを削除 → 直近 115 分を投入 → 以降 10 秒ごとに現在時刻のデータを投入し続ける(Ctrl-C で停止)
```

履歴投入だけで終わらせたい場合は `--no-live`。ライブ時のバッチ間隔は `--interval`(秒, 既定 10)。

投入前に、**このプログラムが書き込むデータストリームだけ**を削除します(他のデータには触れません)。削除したくない場合は `--no-reset`。
エラーがあれば終了コード 1 で終了します。

## CPU 負荷を手動で上げる(ライブ中)

`generate.py` を動かしたまま、別ターミナルで実行します。

```bash
python3 cpu_load.py start redis 80              # redis を CPU 80% に(30 秒かけて上昇、stop まで継続)
python3 cpu_load.py start app 50 --duration 10  # app を 50% で 10 分間
python3 cpu_load.py stop [redis|app|all]        # 30 秒かけて平常値に戻す(既定 all)
python3 cpu_load.py status
```

反映は次のバッチ(最大 `--interval` 秒 + 1 秒)から。以降のデータで CPU・負荷平均・バックアップ暴走(`nightly_backup.sh` / `tar` / `gzip`)のプロセス/ログが現れ、Redis CPU に応じてアプリのレイテンシも上がります。
`control.json` に書かれ、`generate.py` 起動時にリセットされます。履歴部分の自動シナリオ(下記 `scenario.json` の `cpu_load`)が不要なら空配列 `[]` にしてください。

## CPU 負荷シナリオの設定(履歴部分)

`scenario.json` の `cpu_load` を編集するか、コマンドラインで上書きします(複数指定可)。

```bash
# redis を 40〜60 分目に CPU 70% (ramp 30 秒)、app を 10〜20 分目に 40%
python3 generate.py --load redis:40:60:70:30 --load app:10:20:40
```

形式は `host(app|redis):開始分:終了分:CPU%[:ramp秒]`(開始分はウィンドウ先頭からの経過分)。

デフォルト: Redis が 35〜55 分目に 30%、80〜110 分目に 55%。

- `redis_latency_ms_by_cpu` : Redis の CPU% → SETEX 平均レイテンシ(ms) の対応表(折れ線補間、対数正規分布でばらつかせる)。
- `traffic.requests_per_minute` / `--rpm` : リクエスト頻度。
- `baseline_cpu_percent` : 平常時の CPU%。
- 他: `--duration`(分, 既定115) `--end`(ISO8601, 既定 now) `--seed`(既定42) `--jvm-interval`(秒, 既定30)

## 生成されるデータ

| データストリーム | 内容 |
|---|---|
| `traces-generic.otel-default` | フロント(webjs) → `GET /api/status`(Tomcat) → `StatusController.runRedisSet` → Redis `SETEX`、および `PING` |
| `logs-generic.otel-default` | アプリログ(起動ログ、`API Hit`)、フロントの navigation timing イベント |
| `metrics-hostmetricsreceiver.otel-default` | 2VM の cpu/load/memory/disk/filesystem/network/processes とプロセス別メトリクス(負荷中は `nightly_backup.sh`/`tar`/`gzip` も出現) |
| `metrics-generic.otel-default` | JVM / OTel SDK / `http.server.request.duration` / `db.client.operation.duration` |
| `metrics-{transaction,service_transaction,service_summary,service_destination}.{1m,10m,60m}.otel-default` | APM 集計メトリクス(レイテンシ・スループット) |
| `logs-system.syslog-default` / `logs-system.auth-default` | Filebeat 形式の syslog / auth(アプリの JSON ログ、バックアップ開始/終了ログを含む) |

ホスト名は `sample-otel-demo-app` / `sample-otel-demo-redis`、サービス名は `test-card-service` / `java-status-api-frontend-aws`。

## 注意点

- **APM 集計メトリクスの 10m / 60m は、区間が終わってから投入**されます(1m も最大約 1 分遅れ)。実際の集計と同じ挙動で、投入済みバケットとの重複を避けるためです。
- **期間は 115 分前後まで**: time_series(メトリクス)のデータストリームは約2時間より古い時刻を受け付けず、エラーなく黙って捨てられます。Kibana で 3 時間表示にすると先頭が空に見えます。
- **メトリクスの型指定**: メトリクスは bulk の各アクションに `dynamic_templates`(OTel exporter と同じ方式)を付けて投入します。これがないと `transaction.duration.summary` や各種ヒストグラムが単なる float/long に展開され、APM 画面でレイテンシが表示されません。新しいメトリクスを追加するときは `metric_types.json` に型を追記してください(未登録は値の形から推定)。
- **未対応**: エラー/非 200 のリクエスト、スキャナ由来のノイズリクエスト、`exemplars-generic.otel`。

## ダミー CMDB

```bash
python3 cmdb.py --dry-run   # 内容の確認のみ
python3 cmdb.py             # cmdb-ci / cmdb-relationships を作り直して投入
```

業務サービス・アプリ 2 本・サーバ 2 台・Redis・VPC・FW ルールの 9 CI と 11 関係。`host.name` / `service.name` はテレメトリと同じ値なので、アラートや APM データから所有者・重要度・依存先を引けます。`cmdb-ci` には `depends_on` / `depended_by` / `runs_on` も非正規化して入っています。
