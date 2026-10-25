const MAX_BYTES = 5 * 1024 * 1024;
const MAX_NODES = 5000;
const SECRET_KEY = /(token|secret|password|credential|api[_-]?key|authorization|cookie)/i;
const EXAMPLE = {
  name: "Synthetic order notification",
  nodes: [
    { id: "trigger", name: "Webhook", type: "n8n-nodes-base.webhook", parameters: { httpMethod: "POST" }, position: [0, 0] },
    { id: "http", name: "Lookup order", type: "n8n-nodes-base.httpRequest", parameters: { url: "https://api.example.test/orders" }, position: [220, 0] },
    { id: "email", name: "Send confirmation", type: "n8n-nodes-base.emailSend", parameters: { toEmail: "={{ $json.email }}" }, position: [440, 0] },
    { id: "archive", name: "Archive result", type: "n8n-nodes-base.set", disabled: true, parameters: {}, position: [660, 0] }
  ],
  connections: { Webhook: { main: [[{ node: "Lookup order", type: "main", index: 0 }]] }, "Lookup order": { main: [[{ node: "Send confirmation", type: "main", index: 0 }]] } }
};

const asText = (value, limit = 220) => typeof value === "string" ? value.slice(0, limit) : "";
const nodeLabel = (node, index) => asText(node?.name, 100) || `Node ${index + 1}`;
function finding({ code, title, classification, severity = "LOW", confidence, affected = "Workflow", evidence, why, repair, test }) {
  return { code, title, classification, severity, confidence, affected, evidence, why, repair, test };
}
function hasNonEmptySecretValue(value) {
  const queue = [{ value, depth: 0 }]; let visited = 0;
  for (let cursor = 0; cursor < queue.length && visited++ < 10000; cursor++) {
    const current = queue[cursor];
    if (!current.value || typeof current.value !== "object" || current.depth > 24) continue;
    for (const [key, child] of Object.entries(current.value)) {
      if (SECRET_KEY.test(key) && typeof child === "string" && child.trim() && !/^\s*(=|\*+|\[redacted\])/i.test(child)) return true;
      if (child && typeof child === "object") queue.push({ value: child, depth: current.depth + 1 });
    }
  }
  return false;
}
function normalizeWorkflow(input) {
  if (input && typeof input === "object" && !Array.isArray(input) && Array.isArray(input.nodes) && input.connections && typeof input.connections === "object" && !Array.isArray(input.connections)) return { workflow: input, variant: "root nodes + connections" };
  if (input && typeof input === "object" && !Array.isArray(input) && input.workflow && Array.isArray(input.workflow.nodes) && input.workflow.connections && typeof input.workflow.connections === "object" && !Array.isArray(input.workflow.connections)) return { workflow: input.workflow, variant: "workflow wrapper" };
  if (input && typeof input === "object" && !Array.isArray(input) && input.data && Array.isArray(input.data.nodes) && input.data.connections && typeof input.data.connections === "object" && !Array.isArray(input.data.connections)) return { workflow: input.data, variant: "data wrapper" };
  throw new Error("Unsupported workflow export. Expected an object with nodes array and connections object, or a workflow/data wrapper containing them.");
}
function analyzeWorkflow(input) {
  const { workflow, variant } = normalizeWorkflow(input);
  const nodes = workflow.nodes;
  if (nodes.length > MAX_NODES) throw new Error(`Workflow contains ${nodes.length} nodes; the supported limit is ${MAX_NODES}.`);
  const findings = [];
  const validNodes = nodes.filter((node) => node && typeof node === "object" && !Array.isArray(node));
  const names = new Map(), ids = new Map();
  validNodes.forEach((node, i) => {
    if (typeof node.name === "string") names.set(node.name, [...(names.get(node.name) || []), i]);
    if (typeof node.id === "string") ids.set(node.id, [...(ids.get(node.id) || []), i]);
  });
  nodes.forEach((node, i) => {
    if (!node || typeof node !== "object" || Array.isArray(node)) findings.push(finding({ code: "INVALID_NODE", title: "Node entry is not an object", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: `nodes[${i}]`, evidence: "The exported nodes array contains a non-object entry.", why: "n8n cannot reliably interpret this entry as a node.", repair: "Restore this node from a valid export or remove the malformed entry after reviewing dependencies.", test: "Re-import the repaired export and run a non-production test workflow." }));
  });
  for (const [name, indexes] of names) if (indexes.length > 1) findings.push(finding({ code: "DUPLICATE_NODE_NAME", title: "Duplicate node name", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: indexes.map((i) => nodeLabel(nodes[i], i)).join(", "), evidence: `${indexes.length} exported nodes share the name “${name.slice(0, 100)}”.`, why: "Name-based connection references may become ambiguous during review or import.", repair: "Give each affected node a distinct name and review every connection that references those names.", test: "Re-import and confirm each edge resolves to the intended node." }));
  for (const [id, indexes] of ids) if (indexes.length > 1) findings.push(finding({ code: "DUPLICATE_NODE_ID", title: "Duplicate node identifier", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: indexes.map((i) => nodeLabel(nodes[i], i)).join(", "), evidence: `${indexes.length} exported nodes share identifier “${id.slice(0, 100)}”.`, why: "Identifiers are expected to distinguish nodes and can complicate edits and references.", repair: "Regenerate the duplicate identifier in n8n and inspect the resulting connections.", test: "Export again and confirm node identifiers are unique." }));
  const edges = [], incoming = new Set(), outgoing = new Map();
  const validNames = new Set(names.keys());
  for (const [source, outputs] of Object.entries(workflow.connections)) {
    if (!validNames.has(source)) findings.push(finding({ code: "MISSING_CONNECTION_SOURCE", title: "Connection source is missing", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: source.slice(0, 100), evidence: `connections references source “${source.slice(0, 100)}”, which is absent from the node names.`, why: "This edge cannot resolve to a node in the exported definition.", repair: "Restore the referenced source node or remove the stale edge after checking intended flow.", test: "Re-export and confirm all connection sources match node names." }));
    if (!outputs || typeof outputs !== "object" || Array.isArray(outputs)) { findings.push(finding({ code: "INVALID_CONNECTIONS", title: "Malformed connection map", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: source.slice(0, 100), evidence: `connections[${JSON.stringify(source.slice(0, 80))}] is not an output object.`, why: "The output map does not match the supported n8n export structure.", repair: "Regenerate the connection map from n8n.", test: "Import the repaired export into a test workflow." })); continue; }
    for (const [kind, groups] of Object.entries(outputs)) {
      if (!Array.isArray(groups)) { findings.push(finding({ code: "INVALID_CONNECTION_OUTPUTS", title: "Malformed output groups", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: source.slice(0, 100), evidence: `connections[${JSON.stringify(source.slice(0, 60))}].${kind.slice(0, 60)} is not an array.`, why: "The exported output groups do not match the supported n8n structure.", repair: "Regenerate the connection map from n8n and review its output branches.", test: "Import the repaired export in a test workflow." })); continue; }
      groups.forEach((group, outIndex) => {
        if (!Array.isArray(group)) { findings.push(finding({ code: "INVALID_CONNECTION_GROUP", title: "Malformed connection group", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: source.slice(0, 100), evidence: `Output group ${outIndex} for “${source.slice(0, 80)}” is not an array.`, why: "This output group cannot be interpreted as a list of edges.", repair: "Regenerate the connection map from n8n.", test: "Import the repaired export in a test workflow." })); return; }
        for (const edge of group) {
          const target = edge && typeof edge.node === "string" ? edge.node : "";
          if (!edge || typeof edge !== "object" || Array.isArray(edge)) { findings.push(finding({ code: "INVALID_CONNECTION_EDGE", title: "Malformed connection edge", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: source.slice(0, 100), evidence: `An edge in ${kind}[${outIndex}] for “${source.slice(0, 80)}” is not an object.`, why: "The connection destination cannot be resolved from this exported edge.", repair: "Regenerate this connection in n8n.", test: "Re-export and confirm the connection target is present." })); continue; }
          if (!target || !validNames.has(target)) findings.push(finding({ code: "MISSING_REFERENCED_NODE", title: "Connection target is missing", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", affected: `${source.slice(0, 80)} → ${target.slice(0, 80) || "(invalid target)"}`, evidence: `connections[${JSON.stringify(source.slice(0, 60))}].${kind}[${outIndex}] points to ${target ? `“${target.slice(0, 80)}”` : "a non-string target"}, absent from exported nodes.`, why: "This edge cannot reach its declared destination in this export.", repair: "Restore the destination or remove/reconnect the stale edge in n8n.", test: "Re-export and verify every referenced node exists." }));
          if (target && validNames.has(target)) { edges.push({ source, target, kind }); incoming.add(target); outgoing.set(source, (outgoing.get(source) || 0) + 1); }
        }
      });
    }
  }
  if (!nodes.length) findings.push(finding({ code: "EMPTY_WORKFLOW", title: "Workflow has no nodes", classification: "CONFIRMED_STRUCTURE", severity: "HIGH", confidence: "HIGH", evidence: "The exported nodes array is empty.", why: "There is no workflow graph to execute.", repair: "Add a trigger and intended workflow steps.", test: "Export the completed workflow and inspect a test execution." }));
  const triggers = validNodes.map((node, i) => ({ node, i })).filter(({ node }) => /trigger|webhook|schedule|cron|manualtrigger/i.test(String(node.type || "")));
  if (nodes.length && triggers.length === 0) findings.push(finding({ code: "NO_TRIGGER_CANDIDATE", title: "No trigger candidate recognized", classification: "INSUFFICIENT_EVIDENCE", severity: "MEDIUM", confidence: "LOW", affected: "Workflow", evidence: "No node type matched common trigger, webhook, schedule, or manual-trigger names.", why: "The workflow may use a trigger type this static check does not recognize, or may be intended as a sub-workflow.", repair: "Confirm this is a directly executable workflow and review its intended trigger or caller.", test: "Run the workflow using its intended trigger mechanism." }));
  const disconnected = validNodes.map((node, i) => ({ node, i })).filter(({ node, i }) => !incoming.has(String(node.name || "")) && !outgoing.has(String(node.name || "")) && !/trigger|webhook|schedule|cron|manualtrigger/i.test(String(node.type || "")));
  for (const { node, i } of disconnected) findings.push(finding({ code: "DISCONNECTED_NODE", title: "Node has no exported connections", classification: "CONFIRMED_STRUCTURE", severity: "MEDIUM", confidence: "HIGH", affected: nodeLabel(node, i), evidence: `“${nodeLabel(node, i)}” has no incoming or outgoing edge in the exported connections object.`, why: "This node is outside the connected path represented in the export.", repair: "Connect the node if it is intended to run, or remove it after confirming it is not a reusable/manual step.", test: "Inspect the workflow canvas and run the relevant branch." }));
  for (const [source, count] of outgoing) if (count === 0) findings.push(finding({ code: "DEAD_END", title: "Potential terminal branch", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "MEDIUM", affected: source.slice(0, 100), evidence: `The exported node “${source.slice(0, 100)}” has no outgoing connection.`, why: "It may be an intentional final action, but could also leave work incomplete without a visible recovery or completion step.", repair: "Confirm the node is an intended terminal action and check whether downstream acknowledgement or recovery is required.", test: "Verify the branch's expected final state and error behavior." }));
  // Terminal detection uses nodes that have incoming edges and no outgoing edges.
  const targets = new Set(edges.map((edge) => edge.target));
  for (const name of targets) if (!outgoing.has(name)) { const node = validNodes.find((item) => item.name === name); if (node && !findings.some((f) => f.code === "DEAD_END" && f.affected === name)) findings.push(finding({ code: "DEAD_END", title: "Potential terminal branch", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "MEDIUM", affected: name.slice(0, 100), evidence: `The connected node “${name.slice(0, 100)}” has no outgoing connection.`, why: "A terminal node may be correct; static structure cannot tell whether this is a complete business outcome.", repair: "Confirm this is an intended workflow endpoint and that upstream errors are handled.", test: "Check the expected final state for a successful execution." })); }
  for (const { node, i } of validNodes.map((node, i) => ({ node, i })).filter(({ node }) => node.disabled === true)) findings.push(finding({ code: "DISABLED_NODE", title: "Disabled node is present", classification: "CONFIRMED_STRUCTURE", severity: "MEDIUM", confidence: "HIGH", affected: nodeLabel(node, i), evidence: `The exported node “${nodeLabel(node, i)}” has disabled=true.`, why: "A disabled node will not perform its configured step when the workflow executes.", repair: "Confirm disabling is intentional and that connected paths remain valid without this node.", test: "Run a test with the node disabled and enabled if both states are expected." }));
  const graph = new Map(validNodes.map((node) => [node.name, []]));
  for (const edge of edges) if (graph.has(edge.source)) graph.get(edge.source).push(edge.target);
  const visitState = new Map(); let cycleDetected = false;
  for (const start of graph.keys()) {
    if (visitState.has(start)) continue;
    visitState.set(start, 1);
    const stack = [{ name: start, nextIndex: 0 }];
    while (stack.length) {
      const frame = stack[stack.length - 1]; const children = graph.get(frame.name) || [];
      if (frame.nextIndex >= children.length) { visitState.set(frame.name, 2); stack.pop(); continue; }
      const next = children[frame.nextIndex++]; const state = visitState.get(next);
      if (state === 1) { cycleDetected = true; continue; }
      if (!state) { visitState.set(next, 1); stack.push({ name: next, nextIndex: 0 }); }
    }
  }
  if (cycleDetected) findings.push(finding({ code: "CYCLE_PRESENT", title: "Cyclic connection path detected", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "MEDIUM", affected: "Workflow graph", evidence: "The exported connection graph contains a path that leads back to a node already on the inspected path.", why: "Cycles can be intentional, but static inspection cannot confirm that loop conditions terminate or that item counts remain bounded.", repair: "Confirm the cycle is intentional and has a clear exit condition or iteration bound.", test: "Test a normal loop, its exit condition, and a maximum-iteration or timeout guard." }));
  for (const { node, i } of validNodes.map((node, i) => ({ node, i }))) {
    const type = String(node.type || ""); const p = node.parameters && typeof node.parameters === "object" ? node.parameters : {};
    if (/httpRequest/i.test(type)) {
      if (!p.url) findings.push(finding({ code: "HTTP_URL_UNSET", title: "HTTP Request URL appears unset", classification: "POTENTIAL_RISK", severity: "HIGH", confidence: "MEDIUM", affected: nodeLabel(node, i), evidence: `The HTTP Request node “${nodeLabel(node, i)}” has no non-empty parameters.url in this export.`, why: "A URL may be supplied through a supported expression or imported default, but a missing endpoint can make the request unusable.", repair: "Review the URL and expression in n8n; do not paste secret values into shared exports.", test: "Test valid, timeout, non-2xx, and malformed-response cases." }));
      else if (typeof p.url === "string" && !p.url.startsWith("=") && !/^https:\/\//i.test(p.url)) findings.push(finding({ code: "HTTP_URL_SCHEME", title: "HTTP endpoint uses a non-HTTPS literal", classification: "POTENTIAL_RISK", severity: "MEDIUM", confidence: "MEDIUM", affected: nodeLabel(node, i), evidence: `The exported URL starts with “${p.url.slice(0, 24)}”.`, why: "Plain HTTP can expose request data in transit; private network endpoints may be intentional.", repair: "Confirm the destination and use HTTPS where supported and appropriate.", test: "Verify certificate handling and response behavior in a safe environment." }));
      findings.push(finding({ code: "HTTP_REVIEW", title: "Review HTTP integration resilience", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "LOW", affected: nodeLabel(node, i), evidence: `Static definition contains HTTP Request node “${nodeLabel(node, i)}”; runtime responses are unavailable.`, why: "Timeouts, throttling, response changes, or upstream outages can interrupt this branch.", repair: "Review timeout, retry/backoff, status handling, response validation, and any idempotency key for the target API.", test: "Simulate success, timeout, 429/5xx, duplicate input, and unexpected response shape." }));
      if (p.retryOnFail !== true) findings.push(finding({ code: "RETRY_REVIEW", title: "Review retry and idempotency behavior", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "LOW", affected: nodeLabel(node, i), evidence: `The export does not set retryOnFail=true on “${nodeLabel(node, i)}”; workflow-level settings and external retry behavior are not known.`, why: "Transient failures may not retry, while retries around side effects can duplicate work without idempotency safeguards.", repair: "Choose bounded retry/backoff behavior for this request and confirm downstream operations are idempotent or deduplicated.", test: "Simulate a transient failure followed by recovery and check that side effects occur the intended number of times." }));
    }
    if (hasNonEmptySecretValue(p) || hasNonEmptySecretValue(node.credentials)) findings.push(finding({ code: "POSSIBLE_EMBEDDED_SECRET", title: "Possible credential-like value in node configuration", classification: "POTENTIAL_RISK", severity: "HIGH", confidence: "MEDIUM", affected: nodeLabel(node, i), evidence: `A non-empty value under a sensitive-looking configuration key appears in “${nodeLabel(node, i)}”; the value is intentionally not repeated in the report.`, why: "Exported credentials or sensitive settings can be exposed when a workflow file or report is shared.", repair: "Review the field locally. Rotate any confirmed exposed secret and use n8n credentials or an appropriate secret store. Review reports before sharing.", test: "Re-export using credential references and verify no secret values remain in the file." }));
    if (/code|function/i.test(type)) findings.push(finding({ code: "CODE_NODE_INPUT", title: "Review assumptions in code-node inputs", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "LOW", affected: nodeLabel(node, i), evidence: `Node type “${type.slice(0, 120)}” can process workflow data; code and expressions were not executed or parsed.`, why: "Unexpected, missing, or differently shaped input items can cause errors or unsafe downstream assumptions.", repair: "Review input validation and avoid relying on fields without checking their presence and type.", test: "Try missing fields, null values, unexpected arrays, and normal inputs." }));
    if (/if|switch|filter/i.test(type) && p.conditions) findings.push(finding({ code: "BRANCH_REVIEW", title: "Review branch coverage", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "LOW", affected: nodeLabel(node, i), evidence: `Branching node “${nodeLabel(node, i)}” has configured conditions; condition semantics were not evaluated.`, why: "Unexpected input values can take an untested branch or no branch.", repair: "Review fallthrough and add a deliberate route for invalid or unrecognized input when needed.", test: "Test each branch and an input that matches none of the configured conditions." }));
  }
  // Static node properties only: do not assert missing recovery as an issue without execution evidence.
  if (validNodes.length && !validNodes.some((node) => /errorTrigger/i.test(String(node.type || ""))) && !Object.values(workflow.connections).some((outputs) => outputs && typeof outputs === "object" && Object.hasOwn(outputs, "error"))) findings.push(finding({ code: "RECOVERY_NOT_VISIBLE", title: "No explicit recovery path recognized", classification: "INSUFFICIENT_EVIDENCE", severity: "INFO", confidence: "LOW", affected: "Workflow", evidence: "No error-trigger node or connection output named error was recognized in the export.", why: "n8n workflow settings or external monitoring may handle failures outside this graph.", repair: "Confirm how failures are surfaced and recovered; consider an error workflow or alert if needed.", test: "Cause a safe test failure and verify an operator can detect and recover it." }));
  for (const node of validNodes) { const p = node.parameters && typeof node.parameters === "object" ? node.parameters : {}; if (/webhook/i.test(String(node.type || ""))) findings.push(finding({ code: "DUPLICATE_REVIEW", title: "Review duplicate-event handling", classification: "POTENTIAL_RISK", severity: "LOW", confidence: "LOW", affected: nodeLabel(node, nodes.indexOf(node)), evidence: `Webhook trigger “${nodeLabel(node, nodes.indexOf(node))}” is present; the static export does not establish whether callers retry events or downstream writes are idempotent.`, why: "Repeated delivery can create duplicate work when a sender retries after timeouts.", repair: "Review event identifiers, deduplication, and idempotency for side-effecting steps.", test: "Submit the same event twice and verify the intended single or repeat-safe outcome." })); }
  const severityRank = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4 };
  findings.sort((a, b) => severityRank[a.severity] - severityRank[b.severity] || a.title.localeCompare(b.title));
  const nodeCount = nodes.length; const connectionCount = edges.length;
  const labelByType = new Map();
  for (const node of validNodes) { const type = String(node.type || "unknown"); labelByType.set(type, (labelByType.get(type) || 0) + 1); }
  return { schema: "ELYREON_WORKFLOW_RESCUE_REPORT/1", generatedAt: new Date().toISOString(), workflowName: asText(workflow.name, 160) || "(unnamed workflow)", schemaVariant: variant, summary: { nodeCount, connectionCount, triggerTypes: [...new Set(triggers.map(({ node }) => String(node.type || "unknown")))], nodeTypes: Object.fromEntries(labelByType), findingCounts: Object.fromEntries(["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].map((s) => [s, findings.filter((f) => f.severity === s).length])) }, findings, checksPerformed: ["export shape and supported schema", "duplicate node names and IDs", "connection source and target references", "disconnected nodes and terminal branches", "trigger candidates and disabled nodes", "HTTP Request configuration cues", "possible embedded secret patterns without repeating values", "code-node and branch input assumptions", "visible recovery and duplicate-processing cues"], limitations: ["Static export analysis only; no workflow code or expressions were executed.", "No runtime errors, API behavior, credentials, or external state were inspected.", "Potential risks and insufficient-evidence findings require human review.", "A finding's absence does not establish that a reliability control exists."] };
}


export { analyzeWorkflow, normalizeWorkflow };
