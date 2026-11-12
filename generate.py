#!/usr/bin/env python3
"""Dummy APM / host-metrics generator for the "Redis CPU load -> slow app" demo.

Generates OTel-shaped traces, logs and metrics for two VMs (app + redis) ending
at the current time, deletes any existing data in the target data streams, and
bulk-indexes the result. Standard library only.
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- constants
HOSTS = {
    "app": {
        "name": "sample-otel-demo-app", "gce_id": "398070581476752747",
        "ip": "10.146.0.37", "ipv6": "fe80::4001:aff:fe92:25", "mac": "42-01-0A-92-00-25",
        "mem_used": 520e6,
    },
    "redis": {
        "name": "sample-otel-demo-redis", "gce_id": "398070581476752811",
        "ip": "10.146.0.29", "ipv6": "fe80::4001:aff:fe92:1d", "mac": "42-01-0A-92-00-1D",
        "mem_used": 330e6,
    },
}
for _k, _h in HOSTS.items():
    _h["key"] = _k
    _h["agent_id"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, "agent." + _h["name"]))
    _h["host_id"] = hashlib.md5(_h["name"].encode()).hexdigest()
APP_PUBLIC_IP = "35.200.74.7"
CORES = 2
MEM_TOTAL = 4.1e9
SCRAPE = 30  # seconds, hostmetrics interval
SVC = "test-card-service"
FE_SVC = "java-status-api-frontend-aws"
INSTANCE_ID = "aa96b157-0d44-459e-8a04-a595f73af378"
S2M_INSTANCE = "566c7fba-8a53-47d1-8325-9769ffd7714f"
APP_PID = 5998

APP_RES = {
    "cloud.account.id": "elastic-sa", "cloud.availability_zone": "asia-northeast1-a",
    "cloud.platform": "gcp_compute_engine", "cloud.provider": "gcp", "cloud.region": "asia-northeast1",
    "deployment.environment": "production", "deployment.environment.name": "production",
    "gcp.gce.instance.hostname": HOSTS["app"]["name"] + ".asia-northeast1-a.c.elastic-sa.internal",
    "gcp.gce.instance.name": HOSTS["app"]["name"],
    "host.arch": "amd64", "host.id": HOSTS["app"]["gce_id"], "host.name": HOSTS["app"]["name"],
    "host.type": "e2-medium", "os.description": "Linux 7.0.0-1014-gcp", "os.type": "linux",
    "os.version": "7.0.0-1014-gcp",
    "process.executable.path": "/usr/lib/jvm/java-17-openjdk-amd64/bin/java", "process.pid": APP_PID,
    "process.runtime.description": "Ubuntu OpenJDK 64-Bit Server VM 17.0.20.1+1-1-24.04-Ubuntu",
    "process.runtime.name": "OpenJDK Runtime Environment",
    "process.runtime.version": "17.0.20.1+1-1-24.04-Ubuntu",
    "service.instance.id": INSTANCE_ID, "service.name": SVC, "service.version": "1.0.0",
    "telemetry.distro.name": "elastic", "telemetry.distro.version": "1.10.0",
    "telemetry.sdk.language": "java", "telemetry.sdk.name": "opentelemetry", "telemetry.sdk.version": "1.60.1",
}
APP_TRACE_RES = dict(APP_RES, **{
    "agent.name": "opentelemetry/java/elastic", "agent.version": "1.10.0",
    "process.command_args": ["/usr/lib/jvm/java-17-openjdk-amd64/bin/java", "-Xms128m", "-Xmx128m",
                             "-javaagent:/opt/otel-demo/elastic-otel-javaagent.jar", "-jar", "/opt/otel-demo/app.jar"],
})
FE_RES = {
    "agent.name": "opentelemetry/webjs", "agent.version": "1.19.0", "service.name": FE_SVC,
    "service.version": "2.0.0", "telemetry.sdk.language": "webjs", "telemetry.sdk.name": "opentelemetry",
    "telemetry.sdk.version": "1.19.0",
}
APP_METRIC_RES = {"host.name": HOSTS["app"]["name"], "service.name": SVC}
APP_TX_RES = {
    "agent.name": "opentelemetry/java/elastic", "cloud.account.id": "elastic-sa",
    "cloud.availability_zone": "asia-northeast1-a", "cloud.platform": "gcp_compute_engine",
    "cloud.provider": "gcp", "cloud.region": "asia-northeast1", "deployment.environment": "production",
    "host.name": HOSTS["app"]["name"], "os.type": "linux",
    "process.runtime.name": "OpenJDK Runtime Environment",
    "process.runtime.version": "17.0.20.1+1-1-24.04-Ubuntu", "service.instance.id": INSTANCE_ID,
    "service.name": SVC, "service.version": "1.0.0", "signal_to_metrics.service.instance.id": S2M_INSTANCE,
    "telemetry.sdk.language": "java",
}
APP_AGG_RES = {
    "agent.name": "opentelemetry/java/elastic", "deployment.environment": "production",
    "service.name": SVC, "signal_to_metrics.service.instance.id": S2M_INSTANCE, "telemetry.sdk.language": "java",
}
FE_AGG_RES = {
    "agent.name": "opentelemetry/webjs", "service.name": FE_SVC,
    "signal_to_metrics.service.instance.id": S2M_INSTANCE, "telemetry.sdk.language": "webjs",
}
STACKTRACE = (
    "\tat io.opentelemetry.javaagent.shaded.instrumentation.api.instrumenter.Instrumenter.doEnd(Instrumenter.java:300)\n"
    "\tat io.opentelemetry.javaagent.instrumentation.tomcat.common.TomcatHelper.end(TomcatHelper.java:50)\n"
    "\tat org.apache.catalina.connector.CoyoteAdapter.service(CoyoteAdapter.java:422)\n"
    "\tat org.apache.coyote.http11.Http11Processor.service(Http11Processor.java:391)\n"
    "\tat org.apache.tomcat.util.net.NioEndpoint$SocketProcessor.doRun(NioEndpoint.java:1744)\n"
    "\tat java.base/java.lang.Thread.run(Thread.java:840)\n")

USERS = [
    ("Alice", "Chrome", "141.0.0.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36", "203.0.113.24"),
    ("Bob", "Firefox", "158.0", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0", "180.26.98.135"),
    ("Carol", "Safari", "19.0", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15", "198.51.100.77"),
    ("Dave", "Edge", "141.0.0.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0", "192.0.2.150"),
]

STARTUP_LOGS = [  # (offset s, scope, text)
    (0.0, "com.example.demo.Application", "Starting Application v1.0.0 using Java 17.0.20.1 with PID %d (/opt/otel-demo/app.jar started by otel-demo in /opt/otel-demo)" % APP_PID),
    (0.05, "com.example.demo.Application", 'No active profile set, falling back to 1 default profile: "default"'),
    (2.8, "org.springframework.data.repository.config.RepositoryConfigurationDelegate", "Multiple Spring Data modules found, entering strict repository configuration mode"),
    (2.81, "org.springframework.data.repository.config.RepositoryConfigurationDelegate", "Bootstrapping Spring Data Redis repositories in DEFAULT mode."),
    (2.89, "org.springframework.data.repository.config.RepositoryConfigurationDelegate", "Finished Spring Data repository scanning in 25 ms. Found 0 Redis repository interfaces."),
    (4.1, "org.springframework.boot.web.embedded.tomcat.TomcatWebServer", "Tomcat initialized with port 8080 (http)"),
    (4.15, "org.apache.coyote.http11.Http11NioProtocol", 'Initializing ProtocolHandler ["http-nio-8080"]'),
    (4.18, "org.apache.catalina.core.StandardService", "Starting service [Tomcat]"),
    (4.18, "org.apache.catalina.core.StandardEngine", "Starting Servlet engine: [Apache Tomcat/10.1.16]"),
    (4.3, "org.apache.catalina.core.ContainerBase.[Tomcat].[localhost].[/]", "Initializing Spring embedded WebApplicationContext"),
    (4.3, "org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext", "Root WebApplicationContext: initialization completed in 4153 ms"),
    (5.7, "org.springframework.boot.autoconfigure.web.servlet.WelcomePageHandlerMapping", "Adding welcome page: class path resource [static/index.html]"),
    (6.6, "org.apache.coyote.http11.Http11NioProtocol", 'Starting ProtocolHandler ["http-nio-8080"]'),
    (6.7, "org.springframework.boot.web.embedded.tomcat.TomcatWebServer", "Tomcat started on port 8080 (http) with context path ''"),
    (6.73, "com.example.demo.Application", "Started Application in 9.217 seconds (process running for 18.009)"),
    (15.0, "org.springframework.web.servlet.DispatcherServlet", "Initializing Servlet 'dispatcherServlet'"),
    (15.0, "org.apache.catalina.core.ContainerBase.[Tomcat].[localhost].[/]", "Initializing Spring DispatcherServlet 'dispatcherServlet'"),
    (15.01, "org.springframework.web.servlet.DispatcherServlet", "Completed initialization in 4 ms"),
]
APP_START_LEAD = 20.0  # traffic begins this many seconds after window start

PROCS_COMMON = [  # name, pid, ppid, command, exe path, owner, cpu share (core fraction), mem bytes
    ("systemd", 1, 0, "/sbin/init", "/usr/lib/systemd/systemd", "root", 0.0003, 14e6),
    ("systemd-journald", 412, 1, "/usr/lib/systemd/systemd-journald", "/usr/lib/systemd/systemd-journald", "root", 0.0005, 30e6),
    ("sshd", 1040, 1, "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups", "/usr/sbin/sshd", "root", 0.0001, 8e6),
    ("chronyd", 1002, 1, "/usr/sbin/chronyd -F 1", "/usr/sbin/chronyd", "_chrony", 0.0001, 4e6),
    ("rsyslogd", 1011, 1, "/usr/sbin/rsyslogd -n -iNONE", "/usr/sbin/rsyslogd", "syslog", 0.0006, 7e6),
    ("cron", 1013, 1, "/usr/sbin/cron -f -P", "/usr/sbin/cron", "root", 0.0, 3e6),
    ("dbus-daemon", 1015, 1, "@dbus-daemon --system --address=systemd: --nofork --nopidfile --systemd-activation --syslog-only", "/usr/bin/dbus-daemon", "messagebus", 0.0, 5e6),
    ("google_guest_agent", 1030, 1, "/usr/bin/google_guest_agent", "/usr/bin/google_guest_agent", "root", 0.002, 45e6),
    ("elastic-agent", 1170, 1, "/opt/Elastic/Agent/elastic-agent", "/opt/Elastic/Agent/data/elastic-agent-9.5.5/elastic-agent", "root", 0.006, 180e6),
    ("elastic-otel-collector", 1450, 1170, "/opt/Elastic/Agent/data/elastic-agent-9.5.5/components/elastic-otel-collector", "/opt/Elastic/Agent/data/elastic-agent-9.5.5/components/elastic-otel-collector", "root", 0.008, 210e6),
]
PROCS_HOST = {
    "app": [("java", APP_PID, 1, "/usr/lib/jvm/java-17-openjdk-amd64/bin/java -Xms128m -Xmx128m -javaagent:/opt/otel-demo/elastic-otel-javaagent.jar -jar /opt/otel-demo/app.jar",
             "/usr/lib/jvm/java-17-openjdk-amd64/bin/java", "otel-demo", 0.004, 480e6)],
    "redis": [("redis-server", 4517, 1, "/usr/bin/redis-server 127.0.0.1:6379", "/usr/bin/redis-check-rdb", "redis", 0.002, 12e6)],
}

SYSLOG_NOISE = {
    "app": [("systemd", "Started session-{n}.scope - Session {n} of User otel-demo."),
            ("systemd", "session-{n}.scope: Deactivated successfully."),
            ("google_metadata_script_runner", "startup-script: INFO Finished running startup scripts."),
            ("elastic-agent", "Elastic Agent is healthy; 2 components running"),
            ("kernel", "[UFW AUDIT] IN=ens4 OUT= SRC=203.0.113.9 DST=10.146.0.37 PROTO=TCP DPT=22"),
            ("google_guest_agent", "Updating keys for user otel-demo.")],
    "redis": [("systemd", "Started session-{n}.scope - Session {n} of User otel-demo."),
              ("systemd", "session-{n}.scope: Deactivated successfully."),
              ("elastic-agent", "Elastic Agent is healthy; 2 components running"),
              ("redis-server", "Background saving terminated with success"),
              ("google_guest_agent", "Updating keys for user otel-demo.")],
}
AUTH_NOISE = [("systemd-logind", "New session {n} of user otel-demo."),
              ("systemd-logind", "Removed session {n}."),
              ("sshd", "Accepted publickey for otel-demo from 203.0.113.9 port 51732 ssh2: ED25519 SHA256:abc"),
              ("CRON", "pam_unix(cron:session): session opened for user root(uid=0) by root(uid=0)"),
              ("CRON", "pam_unix(cron:session): session closed for user root")]

APM_INTERVALS = (("1m", 60), ("10m", 600), ("60m", 3600))
HASH_TX, HASH_SUM = "fcc0c48a", "31c4f886"
HASH_DEST_COUNT, HASH_DEST_SUM = "10e60215", "5cb11d5"


# ------------------------------------------------------------------ helpers
def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def hexid(rng, n):
    return "%0*x" % (n, rng.getrandbits(n * 4))


def mhash(names):
    return hashlib.md5(",".join(sorted(names)).encode()).hexdigest()[:16]


def interp(curve, x):
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def exp_hist(vals, scale):
    pos, zero = {}, 0
    for v in vals:
        if v <= 0:
            zero += 1
            continue
        i = math.ceil(math.log2(v) * (2 ** scale)) - 1
        pos[i] = pos.get(i, 0) + 1
    idx = sorted(pos)
    h = {"scale": scale, "sum": float(sum(vals)), "min": float(min(vals)), "max": float(max(vals)),
         "positive": {"indices": idx, "counts": [pos[i] for i in idx]}}
    if zero:
        h["zero"] = {"count": zero}
    return h


def _metric_types():
    try:
        with open(os.path.join(HERE, "metric_types.json")) as f:
            return json.load(f)
    except OSError:
        return {}


METRIC_TYPES = _metric_types()


def dynamic_templates(doc):
    """OTel mapping-mode hints (as the Elasticsearch exporter sends them): pick the
    metrics-otel@mappings dynamic template per metric so histograms / summaries get
    their real field types instead of plain float/long sub-fields."""
    out = {}
    for name, v in doc.get("metrics", {}).items():
        t = METRIC_TYPES.get(name)
        if t is None:
            if isinstance(v, dict):
                t = "exponential_histogram" if "positive" in v or "scale" in v else "summary"
            else:
                t = "gauge_long" if isinstance(v, int) else "gauge_double"
        out["metrics." + name] = t
    return out


class Sink:
    def __init__(self):
        self.lines = defaultdict(list)

    def add(self, stream, doc):
        action = {"create": {"dynamic_templates": dynamic_templates(doc)}} if "metrics" in doc else {"create": {}}
        self.lines[stream].append(json.dumps(action, separators=(",", ":")) + "\n" + json.dumps(doc, separators=(",", ":")))


# --------------------------------------------------------------- load model
class Load:
    """CPU load per host over time (scenario driven) and the latency it causes."""

    def __init__(self, sc, start, end, rng):
        self.start, self.rng = start, rng
        self.base = sc["baseline_cpu_percent"]
        self.phases = sc["cpu_load"]
        self.manual = []  # phases from control.json (absolute start_ts / end_ts)
        self.curve = sc["redis_latency_ms_by_cpu"]
        self.noise, self._x = [], 0.0

    def _noise(self, i):
        while len(self.noise) <= i:
            self._x = 0.92 * self._x + self.rng.gauss(0, 0.35)
            self.noise.append(self._x)
        return self.noise[i]

    def all_phases(self):
        return self.phases + self.manual

    def _bounds(self, p):
        if "start_ts" in p:
            return p["start_ts"], p["end_ts"] if p.get("end_ts") is not None else math.inf
        return self.start + p["start_minute"] * 60, self.start + p["end_minute"] * 60

    def level(self, host, t):
        lv = self.base[host]
        for p in self.all_phases():
            if p["host"] != host:
                continue
            s, e = self._bounds(p)
            if s <= t <= e:
                r = max(p.get("ramp_seconds", 30), 1)
                f = min(1.0, (t - s) / r, (e - t) / r)
                lv = max(lv, self.base[host] + (p["cpu_percent"] - self.base[host]) * f)
        return lv

    def cpu(self, host, t):  # percent, whole host
        lv = self.level(host, t)
        i = max(0, int(t - self.start))
        return max(0.1, min(99.5, lv + self._noise(i) * (0.06 * lv + 0.25)))

    def active(self, host, t):
        return any(p["host"] == host and self._bounds(p)[0] <= t <= self._bounds(p)[1] for p in self.all_phases())

    def redis_ms(self, t):
        mean = interp(self.curve, self.cpu("redis", t))
        sigma = 0.45
        return self.rng.lognormvariate(math.log(mean) - sigma ** 2 / 2, sigma)


# ------------------------------------------------------- traces / app logs
def filebeat_doc(h, t, dataset, path, message, pname, pid=None, syslog=None, offset=0):
    d = {
        "@timestamp": iso(t),
        "agent": {"ephemeral_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "eph" + h["name"])), "id": h["agent_id"],
                  "name": h["name"], "type": "filebeat", "version": "9.5.5"},
        "cloud": {"account": {"id": "elastic-sa"}, "availability_zone": "asia-northeast1-a",
                  "instance": {"id": h["gce_id"], "name": h["name"]}, "machine": {"type": "e2-medium"},
                  "project": {"id": "elastic-sa"}, "provider": "gcp", "region": "asia-northeast1",
                  "service": {"name": "GCE"}},
        "data_stream": {"dataset": dataset, "namespace": "default", "type": "logs"},
        "ecs": {"version": "8.11.0"},
        "elastic_agent": {"id": h["agent_id"], "snapshot": False, "version": "9.5.5"},
        "event": {"dataset": dataset, "kind": "event", "module": "system", "timezone": "+00:00"},
        "host": {"architecture": "x86_64", "containerized": False, "hostname": h["name"], "id": h["host_id"],
                 "ip": [h["ip"], h["ipv6"]], "mac": [h["mac"]], "name": h["name"],
                 "os": {"codename": "noble", "family": "debian", "kernel": "7.0.0-1014-gcp", "name": "Ubuntu",
                        "platform": "ubuntu", "type": "linux", "version": "24.04.5 LTS (Noble Numbat)"}},
        "input": {"type": "log"},
        "log": {"file": {"path": path}, "offset": offset},
        "message": message,
        "process": {"name": pname},
    }
    if pid:
        d["process"]["pid"] = pid
    if syslog:
        d["log"]["syslog"] = syslog
        d["related"] = {"hosts": [h["name"]]}
        d["tags"] = ["system-auth"]
    return d


class Traffic:
    def __init__(self, sink, load, rng, start, end, sc):
        self.sink, self.load, self.rng, self.start, self.end = sink, load, rng, start, end
        self.tr = sc["traffic"]
        self.http, self.redis = [], []                # (t, seconds[, op]) for JVM metrics
        self.tx = defaultdict(list)                   # agg key -> [(t, us)]
        self.dest = defaultdict(list)
        self.summary = defaultdict(list)              # service -> [t]
        self.n_requests = 0
        self.syslog_off = 0
        self.t_next = start + APP_START_LEAD
        self.next_ping = self.t_next + rng.uniform(0, self.tr["redis_ping_interval_seconds"])

    def prune(self, before_jvm, before_agg):
        self.http = [x for x in self.http if x[0] >= before_jvm]
        self.redis = [x for x in self.redis if x[0] >= before_jvm]
        for d in (self.tx, self.dest, self.summary):
            for k in list(d):
                d[k] = [e for e in d[k] if (e[0] if isinstance(e, tuple) else e) >= before_agg]

    def _span(self, stream_kind, name, kind, t, dur_ms, trace_id, span_id, parent, res, scope, attrs):
        d = {"@timestamp": iso(t), "attributes": attrs,
             "data_stream": {"dataset": "generic.otel", "namespace": "default", "type": "traces"},
             "duration": int(dur_ms * 1e6), "kind": kind, "links": [], "name": name,
             "resource": {"attributes": res, "schema_url": "https://opentelemetry.io/schemas/1.24.0" if res is APP_TRACE_RES else None},
             "scope": scope, "span_id": span_id, "trace_id": trace_id}
        if res is not APP_TRACE_RES:
            del d["resource"]["schema_url"]
        if parent:
            d["parent_span_id"] = parent
        self.sink.add("traces-generic.otel-default", d)

    def _redis_span(self, op, text, t, ms, trace_id, parent, user):
        rng = self.rng
        a = {"db.operation": op, "db.operation.name": op, "db.query.text": text, "db.statement": text,
             "db.system": "redis", "db.system.name": "redis", "event.outcome": "success", "event.success_count": 1,
             "network.peer.address": HOSTS["redis"]["ip"], "network.peer.port": 6379, "network.type": "ipv4",
             "processor.event": "span", "service.target.name": "", "service.target.type": "redis",
             "span.destination.service.resource": "redis", "span.duration.us": int(ms * 1000),
             "span.representative_count": 1.0, "span.subtype": "redis", "span.type": "db",
             "thread.id": 45, "thread.name": "http-nio-8080-exec-%d" % rng.randint(1, 10),
             "timestamp.us": int(t * 1e6)}
        if user:
            a["user.name"] = user
        self._span("t", op, "Client", t, ms, trace_id, hexid(rng, 16), parent, APP_TRACE_RES,
                   {"attributes": {"service.framework.name": "io.opentelemetry.jedis-4.0", "service.framework.version": "2.26.1-alpha"},
                    "name": "io.opentelemetry.jedis-4.0", "version": "2.26.1-alpha"}, a)
        self.dest[(SVC, op, "redis", "", "redis", "app")].append((t, int(ms * 1000)))
        self.summary[SVC].append(t)
        self.redis.append((t, ms / 1000.0, op))

    def advance(self, until):
        """Generate traffic up to `until`; callable repeatedly with a growing `until`."""
        rng, tr = self.rng, self.tr
        rate = tr["requests_per_minute"] / 60.0
        self.end = until
        lim = until - 1
        while True:
            t = self.t_next
            while self.next_ping <= min(t, lim):
                self.ping(self.next_ping)
                self.next_ping += tr["redis_ping_interval_seconds"] * rng.uniform(0.7, 1.3)
            if t >= lim:
                break
            self.request(t)
            self.t_next = t + rng.expovariate(rate)

    def ping(self, t):
        if t >= self.end:
            return
        ms = self.load.redis_ms(t) * 6.0 if self.load.cpu("redis", t) > 10 else self.rng.uniform(0.6, 4)
        self._redis_span("PING", "PING", t, ms, hexid(self.rng, 32), None, None)

    def request(self, t0):
        rng, load = self.rng, self.load
        user, browser, bver, ua, ip = rng.choice(USERS)
        trace_id, fe_id, srv_id, int_id = hexid(rng, 32), hexid(rng, 16), hexid(rng, 16), hexid(rng, 16)
        net_in, pre, pre_i = rng.uniform(10, 20), rng.uniform(2, 4), rng.uniform(0.3, 0.9)
        t_srv = t0 + net_in / 1000
        t_int = t_srv + pre / 1000
        t_red = t_int + pre_i / 1000
        redis_ms = load.redis_ms(t_red)
        int_ms = pre_i + redis_ms + rng.uniform(0.1, 0.5)
        srv_ms = pre + int_ms + rng.uniform(0.3, 1.5)
        fe_ms = net_in + srv_ms + rng.uniform(8, 20)
        self.n_requests += 1
        thread = "http-nio-8080-exec-%d" % rng.randint(1, 10)
        sid = rng.randint(30000, 60000)

        # frontend (browser) span: transaction + external span
        fe_attr = {"component": "fetch", "destination.address": APP_PUBLIC_IP, "destination.port": 8080,
                   "event.outcome": "success", "event.success_count": 1, "http.host": APP_PUBLIC_IP + ":8080",
                   "http.method": "GET", "http.response_content_length": 181, "http.scheme": "http",
                   "http.status_code": 200, "http.status_text": "", "http.url": "http://%s:8080/api/status" % APP_PUBLIC_IP,
                   "http.user_agent": ua, "processor.event": "transaction",
                   "service.target.name": APP_PUBLIC_IP + ":8080", "service.target.type": "http",
                   "span.destination.service.name": "http://%s:8080" % APP_PUBLIC_IP,
                   "span.destination.service.resource": APP_PUBLIC_IP + ":8080",
                   "span.destination.service.type": "external", "span.duration.us": int(fe_ms * 1000),
                   "span.representative_count": 1.0, "span.subtype": "http", "span.type": "external",
                   "timestamp.us": int(t0 * 1e6), "transaction.duration.us": int(fe_ms * 1000),
                   "transaction.id": fe_id, "transaction.name": "HTTP GET", "transaction.representative_count": 1.0,
                   "transaction.result": "HTTP 2xx", "transaction.root": True, "transaction.sampled": True,
                   "transaction.type": "external.http", "url.original": "http://%s:8080/api/status" % APP_PUBLIC_IP,
                   "user.name": user, "user_agent.name": browser, "user_agent.original": ua, "user_agent.version": bver}
        self._span("t", "HTTP GET", "Client", t0, fe_ms, trace_id, fe_id, None, FE_RES,
                   {"attributes": {"service.framework.name": "@opentelemetry/instrumentation-fetch", "service.framework.version": "0.45.0"},
                    "name": "@opentelemetry/instrumentation-fetch", "version": "0.45.0"}, fe_attr)
        # server span
        srv_attr = {"client.address": ip, "code.stacktrace": STACKTRACE, "event.outcome": "success", "event.success_count": 1,
                    "http.request.header.x-user-id": [user], "http.request.method": "GET",
                    "http.response.status_code": 200, "http.route": "/api/status", "network.peer.address": ip,
                    "network.peer.port": rng.randint(10000, 60000), "network.protocol.version": "1.1",
                    "processor.event": "transaction", "server.address": APP_PUBLIC_IP, "server.port": 8080,
                    "thread.id": 43, "thread.name": thread, "timestamp.us": int(t_srv * 1e6),
                    "transaction.duration.us": int(srv_ms * 1000), "transaction.id": srv_id,
                    "transaction.name": "GET /api/status", "transaction.representative_count": 1.0,
                    "transaction.result": "HTTP 2xx", "transaction.root": False, "transaction.sampled": True,
                    "transaction.type": "request", "url.path": "/api/status", "url.scheme": "http",
                    "user.name": user, "user_agent.name": browser, "user_agent.original": ua, "user_agent.version": bver}
        self._span("t", "GET /api/status", "Server", t_srv, srv_ms, trace_id, srv_id, fe_id, APP_TRACE_RES,
                   {"attributes": {"service.framework.name": "io.opentelemetry.tomcat-10.0", "service.framework.version": "2.26.1-alpha"},
                    "name": "io.opentelemetry.tomcat-10.0", "schema_url": "https://opentelemetry.io/schemas/1.37.0",
                    "version": "2.26.1-alpha"}, srv_attr)
        # internal + redis spans
        int_attr = {"code.function": "runRedisSet", "code.namespace": "com.example.demo.StatusController",
                    "event.outcome": "success", "event.success_count": 1, "processor.event": "span",
                    "span.duration.us": int(int_ms * 1000), "span.representative_count": 1.0, "span.subtype": "internal",
                    "span.type": "app", "thread.id": 45, "thread.name": thread, "timestamp.us": int(t_int * 1e6),
                    "user.name": user}
        self._span("t", "StatusController.runRedisSet", "Internal", t_int, int_ms, trace_id, int_id, srv_id, APP_TRACE_RES,
                   {"attributes": {"service.framework.name": "io.opentelemetry.methods", "service.framework.version": "2.26.1-alpha"},
                    "name": "io.opentelemetry.methods", "version": "2.26.1-alpha"}, int_attr)
        self._redis_span("SETEX", "SETEX status_key 60 ok", t_red, redis_ms, trace_id, int_id, user)

        # aggregation / jvm bookkeeping
        self.tx[(FE_SVC, "HTTP GET", "HTTP 2xx", "external.http", True)].append((t0, int(fe_ms * 1000)))
        self.dest[(FE_SVC, "HTTP GET", "http", APP_PUBLIC_IP + ":8080", APP_PUBLIC_IP + ":8080", "fe")].append((t0, int(fe_ms * 1000)))
        self.tx[(SVC, "GET /api/status", "HTTP 2xx", "request", False)].append((t_srv, int(srv_ms * 1000)))
        self.summary[FE_SVC].append(t0)
        self.summary[SVC] += [t_srv, t_int]
        self.http.append((t_srv, srv_ms / 1000.0))

        # logs: app log, syslog copy, frontend navigation timing events
        text = "API Hit: service_version=1.0.0, behavior=v1"
        tl = t_srv + 0.001
        self.sink.add("logs-generic.otel-default", {
            "@timestamp": iso(tl), "body": {"text": text},
            "data_stream": {"dataset": "generic.otel", "namespace": "default", "type": "logs"},
            "observed_timestamp": iso(tl), "resource": {"attributes": APP_RES, "schema_url": "https://opentelemetry.io/schemas/1.24.0"},
            "scope": {"name": "com.example.demo.StatusController"}, "severity_number": 9, "severity_text": "INFO",
            "span_id": srv_id, "trace_id": trace_id})
        line = json.dumps({"@timestamp": iso(tl), "level": "INFO", "logger_name": "com.example.demo.StatusController",
                           "thread_name": thread, "message": text, "trace_id": trace_id, "trace_flags": "01", "span_id": srv_id},
                          separators=(",", ":"))
        self.syslog_off += len(line) + 1
        self.sink.add("logs-system.syslog-default", filebeat_doc(HOSTS["app"], tl, "system.syslog", "/var/log/syslog", line, "bash", APP_PID, offset=self.syslog_off))
        events = [("fetchStart", 0), ("domainLookupStart", 0.4), ("domainLookupEnd", 0.4), ("connectStart", 0.4),
                  ("connectEnd", 1.2), ("requestStart", 1.6), ("responseStart", fe_ms - 3), ("responseEnd", fe_ms)]
        for name, off in events:
            tt = t0 + off / 1000
            self.sink.add("logs-generic.otel-default", {
                "@timestamp": iso(tt), "attributes": {"event.name": name, "timestamp.us": int(tt * 1e6)},
                "data_stream": {"dataset": "generic.otel", "namespace": "default", "type": "logs"},
                "event_name": name, "resource": {"attributes": FE_RES},
                "scope": {"attributes": {"service.framework.name": "@opentelemetry/instrumentation-fetch", "service.framework.version": "0.45.0"},
                          "name": "@opentelemetry/instrumentation-fetch", "version": "0.45.0"},
                "span_id": fe_id, "trace_id": trace_id})


def gen_app_startup_logs(sink, start):
    for off, scope, text in STARTUP_LOGS:
        t = start + off
        sink.add("logs-generic.otel-default", {
            "@timestamp": iso(t), "body": {"text": text},
            "data_stream": {"dataset": "generic.otel", "namespace": "default", "type": "logs"},
            "resource": {"attributes": APP_RES, "schema_url": "https://opentelemetry.io/schemas/1.24.0"},
            "scope": {"name": scope}, "severity_number": 9, "severity_text": "INFO"})


class SystemLogs:
    """syslog / auth noise per host plus backup-job markers for CPU load phases; incremental."""

    def __init__(self, sink, load, rng, start):
        self.sink, self.load, self.rng = sink, load, rng
        self.st = {k: {"n": 3, "off": 0, "sys": start + rng.uniform(5, 20), "auth": start + rng.uniform(30, 300)} for k in HOSTS}
        self.marks = {}

    def advance(self, until):
        sink, rng = self.sink, self.rng
        for key, h in HOSTS.items():
            s = self.st[key]
            while s["sys"] < until:
                pname, tmpl = rng.choice(SYSLOG_NOISE[key])
                s["off"] += 100
                sink.add("logs-system.syslog-default", filebeat_doc(h, s["sys"], "system.syslog", "/var/log/syslog", tmpl.format(n=s["n"]), pname, offset=s["off"]))
                s["n"] += rng.randint(0, 1)
                s["sys"] += rng.expovariate(1 / 25.0)
            while s["auth"] < until:
                pname, tmpl = rng.choice(AUTH_NOISE)
                sink.add("logs-system.auth-default", filebeat_doc(
                    h, s["auth"], "system.auth", "/var/log/auth.log", tmpl.format(n=s["n"]), pname, offset=s["off"],
                    syslog={"appname": pname, "hostname": h["name"], "procid": str(rng.randint(900, 9000))}))
                s["auth"] += rng.expovariate(1 / 420.0)
        # backup-job (cron tar|gzip) markers for load phases
        for p in self.load.all_phases():
            m = self.marks.setdefault(p.get("id", id(p)), {"pid": rng.randint(20000, 30000), "start": False, "end": False})
            h = HOSTS[p["host"]]
            s, e = self.load._bounds(p)
            pid = m["pid"]
            if s < until and not m["start"]:
                m["start"] = True
                sink.add("logs-system.syslog-default", filebeat_doc(h, s, "system.syslog", "/var/log/syslog", "(root) CMD (/opt/backup/nightly_backup.sh)", "CRON"))
                sink.add("logs-system.syslog-default", filebeat_doc(h, s + 0.1, "system.syslog", "/var/log/syslog", "[%d] starting full backup: tar -cf - /var/lib/redis /var/log | gzip -9 > /backup/full-%s.tar.gz" % (pid, time.strftime("%Y%m%d", time.gmtime(s))), "nightly_backup.sh"))
            if e < until and not m["end"]:
                m["end"] = True
                sink.add("logs-system.syslog-default", filebeat_doc(h, e, "system.syslog", "/var/log/syslog", "[%d] backup completed in %.2fs" % (pid, e - s), "nightly_backup.sh"))


# --------------------------------------------------------------- hostmetrics
class HostSim:
    STATES = ("user", "system", "wait", "softirq", "steal", "nice", "interrupt", "idle")
    DISKS = ("sda", "sda1", "sda15", "loop0", "loop1")
    MOUNTS = (("/dev/sda1", "/", "ext4", "rw", 9.6e9, 3.4e9, 1.2e6, 1.6e5),
              ("/dev/sda16", "/boot", "ext4", "rw", 0.9e9, 0.15e9, 6.5e4, 3e2),
              ("/dev/sda15", "/boot/efi", "vfat", "rw", 1.06e8, 6e6, 0, 0),
              ("/dev/loop0", "/snap/core24/2124", "squashfs", "ro", 7.4e7, 7.4e7, 1e4, 1e4),
              ("/dev/loop1", "/snap/google-cloud-cli/513", "squashfs", "ro", 3.9e8, 3.9e8, 5e4, 5e4),
              ("/dev/loop2", "/snap/snapd/28254", "squashfs", "ro", 5.1e7, 5.1e7, 1e3, 1e3))
    TCP_STATES = ("CLOSE", "CLOSE_WAIT", "CLOSING", "DELETE", "ESTABLISHED", "FIN_WAIT_1", "FIN_WAIT_2",
                  "LAST_ACK", "LISTEN", "SYN_RECV", "SYN_SENT", "TIME_WAIT")

    def __init__(self, key, load, rng, start, sink):
        self.h, self.key, self.load, self.rng, self.start, self.sink = HOSTS[key], key, load, rng, start, sink
        self.boot = start - 3 * 86400 - rng.uniform(0, 3600)
        self.res = {"host.arch": "amd64", "host.cpu.cache.l2.size": 56320, "host.cpu.family": "6",
                    "host.cpu.model.id": "79", "host.cpu.model.name": "Intel(R) Xeon(R) CPU @ 2.20GHz",
                    "host.cpu.stepping": "0", "host.cpu.vendor.id": "GenuineIntel", "host.ip": [self.h["ip"], self.h["ipv6"]],
                    "host.mac": self.h["mac"], "host.name": self.h["name"],
                    "os.description": "Ubuntu 24.04.5 LTS (Noble Numbat) (Linux %s 7.0.0-1014-gcp #14~24.04.1-Ubuntu SMP PREEMPT Thu Sep 17 19:52:35 UTC 2026 x86_64)" % self.h["name"],
                    "os.type": "linux"}
        age = start - self.boot
        self.cpu_time = {s: age * CORES * (0.97 if s == "idle" else 0.004) for s in self.STATES}
        self.load_avg = [0.05, 0.05, 0.05]
        self.created = 4200
        self.ctr = defaultdict(float)
        self.pstate = {}
        self.stress_pids = (rng.randint(20000, 30000), rng.randint(30001, 31000), rng.randint(31001, 32000))
        self.procs = PROCS_COMMON + PROCS_HOST[key]
        for p in self.procs:
            born = start - 600 if p[0] == "java" else self.boot + rng.uniform(5, 60)
            self.pstate[p[1]] = {"start": born, "user": rng.uniform(5, 500), "system": rng.uniform(1, 80), "wait": 0.0,
                                 "rd": rng.uniform(1e6, 5e7), "wr": rng.uniform(1e5, 1e7), "rdo": rng.uniform(100, 1e4), "wro": rng.uniform(10, 5e3)}

    def _m(self, t, scope, metrics, attrs=None, unit=None, schema="1.9.0", res=None):
        d = {"@timestamp": iso(t), "_metric_names_hash": mhash(metrics),
             "data_stream": {"dataset": "hostmetricsreceiver.otel", "namespace": "default", "type": "metrics"},
             "metrics": metrics, "resource": {"attributes": res or self.res, "schema_url": "https://opentelemetry.io/schemas/%s" % schema},
             "scope": {"name": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/hostmetricsreceiver/internal/scraper/" + scope,
                       "version": "9.5.5"},
             "start_timestamp": iso(self.boot)}
        if attrs:
            d["attributes"] = attrs
        if unit:
            d["unit"] = unit
        self.sink.add("metrics-hostmetricsreceiver.otel-default", d)

    def scrape(self, t):
        rng = self.rng
        busy = self.load.cpu(self.key, t) / 100.0
        share = {"user": 0.90, "system": 0.085, "softirq": 0.010, "steal": 0.003, "nice": 0.002}
        util = {s: busy * v for s, v in share.items()}
        util["wait"] = max(0.0, rng.gauss(0.0006, 0.0004))
        util["interrupt"] = 0.0
        util["idle"] = max(0.0, 1 - sum(util.values()))
        for s in self.STATES:
            self.cpu_time[s] += util[s] * SCRAPE * CORES
            self._m(t, "cpuscraper", {"system.cpu.time": round(self.cpu_time[s], 3)}, {"state": s}, "s")
            self._m(t, "cpuscraper", {"system.cpu.utilization": util[s]}, {"state": s}, "1")
        self._m(t, "cpuscraper", {"system.cpu.logical.count": CORES}, unit="{cpu}")
        for i, w in enumerate((SCRAPE / 60, SCRAPE / 300, SCRAPE / 900)):
            a = 1 - math.exp(-w)
            self.load_avg[i] += a * (busy * CORES * 1.1 + 0.02 - self.load_avg[i])
        self._m(t, "loadscraper", {"system.cpu.load_average.1m": self.load_avg[0], "system.cpu.load_average.5m": self.load_avg[1],
                                   "system.cpu.load_average.15m": self.load_avg[2]}, unit="{thread}")
        # memory
        used = self.h["mem_used"] * (1 + rng.gauss(0, 0.004)) + (6e6 if self.load.active(self.key, t) else 0)
        mem = {"used": used, "buffered": 62e6, "cached": 1.3e9 + rng.gauss(0, 2e6), "slab_reclaimable": 150e6, "slab_unreclaimable": 90e6}
        mem["free"] = MEM_TOTAL - sum(mem.values())
        for s, v in mem.items():
            self._m(t, "memoryscraper", {"system.memory.usage": int(v)}, {"state": s}, "By")
            self._m(t, "memoryscraper", {"system.memory.utilization": v / MEM_TOTAL}, {"state": s}, "1")
        # disk
        for dev in self.DISKS:
            f = 0.0 if dev.startswith("loop") else (1 + busy)
            for d in ("read", "write"):
                base = 4e4 if d == "read" else 1.5e5
                k = (dev, d)
                self.ctr[k, "io"] += int(f * rng.uniform(0.2, 1.6) * base)
                self.ctr[k, "ops"] += int(f * rng.uniform(0, 12))
                self.ctr[k, "mrg"] += int(f * rng.uniform(0, 3))
                self.ctr[k, "ot"] += f * rng.uniform(0, 0.02)
                self._m(t, "diskscraper", {"system.disk.io": int(self.ctr[k, "io"] + 4e7)}, {"device": dev, "direction": d}, "By")
                self._m(t, "diskscraper", {"system.disk.operations": int(self.ctr[k, "ops"] + 9e3), "system.disk.merged": int(self.ctr[k, "mrg"] + 100)},
                        {"device": dev, "direction": d}, "{operations}")
                self._m(t, "diskscraper", {"system.disk.operation_time": round(self.ctr[k, "ot"] + 3, 4)}, {"device": dev, "direction": d}, "s")
            self.ctr[dev, "iot"] += f * rng.uniform(0, 0.03)
            self._m(t, "diskscraper", {"system.disk.pending_operations": 0}, {"device": dev}, "{operations}")
            self._m(t, "diskscraper", {"system.disk.io_time": round(self.ctr[dev, "iot"] + 2, 4),
                                       "system.disk.weighted_io_time": round(self.ctr[dev, "iot"] * 1.2 + 2, 4)}, {"device": dev}, "s")
        # filesystem
        grow = (t - self.start) * 40
        for dev, mp, typ, mode, total, used_b, ino_total, ino_used in self.MOUNTS:
            ub = used_b + (grow if mp == "/" else 0)
            a = {"device": dev, "mode": mode, "mountpoint": mp, "type": typ}
            for s, v in (("used", ub), ("free", total - ub), ("reserved", 0 if typ != "ext4" else total * 0.05)):
                self._m(t, "filesystemscraper", {"system.filesystem.usage": int(v)}, dict(a, state=s), "By")
            for s, v in (("used", ino_used), ("free", ino_total - ino_used)):
                self._m(t, "filesystemscraper", {"system.filesystem.inodes.usage": int(v)}, dict(a, state=s), "{inodes}")
        # network
        for dev, f in (("ens4", 1.0 + busy), ("lo", 0.4)):
            for d in ("receive", "transmit"):
                k = (dev, d)
                inc = int(f * rng.uniform(2e4, 9e4))
                self.ctr[k, "b"] += inc
                self.ctr[k, "p"] += max(1, inc // 700)
                a = {"device": dev, "direction": d}
                self._m(t, "networkscraper", {"system.network.io": int(self.ctr[k, "b"] + 2e8)}, a, "By")
                self._m(t, "networkscraper", {"system.network.errors": 0}, a, "{errors}")
                self._m(t, "networkscraper", {"system.network.dropped": 0, "system.network.packets": int(self.ctr[k, "p"] + 4e5)}, a, "{packets}")
        est = rng.randint(3, 8)
        for s in self.TCP_STATES:
            v = {"ESTABLISHED": est, "LISTEN": 6, "TIME_WAIT": rng.randint(0, 5)}.get(s, 0)
            self._m(t, "networkscraper", {"system.network.connections": v}, {"protocol": "tcp", "state": s}, "{connections}")
        # processes
        active = self.load.active(self.key, t)
        run = 1 + (CORES if active else 0) + rng.randint(0, 1)
        for s, v in (("blocked", 0), ("idle", 52 + rng.randint(0, 2)), ("running", run), ("sleeping", 62 + rng.randint(0, 3))):
            self._m(t, "processesscraper", {"system.processes.count": v}, {"status": s}, "{processes}")
        self.created += rng.randint(1, 6)
        self._m(t, "processesscraper", {"system.processes.created": self.created}, unit="{processes}")
        self.process_docs(t, busy, active)

    def process_docs(self, t, busy, active):
        rng = self.rng
        roster = [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]) for p in self.procs]
        if active:
            roster.append(("nightly_backup.sh", self.stress_pids[0], 1, "/bin/bash /opt/backup/nightly_backup.sh", "/opt/backup/nightly_backup.sh", "root", 0.0, 4e6))
            roster.append(("tar", self.stress_pids[1], self.stress_pids[0], "tar -cf - /var/lib/redis /var/log", "/usr/bin/tar", "root", "stress", 6e6))
            roster.append(("gzip", self.stress_pids[2], self.stress_pids[0], "gzip -9", "/usr/bin/gzip", "root", "stress", 3e6))
        for name, pid, ppid, cmd, path, owner, cpu_share, mem in roster:
            st = self.pstate.setdefault(pid, {"start": t, "user": 0.0, "system": 0.0, "wait": 0.0, "rd": 0.0, "wr": 0.0, "rdo": 0.0, "wro": 0.0})
            if cpu_share == "stress":
                cpu_share = busy  # each worker pins about `busy` of one core
            elif name == "java":
                cpu_share = cpu_share + 0.002 * (1 + busy)
            share = cpu_share * (1 + rng.gauss(0, 0.15)) * SCRAPE
            st["user"] += max(0, share * 0.9)
            st["system"] += max(0, share * 0.1)
            st["wr"] += rng.uniform(0, 4e3) * (1 + cpu_share * 50)
            st["wro"] += rng.randint(0, 3)
            res = dict(self.res, **{"process.command": cmd, "process.command_line": cmd, "process.executable.name": name,
                                    "process.executable.path": path, "process.owner": owner, "process.parent_pid": ppid, "process.pid": pid})

            def m(metrics, attrs=None, unit=None):
                d = {"@timestamp": iso(t), "_metric_names_hash": mhash(metrics),
                     "data_stream": {"dataset": "hostmetricsreceiver.otel", "namespace": "default", "type": "metrics"},
                     "metrics": metrics, "resource": {"attributes": res, "schema_url": "https://opentelemetry.io/schemas/1.43.0"},
                     "scope": {"name": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/hostmetricsreceiver/internal/scraper/processscraper", "version": "9.5.5"},
                     "start_timestamp": iso(st["start"])}
                if attrs:
                    d["attributes"] = attrs
                if unit:
                    d["unit"] = unit
                self.sink.add("metrics-hostmetricsreceiver.otel-default", d)
            for s in ("user", "system", "wait"):
                m({"process.cpu.time": round(st[s], 3)}, {"state": s}, "s")
            m({"process.disk.io": int(st["rd"])}, {"direction": "read"}, "By")
            m({"process.disk.io": int(st["wr"])}, {"direction": "write"}, "By")
            m({"process.disk.operations": int(st["rdo"])}, {"direction": "read"}, "{operations}")
            m({"process.disk.operations": int(st["wro"])}, {"direction": "write"}, "{operations}")
            m({"process.threads": 45 if name == "java" else (3 if name == "redis-server" else 1)}, unit="{threads}")
            m({"process.memory.utilization": mem / MEM_TOTAL * (1 + rng.gauss(0, 0.003))}, unit="1")
            m({"process.open_file_descriptors": 24 if name == "java" else 8}, unit="{count}")
            m({"process.memory.usage": int(mem * (1 + rng.gauss(0, 0.003))), "process.memory.virtual": int(mem * 4)}, unit="By")


# ------------------------------------------------------------- JVM metrics
def gen_jvm(sink, traffic, load, rng, start, end, interval, cur):
    """`cur` holds the cursor/state between calls (empty dict on the first call)."""
    scope = {"name": "io.opentelemetry.runtime-telemetry-java8", "version": "2.26.1-alpha"}
    res = {"attributes": APP_METRIC_RES, "schema_url": "https://opentelemetry.io/schemas/1.24.0"}
    http_b, redis_b = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for tt, sv in traffic.http:
        http_b[int((tt - start) // interval)].append(sv)
    for tt, sv, op in traffic.redis:
        redis_b[int((tt - start) // interval)][op].append(sv)
    jvm_start = start
    state = cur.setdefault("state", {"eden": 40e6, "old": 30e6, "meta": 78e6, "classes": 16000, "ctx": 3700.0})
    t, i = cur.get("t", start + 10.0), 0
    while t < end:
        i += 1
        ib = int((t - start) // interval) - 1

        def m(metrics, attrs=None, unit=None, cumulative=True, sc=scope):
            d = {"@timestamp": iso(t), "_metric_names_hash": mhash(metrics),
                 "data_stream": {"dataset": "generic.otel", "namespace": "default", "type": "metrics"},
                 "metrics": metrics, "resource": res, "scope": sc,
                 "start_timestamp": iso(jvm_start if cumulative else t - interval)}
            if attrs:
                d["attributes"] = attrs
            if unit:
                d["unit"] = unit
            d["temporality"] = "cumulative" if cumulative else "delta"
            sink.add("metrics-generic.otel-default", d)
        busy = load.cpu("app", t) / 100.0
        state["eden"] = (state["eden"] + rng.uniform(1e6, 6e6)) % 70e6
        state["old"] += rng.uniform(0, 3e4)
        state["classes"] += rng.randint(0, 2)
        for daemon, st, v in ((True, "waiting", 10), (True, "runnable", 2), (True, "timed_waiting", 3), (False, "runnable", 1), (False, "waiting", 1)):
            m({"jvm.thread.count": v + rng.randint(0, 1)}, {"jvm.thread.daemon": daemon, "jvm.thread.state": st}, "{thread}")
        for pool in ("direct", "mapped", "mapped - 'non-volatile memory'"):
            used = 0 if pool != "direct" else 81920
            m({"jvm.buffer.count": 0 if pool != "direct" else 10}, {"jvm.buffer.pool.name": pool}, "{buffer}")
            m({"jvm.buffer.memory.limit": used, "jvm.buffer.memory.used": used}, {"jvm.buffer.pool.name": pool}, "By")
        m({"jvm.class.count": int(state["classes"])}, unit="{class}")
        m({"jvm.class.loaded": rng.randint(0, 12), "jvm.class.unloaded": 0}, unit="{class}", cumulative=False)
        m({"jvm.cpu.count": CORES}, unit="{cpu}")
        m({"jvm.cpu.context_switch": state["ctx"] * (1 + rng.gauss(0, 0.05))}, unit="Hz")
        m({"jvm.cpu.longlock": exp_hist([rng.uniform(0.2, 2.5) for _ in range(rng.randint(3, 15))], 38),
           "jvm.cpu.time": rng.uniform(0.1, 0.4) * (1 + busy)}, unit="s", cumulative=False)
        m({"jvm.cpu.recent_utilization": 0.008 + busy * 0.5 + rng.uniform(0, 0.006),
           "jvm.system.cpu.utilization": 0.03 + busy + rng.uniform(0, 0.03)}, unit="1", cumulative=False)
        m({"jvm.system.cpu.load_1m": 0.01 + busy * CORES}, unit="{run_queue_item}", cumulative=False)
        m({"jvm.file_descriptor.count": 22 + rng.randint(0, 3), "jvm.file_descriptor.limit": 524288}, unit="{file_descriptor}")
        heap = (("G1 Eden Space", state["eden"], 0, 71e6, None), ("G1 Survivor Space", 8.1e6, 0, 8.4e6, None),
                ("G1 Old Gen", state["old"], 126877696, 71.3e6, 134217728))
        for pool, used, init, comm, lim in heap:
            mm = {"jvm.memory.committed": int(comm), "jvm.memory.init": init, "jvm.memory.used": int(used),
                  "jvm.memory.used_after_last_gc": int(used if pool != "G1 Eden Space" else 0)}
            if lim:
                mm["jvm.memory.limit"] = lim
            m(mm, {"jvm.memory.pool.name": pool, "jvm.memory.type": "heap"}, "By")
        for pool, used, init, lim in (("Metaspace", 81.9e6, 0, None), ("CodeHeap 'non-profiled nmethods'", 9.2e6, 2555904, 122916864),
                                      ("CodeHeap 'profiled nmethods'", 17.0e6, 2555904, 122912768), ("CodeHeap 'non-nmethods'", 1.3e6, 2555904, 5840896),
                                      ("Compressed Class Space", 10.4e6, 0, 1073741824)):
            mm = {"jvm.memory.committed": int(used * 1.01), "jvm.memory.init": init, "jvm.memory.used": int(used + rng.uniform(0, 2e4))}
            if lim:
                mm["jvm.memory.limit"] = lim
            m(mm, {"jvm.memory.pool.name": pool, "jvm.memory.type": "non_heap"}, "By")
        m({"jvm.memory.allocation": exp_hist([rng.uniform(3e3, 2e5) for _ in range(rng.randint(3, 8))], 38)}, {"arena": "TLAB"}, "By", cumulative=False)
        for mode in ("read", "write"):
            m({"jvm.network.io": exp_hist([rng.uniform(100, 8000) for _ in range(rng.randint(5, 20))], 38)}, {"mode": mode}, "By", cumulative=False)
            m({"jvm.network.time": exp_hist([rng.uniform(1e-6, 2e-4) for _ in range(rng.randint(5, 20))], 38)}, {"mode": mode}, "s", cumulative=False)
        if rng.random() < 0.15:
            d = rng.uniform(0.008, 0.035)
            m({"jvm.gc.duration": exp_hist([d], 38)}, {"jvm.gc.action": "end of minor GC", "jvm.gc.name": "G1 Young Generation"}, "s", cumulative=False)
        # exporter / sdk
        ob = {"processorType": "BatchSpanProcessor"}
        n_http = len(http_b.get(ib, []))
        for typ, v in (("span", 3 * n_http + 4), ("log", 11 * n_http + 2), ("metric", 40)):
            m({"otlp.exporter.seen": v}, {"type": typ}, cumulative=False, sc={"name": "io.opentelemetry.exporters.otlp-http"})
            m({"otlp.exporter.exported": v}, {"success": True, "type": typ}, cumulative=False, sc={"name": "io.opentelemetry.exporters.otlp-http"})
        m({"processedSpans": 3 * n_http + 4}, {"dropped": False, **ob}, "1", cumulative=False, sc={"name": "io.opentelemetry.sdk.trace"})
        m({"queueSize": 0}, ob, "1", cumulative=False, sc={"name": "io.opentelemetry.sdk.trace"})
        m({"queueSize": 0}, {"processorType": "BatchLogRecordProcessor"}, "1", cumulative=False, sc={"name": "io.opentelemetry.sdk.logs"})
        m({"otel.sdk.metric_reader.collection.duration": exp_hist([rng.uniform(0.002, 0.006)], 38)},
          {"otel.component.name": "periodic_metric_reader/0", "otel.component.type": "periodic_metric_reader"}, "s",
          cumulative=False, sc={"name": "io.opentelemetry.sdk.metrics"})
        # request / redis client durations observed in the previous interval
        if http_b.get(ib):
            m({"http.server.request.duration": exp_hist(http_b[ib], 38)},
              {"http.request.method": "GET", "http.response.status_code": 200, "http.route": "/api/status",
               "network.protocol.version": "1.1", "url.scheme": "http"}, "s", cumulative=False,
              sc={"name": "io.opentelemetry.tomcat-10.0", "schema_url": "https://opentelemetry.io/schemas/1.37.0", "version": "2.26.1-alpha"})
        for op, vals in redis_b.get(ib, {}).items():
            m({"db.client.operation.duration": exp_hist(vals, 38)},
              {"db.operation.name": op, "db.system.name": "redis", "network.peer.address": HOSTS["redis"]["ip"], "network.peer.port": 6379},
              "s", cumulative=False, sc={"name": "io.opentelemetry.jedis-4.0", "version": "2.26.1-alpha"})
        t += interval
    cur["t"] = t


# -------------------------------------------------- APM aggregated metrics
def gen_aggregates(sink, tr, start, upto, done):
    """Emit buckets that are complete (end <= upto) and not emitted yet; `done` remembers progress per interval."""
    sc = {"name": "github.com/open-telemetry/opentelemetry-collector-contrib/connector/signaltometricsconnector"}
    for ivn, iv in APM_INTERVALS:
        lo = done.get(ivn, -math.inf)

        def ready(b):
            return lo <= b and b + iv <= upto

        def doc(stream_kind, bucket, attrs, metrics, res, h, unit=None, extra=None):
            d = {"@timestamp": iso(max(bucket + 1.958, start + 1.0)), "_metric_names_hash": h,
                 "attributes": dict({"metricset.interval": ivn, "metricset.name": stream_kind, "processor.event": "metric"}, **attrs),
                 "data_stream": {"dataset": "%s.%s.otel" % (stream_kind, ivn), "namespace": "default", "type": "metrics"},
                 "metrics": metrics, "resource": {"attributes": res}, "scope": sc, "temporality": "delta"}
            if unit:
                d["unit"] = unit
            if extra:
                d.update(extra)
            sink.add("metrics-%s.%s.otel-default" % (stream_kind, ivn), d)

        for (svc, tname, tres, ttype, root), evs in tr.tx.items():
            buckets = defaultdict(list)
            for t, us in evs:
                buckets[int(t // iv) * iv].append(us)
            scale = 9 if svc == FE_SVC else 8
            for b, vals in buckets.items():
                if not ready(b):
                    continue
                n = len(vals)
                common = {"metrics": {"event.success_count": {"sum": float(n), "value_count": n},
                                      "transaction.duration.histogram": exp_hist(vals, scale),
                                      "transaction.duration.summary": {"sum": float(sum(vals)), "value_count": n}}}
                res_tx = APP_TX_RES if svc == SVC else FE_AGG_RES
                res_st = APP_AGG_RES if svc == SVC else FE_AGG_RES
                doc("transaction", b, {"event.outcome": "success", "transaction.name": tname, "transaction.result": tres,
                                       "transaction.root": root, "transaction.type": ttype},
                    common["metrics"], res_tx, HASH_TX, "us", {"_doc_count": n})
                doc("service_transaction", b, {"transaction.root": root, "transaction.type": ttype},
                    common["metrics"], res_st, HASH_TX, "us", {"_doc_count": n})
        for svc, ts in tr.summary.items():
            buckets = defaultdict(int)
            for t in ts:
                buckets[int(t // iv) * iv] += 1
            for b, n in buckets.items():
                if not ready(b):
                    continue
                doc("service_summary", b, {}, {"service_summary": n}, APP_AGG_RES if svc == SVC else FE_AGG_RES, HASH_SUM)
        for (svc, name, ttype, tname, resource, who), evs in tr.dest.items():
            buckets = defaultdict(list)
            for t, us in evs:
                buckets[int(t // iv) * iv].append(us)
            res = APP_AGG_RES if who == "app" else FE_AGG_RES
            a = {"event.outcome": "success", "service.target.name": tname, "service.target.type": ttype,
                 "span.destination.service.resource": resource, "span.name": name}
            for b, vals in buckets.items():
                if not ready(b):
                    continue
                doc("service_destination", b, a, {"span.destination.service.response_time.count": len(vals)}, res, HASH_DEST_COUNT)
                doc("service_destination", b, a, {"span.destination.service.response_time.sum.us": float(sum(vals))}, res, HASH_DEST_SUM, "us")
        if upto != math.inf:
            done[ivn] = int(upto // iv) * iv


# ----------------------------------------------------- manual CPU load control
CONTROL_FILE = os.path.join(HERE, "control.json")  # written by cpu_load.py, polled by the live loop


def read_control(prev=None):
    try:
        with open(CONTROL_FILE) as f:
            return [p for p in json.load(f).get("phases", []) if p.get("host") in HOSTS and "start_ts" in p]
    except (OSError, ValueError, AttributeError):
        return prev if prev is not None else []


def write_control(phases):
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"phases": phases}, f, indent=2)
    os.replace(tmp, CONTROL_FILE)


# ---------------------------------------------------------------- generator
def scrape_until(sims, rng, t, until):
    while t < until:
        for sim in sims.values():
            sim.scrape(t + rng.uniform(0, 0.4))
        t += SCRAPE
    return t


class Generator:
    """Holds every cursor so data can be produced in consecutive windows (backfill, then live)."""

    def __init__(self, sc, start, end, rng, jvm_interval):
        self.start, self.rng, self.jvm_interval = start, rng, jvm_interval
        self.sink = Sink()
        self.load = Load(sc, start, end, rng)
        self.traffic = Traffic(self.sink, self.load, rng, start, end, sc)
        self.sims = {k: HostSim(k, self.load, rng, start, self.sink) for k in HOSTS}
        self.syslogs = SystemLogs(self.sink, self.load, rng, start)
        self.scrape_t, self.jvm, self.done = start + 5, {}, {}
        gen_app_startup_logs(self.sink, start)

    def advance(self, until, hold_open=True):
        """hold_open: leave APM buckets that are still filling for a later call."""
        self.traffic.advance(until)
        self.scrape_t = scrape_until(self.sims, self.rng, self.scrape_t, until)
        gen_jvm(self.sink, self.traffic, self.load, self.rng, self.start, until, self.jvm_interval, self.jvm)
        self.syslogs.advance(until)
        gen_aggregates(self.sink, self.traffic, self.start, until - 2 if hold_open else math.inf, self.done)

    def prune(self, until):
        self.traffic.prune(until - 4 * self.jvm_interval - 5, min(self.done.values(), default=-math.inf))


# ------------------------------------------------------------------- client
class ES:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def req(self, method, path, body=None, raw=None, gz=False, retries=4):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Authorization": "ApiKey " + self.key, "Content-Type": "application/x-ndjson" if raw is not None else "application/json"}
        if gz and data:
            data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
        for a in range(retries):
            try:
                r = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
                with urllib.request.urlopen(r, timeout=120) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 502, 503, 504) and a < retries - 1:
                    time.sleep(2 ** a)
                    continue
                raise RuntimeError("%s %s -> %s %s" % (method, path, e.code, e.read().decode()[:300]))
            except (urllib.error.URLError, TimeoutError):
                if a == retries - 1:
                    raise
                time.sleep(2 ** a)

    def bulk(self, stream, lines):
        body = ("".join(l + "\n" for l in lines)).encode()
        r = self.req("POST", "/%s/_bulk" % stream, raw=body, gz=True)
        errs = defaultdict(int)
        sample = {}
        if r.get("errors"):
            for it in r["items"]:
                e = it.get("create", {}).get("error")
                if e:
                    k = e.get("type", "?")
                    errs[k] += 1
                    sample.setdefault(k, e.get("reason", "")[:200])
        return len(lines), errs, sample


def index_all(es, sink, pool, size=3000):
    jobs = []
    for s, lines in sink.lines.items():
        for i in range(0, len(lines), size):
            jobs.append((s, lines[i:i + size]))
    ok, errs, sample = 0, defaultdict(int), {}
    for n, e, smp in pool.map(lambda j: es.bulk(*j), jobs):
        ok += n - sum(e.values())
        for k, v in e.items():
            errs[k] += v
        sample.update(smp)
    return len(jobs), ok, errs, sample


def load_env():
    env = {}
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("ELASTIC_") or k.startswith("KIBANA_")})
    return env


def parse_load(s):
    p = s.split(":")  # host:start_min:end_min:cpu[:ramp]
    if len(p) < 4 or p[0] not in HOSTS:
        raise SystemExit("--load format: host(app|redis):start_minute:end_minute:cpu_percent[:ramp_seconds]  got %r" % s)
    d = {"host": p[0], "start_minute": float(p[1]), "end_minute": float(p[2]), "cpu_percent": float(p[3])}
    if len(p) > 4:
        d["ramp_seconds"] = float(p[4])
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default=os.path.join(HERE, "scenario.json"))
    ap.add_argument("--duration", type=float, default=115,
                    help="window length in minutes (default 115; TSDB streams reject data older than ~2h, so keep it <= 115)")
    ap.add_argument("--end", default="now", help="window end: 'now' or ISO8601 (default now)")
    ap.add_argument("--load", action="append", help="override cpu_load phases; repeatable: host:start_min:end_min:cpu[:ramp_s]")
    ap.add_argument("--rpm", type=float, help="override requests per minute")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jvm-interval", type=int, default=30)
    ap.add_argument("--no-reset", action="store_true", help="do not delete existing data first")
    ap.add_argument("--no-live", action="store_true", help="index the backfill only, then exit (default: keep streaming real-time data)")
    ap.add_argument("--interval", type=float, default=10, help="live mode: seconds of data generated/indexed per batch (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="generate in memory, print summary, do not touch the cluster")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if a.duration > 118:
        print("WARNING: duration > 118 min: the oldest metrics will be rejected by the time_series look-back (2h)", file=sys.stderr)
    sc = json.load(open(a.scenario))
    if a.load:
        sc["cpu_load"] = [parse_load(x) for x in a.load]
    if a.rpm:
        sc["traffic"]["requests_per_minute"] = a.rpm
    end = time.time() if a.end == "now" else datetime.fromisoformat(a.end.replace("Z", "+00:00")).timestamp()
    start = end - a.duration * 60
    rng = random.Random(a.seed)

    live = a.end == "now" and not a.no_live and not a.dry_run
    gen = Generator(sc, start, end, rng, a.jvm_interval)
    load, sink, traffic = gen.load, gen.sink, gen.traffic
    t0 = time.time()
    gen.advance(end, hold_open=live)

    jst = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%H:%M")
    print("window: %s -> %s (JST %s -> %s)  requests=%d  generated in %.1fs" % (iso(start), iso(end), jst(start), jst(end), traffic.n_requests, time.time() - t0))
    print("cpu_load phases:", ", ".join("%s %g-%g min @%g%%" % (p["host"], p["start_minute"], p["end_minute"], p["cpu_percent"]) for p in sc["cpu_load"]) or "(none)")
    print("\n  JST    redis CPU%  app CPU%  SETEX avg ms   (10-min bins)")
    bins = defaultdict(list)
    for t, s, op in traffic.redis:
        if op == "SETEX":
            bins[int((t - start) // 600)].append(s * 1000)
    for b in sorted(bins):
        mid = start + b * 600 + 300
        print("  %s   %6.1f     %6.1f    %8.1f" % (jst(start + b * 600), load.cpu("redis", mid), load.cpu("app", mid), sum(bins[b]) / len(bins[b])))
    total = sum(len(v) for v in sink.lines.values())
    print("\ndocs per data stream (total %d):" % total)
    for s in sorted(sink.lines):
        print("  %-52s %8d" % (s, len(sink.lines[s])))
    if a.dry_run:
        return

    env = load_env()
    for k in ("ELASTIC_URL", "ELASTIC_API_KEY"):
        if not env.get(k):
            raise SystemExit("%s is not set (.env or environment)" % k)
    es = ES(env["ELASTIC_URL"], env["ELASTIC_API_KEY"])
    print("\ntarget:", env["ELASTIC_URL"])
    if not a.no_reset:
        for s in sorted(sink.lines):
            if es.req("GET", "/_data_stream/" + s) is not None:
                cnt = (es.req("GET", "/%s/_count" % s) or {}).get("count", "?")
                es.req("DELETE", "/_data_stream/" + s)
                print("  deleted existing data stream %s (%s docs)" % (s, cnt))
    streams = sorted(sink.lines)
    expected = {s: len(sink.lines[s]) for s in streams}
    with ThreadPoolExecutor(4) as pool:
        print("indexing backfill ...")
        n_jobs, ok, errs, sample = index_all(es, sink, pool)
        print("indexed %d docs in %d bulk requests, %d errors" % (ok, n_jobs, sum(errs.values())))
        for k, v in errs.items():
            print("  ERROR %s x%d: %s" % (k, v, sample.get(k)))
        time.sleep(8)
        print("\ncounts in cluster:")
        for s in streams:
            c = (es.req("GET", "/%s/_count" % s) or {}).get("count", "?")
            print("  %-52s %8s / %d" % (s, c, expected[s]))
        if not live:
            if sum(errs.values()):
                sys.exit(1)
            return

        # ---- live: keep generating real-time data until Ctrl-C
        sink.lines.clear()
        write_control([])
        print("\nlive mode: indexing every %gs (Ctrl-C to stop)" % a.interval)
        print("manual CPU load:  python3 cpu_load.py start redis 80   /   python3 cpu_load.py stop   /   python3 cpu_load.py status")
        cursor, manual = end, []
        try:
            while True:
                target = cursor + a.interval
                time.sleep(max(0.0, target + 1.0 - time.time()))
                manual = read_control(manual)
                load.manual = manual
                gen.advance(target)
                n = sum(len(v) for v in sink.lines.values())
                try:
                    _, ok, errs, sample = index_all(es, sink, pool)
                except (RuntimeError, OSError) as e:
                    ok, errs, sample = 0, {"request_failed": n}, {"request_failed": str(e)[:200]}
                sink.lines.clear()
                gen.prune(target)
                cursor = target
                active = ["%s@%g%%" % (p["host"], p["cpu_percent"]) for p in manual if load._bounds(p)[0] <= target <= load._bounds(p)[1]]
                print("[%s JST] +%d docs  redis CPU %.0f%%  app CPU %.0f%%  manual load: %s" % (
                    datetime.fromtimestamp(target, timezone(timedelta(hours=9))).strftime("%H:%M:%S"), ok,
                    load.cpu("redis", target), load.cpu("app", target), ", ".join(active) or "none"))
                for k, v in errs.items():
                    print("  ERROR %s x%d: %s" % (k, v, sample.get(k)))
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
