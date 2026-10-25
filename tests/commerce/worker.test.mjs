import test from 'node:test';
import assert from 'node:assert/strict';
import {validateInput} from '../../dotz/phoenix/commerce/worker.mjs';
import {analyzeWorkflow} from '../../dotz/phoenix/commerce/vendor/workflow-rescue.mjs';
import {preparePaidServer} from '../../dotz/phoenix/commerce/x402.mjs';

test('existing analyzer finds a broken reference with explicit static evidence',()=>{
 const w={nodes:[{id:'s',name:'Start',type:'n8n-nodes-base.manualTrigger'}],connections:{Start:{main:[[{node:'Missing',type:'main',index:0}]]}}};
 const r=analyzeWorkflow(validateInput({workflow:w}));
 assert.ok(r.findings.some(f=>f.code==='MISSING_REFERENCED_NODE'&&f.classification==='CONFIRMED_STRUCTURE'));
 assert.ok(r.limitations.some(x=>x.includes('no workflow code')));
});
test('untrusted exports are bounded and credential values rejected',()=>{
 assert.throws(()=>validateInput({workflow:{nodes:Array(201).fill({}),connections:{}}}),/200 nodes/);
 assert.throws(()=>validateInput({workflow:{nodes:[{parameters:{api_key:'private-value'}}],connections:{}}}),/credential-like/);
 let deep={};let x=deep;for(let i=0;i<30;i++){x.next={};x=x.next;}
 assert.throws(()=>validateInput({workflow:{nodes:[deep],connections:{}}}),/nesting/);
});
test('no x402 provider module or payment path loads without verified activation',async()=>{
 await assert.rejects(preparePaidServer({payTo:'0x0000000000000000000000000000000000000000'}),/Paid activation blocked/);
});
