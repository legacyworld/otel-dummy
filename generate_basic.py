#!/usr/bin/env python3
"""generate.py variant that authenticates with ELASTICSEARCH_URL / ELASTICSEARCH_AUTH_BASE64.

Same generator and CLI options as generate.py (--duration, --load, --dry-run, ...);
only the connection settings differ. Values are loaded by sourcing /home/elastic/.k8s-vm-env
(set -a; . /home/elastic/.k8s-vm-env; set +a).

  ELASTICSEARCH_URL          https://<host>:443
  ELASTICSEARCH_AUTH_BASE64  base64 credential (base64 of "user:password" by default)
  ELASTICSEARCH_AUTH_SCHEME  optional, "Basic" (default) or "ApiKey" for an encoded API key
"""
import functools
import gzip
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import generate


ENV_FILE = "/home/elastic/.k8s-vm-env"

# Equivalent of:  set -a; . /home/elastic/.k8s-vm-env; set +a
_SOURCE_CMD = 'set -a; . "$1"; set +a; env -0'


@functools.lru_cache(maxsize=None)
def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        r = subprocess.run(["bash", "-c", _SOURCE_CMD, "bash", ENV_FILE],
                           capture_output=True, check=False)
        if r.returncode != 0:
            raise SystemExit("failed to source %s: %s" % (ENV_FILE, r.stderr.decode().strip()))
        for item in r.stdout.split(b"\0"):
            k, sep, v = item.decode().partition("=")
            if sep and k.startswith("ELASTICSEARCH_"):
                env[k] = v
    for k in ("ELASTICSEARCH_URL", "ELASTICSEARCH_AUTH_BASE64"):
        if not env.get(k):
            raise SystemExit("%s is not set (%s)" % (k, ENV_FILE))
    # generate.main() reads its connection settings under these internal names;
    # ELASTIC_* variables from the user's environment are never consulted here
    env["ELASTIC_URL"] = env["ELASTICSEARCH_URL"]
    env["ELASTIC_API_KEY"] = env["ELASTICSEARCH_AUTH_BASE64"]
    return env


class ES(generate.ES):
    def req(self, method, path, body=None, raw=None, gz=False, retries=4):
        scheme = load_env().get("ELASTICSEARCH_AUTH_SCHEME") or "Basic"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Authorization": "%s %s" % (scheme, self.key),
                   "Content-Type": "application/x-ndjson" if raw is not None else "application/json"}
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


generate.load_env = load_env
generate.ES = ES

if __name__ == "__main__":
    generate.main()
