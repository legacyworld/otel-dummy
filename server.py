from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from html import escape
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
import random
import secrets
import string
import time
import logging
import subprocess
import os

app = FastAPI(title="Mock Remediation API")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

start_time = time.time()

ENV_FILE = "/home/elastic/.k8s-vm-env"


def load_env():
    """Source ENV_FILE (same as generate_basic.py) and return ELASTICSEARCH_* / KIBANA_* vars."""
    env = {}
    if os.path.exists(ENV_FILE):
        r = subprocess.run(
            ["bash", "-c", 'set -a; . "$1"; set +a; env -0', "bash", ENV_FILE],
            capture_output=True, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"failed to source {ENV_FILE}: {r.stderr.decode().strip()}")
        for item in r.stdout.split(b"\0"):
            k, sep, v = item.decode().partition("=")
            if sep and (k.startswith("ELASTICSEARCH_") or k.startswith("KIBANA_")):
                env[k] = v
    for k in ("KIBANA_URL", "ELASTICSEARCH_AUTH_BASE64"):
        if not env.get(k):
            raise RuntimeError(f"{k} is not set ({ENV_FILE})")
    return env


def resume_workflow(execution_id: str, input_data: dict) -> dict:
    """Resume a workflow execution paused at a waitForInput step."""
    env = load_env()
    scheme = env.get("ELASTICSEARCH_AUTH_SCHEME") or "Basic"
    key = env["ELASTICSEARCH_AUTH_BASE64"].strip()
    url = f"{env['KIBANA_URL'].rstrip('/')}/api/workflows/executions/{execution_id}/resume"
    req = urllib.request.Request(
        url,
        data=json.dumps({"input": input_data}).encode(),
        method="POST",
        headers={
            "Authorization": f"{scheme} {key}",
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


# Pending approval requests registered by the workflow (in-memory; lost on restart)
PENDING: dict[str, dict] = {}


class ApprovalRequest(BaseModel):
    execution_id: str
    service_name: str = "unknown"
    request_id: str = ""
    description: str = "Remediate high CPU utilization"
    action: str = "python3 cpu_load.py stop"
    rca_result: str = ""  # investigate_root_cause step output
    infra_check: str = ""  # check_infra step output


class ApprovalDecision(BaseModel):
    decision: str  # "approved" | "rejected"
    service_name: str = "unknown"
    request_id: str = "unknown"
    execution_id: str  # paused workflow execution to resume
    comments: str = ""


CSS = """
*{box-sizing:border-box}body{margin:0;font-family:"Source Sans Pro",Helvetica,Arial,sans-serif;background:#f4f5f7;color:#2b2f36;font-size:14px}
header{background:#293e40;color:#fff;height:52px;display:flex;align-items:center;padding:0 20px;gap:24px}
header .brand{font-size:18px;font-weight:700;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
header .dot{width:22px;height:22px;border-radius:50%;background:#81b5a1;display:inline-block}
header nav a{color:#d7e3e0;text-decoration:none;margin-right:18px}header nav a:hover{color:#fff}
header .user{margin-left:auto;font-size:13px;color:#d7e3e0}
.layout{display:flex;min-height:calc(100vh - 52px)}
aside{width:210px;background:#fff;border-right:1px solid #dde1e6;padding:14px 0}
aside h4{margin:12px 18px 6px;font-size:11px;text-transform:uppercase;color:#7b8794}
aside a{display:block;padding:7px 18px;color:#2b2f36;text-decoration:none}aside a:hover,aside a.on{background:#e8f1ee;color:#1d5d4b}
main{flex:1;padding:22px 28px;max-width:1100px}
h1{font-size:22px;margin:0 0 16px;font-weight:600}
.cards{display:flex;gap:14px;margin-bottom:20px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #dde1e6;border-radius:4px;padding:14px 18px;min-width:160px}
.card .n{font-size:28px;font-weight:600}.card .l{color:#7b8794;font-size:12px;text-transform:uppercase}
.panel{background:#fff;border:1px solid #dde1e6;border-radius:4px;margin-bottom:20px}
.panel h3{margin:0;padding:12px 16px;font-size:14px;border-bottom:1px solid #dde1e6;background:#fafbfc}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 16px;border-bottom:1px solid #eceff2}
th{font-size:12px;color:#7b8794;text-transform:uppercase;font-weight:600}
.pill{padding:2px 9px;border-radius:10px;font-size:12px;color:#fff}
.p1{background:#c0392b}.p2{background:#e67e22}.p3{background:#d4ac0d}.ok{background:#2e8b57}
.rca{white-space:pre-wrap;word-break:break-word;margin:0;padding:14px 16px;font:13px/1.6 inherit;max-height:420px;overflow:auto}
.form{padding:16px}.form dl{display:grid;grid-template-columns:170px 1fr;gap:10px 14px;margin:0 0 18px}
.form dt{color:#7b8794}.form dd{margin:0}
.btn{border:1px solid #1d5d4b;background:#fff;color:#1d5d4b;padding:8px 22px;border-radius:3px;font-size:14px;cursor:pointer;margin-right:10px}
.btn.primary{background:#1d5d4b;color:#fff}.btn.danger{border-color:#b03a2e;color:#b03a2e}
.btn.danger:hover{background:#b03a2e;color:#fff}.btn:disabled{opacity:.5;cursor:not-allowed}
#result{margin-top:16px;padding:10px 14px;border-radius:3px;display:none}
#result.good{display:block;background:#e3f4ea;border:1px solid #2e8b57}
#result.bad{display:block;background:#fbe9e7;border:1px solid #c0392b}
"""


def page(title: str, body: str, active: str = "") -> str:
    def on(name):
        return ' class="on"' if name == active else ""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>{escape(title)} | IT Service Portal</title><style>{CSS}</style></head><body>
<header><div class="brand"><span class="dot"></span>IT Service Portal</div>
<nav><a href="/">Home</a><a href="/approval">Approvals</a></nav>
<div class="user">System Administrator</div></header>
<div class="layout"><aside>
<h4>Self-Service</h4><a href="/"{on("home")}>Dashboard</a>
<h4>Process</h4><a href="/approval"{on("approval")}>My Approvals</a>
<a href="#">Incidents</a><a href="#">Change Requests</a><a href="#">Problems</a>
<h4>Configuration</h4><a href="#">CMDB</a><a href="#">Service Catalog</a>
</aside><main>{body}</main></div></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    body = """<h1>Service Operations Workspace</h1>
<div class="cards">
<div class="card"><div class="n">12</div><div class="l">Open incidents</div></div>
<div class="card"><div class="n">3</div><div class="l">Pending approvals</div></div>
<div class="card"><div class="n">5</div><div class="l">Changes this week</div></div>
<div class="card"><div class="n">99.2%</div><div class="l">SLA compliance</div></div></div>
<div class="panel"><h3>Recent incidents</h3><table>
<tr><th>Number</th><th>Short description</th><th>Priority</th><th>State</th></tr>
<tr><td>INC0010231</td><td>Redis VM high CPU utilization</td><td><span class="pill p1">1 - Critical</span></td><td>In Progress</td></tr>
<tr><td>INC0010230</td><td>Application response time degraded</td><td><span class="pill p2">2 - High</span></td><td>New</td></tr>
<tr><td>INC0010227</td><td>Disk usage warning on batch node</td><td><span class="pill p3">3 - Moderate</span></td><td>On Hold</td></tr>
<tr><td>INC0010219</td><td>Certificate renewal completed</td><td><span class="pill ok">4 - Low</span></td><td>Resolved</td></tr>
</table></div>
<div class="panel"><h3>Pending approvals</h3><table>
<tr><th>Request</th><th>Description</th><th>Action</th></tr>
<tr><td>APR0001001</td><td>Remediate high CPU on redis</td><td><a href="/approval">Open</a></td></tr></table></div>"""
    return page("Home", body, "home")


@app.get("/approval", response_class=HTMLResponse)
async def approval(execution_id: str = ""):
    # Show the requested execution, or the latest pending one when opened from the "Approvals" menu
    pending = [r for r in PENDING.values() if r["state"] == "Requested"]
    item = PENDING.get(execution_id) or (pending[-1] if pending else None)
    if item is None:
        return page("Approval", "<h1>Approval Request</h1><div class=\"panel\"><h3>No pending approvals</h3>"
                    "<div class=\"form\">There are no approval requests waiting.</div></div>", "approval")
    service_name, request_id, execution_id = item["service_name"], item["request_id"], item["execution_id"]
    analysis_panels = ""
    for key, label in (("rca_result", "Root Cause Analysis (RCA)"), ("infra_check", "Infrastructure Check")):
        if item.get(key):
            analysis_panels += f'<div class="panel"><h3>{label}</h3><pre class="rca">{escape(item[key])}</pre></div>'
    body = f"""<h1>Approval Request</h1>
<div class="panel"><h3>{escape(request_id)}</h3><div class="form">
<dl><dt>Request</dt><dd>{escape(request_id)}</dd>
<dt>Short description</dt><dd>{escape(item["description"])}</dd>
<dt>Configuration item</dt><dd>{escape(service_name)}</dd>
<dt>Requested action</dt><dd>{escape(item["action"])}</dd>
<dt>State</dt><dd>{escape(item["state"])}</dd></dl>
<button class="btn primary" id="approve">Approve</button>
<button class="btn danger" id="reject">Reject</button>
<div id="result"></div></div></div>
{analysis_panels}
<script>
const payload = d => ({{decision: d, service_name: {json.dumps(service_name)}, request_id: {json.dumps(request_id)}, execution_id: {json.dumps(execution_id)}}});
async function send(d) {{
  const btns = document.querySelectorAll('.btn'); btns.forEach(b => b.disabled = true);
  const r = document.getElementById('result');
  try {{
    const res = await fetch('/approval', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload(d))}});
    const j = await res.json();
    r.className = j.success ? 'good' : 'bad';
    r.textContent = j.success ? (d === 'approved' ? 'Approved. Result sent to workflow.' : 'Rejected. Result sent to workflow.') : 'Failed to send result: ' + j.error;
    if (!j.success) btns.forEach(b => b.disabled = false);
  }} catch (e) {{ r.className = 'bad'; r.textContent = 'Error: ' + e; btns.forEach(b => b.disabled = false); }}
}}
document.getElementById('approve').onclick = () => send('approved');
document.getElementById('reject').onclick = () => send('rejected');
</script>"""
    return page("Approval", body, "approval")


@app.post("/approval_requests")
async def create_approval_request(req: ApprovalRequest):
    """Called by the first workflow step: register the execution so /approval can show it."""
    data = req.model_dump()
    data["request_id"] = data["request_id"] or f"APR{len(PENDING) + 1001:07d}"
    data["state"] = "Requested"
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    PENDING[req.execution_id] = data
    logging.info(f"[APPROVAL] registered {data}")
    return {"success": True, **data}


@app.post("/approval")
async def approval_decision(req: ApprovalDecision):
    if req.decision not in ("approved", "rejected"):
        return {"success": False, "error": "decision must be 'approved' or 'rejected'"}
    # Keys match the waitForInput schema in remediation_approval.yml
    inputs = {
        "approved": req.decision == "approved",
        "comments": req.comments or f"{req.decision} via /approval ({req.request_id}) at "
                    + datetime.now(timezone.utc).isoformat(),
    }
    logging.info(f"[APPROVAL] {inputs} -> resume execution {req.execution_id}")
    try:
        result = resume_workflow(req.execution_id, inputs)
        if req.execution_id in PENDING:
            PENDING[req.execution_id]["state"] = req.decision.capitalize()
        return {"success": True, "execution_id": req.execution_id, "decision": req.decision, "result": result}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        logging.error(f"[APPROVAL] workflow call failed: {e.code} {detail}")
        return {"success": False, "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        logging.exception("[APPROVAL] workflow call failed")
        return {"success": False, "error": str(e)}


class ServiceRequest(BaseModel):
    service_name: str = "unknown"


@app.post("/remediate_service")
async def remediate_service(req: ServiceRequest):
    service_name = req.service_name

    # Stop CPU load simulation
    command = [
        "python3",
        "cpu_load.py",
        "stop"
    ]

    logging.info(f"[REMEDIATION] Executing: {' '.join(command)}")

    try:
        log_file = open("cpu_load.log", "a")

        process = subprocess.Popen(
            command,
            cwd="/home/elastic/workshop-scripts",
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

        log_file.close()

        timestamp = datetime.now(timezone.utc).isoformat()

        return {
            "success": True,
            "status": "remediation_triggered",
            "remediation_id": f"rem-{process.pid}",
            "service": service_name,
            "action": "stop_cpu_load",
            "command": "python3 cpu_load.py stop",
            "pid": process.pid,
            "timestamp": timestamp,
            "message": f"CPU load simulation stopped for {service_name}"
        }

    except Exception as e:
        logging.exception("[REMEDIATION] Failed to execute command")
        return {
            "success": False,
            "status": "remediation_failed",
            "error": str(e)
        }

@app.post("/scale_service")
async def scale_service(req: ServiceRequest):
    service_name = req.service_name
    current_instances = random.randint(2, 4)
    new_instances = current_instances + 2
    timestamp = datetime.now(timezone.utc).isoformat()

    logging.info(
        f"[SCALE] {service_name}: "
        f"{current_instances} -> {new_instances} instances"
    )

    return {
        "success": True,
        "service": service_name,
        "action": "scale_up",
        "previous_instances": current_instances,
        "new_instances": new_instances,
        "timestamp": timestamp,
        "message": (
            f"Auto-scaling triggered: {service_name} "
            f"scaled from {current_instances} to {new_instances} instances"
        ),
        "estimated_time": "45s"
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "uptime": time.time() - start_time
    }