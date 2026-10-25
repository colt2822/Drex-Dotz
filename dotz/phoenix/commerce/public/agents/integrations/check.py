"""Run a free check using a saved token, or a controlled acceptance with --test.

pip install httpx mcp==2.3.0
python check.py --service payload --test
python check.py --service workflow --input sanitized-request.json
Set ELYREON_PAYLOAD_TOKEN / ELYREON_WORKFLOW_TOKEN for repeated use.
"""
import argparse,asyncio,json,os,uuid
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.streamable_http import httpx2

SERVICES={
 'workflow':('https://elyraon.com.co/agents','check_n8n_workflow',{'workflow':{'nodes':[{'id':'start','name':'Start','type':'n8n-nodes-base.manualTrigger','typeVersion':1,'position':[0,0],'parameters':{}}],'connections':{}}}),
 'payload':('https://elyreon-payload-validator.colt2822.workers.dev','validate_payload',{'schema':{'type':'object','required':['id'],'properties':{'id':{'type':'integer'}},'additionalProperties':False},'payload':{'id':42}})
}
async def run(service,token,data,mode='mcp',repeat=1):
 base,tool,_=SERVICES[service];headers={'Authorization':'Bearer '+token};results=[]
 async with httpx.AsyncClient(timeout=20) as web:
  if mode=='http':
   for _ in range(repeat):
    r=await web.post(base+('/check' if service=='workflow' else '/validate'),headers={**headers,'Idempotency-Key':str(uuid.uuid4())},json=data);r.raise_for_status();results.append(r.json())
  else:
   async with httpx2.AsyncClient(headers=headers,timeout=20) as mcp_http, streamable_http_client(base+'/mcp',http_client=mcp_http) as (read,write):
    async with ClientSession(read,write) as session:
     await session.initialize();tools=await session.list_tools();assert tool in [t.name for t in tools.tools]
     for _ in range(repeat):
      r=await session.call_tool(tool,data);assert not r.is_error,r.model_dump()
      result=r.structured_content or json.loads(r.content[0].text);results.append(result)
 return results
async def main():
 a=argparse.ArgumentParser();a.add_argument('--service',choices=SERVICES,required=True);a.add_argument('--test',action='store_true');a.add_argument('--input');a.add_argument('--transport',choices=['mcp','http'],default='mcp');a.add_argument('--repeat',type=int,choices=range(1,4),default=1);a.add_argument('--source',default='github');args=a.parse_args()
 base,_,sample=SERVICES[args.service];token=os.environ.get('ELYREON_'+args.service.upper()+'_TOKEN')
 if not token and not args.test:raise SystemExit('Set ELYREON_'+args.service.upper()+'_TOKEN to a saved free trial token; see the quickstart. Use --test for controlled acceptance.')
 if not token:
  async with httpx.AsyncClient(timeout=20) as web:
   r=await web.post(base+'/trial',json={'source':args.source,'campaign':'agent-acquisition-v2','test':args.test});r.raise_for_status();trial=r.json()
   if args.test:assert trial.get('synthetic') is True
   token=trial['token']
  if not args.test:raise SystemExit('Trial created. To reuse a token, enroll once with curl, save it privately, and set ELYREON_'+args.service.upper()+'_TOKEN before running this script.')
 data=json.load(open(args.input)) if args.input else sample
 results=await run(args.service,token,data,args.transport,args.repeat)
 for r in results:
  assert r.get('receipt_id'),r
  if args.service=='payload':assert r['verdict'] in ('PASS','FAIL'),r
  else:assert isinstance(r['structural_pass'],bool),r
 print(json.dumps({'service':args.service,'transport':args.transport,'controlled':args.test,'calls':results},indent=2))
if __name__=='__main__':asyncio.run(main())
