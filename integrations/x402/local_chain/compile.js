// Compile pinned contract sources with solcjs (WASM) for the LOCAL validation chain.
// usage: node compile.js <config.json>   (config: {jobs:[{name, solc, entry, contracts, settings, remappings, roots}]})
const fs = require('fs'); const path = require('path');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const job of cfg.jobs) {
  const solc = require(job.solc_module);
  function resolve(p) {
    for (const [prefix, target] of Object.entries(job.remappings)) {
      if (p.startsWith(prefix)) return path.join(target, p.slice(prefix.length));
    }
    for (const root of job.roots) { const c = path.join(root, p); if (fs.existsSync(c)) return c; }
    return null;
  }
  function findImports(p) {
    const f = resolve(p);
    if (f && fs.existsSync(f)) return { contents: fs.readFileSync(f, 'utf8') };
    return { error: 'not found: ' + p };
  }
  const sources = {};
  for (const [name, file] of Object.entries(job.entry)) sources[name] = { content: fs.readFileSync(file, 'utf8') };
  const input = { language: 'Solidity', sources, settings: Object.assign({ outputSelection: { '*': { '*': ['abi', 'evm.bytecode.object', 'evm.deployedBytecode.object', 'metadata'] } } }, job.settings) };
  const res = JSON.parse(solc.compile(JSON.stringify(input), { import: findImports }));
  const errors = (res.errors || []).filter(e => e.severity === 'error');
  if (errors.length) { console.error(JSON.stringify(errors, null, 1)); process.exit(2); }
  out[job.name] = { compiler: solc.version(), settings: job.settings, contracts: {} };
  for (const want of job.contracts) {
    for (const [src, cs] of Object.entries(res.contracts)) {
      if (cs[want]) out[job.name].contracts[want] = { source: src, abi: cs[want].abi, bytecode: cs[want].evm.bytecode.object, deployed: cs[want].evm.deployedBytecode.object };
    }
    if (!out[job.name].contracts[want]) { console.error('missing contract ' + want); process.exit(3); }
  }
}
fs.writeFileSync(cfg.out, JSON.stringify(out));
console.log(JSON.stringify(Object.fromEntries(Object.entries(out).map(([k, v]) => [k, { compiler: v.compiler, contracts: Object.keys(v.contracts) }]))));
