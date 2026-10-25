// Prepared provider integration, intentionally not imported by the free production worker.
// Use the provider SDK; no custom wallet, payment verifier or settlement processor.
export async function preparePaidServer(approval) {
  const required=['founder_authorization_ref','receiver_verification_ref','settlement_verification_ref','reconciliation_verification_ref','price_approval_ref','payTo'];
  if(!approval || required.some(k=>!approval[k])) throw new Error('Paid activation blocked: verified authorization, receiver, settlement, price and reconciliation required');
  if(!/^0x[0-9a-fA-F]{40}$/.test(approval.payTo)) throw new Error('Invalid Base receiving address');
  const { createX402Server }=await import('@coinbase/cdp-sdk/x402');
  const { declareDiscoveryExtension }=await import('@x402/extensions/bazaar');
  return createX402Server({payTo:approval.payTo, routes:{'POST /agents/paid':{
    price:'$0.01',description:'Bounded static n8n workflow export validation; maximum 200 nodes and 64 KiB; no code execution.',
    extensions:{...declareDiscoveryExtension({method:'POST',bodyType:'json',input:{workflow:{nodes:[],connections:{}}},inputSchema:{type:'object',properties:{workflow:{type:'object',required:['nodes','connections']}},required:['workflow']},output:{example:{structural_pass:false,payment:{status:'SETTLED'}}}})}
  }}});
}
