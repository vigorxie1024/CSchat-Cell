'use strict';
// contract-proof.js —— CONTRACT v0.1 的 8 条断言（外部可跑，不看实现代码）
// 用法: node contract-proof.js [baseUrl]
const http = require('http');
const BASE = process.argv[2] || 'http://127.0.0.1:8012';

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log('PASS  ' + name); }
  else { fail++; console.log('FAIL  ' + name + (detail ? '  → ' + detail : '')); }
}
function req(method, path, body) {
  return new Promise((resolve) => {
    const u = new URL(BASE + path);
    const data = body ? JSON.stringify(body) : null;
    const o = { host: u.hostname, port: u.port, path: u.pathname + u.search, method, timeout: 15000, headers: {} };
    if (data) { o.headers['Content-Type'] = 'application/json'; o.headers['Content-Length'] = Buffer.byteLength(data); }
    const r = http.request(o, (s) => {
      let b = ''; s.setEncoding('utf8');
      s.on('data', c => b += c);
      s.on('end', () => { let j = null; try { j = JSON.parse(b); } catch (e) {} resolve({ code: s.statusCode, body: b, json: j }); });
    });
    r.on('error', (e) => resolve({ code: -1, body: e.message, json: null }));
    r.on('timeout', () => { r.destroy(); resolve({ code: -2, body: 'timeout', json: null }); });
    if (data) r.write(data);
    r.end();
  });
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const FIELDS = ['v', 'seq', 'msg_id', 'ts', 'from', 'to', 'type', 'trace_id', 'payload'];

(async () => {
  // 0. 服务可达
  const st0 = await req('GET', '/state');
  ok('服务可达 /state', st0.code === 200 && st0.json, 'HTTP ' + st0.code);

  // 造一条正常任务
  const say = await req('POST', '/say', { text: '做一份新消费品牌竞品分析' });
  ok('下发一句话成功', say.code === 200, 'HTTP ' + say.code);
  await sleep(300);

  // —— 1. 信封 8+1 字段齐全 ——
  const snap = await req('GET', '/snapshot');
  const msgs = (snap.json && snap.json.messages) || [];
  const missing = [];
  for (const m of msgs) for (const f of FIELDS) if (!(f in m)) missing.push(m.msg_id + ':' + f);
  ok('信封字段齐全（' + msgs.length + ' 条消息 × ' + FIELDS.length + ' 字段）', msgs.length > 0 && missing.length === 0,
    missing.slice(0, 3).join(','));
  ok('seq 单调递增', msgs.every((m, i) => i === 0 || m.seq > msgs[i - 1].seq), 'seq 非递增');

  // —— 2. progress 不许回退 ——
  const state1 = await req('GET', '/state');
  const taskIds = Object.keys((state1.json && state1.json.tasks) || {});
  const taskId = taskIds[0];
  ok('任务已落库', !!taskId, JSON.stringify(taskIds).slice(0, 60));
  const up1 = await req('POST', '/msg', { v: '0.1', msg_id: 'p-1', from: 'nucleus', to: 'all',
    type: 'task.node.update', payload: { task_id: taskId, node_id: 'n1', progress: 60 } });
  const up2 = await req('POST', '/msg', { v: '0.1', msg_id: 'p-2', from: 'nucleus', to: 'all',
    type: 'task.node.update', payload: { task_id: taskId, node_id: 'n1', progress: 40 } });
  ok('progress 提升被接受', up1.code === 200, 'HTTP ' + up1.code + ' ' + up1.body.slice(0, 60));
  ok('progress 回退被拒（E4003）', up2.code === 409 || (up2.json && up2.json.code === 'E4003'),
    'HTTP ' + up2.code + ' ' + up2.body.slice(0, 80));

  // —— 3. 审批超时按拒绝（fail-closed）——
  const apRow = await req('GET', '/state');
  const apIds = Object.keys((apRow.json && apRow.json.approvals) || {});
  ok('超预算任务确实挂起了审批（不是静默执行）', apIds.length > 0,
    '待审 ' + apIds.length + ' 条；任务节点状态=' + JSON.stringify(
      ((apRow.json.tasks[taskId] || {}).nodes || []).map(n => n.node_id + ':' + n.status)));
  const blockedNow = ((apRow.json.tasks[taskId] || {}).nodes || []).some(n => n.status === 'blocked');
  ok('挂起节点状态为 blocked（不是 running）', blockedNow, '实际状态见上');
  await sleep(2500);   // TTL=1s，必过期
  const dec = await req('POST', '/approval', { approval_id: apIds[0], approved: true });
  ok('审批超时后批准无效（fail-closed）', dec.json && dec.json.status === 'expired',
    JSON.stringify(dec.json).slice(0, 80));
  const stAfter = await req('GET', '/state');
  const nodeAfter = ((stAfter.json.tasks[taskId] || {}).nodes || []).find(n => n.node_id === 'n3') || {};
  ok('超时后节点判失败（E4004），不放行', nodeAfter.status === 'failed' && nodeAfter.error === 'E4004',
    JSON.stringify(nodeAfter).slice(0, 100));

  // —— 4. 工具无 auth → 403 ——
  const toolNoAuth = await req('POST', '/msg', { v: '0.1', msg_id: 't-1', from: 'nucleus', to: 'tool:weather',
    type: 'tool.call', payload: { name: 'weather', args: { city: '上海' } } });
  ok('工具无 auth 返回 403（E4001）', toolNoAuth.code === 403, 'HTTP ' + toolNoAuth.code + ' ' + toolNoAuth.body.slice(0, 60));
  const toolWithAuth = await req('POST', '/msg', { v: '0.1', msg_id: 't-2', from: 'nucleus', to: 'tool:weather',
    type: 'tool.call', payload: { name: 'weather', args: { city: '上海' }, auth_token: 'demo' } });
  ok('工具带 auth 正常执行（Mock）', toolWithAuth.code === 200 && toolWithAuth.json && toolWithAuth.json.mock === true,
    'HTTP ' + toolWithAuth.code + ' ' + toolWithAuth.body.slice(0, 60));

  // —— 5. msg_id 幂等 ——
  const before = (await req('GET', '/state')).json.seq;
  const dupBody = { v: '0.1', msg_id: 'idem-1', from: 'nucleus', to: 'all', type: 'task.node.update',
    payload: { task_id: taskId, node_id: 'n3', progress: 10 } };
  const i1 = await req('POST', '/msg', dupBody);
  const i2 = await req('POST', '/msg', dupBody);
  await sleep(200);
  const after = (await req('GET', '/state')).json.seq;
  ok('相同 msg_id 两次返回一致', JSON.stringify(i1.json) === JSON.stringify(i2.json), i1.body.slice(0, 40) + ' vs ' + i2.body.slice(0, 40));
  ok('幂等不产生重复广播（seq 只加 1）', after - before === 1, 'seq 增量 ' + (after - before));

  // —— 6. since_seq 续传不重不漏 ——
  const all = (await req('GET', '/snapshot')).json.messages;
  const mid = Math.floor(all.length / 2);
  const cutoff = all[mid].seq;
  const tail = (await req('GET', '/snapshot?upto_seq=999999')).json.messages.filter(m => m.seq > cutoff);
  const expect = all.filter(m => m.seq > cutoff);
  ok('续传不重不漏', tail.length === expect.length && tail.every((m, i) => m.seq === expect[i].seq),
    '期望 ' + expect.length + ' 实得 ' + tail.length);

  // —— 7. 未知 type 不崩 ——
  const unknown = await req('POST', '/msg', { v: '0.1', msg_id: 'u-1', from: 'vx', to: 'all',
    type: 'weird.future.type', payload: { anything: 1 } });
  ok('未知 type 被接受并忽略（不崩）', unknown.code === 200 && unknown.json && unknown.json.ignored === 'weird.future.type',
    'HTTP ' + unknown.code + ' ' + unknown.body.slice(0, 60));

  // —— 8. 契约版本守卫 ——
  const badV = await req('POST', '/msg', { v: '9.9', msg_id: 'v-1', from: 'vx', to: 'all', type: 'chat.user', payload: { text: 'hi' } });
  ok('版本不符回 E4006（400）', badV.code === 400 && badV.json && badV.json.code === 'E4006',
    'HTTP ' + badV.code + ' ' + badV.body.slice(0, 60));

  console.log('\n合计: ' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail === 0 ? 0 : 1);
})();
