"""HTTP routes of the lunar products under /api/v1/lunar/*."""
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool


def mount(app, svc, run, read_body, API):
    L = svc.lunar

    @app.get(API + '/lunar/sources')
    async def lunar_sources(request: Request):
        return await run(request, False, lambda db, p: L.sources(db, p))

    @app.get(API + '/lunar/examples/{name}')
    async def lunar_example(request: Request, name: str):
        return await run(request, False, lambda db, p: L.example(db, p, name, request.query_params.get('source', 'apollo11_32')))

    @app.get(API + '/lunar/products')
    async def lunar_products(request: Request):
        return await run(request, False, lambda db, p: {'items': L.list(db, p, request.query_params.get('kind'))})

    @app.get(API + '/lunar/products/{jid}')
    async def lunar_product(request: Request, jid: str):
        return await run(request, False, lambda db, p: L.view(db, p, jid))

    def _binary(request, fn):
        def sync():
            with svc.db.read() as db:
                from ..api import principal_of
                p = principal_of(request, db, False)
                return fn(db, p)
        return run_in_threadpool(sync)

    @app.get(API + '/lunar/products/{jid}/preview.png')
    async def lunar_preview(request: Request, jid: str):
        data = await _binary(request, lambda db, p: L.preview_png(db, p, jid))
        return Response(content=data, media_type='image/png')

    @app.get(API + '/lunar/products/{jid}/download')
    async def lunar_download(request: Request, jid: str):
        data, media, name = await _binary(request, lambda db, p: L.download(db, p, jid))
        return Response(content=data, media_type=media, headers={'Content-Disposition': 'attachment; filename="%s"' % name})
