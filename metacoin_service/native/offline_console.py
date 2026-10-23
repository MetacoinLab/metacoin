"""Offline operator console (order 2026-10-23 §21 extension 5): renders a signed export (GET /native/export) into one static HTML
page with no live API: the independent checker runs under the supplied roots, every section carries the export's commit sequence
and the age of the data at render time (freshness is explicit: "data as of"), and nothing on the page can submit anything.

    python -m metacoin_service.native.offline_console EXPORT.json --roots ROOTS.json --out page.html"""
import argparse
import html
import json
import sys
import time

from . import checker


def esc(x):
    return html.escape(str(x))


def table(rows, cols):
    if not rows:
        return '<p class="note">none</p>'
    return '<div class="tablewrap"><table><tr>' + ''.join('<th>%s</th>' % esc(c) for c in cols) + '</tr>' + ''.join('<tr>' + ''.join('<td>%s</td>' % esc(r.get(c, '')) for c in cols) + '</tr>' for r in rows) + '</table></div>'


def render(export, roots, now=None):
    now = now or int(time.time()); rep = checker.check(export, roots); dom = export.get('domain') or {}
    if not isinstance(dom, dict):
        dom = {'id': dom, 'commit_sequence': export.get('commit_sequence') or export.get('sequence'), 'authority': export.get('authority') or {}, 'default_asset': export.get('default_asset')}
    seq = dom.get('commit_sequence') or export.get('commit_sequence'); exported_at = export.get('exported_at') or export.get('generated_at') or export.get('created_at') or dom.get('updated_at')
    age = (now - exported_at) if isinstance(exported_at, int) else None; fresh = 'data as of commit sequence %s, exported %s (%s)' % (seq, exported_at, ('%d s old at render time' % age) if age is not None else 'export time unknown')
    accts = [{'account': a.get('id'), 'kind': a.get('kind'), 'label': a.get('label'), 'custody': a.get('custody'), 'state': a.get('state')} for a in export.get('accounts', [])]
    pools = [{'pool': p.get('id'), 'kind': p.get('kind'), 'label': p.get('label'), 'state': p.get('state'), 'schedule': json.dumps(p.get('schedule'))} for p in export.get('resource_pools', [])]
    commits = [{'pool': c.get('pool_id'), 'reservation': c.get('reservation_id'), 'start': c.get('start'), 'end': c.get('end'), 'slots': c.get('slots'), 'state': c.get('state')} for c in export.get('pool_commitments', [])]
    lots = [{'lot': l.get('lot_id'), 'origin': l.get('origin_allocation'), 'owner': l.get('owner_account'), 'amount': l.get('amount'), 'status': l.get('status'), 'transferred': l.get('transferred')} for l in export.get('claim_lots', [])]
    obls = [{'obligation': o.get('id'), 'kind': o.get('kind'), 'payer': o.get('payer_account'), 'payee': o.get('payee_account'), 'amount': o.get('amount'), 'settled': o.get('settled_amount'), 'state': o.get('state')} for o in export.get('obligations', [])]
    allocs = [{'allocation': a.get('id'), 'direction': a.get('direction'), 'amount': a.get('amount'), 'returned': a.get('returned_amount'), 'state': a.get('state'), 'peer': a.get('destination_domain') or a.get('source_domain')} for a in export.get('allocations', [])]
    props = [{'property': k, 'ok': v.get('ok'), 'scope': v.get('scope', ''), 'failed_checks': ', '.join(c['check'] for c in v.get('checks', []) if not c.get('ok'))[:200]} for k, v in rep['properties'].items()]
    page = ['<!doctype html><html><head><meta charset="utf-8"><title>MetaCoin offline console</title><meta name="viewport" content="width=device-width, initial-scale=1">',
            '<style>body{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#111}.tablewrap{overflow-x:auto}table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}.fresh{background:#fff3cd;padding:8px;border:1px solid #e0c060}.note{color:#555}h2{margin-top:24px}</style></head><body>',
            '<h1>MetaCoin offline console</h1><p class="fresh"><b>Freshness:</b> %s. This page is rendered from a signed export only; it has no live connection, cannot submit anything and shows nothing newer than the export.</p>' % esc(fresh),
            '<p>Domain <code>%s</code> · environment <code>%s</code> · asset <code>%s</code> · authority key <code>%s</code> · roots supplied for: %s</p>' % (esc(dom.get('id')), esc(export.get('environment')), esc(export.get('default_asset') or dom.get('default_asset')), esc((dom.get('authority') or {}).get('key_id', ''))[:16], esc(', '.join(sorted(roots)) or 'none (signatures cannot be verified)')),
            '<h2>Independent checker (under the supplied roots)</h2>' + table(props, ['property', 'ok', 'scope', 'failed_checks']),
            '<h2>Accounts</h2>' + table(accts, ['account', 'kind', 'label', 'custody', 'state']), '<h2>Resource pools</h2>' + table(pools, ['pool', 'kind', 'label', 'state', 'schedule']), '<h2>Pool commitments</h2>' + table(commits, ['pool', 'reservation', 'start', 'end', 'slots', 'state']),
            '<h2>Claim lots</h2>' + table(lots, ['lot', 'origin', 'owner', 'amount', 'status', 'transferred']), '<h2>Obligations</h2>' + table(obls, ['obligation', 'kind', 'payer', 'payee', 'amount', 'settled', 'state']), '<h2>Allocations</h2>' + table(allocs, ['allocation', 'direction', 'amount', 'returned', 'state', 'peer']),
            '<p class="note">Rendered %s by metacoin_service.native.offline_console; checker schema %s.</p></body></html>' % (esc(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))), esc(rep.get('schema', '')))]
    return '\n'.join(page), rep


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-offline-console'); ap.add_argument('export'); ap.add_argument('--roots', required=True); ap.add_argument('--out', required=True); a = ap.parse_args(argv)
    export = json.load(open(a.export)); roots = json.load(open(a.roots)); page, rep = render(export, roots)
    with open(a.out, 'w') as f:
        f.write(page)
    print(json.dumps({'out': a.out, 'bytes': len(page), 'checker': rep.get('summary')})); return 0


if __name__ == '__main__':
    sys.exit(main())
