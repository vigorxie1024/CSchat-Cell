#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSchat-Cell 服务端 v0.1 —— 严格实现 CONTRACT v0.1
零依赖（Python 标准库）。契约见 CONTRACT-v0.1.md。
铁律：拆解权唯一归 nucleus；Aina 只复述意图；审批超时按拒绝。
"""
import json
import os
import queue
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CELL_HOST", "127.0.0.1")
PORT = int(os.environ.get("CELL_PORT", "8000"))
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BUDGET_TOKENS = int(os.environ.get("CELL_BUDGET", "200000"))
SINGLE_APPROVAL = int(os.environ.get("CELL_APPROVAL_TOKENS", "20000"))
APPROVAL_TTL = int(os.environ.get("CELL_APPROVAL_TTL", "1800"))   # 秒；测试可调到 1
CONTRACT_V = "0.1"

# ---------------- 状态 ----------------
LOCK = threading.RLock()
SEQ = {"n": 0}
LOG = []              # 所有信封消息（用于 since_seq 续传）
IDEMP = {}            # msg_id -> 响应（24h 幂等，进程内简化为不清理）
TASKS = {}            # task_id -> {title, root_node_id, nodes:{node_id: node}}
APPROVALS = {}        # approval_id -> {status, task_id, node_id, expires_at, reason}
ENERGY = {"used_tokens": 0, "budget_tokens": BUDGET_TOKENS}
SUBS = []             # SSE 订阅队列


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())


def emit(etype, payload, frm="nucleus", to="all", trace_id=None):
    """统一出口：所有消息只能经这里产生，保证信封字段齐全。"""
    with LOCK:
        SEQ["n"] += 1
        env = {
            "v": CONTRACT_V, "seq": SEQ["n"], "msg_id": "m-" + uuid.uuid4().hex[:12],
            "ts": now_iso(), "from": frm, "to": to, "type": etype,
            "trace_id": trace_id or ("t-" + uuid.uuid4().hex[:8]), "payload": payload,
        }
        LOG.append(env)
        if len(LOG) > 5000:
            del LOG[:1000]
        for q in list(SUBS):
            try:
                q.put_nowait(env)
            except Exception:
                pass
        return env


def err(code, message, ref=None, trace_id=None):
    p = {"code": code, "message": message}
    if ref:
        p["ref"] = ref
    return emit("system.error", p, frm="system", trace_id=trace_id)


# ---------------- 中枢（nucleus）：唯一的拆解者 ----------------
def plan_task(text, trace_id):
    """Mock 拆解：真实实现应调用大模型。拆解失败必须回 E4007，不许静默。"""
    if not text or len(text.strip()) < 2:
        err("E4007", "中枢拆解失败：指令太短", trace_id=trace_id)
        return None
    task_id = "tk-" + uuid.uuid4().hex[:6]
    root = "n1"
    nodes = {
        root: {"node_id": root, "parent_id": None, "title": text.strip()[:40], "owner": "nucleus",
               "status": "running", "progress": 10, "depends_on": []},
        "n2": {"node_id": "n2", "parent_id": root, "title": "抓取与整理数据", "owner": "agent:crawler",
               "status": "done", "progress": 100, "depends_on": [], "output_ref": "out-1"},
        "n3": {"node_id": "n3", "parent_id": root, "title": "生成交付物", "owner": "agent:writer",
               "status": "pending", "progress": 0, "depends_on": ["n2"]},
    }
    TASKS[task_id] = {"title": text.strip()[:40], "root_node_id": root, "nodes": nodes, "trace_id": trace_id}
    emit("task.plan", {"task_id": task_id, "root_node_id": root,
                       "nodes": [nodes[k] for k in ("n1", "n2", "n3")]}, trace_id=trace_id)
    emit("energy.update", energy_payload(), frm="system", trace_id=trace_id)
    # 契约 §6：预估消耗超阈值 → 挂起为审批（节点转 blocked，下游不得启动）
    cost = 30000 if len(text) > 8 else 1000
    if cost > SINGLE_APPROVAL:
        request_approval(task_id, "n3", "预计消耗 %d tokens，超过单节点阈值 %d" % (cost, SINGLE_APPROVAL),
                         cost, trace_id=trace_id)
    return task_id


def energy_payload():
    pct = round(ENERGY["used_tokens"] / max(1, ENERGY["budget_tokens"]) * 100, 1)
    level = "exhausted" if pct >= 90 else ("low" if pct >= 70 else "ok")
    return {"used_tokens": ENERGY["used_tokens"], "budget_tokens": ENERGY["budget_tokens"],
            "pct": pct, "level": level}


def node_update(task_id, node_id, status=None, progress=None, note=None, trace_id=None):
    task = TASKS.get(task_id)
    if not task:
        return err("E4002", "任务不存在", trace_id=trace_id), 404
    node = task["nodes"].get(node_id)
    if not node:
        return err("E4002", "节点不存在", trace_id=trace_id), 404
    if progress is not None:
        if progress < node.get("progress", 0):
            # 契约 §5：progress 只许单调不减
            return err("E4003", "progress 不允许回退（%s → %s）" % (node.get("progress", 0), progress), trace_id=trace_id), 409
        node["progress"] = progress
    if status:
        if status not in ("pending", "running", "done", "failed", "blocked", "skipped"):
            return err("E4003", "非法状态：" + status, trace_id=trace_id), 409
        node["status"] = status
    if note:
        node["note"] = note
    emit("task.node.update", {"task_id": task_id, "node_id": node_id,
                              "status": node["status"], "progress": node.get("progress", 0)},
         frm="nucleus", trace_id=trace_id)
    return {"ok": True, "node": node}, 200


# ---------------- 审批（超时按拒绝，fail-closed）----------------
def request_approval(task_id, node_id, reason, cost, trace_id=None):
    aid = "ap-" + uuid.uuid4().hex[:6]
    APPROVALS[aid] = {"status": "pending", "task_id": task_id, "node_id": node_id,
                      "reason": reason, "cost_estimate": cost,
                      "expires_at": time.time() + APPROVAL_TTL}
    task = TASKS.get(task_id)
    if task and node_id in task["nodes"]:
        task["nodes"][node_id]["status"] = "blocked"     # 契约 §6：挂起为 blocked，不是 running
    emit("approval.request", {"approval_id": aid, "task_id": task_id, "node_id": node_id,
                              "reason": reason, "cost_estimate": cost,
                              "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                                          time.localtime(time.time() + APPROVAL_TTL))},
         frm="nucleus", to="vx", trace_id=trace_id)
    return aid


def decide_approval(aid, approved, note=None):
    ap = APPROVALS.get(aid)
    if not ap:
        return {"ok": False, "code": "E4002", "error": "审批不存在"}, 404
    if ap["status"] != "pending":
        return {"ok": True, "already": True, "status": ap["status"]}, 200   # 幂等
    if time.time() > ap["expires_at"]:
        ap["status"] = "expired"          # 过期 = 拒绝
        mark_failed(ap, "E4004", "审批超时，按拒绝处理")
        return {"ok": True, "status": "expired"}, 200
    ap["status"] = "approved" if approved else "denied"
    if approved:
        TASKS[ap["task_id"]]["nodes"][ap["node_id"]]["status"] = "running"
    else:
        mark_failed(ap, "E4004", "碳基主理人拒绝")
    emit("approval.decision", {"approval_id": aid, "approved": bool(approved), "by": "vx", "at": now_iso()},
         frm="vx", to="nucleus")
    return {"ok": True, "status": ap["status"]}, 200


def mark_failed(ap, code, msg):
    task = TASKS.get(ap["task_id"])
    if task and ap["node_id"] in task["nodes"]:
        task["nodes"][ap["node_id"]]["status"] = "failed"
        task["nodes"][ap["node_id"]]["error"] = code
    err(code, msg)


def sweep_approvals():
    """后台扫过期审批：到期未决 → 拒绝（fail-closed），并让节点失败。"""
    while True:
        time.sleep(1)
        with LOCK:
            for aid, ap in list(APPROVALS.items()):
                if ap["status"] == "pending" and time.time() > ap["expires_at"]:
                    ap["status"] = "expired"
                    mark_failed(ap, "E4004", "审批超时，按拒绝处理")


# ---------------- 工具执行（无 auth 直接 403）----------------
def tool_call(name, args, token):
    if not token:
        return err("E4001", "工具调用缺少 auth_token", ref="tool:" + name), 403
    if name == "weather":
        return {"ok": True, "tool": "weather", "city": args.get("city", "上海"), "result": "小雨", "mock": True}, 200
    if name == "notify":
        emit("chat.agent", {"text": "[通知] " + str(args.get("text", ""))[:60]}, frm="tool:notify")
        return {"ok": True, "tool": "notify", "sent": True, "mock": True}, 200
    return err("E4002", "未知工具：" + name), 404


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CSchat-Cell</title>
<style>
:root{--bg:#14161a;--pn:#1c1f24;--ln:#2a2f36;--tx:#e6e8eb;--dim:#8b949e;--ok:#7fd1ae}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.65 -apple-system,'Segoe UI',system-ui,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:10px 14px;background:var(--pn);border-bottom:1px solid var(--ln);flex-wrap:wrap}
header b{color:var(--ok)}#bar{flex:1;min-width:120px;height:8px;background:#0f1114;border-radius:6px;overflow:hidden}
#bar i{display:block;height:100%;width:0;background:var(--ok);transition:.3s}
#pause{padding:6px 12px;background:#5a2d2d;color:#ffd7d7;border:0;border-radius:6px;cursor:pointer}
main{display:grid;grid-template-columns:1fr 300px;gap:0;height:calc(100vh - 58px)}
#log{overflow-y:auto;padding:12px 14px}
.m{padding:7px 0;border-bottom:1px solid #1e2228}
.who{color:var(--ok);font-weight:600}.t{color:var(--dim);font-size:11px;margin-left:8px}
.txt{white-space:pre-wrap;margin-top:2px}
.card{border:1px solid var(--ln);border-radius:10px;margin:8px 0;overflow:hidden}
.card h4{margin:0;padding:9px 12px;background:#171a1f;font-size:13.5px;cursor:pointer}
.card .nodes{padding:8px 12px;display:none}
.card.open .nodes{display:block}
.node{display:flex;gap:8px;align-items:center;padding:5px 0;font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.s-running{background:#e3b341}.s-done{background:var(--ok)}.s-failed{background:#e5534b}.s-pending{background:var(--dim)}.s-blocked{background:#a371f7}
aside{border-left:1px solid var(--ln);padding:12px;overflow-y:auto;background:#171a1f}
aside h3{font-size:13px;color:var(--dim);margin:0 0 8px}
.ag{display:flex;gap:8px;align-items:center;padding:6px 0;font-size:13px}
#empty{padding:26px 18px;text-align:center;color:var(--dim)}
#empty h2{font-size:17px;color:var(--tx);margin:0 0 6px;font-weight:600}
#empty p{margin:0 0 18px;font-size:13px;line-height:1.9}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.chip{padding:9px 14px;background:#171a1f;border:1px solid var(--ln);border-radius:20px;font-size:13px;color:var(--tx);cursor:pointer}
.chip:hover{border-color:var(--ok);color:var(--ok)}
.legend{font-size:11.5px;color:#5c6670;padding:10px 0 0;line-height:1.9;border-top:1px solid var(--ln);margin-top:10px}
footer{padding:10px 14px;background:var(--pn);border-top:1px solid var(--ln);display:flex;gap:8px}
#say{flex:1;padding:11px;background:#0f1114;border:1px solid var(--ln);color:var(--tx);border-radius:8px;font-size:14px}
#go{padding:11px 18px;background:#2d6a4f;color:#fff;border:0;border-radius:8px;font-weight:600;cursor:pointer}
@media(max-width:820px){main{grid-template-columns:1fr}aside{display:none}}
</style>
<header><b>CSchat-Cell</b><span id="st" style="color:var(--dim);font-size:12px">连接中…</span>
<div id="bar"><i></i></div><button id="pause">全局暂停</button></header>
<main>
<div id="logwrap" style="overflow-y:auto"><div id="empty">
  <h2>一个会自己干活的细胞</h2>
  <p>说一句话，中枢把它拆成任务树，硅基劳动力分头去做。<br>碳基人只管两件事：<b>下指令</b>、<b>拍板</b>。</p>
  <div class="chips">
    <div class="chip" onclick="fire('做一份新消费品牌竞品分析，明天下午三点前要')">做份竞品分析</div>
    <div class="chip" onclick="fire('查一下上海天气，如果下雨就发通知给我')">查天气并通知</div>
    <div class="chip" onclick="fire('整理一份明年放假安排表并发到群里')">整理放假安排</div>
  </div>
</div><div id="log"></div></div>
<aside><h3>硅基劳动力（细胞器）</h3><div id="agents"></div><div class="legend">● 绿=待命　● 黄=运行中　● 紫=等你拍板　● 红=报错<br>细胞核：中枢（唯一拆解者）<br>伴生：Aina（只复述意图，不拆解）</div></aside></main>
<footer><input id="say" placeholder="说一句，回车下发（中枢负责拆解）" autofocus><button id="go">下发</button></footer>
<script>
let last=0;const seen=new Set();
const AGENTS=[{n:'中枢 nucleus',s:'idle'},{n:'Aina 伴生',s:'idle'},{n:'crawler 抓取',s:'idle'},{n:'writer 交付',s:'idle'},{n:'tool:weather',s:'idle'}];
function paintAgents(override){
  const box=document.getElementById('agents');box.innerHTML='';
  AGENTS.forEach(a=>{
    const st=(override&&override[a.n])||a.s;
    const d=document.createElement('div');d.className='ag';
    d.innerHTML='<span class="dot s-'+(st==='idle'?'pending':st)+'"></span><span>'+a.n+'</span>';
    box.appendChild(d);
  });
}
paintAgents();
function fire(t){document.getElementById('say').value=t;say();}
function add(m){
  if(seen.has(m.msg_id))return;seen.add(m.msg_id);
  const e=document.getElementById('empty');if(e)e.style.display='none';
  const log=document.getElementById('log');
  if(m.type==='agent.status'&&m.payload&&m.payload.agent_id){AGENTS.forEach(a=>{if(a.n===m.payload.agent_id)a.s=m.payload.state;});paintAgents();}
  if(m.type==='task.plan'){
    const t=document.createElement('div');t.className='card';
    const id='card-'+m.payload.task_id;
    t.innerHTML='<h4>📋 任务树 · '+esc(m.payload.nodes[0]?m.payload.nodes[0].title:'任务')+'　（点击展开）</h4><div class="nodes" id="'+id+'"></div>';
    t.querySelector('h4').onclick=()=>t.classList.toggle('open');
    log.appendChild(t);
    m.payload.nodes.forEach(n=>renderNode(id,n));
  }else if(m.type==='task.node.update'){updateNode(m.payload);}
  else if(m.type==='system.error'){line('⚠️',m.payload.code+' '+m.payload.message,m.ts);}
  else if(m.type==='approval.request'){line('🔐','需要你拍板：'+m.payload.reason,m.ts);}
  else if(m.type==='energy.update'){setEnergy(m.payload);}
  else if(m.type==='chat.agent'){line('🤖',m.payload.text,m.ts);}
  else if(m.type==='chat.user'){line('👤',m.payload.text,m.ts);}
  log.scrollTop=log.scrollHeight;
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function line(who,text,ts){
  const d=document.createElement('div');d.className='m';
  d.innerHTML='<span class="who">'+who+'</span><span class="t">'+(ts||'').slice(11,19)+'</span><div class="txt">'+esc(text)+'</div>';
  document.getElementById('log').appendChild(d);
}
function renderNode(cardId,n){
  const box=document.getElementById(cardId);if(!box)return;
  let el=document.getElementById('node-'+n.node_id);
  if(!el){el=document.createElement('div');el.className='node';el.id='node-'+n.node_id;box.appendChild(el);}
  el.innerHTML='<span class="dot s-'+n.status+'"></span><span>'+esc(n.title)+'</span><span style="color:var(--dim);font-size:12px">'+n.status+' · '+n.progress+'%</span>';
}
function updateNode(p){renderNode('card-'+p.task_id,{node_id:p.node_id,title:'',status:p.status,progress:p.progress});}
function setEnergy(e){const i=document.querySelector('#bar i');i.style.width=e.pct+'%';i.style.background=e.level==='ok'?'var(--ok)':(e.level==='low'?'#e3b341':'#e5534b');}
const es=new EventSource('/events?since_seq=0');
es.onopen=()=>document.getElementById('st').textContent='实时在线';
es.onerror=()=>document.getElementById('st').textContent='重连中…';
es.onmessage=e=>{try{const m=JSON.parse(e.data);last=Math.max(last,m.seq);add(m);}catch(_){}};
async function say(){
  const el=document.getElementById('say');const v=el.value.trim();if(!v)return;el.value='';
  await fetch('/say',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:v})});
}
document.getElementById('go').onclick=say;
document.getElementById('say').addEventListener('keydown',e=>{if(e.key==='Enter')say();});
document.getElementById('pause').onclick=async()=>{await fetch('/control/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'VX 手动暂停'})});};
fetch('/state').then(r=>r.json()).then(j=>{if(j.energy)setEnergy(j.energy);});
</script></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return None

    # ---------- GET ----------
    def do_GET(self):
        u = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(self.path)
        path = u.path
        qs = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(u.query)

        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/state":
            with LOCK:
                return self._json(200, {"energy": energy_payload(), "seq": SEQ["n"],
                                        "tasks": {k: {"title": v["title"],
                                                      "nodes": list(v["nodes"].values())} for k, v in TASKS.items()},
                                        "approvals": APPROVALS})
        if path == "/events":
            since = int((qs.get("since_seq") or ["0"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue()
            try:
                with LOCK:
                    backlog = [m for m in LOG if m["seq"] > since]
                for m in backlog:               # 契约 §9：按 seq 升序补齐
                    self.wfile.write(("data: " + json.dumps(m, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                with LOCK:
                    SUBS.append(q)
                while True:
                    try:
                        m = q.get(timeout=20)
                        self.wfile.write(("data: " + json.dumps(m, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ka\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with LOCK:
                    if q in SUBS:
                        SUBS.remove(q)
            return
        if path == "/snapshot":                 # 时光机：只读
            # 注意：snapshot 的截止参数是 upto_seq，不是 since_seq（后者是续传用的起点）
            tid = (qs.get("trace_id") or [""])[0]
            upto = int((qs.get("upto_seq") or ["999999999"])[0])
            with LOCK:
                msgs = [m for m in LOG if m["seq"] <= upto and (not tid or m["trace_id"] == tid)]
            return self._json(200, {"readonly": True, "messages": msgs})
        self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if body is None:
            return self._json(400, {"error": "bad json"})

        # 上行通用信封入口（契约 §3/§9）
        if path == "/msg":
            v = body.get("v")
            if v != CONTRACT_V:
                err("E4006", "契约版本不符：期望 %s，收到 %s" % (CONTRACT_V, v))
                return self._json(400, {"ok": False, "code": "E4006"})
            mid = body.get("msg_id")
            if mid and mid in IDEMP:                     # 幂等
                return self._json(200, IDEMP[mid])
            t = body.get("type")
            if t == "chat.user":
                text = str((body.get("payload") or {}).get("text") or "")
                tid = body.get("trace_id")
                emit("chat.user", {"text": text}, frm="vx", trace_id=tid)
                # Aina 意图预审（只复述，不拆解）
                emit("task.proposed", {"precheck_id": "pc-" + uuid.uuid4().hex[:6],
                                       "restatement": "你是要我：" + text[:60], "intent": text[:60]},
                     frm="aina", to="vx", trace_id=tid)
                plan_task(text, tid)
                resp = {"ok": True}
            elif t == "task.node.update":
                p = body.get("payload") or {}
                out, code = node_update(p.get("task_id"), p.get("node_id"),
                                        p.get("status"), p.get("progress"), p.get("note"), body.get("trace_id"))
                resp = out if code == 200 else {"ok": False, "code": out.get("payload", {}).get("code")}
            elif t == "approval.decision":
                p = body.get("payload") or {}
                out, code = decide_approval(p.get("approval_id"), bool(p.get("approved")), p.get("note"))
                resp = out
            elif t == "tool.call":
                p = body.get("payload") or {}
                out, code = tool_call(p.get("name"), p.get("args") or {}, p.get("auth_token"))
                if code != 200:
                    return self._json(code, out if isinstance(out, dict) else {"ok": False})
                resp = out
            else:
                # 未知类型：接受并忽略（契约 §3 向前兼容），不崩
                resp = {"ok": True, "ignored": t}
            if mid:
                IDEMP[mid] = resp
            return self._json(200, resp)

        if path == "/say":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"ok": False, "error": "empty"})
            emit("chat.user", {"text": text}, frm="vx")
            emit("task.proposed", {"precheck_id": "pc-" + uuid.uuid4().hex[:6],
                                   "restatement": "你是要我：" + text[:60], "intent": text[:60]},
                 frm="aina", to="vx")
            plan_task(text, None)
            return self._json(200, {"ok": True})

        if path == "/approval":
            out, code = decide_approval(body.get("approval_id"), bool(body.get("approved")))
            return self._json(code, out)

        if path == "/control/pause":
            with LOCK:
                for tid, task in TASKS.items():
                    for n in task["nodes"].values():
                        if n["status"] == "running":
                            n["status"] = "blocked"
            emit("energy.update", energy_payload(), frm="system")
            return self._json(200, {"ok": True, "paused": True})

        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=sweep_approvals, daemon=True).start()
    print("CSchat-Cell v0.1  http://%s:%d/   budget=%d  approval_ttl=%ds" % (HOST, PORT, BUDGET_TOKENS, APPROVAL_TTL))
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
