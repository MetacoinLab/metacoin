# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""Reproducible build of the local validation contracts from PINNED upstream sources (never from a live chain):

  - Permit2 (Uniswap/permit2, MIT), solc 0.8.17, via_ir, optimizer runs 1000000 (the repository's own foundry profile)
  - x402UptoPermit2Proxy + MockGenericERC20 (coinbase/x402 contracts/evm, MIT/Apache-2.0), solc 0.8.28, optimizer runs 200
  - OpenZeppelin v5.1.0 and solmate as import dependencies

    python -m integrations.x402.local_chain.build --work /tmp/scratch/contracts --out integrations/x402/local_chain/artifacts.json

Sources are cloned at the recorded commits; solcjs (WASM builds of the pinned compilers) does the compilation, so the
result does not depend on a native solc for this CPU architecture. The bytecode is NOT byte-identical to the
canonical mainnet deployments (different toolchain, metadata settings): it is the same source at a pinned commit."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PINS = {
    'x402': ('https://github.com/coinbase/x402.git', 'dd927a26cfefc98c24b3ec38b3a8f204dad0c60d'),
    'permit2': ('https://github.com/Uniswap/permit2.git', 'cc56ad0f3439c502c246fc5cfcc3db92bb8b7219'),
    'openzeppelin': ('https://github.com/OpenZeppelin/openzeppelin-contracts.git', '69c8def5f222ff96f2b5beff05dfba996368aa79'),
    'solmate': ('https://github.com/transmissions11/solmate.git', '89365b880c4f3c786bdd453d4b8e8fe410344a69'),
}
SOLC = {'0.8.17': 'solc', '0.8.28': 'solc-0828'}


def clone(work, name):
    url, commit = PINS[name]
    d = work / (name + '-src')
    if not d.exists():
        subprocess.run(['git', 'clone', '-q', url, str(d)], check=True)
    subprocess.run(['git', '-C', str(d), 'checkout', '-q', commit], check=True)
    head = subprocess.check_output(['git', '-C', str(d), 'rev-parse', 'HEAD'], text=True).strip()
    assert head == commit, (name, head)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--work', required=True); ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    work = Path(a.work); work.mkdir(parents=True, exist_ok=True)
    srcs = {n: clone(work, n) for n in PINS}
    build = work / 'build'; build.mkdir(exist_ok=True)
    if not (build / 'node_modules' / 'solc').exists():
        subprocess.run(['npm', 'init', '-y'], cwd=build, check=True, capture_output=True)
        subprocess.run(['npm', 'install', '--silent', 'solc@0.8.17', 'solc-0828@npm:solc@0.8.28'], cwd=build, check=True)
    cfg = {'out': str(build / 'raw.json'), 'jobs': [
        {'name': 'permit2', 'solc_module': 'solc', 'entry': {'Permit2.sol': str(srcs['permit2'] / 'src' / 'Permit2.sol')}, 'contracts': ['Permit2'],
         'settings': {'optimizer': {'enabled': True, 'runs': 1000000}, 'viaIR': True, 'metadata': {'bytecodeHash': 'none'}},
         'remappings': {'solmate/': str(srcs['solmate']) + '/', 'openzeppelin-contracts/': str(srcs['openzeppelin']) + '/'}, 'roots': [str(srcs['permit2'] / 'src')]},
        {'name': 'x402', 'solc_module': 'solc-0828', 'entry': {'x402UptoPermit2Proxy.sol': str(srcs['x402'] / 'contracts' / 'evm' / 'src' / 'x402UptoPermit2Proxy.sol'),
                                                             'MockGenericERC20.sol': str(srcs['x402'] / 'contracts' / 'evm' / 'src' / 'mocks' / 'MockGenericERC20.sol')},
         'contracts': ['x402UptoPermit2Proxy', 'MockGenericERC20'], 'settings': {'optimizer': {'enabled': True, 'runs': 200}, 'viaIR': False, 'metadata': {'bytecodeHash': 'none'}},
         'remappings': {'@openzeppelin/contracts/': str(srcs['openzeppelin'] / 'contracts') + '/', './': str(srcs['x402'] / 'contracts' / 'evm' / 'src') + '/'},
         'roots': [str(srcs['x402'] / 'contracts' / 'evm' / 'src')]},
    ]}
    (build / 'config.json').write_text(json.dumps(cfg))
    t0 = time.time()
    subprocess.run(['node', str(Path(__file__).parent / 'compile.js'), str(build / 'config.json')], cwd=build, check=True, env=dict(os.environ, NODE_PATH=str(build / 'node_modules')))
    raw = json.loads((build / 'raw.json').read_text())
    record = {'schema': 'metacoin-local-chain-artifacts/v1', 'built_at': int(time.time()), 'build_seconds': round(time.time() - t0, 1), 'pins': {n: {'url': u, 'commit': c} for n, (u, c) in PINS.items()},
              'toolchain': {n: raw[n]['compiler'] for n in raw}, 'node': subprocess.check_output(['node', '--version'], text=True).strip(),
              'note': 'same pinned source, WASM solc; bytecode differs from the canonical CREATE2 deployments (metadata/toolchain); local validation only, never a public network',
              'contracts': {}}
    for job in raw:
        for name, c in raw[job]['contracts'].items():
            record['contracts'][name] = {'job': job, 'compiler': raw[job]['compiler'], 'settings': raw[job]['settings'], 'source': c['source'], 'abi': c['abi'], 'bytecode': c['bytecode'],
                                         'deployed_sha256': hashlib.sha256(bytes.fromhex(c['deployed'])).hexdigest(), 'bytecode_sha256': hashlib.sha256(bytes.fromhex(c['bytecode'])).hexdigest()}
    Path(a.out).write_text(json.dumps(record))
    print(json.dumps({k: {'bytecode_bytes': len(v['bytecode']) // 2, 'compiler': v['compiler']} for k, v in record['contracts'].items()}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
