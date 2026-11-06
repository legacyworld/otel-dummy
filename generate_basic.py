#!/usr/bin/env python3
"""generate.py variant that authenticates with ELASTICSEARCH_URL / ELASTICSEARCH_AUTH_BASE64.

Same generator and CLI options as generate.py (--duration, --load, --dry-run, ...);
only the connection settings differ. Values are read from the environment or .env.

  ELASTICSEARCH_URL          https://<host>:443
  ELASTICSEARCH_AUTH_BASE64  base64 credential (base64 of "user:password" by default)
  ELASTICSEARCH_AUTH_SCHEME  optional, "Basic" (default) or "ApiKey" for an encoded API key
"""
import gzip
import json
import os
import time
import urllib.error
import urllib.request

import generate


def load_env():
    env = {}
    p = os.path.join(generate.HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("ELASTICSEARCH_")})
    for k in ("ELASTICSEARCH_URL", "ELASTICSEARCH_AUTH_BASE64"):
        if not env.get(k):
            raise SystemExit("%s is not set (.env or environment)" % k)
    # generate.main() reads its connection settings under these internal names;
    # ELASTIC_* variables from the user's environment are never consulted here
    env["ELASTIC_URL"] = env["ELASTICSEARCH_URL"]
    env["ELASTIC_API_KEY"] = env["ELASTICSEARCH_AUTH_BASE64"]
    return env


class ES(generate.ES):
    def req(self, method, path, body=None, raw=None, gz=False, retries=4):
        scheme = os.environ.get("ELASTICSEARCH_AUTH_SCHEME") or load_env().get("ELASTICSEARCH_AUTH_SCHEME") or "Basic"
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
