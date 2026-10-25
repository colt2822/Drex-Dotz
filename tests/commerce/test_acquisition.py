from dotz.phoenix.store import Store
from dotz.phoenix import lifecycle
from dotz.phoenix.personas import SLOTS
from dotz.phoenix.commerce import acquisition

def test_research_uses_existing_ownership_without_claiming_customer_or_resetting_leases(tmp_path):
 s=Store(tmp_path/'crm.sqlite3',clock=lambda:1800000000.,synthetic=True)
 for slot in SLOTS:lifecycle.spawn(s,slot)
 before=[tuple(r) for r in s.c.execute('SELECT instance_id,spawn_at,deadline_at FROM phoenix_instance ORDER BY slot')]
 seen=[]
 def transport(q):
  seen.append(q)
  return {'items':[{'html_url':'https://github.com/example/framework/issues/12','state':'open','title':'JSON payload validation needed','body':'Need bounded checks','comments':2}],'total_count':1}
 result=acquisition.run(s,transport)
 assert len(seen)==3
 assert 'error' not in str(result)
 assert s.c.execute('SELECT count(*) FROM phoenix_opportunity').fetchone()[0]==1
 assert s.c.execute('SELECT stage FROM phoenix_opportunity').fetchone()[0]=='RESEARCH_ONLY'
 assert s.c.execute('SELECT count(*) FROM phoenix_ownership').fetchone()[0]==1
 assert s.c.execute('SELECT count(*) FROM phoenix_outcome').fetchone()[0]==0
 assert [tuple(r) for r in s.c.execute('SELECT instance_id,spawn_at,deadline_at FROM phoenix_instance ORDER BY slot')]==before
 assert acquisition.run(s,transport)=={'throttled':True}
 assert len(seen)==3

def test_denied_feed_backs_off_without_killing_controller(tmp_path):
 s=Store(tmp_path/'crm.sqlite3',clock=lambda:1800000000.,synthetic=True)
 for slot in SLOTS:lifecycle.spawn(s,slot)
 def denied(q):raise TimeoutError('blocked')
 result=acquisition.run(s,denied)
 assert all(x['error']=='TimeoutError' for x in result['sources'])
 assert acquisition.run(s,denied)=={'throttled':True}
