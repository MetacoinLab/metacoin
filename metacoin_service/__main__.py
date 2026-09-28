"""python -m metacoin_service <command> — operator entry points."""
import argparse
import json
import os
import signal
import sys
from pathlib import Path
from .config import Settings
from .errors import ServiceError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='metacoin_service', description=__doc__)
    parser.add_argument('--home', help='service home directory (default $METACOIN_SERVICE_HOME or ~/.local/state/metacoin-service)')
    parser.add_argument('--provider-mode', choices=('simulation', 'test-http', 'production'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='initialize a fresh service home (keys, database, principals, one-time credentials)')
    serve = sub.add_parser('serve', help='start the API + console (loopback)')
    serve.add_argument('--port', type=int)
    serve.add_argument('--host')
    serve.add_argument('--tls', action='store_true', help='serve over TLS with the local node trust domain certificate (keys/node-tls); nodes pin ca.pem')
    nw = sub.add_parser('node-worker', help='run a federated worker node against a coordinator (separate home, no database access)')
    nw.add_argument('--identity', required=True); nw.add_argument('--coordinator', required=True); nw.add_argument('--ca'); nw.add_argument('--node-home', required=True)
    nw.add_argument('--once', action='store_true'); nw.add_argument('--stop-file'); nw.add_argument('--compute-python')
    sub.add_parser('node-tls', help='create (once) the local node trust domain CA and server certificate; prints the CA path for nodes to pin')
    work = sub.add_parser('worker', help='start a background worker')
    work.add_argument('--once', action='store_true')
    work.add_argument('--name', help='worker name shown in the queue view')
    work.add_argument('--capabilities', help='comma-separated job kinds this worker runs (default: all installed kinds)')
    work.add_argument('--stop-file')
    sub.add_parser('status', help='queue, review, payment and chain status')
    sub.add_parser('health', help='probe a running API over HTTP')
    sub.add_parser('migrate', help='apply pending schema migrations')
    bk = sub.add_parser('backup'); bk.add_argument('dest'); bk.add_argument('--include-keys', action='store_true')
    rs = sub.add_parser('restore'); rs.add_argument('backup_dir'); rs.add_argument('--keys-dir')
    sub.add_parser('clear-reconciliation-gate', help='after reconciling restored payment state')
    cl = sub.add_parser('cleanup', help='retention cleanup of expired private payloads'); cl.add_argument('--at', type=int)
    cr = sub.add_parser('credentials'); cr.add_argument('action', choices=('path', 'rotate', 'revoke')); cr.add_argument('id', nargs='?')
    ky = sub.add_parser('reviewer-key'); ky.add_argument('action', choices=('rotate', 'revoke')); ky.add_argument('id')
    sub.add_parser('openapi', help='print the API schema')
    args = parser.parse_args(argv)
    overrides = {}
    if args.home:
        overrides['home'] = Path(args.home)
    if args.provider_mode:
        overrides['provider_mode'] = args.provider_mode
    try:
        settings = Settings.from_env(**overrides)
        result = run(args, settings)
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
    except ServiceError as exc:
        print(json.dumps(exc.body()), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(json.dumps({'error': True, 'code': 'CONFIGURATION', 'detail': str(exc)}), file=sys.stderr)
        return 2


def run(args, settings):
    from . import ops
    if args.command == 'init':
        from . import bootstrap
        return bootstrap.init(settings)
    if args.command == 'serve':
        import uvicorn
        from .api import create_app
        if args.port:
            settings.port = args.port
        if args.host:
            settings.host = args.host
        settings.validate()
        settings.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (settings.run_dir / 'api.pid').write_text(str(os.getpid()))
        tls = {}
        if args.tls:
            from .federation.tls import ensure_node_tls
            t = ensure_node_tls(settings)
            tls = {'ssl_certfile': t['cert'], 'ssl_keyfile': t['key']}
        try:
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level='warning', access_log=False, **tls)
        finally:
            try:
                (settings.run_dir / 'api.pid').unlink()
            except FileNotFoundError:
                pass
        return None
    if args.command == 'worker':
        from .artifacts import ArtifactStore
        from .db import Database
        from .worker import Worker
        caps = [c for c in (args.capabilities or '').split(',') if c] or None
        worker = Worker(Database(settings.db_path), ArtifactStore(settings), settings, name=args.name, capabilities=caps)
        if args.once:
            try:
                advanced_before = worker.tick_workflows()                 # one scheduler tick (workflows + campaigns), one job, one more tick
                ran = worker.run_once()
                advanced_after = worker.tick_workflows()
                return {'worker_id': worker.worker_id, 'ran': ran, 'capabilities': worker.capabilities, 'scheduler_ticks': [advanced_before, advanced_after]}
            finally:
                worker.offline()
        settings.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (settings.run_dir / 'worker.pid').write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            worker.run_forever(stop_file=args.stop_file)
        finally:
            try:
                (settings.run_dir / 'worker.pid').unlink()
            except FileNotFoundError:
                pass
        return None
    if args.command == 'node-worker':
        from .federation import node_worker
        argv = ['--identity', args.identity, '--coordinator', args.coordinator, '--home', args.node_home]
        if args.ca: argv += ['--ca', args.ca]
        if args.once: argv += ['--once']
        if args.stop_file: argv += ['--stop-file', args.stop_file]
        if args.compute_python: argv += ['--compute-python', args.compute_python]
        return node_worker.main(argv) and None
    if args.command == 'node-tls':
        from .federation.tls import ensure_node_tls
        return ensure_node_tls(settings)
    if args.command == 'status':
        return ops.status(settings)
    if args.command == 'health':
        import urllib.request
        with urllib.request.urlopen('http://' + settings.host + ':' + str(settings.port) + '/api/health', timeout=5) as r:
            return json.loads(r.read())
    if args.command == 'migrate':
        from . import db
        return {'applied': db.migrate(settings.db_path), 'schema': db.schema_version(settings.db_path)}
    if args.command == 'backup':
        return ops.backup(settings, args.dest, include_keys=args.include_keys)
    if args.command == 'restore':
        return ops.restore(args.backup_dir, settings, keys_dir=args.keys_dir)
    if args.command == 'clear-reconciliation-gate':
        ops.clear_gate(settings)
        return {'reconciliation_gate': False}
    if args.command == 'cleanup':
        return ops.cleanup(settings, at=args.at)
    if args.command == 'credentials':
        if args.action == 'path':
            return {'credential_file': str(settings.credentials_dir / 'bootstrap.json'), 'permissions': '0600',
                    'note': 'private; values are never printed by this tool'}
        if args.action == 'rotate':
            return ops.rotate_credential(settings, args.id)
        ops.revoke_credential(settings, args.id)
        return {'revoked': args.id}
    if args.command == 'reviewer-key':
        if args.action == 'rotate':
            return {'new_key_id': ops.rotate_reviewer_key(settings, args.id)}
        ops.revoke_reviewer_key(settings, args.id)
        return {'revoked': args.id}
    if args.command == 'openapi':
        from .api import create_app
        return create_app(settings).openapi()


if __name__ == '__main__':
    sys.exit(main())
