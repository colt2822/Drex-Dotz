import { analyzeWorkflow } from './vendor/workflow-rescue.mjs';

export const TOOL = {
  name: 'check_n8n_workflow',
  description: 'Free static structural validation of a sanitized n8n workflow export. Finds missing node references, duplicate IDs, malformed connections and configuration risks. Returns evidence, classifications, repair suggestions and limitations. Does not execute code, test credentials, fetch URLs or prove runtime behavior. Requires a free bearer trial token from POST /agents/trial; 20 calls/client/day, 64 KiB and 200 nodes maximum. No payment or fleet enrollment.',
  inputSchema: { type: 'object', properties: { workflow: { type: 'object', description: 'Sanitized n8n export containing nodes array and connections object.' } }, required: ['workflow'], additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
};
const enc = new TextEncoder();
const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'access-control-allow-origin': '*', 'cache-control': 'no-store', ...headers } });
const fail = (status, message) => { throw Object.assign(new Error(message), { status }); };
export async function hash(value) { return [...new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(value)))].map(x => x.toString(16).padStart(2,'0')).join(''); }
const sources = new Set(['mcp_registry','skill_directory','x402_bazaar','ard','a2a','github','direct_api','codex','claude_code','opencode','cursor','hermes','n8n','langchain','crewai','openai_agents','vscode','other']);
async function quota(env, bucket, limit, seconds) {
  const now = Math.floor(Date.now()/1000), expires = (Math.floor(now/seconds)+1)*seconds;
  const key = `${bucket}:${expires}`;
  const row = await env.DB.prepare('INSERT INTO phoenix_ac_quota(bucket,n,expires_at) VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET n=n+1 WHERE n < ? RETURNING n').bind(key,expires,limit).first();
  if (!row) fail(429, 'Quota reached. Retry next quota window. Paid service is not enabled.');
}
async function event(env, client, stage, tool = null, success = true, elapsed = null, eventId = crypto.randomUUID()) {
  await env.DB.prepare('INSERT OR IGNORE INTO phoenix_ac_event(event_id,client_id,stage,source,campaign,tool,success,synthetic,evidence_class,elapsed_ms,provider_cost_usd,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)')
    .bind(eventId,client?.client_id || null,stage,client?.source || 'other',client?.campaign || '',tool,Number(success),client?.synthetic ?? 0,client?.synthetic ? 'CONTROLLED_TEST' : 'UNVERIFIED_PUBLIC',elapsed,0,Math.floor(Date.now()/1000)).run();
}
async function body(req) {
  if (!req.headers.get('content-type')?.includes('application/json')) fail(415,'Use application/json');
  const reader = req.body?.getReader(); if (!reader) fail(400,'JSON body required');
  let size = 0; const chunks = [];
  while (true) { const {done,value} = await reader.read(); if(done) break; size += value.length; if(size > 65536) { await reader.cancel(); fail(413,'Maximum request size is 64 KiB'); } chunks.push(value); }
  const all = new Uint8Array(size); let offset=0; for(const c of chunks) { all.set(c,offset); offset+=c.length; }
  try { return JSON.parse(new TextDecoder().decode(all)); } catch { fail(400,'Invalid JSON'); }
}
async function client(req, env) {
  const token = req.headers.get('authorization')?.match(/^Bearer ([A-Za-z0-9_-]{20,150})$/)?.[1];
  if (!token) fail(401,'Get a free token at POST /agents/trial. Send Authorization: Bearer TOKEN.');
  const c = await env.DB.prepare('SELECT * FROM phoenix_ac_client WHERE token_hash=?').bind(await hash(token)).first();
  if (!c) fail(401,'Unknown trial token');
  if(c.created_at < Math.floor(Date.now()/1000)-30*86400) fail(401,'Trial token expired; request a new token');
  await event(env,c,'CONNECTED',null,true,null,`connected:${c.client_id}`);
  return c;
}
export function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(k=>k!=='workflow')) fail(400,'Expected {workflow: sanitized n8n export}');
  const w = input.workflow;
  if (!w || !Array.isArray(w.nodes) || !w.connections || typeof w.connections !== 'object' || Array.isArray(w.connections)) fail(400,'workflow requires nodes array and connections object');
  if (w.nodes.length > 200 || Object.keys(w.connections).length > 200) fail(413,'Maximum 200 nodes and connection sources');
  // Bound nesting, total objects and edges before invoking the existing static analyzer.
  const todo = [[w,0]]; let visited=0;
  while(todo.length) { const [v,d]=todo.pop(); if(++visited>10000 || d>24) fail(413,'Workflow nesting or object limit exceeded');
    if(v && typeof v==='object') for(const [k,x] of Object.entries(v)) {
      if(/token|secret|password|api[_-]?key|authorization|cookie/i.test(k) && typeof x==='string' && x.trim() && !/^(\[redacted\]|\*+)$/i.test(x)) fail(400,'Remove credential-like values before uploading');
      if(x && typeof x==='object') todo.push([x,d+1]);
    }
  }
  return w;
}
async function invoke(req, env, c, input, key) {
  const workflow = validateInput(input);
  if (!key || !/^[A-Za-z0-9._:-]{1,120}$/.test(key)) fail(400,'Idempotency-Key required (1-120 letters, digits, . _ : -)');
  const fingerprint=await hash(JSON.stringify(workflow)), receipt=crypto.randomUUID(), at=Math.floor(Date.now()/1000);
  const reserved=await env.DB.prepare("INSERT INTO phoenix_ac_request(client_id,request_key,input_hash,receipt_id,state,at) VALUES(?,?,?,?,'RUNNING',?) ON CONFLICT DO NOTHING RETURNING receipt_id").bind(c.client_id,key,fingerprint,receipt,at).first();
  let receiptId=receipt;
  if(!reserved) {
    const prior=await env.DB.prepare('SELECT * FROM phoenix_ac_request WHERE client_id=? AND request_key=?').bind(c.client_id,key).first();
    if(prior.input_hash!==fingerprint) fail(409,'Idempotency key already used with different input');
    if(prior.state!=='DONE') fail(409,'Request pending or interrupted. Do not assume delivery; retry this key later or contact support.');
    receiptId=prior.receipt_id;
  }
  const start=performance.now();
  try {
    if(reserved) {
      await quota(env,`client:${c.client_id}`,20,86400);
      await quota(env,`ip:${await hash(env.PRIVACY_SALT + (req.headers.get('cf-connecting-ip') || 'local'))}`,60,3600);
      await quota(env,'global:invocation',1000,86400);
    }
    const report=analyzeWorkflow(workflow); delete report.generatedAt;
    const result={ receipt_id:receiptId, tool:TOOL.name, version:'1.0.0', input_sha256:fingerprint,
      structural_pass:!report.findings.some(f=>f.classification==='CONFIRMED_STRUCTURE' && ['HIGH','CRITICAL'].includes(f.severity)),
      report, payment:{status:'NOT_PAID',charged_usd:0}, replay:!reserved };
    if(reserved) {
      // State and success telemetry commit atomically. No request or report content is stored.
      await env.DB.batch([
        env.DB.prepare("UPDATE phoenix_ac_request SET state='DONE' WHERE client_id=? AND request_key=?").bind(c.client_id,key),
        env.DB.prepare('INSERT INTO phoenix_ac_event(event_id,client_id,stage,source,campaign,tool,success,synthetic,evidence_class,elapsed_ms,provider_cost_usd,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)')
          .bind(receiptId,c.client_id,'FREE_CALL',c.source,c.campaign,TOOL.name,1,c.synthetic,c.synthetic?'CONTROLLED_TEST':'UNVERIFIED_PUBLIC',performance.now()-start,0,at)
      ]);
    }
    return result;
  } catch(e) {
    if(reserved) { await env.DB.prepare('DELETE FROM phoenix_ac_request WHERE client_id=? AND request_key=?').bind(c.client_id,key).run(); await event(env,c,'CALL_FAILED',TOOL.name,false,performance.now()-start); }
    throw e;
  }
}
async function rpc(req,env,path,b) {
  const id=b?.id ?? null, ok=result=>json({jsonrpc:'2.0',id,result});
  if(!b || b.jsonrpc!=='2.0' || typeof b.method!=='string') return json({jsonrpc:'2.0',id,error:{code:-32600,message:'Invalid request'}},400);
  if(path==='/agents/mcp') {
    if(b.method==='initialize') {
      return ok({protocolVersion:'2025-11-25',capabilities:{tools:{}},serverInfo:{name:'elyreon-workflow-rescue',version:'1.0.0'},instructions:TOOL.description});
    }
    if(b.method.startsWith('notifications/') && b.id===undefined) return new Response(null,{status:202});
    if(b.method==='ping') return ok({});
    if(b.method==='tools/list') return ok({tools:[TOOL]});
    if(b.method==='tools/call') {
      const c=await client(req,env);
      if(b.params?.name!==TOOL.name) return json({jsonrpc:'2.0',id,error:{code:-32602,message:'Unknown tool'}});
      try { const r=await invoke(req,env,c,b.params.arguments,req.headers.get('idempotency-key') || String(b.id)); return ok({content:[{type:'text',text:JSON.stringify(r)}],structuredContent:r,isError:false}); }
      catch(e) { return ok({content:[{type:'text',text:e.message}],isError:true}); }
    }
  } else if(b.method==='message/send') {
    const c=await client(req,env), m=b.params?.message;
    if(m?.role!=='user' || typeof m.messageId!=='string' || !Array.isArray(m.parts) || m.parts.length!==1 || m.parts[0].kind!=='data') fail(400,'A2A 0.3 message requires user role, messageId and one data part containing {workflow:...}');
    const r=await invoke(req,env,c,m.parts[0].data,req.headers.get('idempotency-key')||m.messageId);
    return ok({kind:'message',role:'agent',messageId:r.receipt_id,parts:[{kind:'data',data:r}]});
  }
  return json({jsonrpc:'2.0',id,error:{code:-32601,message:'Method not found'}});
}
export default { async fetch(req,env) {
  const u=new URL(req.url), path=u.pathname;
  try {
    if(req.method==='OPTIONS') return new Response(null,{status:204,headers:{'access-control-allow-origin':'*','access-control-allow-methods':'GET, POST, OPTIONS','access-control-allow-headers':'authorization, content-type, idempotency-key, mcp-protocol-version, mcp-session-id'}});
    if(path==='/agents/health') return json({ok:true,capability:TOOL.name,paid_enabled:false});
    if(path==='/agents/events') {
      if(!env.SYNC_TOKEN || req.headers.get('authorization')!==`Bearer ${env.SYNC_TOKEN}`) fail(401,'Operator authorization required');
      const after=Number(u.searchParams.get('after')||0); if(!Number.isSafeInteger(after)||after<0) fail(400,'Invalid cursor');
      const events=await env.DB.prepare('SELECT * FROM phoenix_ac_event WHERE seq>? ORDER BY seq LIMIT 500').bind(after).all();
      return json({events:events.results});
    }
    if(path==='/agents/maintenance' && req.method==='POST') {
      if(!env.SYNC_TOKEN || req.headers.get('authorization')!==`Bearer ${env.SYNC_TOKEN}`) fail(401,'Operator authorization required');
      const cutoff=Math.floor(Date.now()/1000)-30*86400;
      await env.DB.batch([
        env.DB.prepare('DELETE FROM phoenix_ac_event WHERE at < ?').bind(cutoff),
        env.DB.prepare('DELETE FROM phoenix_ac_request WHERE at < ?').bind(cutoff),
        env.DB.prepare('DELETE FROM phoenix_ac_client WHERE created_at < ?').bind(cutoff),
        env.DB.prepare('DELETE FROM phoenix_ac_quota WHERE expires_at < ?').bind(Math.floor(Date.now()/1000))
      ]);
      return json({retention_days:30,ok:true});
    }
    if(path==='/agents/trial' && req.method==='POST') {
      if(!env.PRIVACY_SALT) fail(503,'Trial enrollment unavailable');
      const b=await body(req), ip=await hash(env.PRIVACY_SALT+(req.headers.get('cf-connecting-ip')||'local'));
      const synthetic=b.test===true || Boolean(env.TEST_TOKEN && req.headers.get('x-elyreon-test')===env.TEST_TOKEN);
      await quota(env,`${synthetic?'enroll-test':'enroll'}:${ip}`,synthetic?20:5,3600); await quota(env,'global:enroll',100,86400);
      const token=`ely_${crypto.randomUUID().replaceAll('-','')}${crypto.randomUUID().replaceAll('-','')}`;
      const c={client_id:crypto.randomUUID(),source:sources.has(b.source)?b.source:'direct_api',campaign:typeof b.campaign==='string'?b.campaign.replace(/[^a-zA-Z0-9_.-]/g,'').slice(0,80):'',synthetic:Number(synthetic)};
      await env.DB.prepare('INSERT INTO phoenix_ac_client(token_hash,client_id,source,campaign,synthetic,created_at) VALUES(?,?,?,?,?,?)').bind(await hash(token),c.client_id,c.source,c.campaign,c.synthetic,Math.floor(Date.now()/1000)).run();
      await event(env,c,'TOKEN_ISSUED');
      return json({token,client_id:c.client_id,free_calls_per_day:20,token_lifetime_days:30,synthetic:Boolean(c.synthetic),source_evidence:'CLIENT_CLAIMED_UNVERIFIED',instructions:'Use Authorization: Bearer TOKEN and Idempotency-Key on POST /agents/check. Request body: {workflow: sanitized n8n export}.',mcp_url:`${u.origin}/agents/mcp`},201);
    }
    if(path==='/agents/check' && req.method==='POST') { const c=await client(req,env); return json(await invoke(req,env,c,await body(req),req.headers.get('idempotency-key'))); }
    if(path==='/agents/paid') return json({enabled:false,candidate_price_usd:'0.01',blockers:['founder-approved price','verified receiving account and settlement flow','provider reconciliation','independent demand'],payment_required:false},503);
    if(['/agents/mcp','/agents/a2a'].includes(path)) {
      if(req.method!=='POST') return json({error:'POST JSON-RPC required'},405,{'allow':'POST, OPTIONS'});
      const origin=req.headers.get('origin'); if(origin && origin!==env.PUBLIC_ORIGIN) fail(403,'Origin not allowed for RPC');
      return await rpc(req,env,path,await body(req));
    }
    if(req.method==='GET' || req.method==='HEAD') {
      // Preserve the directory URL: the assets binding handles index.html itself.
      // Rewriting it to index.html causes the binding's canonical redirect to loop.
      return env.ASSETS.fetch(req);
    }
    return json({error:'Not found'},404);
  } catch(e) { return json({error:e.status?e.message:'Service temporarily unavailable'},e.status||503,e.status===429?{'retry-after':'3600'}:{}); }
} };
