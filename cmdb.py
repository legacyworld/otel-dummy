#!/usr/bin/env python3
"""ダミー CMDB を Elasticsearch に投入する(標準ライブラリのみ)。

generate.py が出力するテレメトリ(host.name / service.name)と突き合わせられるよう、
同じホスト名・サービス名・IP を持つ構成情報(CI)を作る。

  python3 cmdb.py --dry-run     # 投入せず内容を表示
  python3 cmdb.py --out cmdb.ndjson  # ファイルにも書き出す
  python3 cmdb.py               # cmdb-ci / cmdb-relationships を作り直して投入
"""
import argparse
import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CI_INDEX = "cmdb-ci"
REL_INDEX = "cmdb-relationships"

APP_HOST = "sample-otel-demo-app"
REDIS_HOST = "sample-otel-demo-redis"
SVC = "test-card-service"
FE_SVC = "java-status-api-frontend-aws"

GCP = {"cloud.provider": "gcp", "cloud.account.id": "elastic-sa", "cloud.region": "asia-northeast1",
       "cloud.availability_zone": "asia-northeast1-a"}
SINCE = "2026-10-01T09:00:00+09:00"

# ci_id -> CI。ci_id は "<class>:<name>" 形式。
CIS = [
    {"ci_id": "business_service:card-status", "ci_name": "カードステータス照会", "ci_class": "business_service",
     "description": "カード処理状況を照会する社内向けサービス", "environment": "production",
     "criticality": "high", "lifecycle_status": "operational", "owner_group": "payments-platform",
     "owner_email": "payments-platform@example.com", "support_group": "sre-oncall",
     "sla": {"availability_pct": 99.9, "response_time_ms_p95": 300}, "service_now_sys_id": "bs0001"},

    {"ci_id": "application:" + SVC, "ci_name": SVC, "ci_class": "application",
     "description": "Spring Boot (Tomcat) の API。GET /api/status で Redis に SETEX する",
     "service.name": SVC, "service.version": "1.0.0", "language": "java", "runtime": "OpenJDK 21",
     "environment": "production", "criticality": "high", "lifecycle_status": "operational",
     "owner_group": "payments-platform", "support_group": "sre-oncall",
     "repository": "https://github.com/example/test-card-service", "port": 8080, "service_now_sys_id": "app0001"},

    {"ci_id": "application:" + FE_SVC, "ci_name": FE_SVC, "ci_class": "application",
     "description": "ブラウザ計測(OTel webjs)付きのフロント。/api/status を呼び出す",
     "service.name": FE_SVC, "service.version": "2.0.0", "language": "webjs",
     "environment": "production", "criticality": "medium", "lifecycle_status": "operational",
     "owner_group": "web-frontend", "support_group": "sre-oncall",
     "repository": "https://github.com/example/java-status-api-frontend", "service_now_sys_id": "app0002"},

    {"ci_id": "server:" + APP_HOST, "ci_name": APP_HOST, "ci_class": "server", "host.name": APP_HOST,
     "description": "アプリケーションサーバ(test-card-service 稼働)", "host.id": "398070581476752747",
     "host.ip": ["10.146.0.37"], "host.public_ip": "35.200.74.7", "host.mac": ["42-01-0A-92-00-25"],
     "host.type": "e2-medium", "cpu_cores": 2, "memory_gb": 4, "disk_gb": 20,
     "os.type": "linux", "os.name": "Ubuntu", "os.version": "24.04.5 LTS",
     "environment": "production", "criticality": "high", "lifecycle_status": "operational",
     "owner_group": "infra-team", "support_group": "sre-oncall", "monitoring": "otel-hostmetrics",
     "backup_policy": "daily", "patch_group": "weekly-sun-03", "service_now_sys_id": "srv0001", **GCP},

    {"ci_id": "server:" + REDIS_HOST, "ci_name": REDIS_HOST, "ci_class": "server", "host.name": REDIS_HOST,
     "description": "Redis 専用サーバ", "host.id": "398070581476752811",
     "host.ip": ["10.146.0.29"], "host.mac": ["42-01-0A-92-00-1D"],
     "host.type": "e2-medium", "cpu_cores": 2, "memory_gb": 4, "disk_gb": 20,
     "os.type": "linux", "os.name": "Ubuntu", "os.version": "24.04.5 LTS",
     "environment": "production", "criticality": "high", "lifecycle_status": "operational",
     "owner_group": "infra-team", "support_group": "sre-oncall", "monitoring": "otel-hostmetrics",
     "backup_policy": "none", "patch_group": "weekly-sun-03", "service_now_sys_id": "srv0002", **GCP},

    {"ci_id": "database:redis-cache", "ci_name": "redis-cache", "ci_class": "database",
     "description": "ステータス照会のキャッシュ(SETEX / PING)", "db.system": "redis", "db.version": "7.2.4",
     "port": 6379, "persistence": "none", "maxmemory_mb": 2048,
     "environment": "production", "criticality": "high", "lifecycle_status": "operational",
     "owner_group": "infra-team", "support_group": "sre-oncall", "service_now_sys_id": "db0001"},

    {"ci_id": "network:vpc-default", "ci_name": "default", "ci_class": "vpc_network",
     "description": "GCP VPC", "cidr": "10.146.0.0/20", "environment": "production",
     "lifecycle_status": "operational", "owner_group": "network-team", **GCP},

    {"ci_id": "network:fw-allow-redis", "ci_name": "allow-redis-from-app", "ci_class": "firewall_rule",
     "description": "app -> redis 6379/tcp を許可", "direction": "ingress", "protocol": "tcp", "port": 6379,
     "source": "10.146.0.37/32", "destination": "10.146.0.29/32", "environment": "production",
     "lifecycle_status": "operational", "owner_group": "network-team", **GCP},

    {"ci_id": "network:fw-allow-http", "ci_name": "allow-http-public", "ci_class": "firewall_rule",
     "description": "公開 HTTP(8080) を許可", "direction": "ingress", "protocol": "tcp", "port": 8080,
     "source": "0.0.0.0/0", "destination": "10.146.0.37/32", "environment": "production",
     "lifecycle_status": "operational", "owner_group": "network-team", **GCP},
]

# (source, relationship, target)。"depends_on" は source が target に依存する。
RELS = [
    ("business_service:card-status", "depends_on", "application:" + FE_SVC),
    ("business_service:card-status", "depends_on", "application:" + SVC),
    ("application:" + FE_SVC, "depends_on", "application:" + SVC),
    ("application:" + SVC, "depends_on", "database:redis-cache"),
    ("application:" + SVC, "runs_on", "server:" + APP_HOST),
    ("database:redis-cache", "runs_on", "server:" + REDIS_HOST),
    ("server:" + APP_HOST, "connected_to", "server:" + REDIS_HOST),
    ("server:" + APP_HOST, "member_of", "network:vpc-default"),
    ("server:" + REDIS_HOST, "member_of", "network:vpc-default"),
    ("network:fw-allow-redis", "protects", "server:" + REDIS_HOST),
    ("network:fw-allow-http", "protects", "server:" + APP_HOST),
]

CI_MAPPING = {"properties": {
    "ci_id": {"type": "keyword"}, "ci_name": {"type": "keyword"}, "ci_class": {"type": "keyword"},
    "description": {"type": "text"}, "environment": {"type": "keyword"},
    "criticality": {"type": "keyword"}, "lifecycle_status": {"type": "keyword"},
    "owner_group": {"type": "keyword"}, "owner_email": {"type": "keyword"}, "support_group": {"type": "keyword"},
    "host.name": {"type": "keyword"}, "host.id": {"type": "keyword"}, "host.ip": {"type": "ip"},
    "host.public_ip": {"type": "ip"}, "host.mac": {"type": "keyword"}, "host.type": {"type": "keyword"},
    "service.name": {"type": "keyword"}, "service.version": {"type": "keyword"},
    "cpu_cores": {"type": "integer"}, "memory_gb": {"type": "integer"}, "disk_gb": {"type": "integer"},
    "port": {"type": "integer"}, "cidr": {"type": "ip_range"},
    "sla": {"properties": {"availability_pct": {"type": "float"}, "response_time_ms_p95": {"type": "integer"}}},
    "created_at": {"type": "date"}, "updated_at": {"type": "date"},
    "depends_on": {"type": "keyword"}, "depended_by": {"type": "keyword"}, "runs_on": {"type": "keyword"},
}}
REL_MAPPING = {"properties": {
    "source_id": {"type": "keyword"}, "source_name": {"type": "keyword"}, "source_class": {"type": "keyword"},
    "relationship": {"type": "keyword"},
    "target_id": {"type": "keyword"}, "target_name": {"type": "keyword"}, "target_class": {"type": "keyword"},
    "created_at": {"type": "date"},
}}


def build():
    """(CI ドキュメント, 関係ドキュメント) を返す。CI 側にも依存関係を非正規化して持たせる。"""
    by_id = {c["ci_id"]: dict(c, created_at=SINCE, updated_at=SINCE) for c in CIS}
    rels = []
    for s, r, t in RELS:
        rels.append({"source_id": s, "source_name": by_id[s]["ci_name"], "source_class": by_id[s]["ci_class"],
                     "relationship": r,
                     "target_id": t, "target_name": by_id[t]["ci_name"], "target_class": by_id[t]["ci_class"],
                     "created_at": SINCE})
        if r in ("depends_on", "runs_on"):
            by_id[s].setdefault(r, []).append(t)
            if r == "depends_on":
                by_id[t].setdefault("depended_by", []).append(s)
    return list(by_id.values()), rels


def load_env():
    env = {}
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("ELASTIC_")})
    return env


def req(env, method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    ctype = "application/x-ndjson" if raw is not None else "application/json"
    r = urllib.request.Request(env["ELASTIC_URL"].rstrip("/") + path, data=data, method=method,
                               headers={"Authorization": "ApiKey " + env["ELASTIC_API_KEY"], "Content-Type": ctype})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit("%s %s -> %s %s" % (method, path, e.code, e.read().decode()[:300]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="クラスタに触れず内容だけ表示")
    ap.add_argument("--out", help="NDJSON(bulk 形式)をファイルにも書き出す")
    args = ap.parse_args()

    cis, rels = build()
    actions = [(CI_INDEX, c["ci_id"], c) for c in cis] + \
              [(REL_INDEX, "%s|%s|%s" % (r["source_id"], r["relationship"], r["target_id"]), r) for r in rels]
    lines = []
    for idx, _id, doc in actions:
        lines += [json.dumps({"index": {"_index": idx, "_id": _id}}), json.dumps(doc, ensure_ascii=False)]

    print("CI: %d 件 / 関係: %d 件" % (len(cis), len(rels)))
    for c in cis:
        print("  %-16s %s" % (c["ci_class"], c["ci_name"]))
    if args.out:
        with open(args.out, "w") as f:
            f.write("".join(l + "\n" for l in lines))
        print("wrote", args.out)
    if args.dry_run:
        return

    env = load_env()
    for k in ("ELASTIC_URL", "ELASTIC_API_KEY"):
        if not env.get(k):
            raise SystemExit("%s が未設定です(.env を確認)" % k)
    for idx, mapping in ((CI_INDEX, CI_MAPPING), (REL_INDEX, REL_MAPPING)):
        req(env, "DELETE", "/" + idx)  # このプログラムのインデックスだけ作り直す
        req(env, "PUT", "/" + idx, {"mappings": mapping})
    r = req(env, "POST", "/_bulk?refresh=wait_for", raw=("".join(l + "\n" for l in lines)).encode())
    if r.get("errors"):
        bad = [i["index"]["error"] for i in r["items"] if i["index"].get("error")]
        raise SystemExit("bulk エラー: %s" % bad[:3])
    print("投入完了:", CI_INDEX, REL_INDEX)


if __name__ == "__main__":
    main()
