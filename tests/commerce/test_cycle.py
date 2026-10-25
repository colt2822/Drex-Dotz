import json
from pathlib import Path

import pytest
import jsonschema

from dotz.phoenix.store import Store
from dotz.phoenix.commerce import cycle


def row(event_id="e1", stage="FREE_CALL", synthetic=0):
    return dict(event_id=event_id, client_id="c1", stage=stage, source="mcp_registry", campaign="trial", tool="check_n8n_workflow", success=1, synthetic=synthetic, at=1000, payment_status="SETTLED", evidence_class="VERIFIED_EXTERNAL")


def test_transport_cannot_assert_external_payment_or_revenue(tmp_path):
    s=Store(tmp_path/"crm.sqlite3")
    assert cycle.ingest(s,[row(),row(),row("e2","PAID_CALL"),row("e3","REVENUE_COLLECTED")])==1
    status=cycle.summary(s)
    assert sum(status["verified_external_funnel"].values())==0
    assert status["verified_collected_cents"]==0
    stored=dict(s.c.execute("SELECT * FROM phoenix_agent_usage").fetchone())
    assert stored["payment_status"]=="NOT_PAID"
    assert stored["evidence_class"]=="UNVERIFIED_PUBLIC"
    assert s.c.execute("SELECT count(*) FROM phoenix_outcome").fetchone()[0]==0


def test_controlled_install_never_becomes_external(tmp_path):
    s=Store(tmp_path/"crm.sqlite3")
    cycle.ingest(s,[row(synthetic=1)])
    with pytest.raises(ValueError):
        cycle.verify_external(s,"c1",evidence="a genuine external proof reference",by="owner:test")
    assert cycle.summary(s)["verified_external_funnel"]["FREE_CALL"]==0


def test_identity_proof_does_not_invent_installation(tmp_path):
    s=Store(tmp_path/"crm.sqlite3")
    cycle.ingest(s,[row()])
    cycle.verify_external(s,"c1",evidence="external operator confirmation reference",by="owner:test")
    status=cycle.summary(s)["verified_external_funnel"]
    assert status["FREE_CALL"]==1
    assert status["INSTALLED"]==0
    assert status["PAID_CALL"]==0


def test_registry_manifests_match_authoritative_schemas():
    root=Path(__file__).resolve().parents[2]
    for schema,doc in [("mcp-server.schema.json","agents/server.json"),("ard-entry.schema.json",".well-known/ard.json")]:
        s=json.loads((Path(__file__).parent/schema).read_text())
        if schema.startswith("ard"):
            s={**s,"$ref":"#/$defs/ArdManifest"}
        jsonschema.Draft202012Validator(s).validate(json.loads((root/"dotz/phoenix/commerce/public"/doc).read_text()))


def test_existing_five_seat_cycle_is_due_bounded_and_clock_preserving(tmp_path,monkeypatch):
    from dotz.phoenix import lifecycle
    from dotz.phoenix.personas import SLOTS
    now=[1800000000.0]
    s=Store(tmp_path/"crm.sqlite3",clock=lambda:now[0],synthetic=True)
    # Existing lifecycle spawn and guard remain in control of identities and leases.
    for slot in SLOTS:
        lifecycle.spawn(s,slot)
    before=[tuple(r) for r in s.c.execute('SELECT instance_id,spawn_at,deadline_at FROM phoenix_instance ORDER BY slot')]
    def transport(path,token=None):
        if path.startswith('/agents/events'):return {'events':[dict(row(synthetic=1),seq=1)]}
        if path.endswith('ard.json'):return {'entries':[{}]}
        if path.endswith('server.json'):return {'name':'co.com.elyraon/workflow-rescue','version':'1.0.0'}
        return {'ok':True}
    result=cycle.tick(s,transport=transport,config={'enabled':True,'sync_token':'fixture'})
    assert set(result)==set(SLOTS)
    assert result['PHOENIX-01']['imported']==1
    assert cycle.tick(s,transport=transport,config={'enabled':True})=={}
    assert [tuple(r) for r in s.c.execute('SELECT instance_id,spawn_at,deadline_at FROM phoenix_instance ORDER BY slot')]==before
    assert s.c.execute('SELECT count(*) FROM phoenix_outcome').fetchone()[0]==0


def test_default_transport_ingests_and_runs_retention(tmp_path,monkeypatch):
    from dotz.phoenix import lifecycle
    s=Store(tmp_path/'crm.sqlite3',clock=lambda:1800000000.0,synthetic=True)
    lifecycle.spawn(s,'PHOENIX-01')
    seen=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):return json.dumps({'events':[{**row(synthetic=1),'at':s.now(),'seq':1}]}).encode()
    def open_url(req,timeout):
        assert req.get_header('User-agent')=='ELYREON-PHOENIX-maintenance/1'
        seen.append(req.full_url)
        return Response()
    monkeypatch.setattr(cycle.urllib.request,'urlopen',open_url)
    result=cycle.tick(s,config={'enabled':True,'sync_token':'fixture'})
    assert result['PHOENIX-01']['imported']==1
    assert any(x.endswith('/agents/maintenance') for x in seen)
    assert s.kv_get('agent_commerce:cursor')==1
