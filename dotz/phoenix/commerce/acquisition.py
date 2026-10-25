"""Bounded acquisition maintenance called by the existing PHOENIX commerce cycle.

Uses existing CRM opportunity, ownership, event and kv records. Does not send,
post, buy, pay, create seats or alter the 24-hour lifecycle. Public issues are
untrusted demand observations and cannot qualify a buyer or attest revenue.
"""
import hashlib,json,urllib.parse,urllib.request
from dotz.phoenix import lifecycle,opportunities
from dotz.phoenix.executor import guard

QUERIES=(
 ('repo:czlonkowski/n8n-mcp is:issue is:open validation','workflow-rescue','PHOENIX-04'),
 ('repo:langchain-ai/langchain is:issue is:open MCP schema','payload-validator','PHOENIX-02'),
 ('is:issue is:open "JSON schema" "bounty"','payload-validator','PHOENIX-03'),
)

def fetch(query):
 url='https://api.github.com/search/issues?'+urllib.parse.urlencode({'q':query,'sort':'updated','per_page':5})
 req=urllib.request.Request(url,headers={'User-Agent':'ELYREON-PHOENIX-acquisition/2','Accept':'application/vnd.github+json'})
 with urllib.request.urlopen(req,timeout=3) as response:
  raw=response.read(500001)
  if len(raw)>500000:raise ValueError('response too large')
  return json.loads(raw)

def run(s,transport=fetch):
 if s.now()<s.kv_get('agent_acquisition:due',0):return {'throttled':True}
 # Reserve the network window before requests. Failures back off; never loop on a denied source.
 s.kv_set('agent_acquisition:due',s.now()+6*3600)
 result={'at':s.now(),'sources':[],'outreach':'STOP preserved; prepared contributions only','paid_enabled':False}
 for query,service,slot in QUERIES:
  inst=lifecycle.live(s,slot)
  if inst is None:continue
  try:
   guard(s,inst)
   response=transport(query);found=[]
   for item in response.get('items',[])[:5]:
    url=item.get('html_url','');title=item.get('title','')[:300]
    if not url.startswith('https://github.com/') or '/issues/' not in url or item.get('state')!='open' or item.get('pull_request'):continue
    # Do not ingest excluded projects or our own controlled fixtures.
    if any(x in url.lower() for x in ('colt2822/','reddit','dragon-ball','card-breaker','titan','nova')):continue
    excerpt=(item.get('body') or '')[:1200]
    key='acq-'+hashlib.sha256(url.encode()).hexdigest()[:18]
    fit=('Static independent structural check before import; does not fix upstream runtime or node semantics.' if service=='workflow-rescue' else 'Bounded JSON input check before an API/tool invocation; does not establish full JSON Schema compatibility.')
    packet={'source_url':url,'title':title,'demand_excerpt':excerpt,'observed_updated_at':item.get('updated_at'),'engagement':{'comments':item.get('comments'),'reactions':item.get('reactions',{}).get('total_count')},'service':service,'fit':fit,'supported_method':'MCP Streamable HTTP with free bearer token or HTTP subworkflow','useful_example':'https://elyraon.com.co/agents/integrations/'+('n8n-workflow.json' if service=='workflow-rescue' else 'langchain_check.py'),'contribution':{'title':'Sanitized validation example for '+title,'request_context':excerpt[:350],'steps':['Extract only the shareable failing JSON or n8n export described in this issue.','Run the published integration, preserve the structured receipt and limitations.','Compare the reported failure with current bounded coverage; do not claim to fix runtime, auth or adapter bugs.'],'status':'PREPARED_EXAMPLE; issue-specific reproduction and contribution permission still required'},'capability_fit':'REQUIRES_REVIEW: issue is evidence of a problem, not proof this static service solves it','estimated_effort_hours':[1,3],'expected_value_usd':None,'willingness_to_pay':'UNKNOWN','funding_verified':False,'next_action':'Assess exact capability fit and maintainer contribution policy before any contact','owner_instance':inst['instance_id']}
    s.c.execute("INSERT OR IGNORE INTO phoenix_opportunity(opportunity_id,source,source_ref,source_url,summary,service,est_value_cents,discovered_by,stage,synthetic,first_seen,updated_at) VALUES(?,?,?,?,?,?,NULL,?,'RESEARCH_ONLY',?,?,?)",(key,'agent_acquisition',url,url,title,service,inst['instance_id'],s.synthetic,s.now(),s.now()))
    # Existing ownership ledger prevents competing PHOENIX lineages contacting the same prospect.
    opp=dict(s.c.execute('SELECT * FROM phoenix_opportunity WHERE opportunity_id=?',(key,)).fetchone())
    claimed,ownership=opportunities.claim(s,lineage=inst['lineage'],instance_id=inst['instance_id'],opp=opp)
    packet['ownership']={'claimed':claimed,'key_or_hold':ownership}
    s.kv_set('agent_acquisition:prospect:'+key,packet);found.append(key)
   detail={'query':query,'prospects':found,'total_reported':response.get('total_count'),'independent_demand_not_customer':True}
   s.event(inst['instance_id'],slot,'AGENT_ACQUISITION_RESEARCH',query,json.dumps(detail));result['sources'].append(detail)
  except Exception as e:result['sources'].append({'query':query,'error':type(e).__name__,'retry_after_seconds':21600})
 # Hand off actionable work to all five existing owners, without resetting their leases.
 for slot,action in {'PHOENIX-01':'Review independently verified repeat calls and opt-in commercial inquiries; ask scope and budget only through authorized channels.','PHOENIX-02':'Review framework packets and supported contribution routes; reproduce integration before proposing partnership.','PHOENIX-03':'Verify payout, funding, eligibility and task scope before any bounty claim.','PHOENIX-04':'Check registry readbacks, install failures and relevant catalog search; prepare targeted distribution fixes.','PHOENIX-05':'Compare newly observed demand with current limits and costs; do not infer willingness to pay from issues.'}.items():
  inst=lifecycle.live(s,slot)
  if inst:
   detail={'owner_instance':inst['instance_id'],'action':action,'research':result['sources'],'external_installations':'UNKNOWN until independent proof','hosting_cost_usd':None,'next_review_after':s.now()+21600}
   s.kv_set('agent_acquisition:work:'+slot,detail);s.event(inst['instance_id'],slot,'AGENT_ACQUISITION_HANDOFF',action,json.dumps(detail))
 s.kv_set('agent_acquisition:last_tick',result)
 return result
