// Offline harness: run one n8n Code node (mode "Run Once for All Items") or
// evaluate one n8n expression, with stubbed upstream node outputs.
//
// stdin : {"mode": "code"|"expression", "code"|"expression": "...",
//          "items": [{json}], "json": {...}, "nodes": {"Name": [[{json}], ...]}}
// stdout: {"result": ...}  |  exit 1 with {"error": "..."}
// No network, no credentials: it cannot reach Graph, n8n or a database.
const chunks = [];
process.stdin.on('data', chunk => chunks.push(chunk));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const nodes = request.nodes || {};
    const $items = (name, outputIndex = 0) => {
      if (!(name in nodes)) throw new Error(`harness: no stubbed output for node "${name}"`);
      return nodes[name][outputIndex] || [];
    };
    let result;
    if (request.mode === 'expression') {
      const text = String(request.expression || '');
      const match = /^=\{\{([\s\S]*)\}\}$/.exec(text.trim());
      if (!match) throw new Error('harness: not an n8n expression');
      result = new Function('$json', '$items', `return (${match[1]});`)(request.json || {}, $items);
    } else {
      result = new Function('items', '$items', 'Buffer', request.code)(
        request.items || [], $items, Buffer);
    }
    process.stdout.write(JSON.stringify({ result }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ error: String(error && error.message || error) }));
    process.exitCode = 1;
  }
});
