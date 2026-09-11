"""Verification dependencies are scoped subresources, never navigation grants."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from ore.policy import AccessDenied, AccessPolicy
from test_browser import browser_dependencies, site, make_manager

@pytest.mark.asyncio
async def test_support_origin_keeps_navigation_download_and_profile_restrictions():
    page='https://www.jacc.org/toc/jacc/83/1';host='https://challenges.cloudflare.com';url=host+'/turnstile/v0/api.js'
    mission={'allowed_origins':['https://www.jacc.org'],'scope':{'browser_support_origins':[host]}}
    profile={'allowed_hosts':['www.jacc.org','challenges.cloudflare.com']}
    policy=AccessPolicy(mission,profile)
    await policy.check_browser_request(url,top_level_url=page,is_top_level_navigation=False,resource_type='script')
    # An iframe may load a widget document, but an ordinary tab/popup cannot.
    await policy.check_browser_request(url,top_level_url=page,is_top_level_navigation=False,resource_type='document')
    with pytest.raises(AccessDenied):await policy.check_browser_request(url,top_level_url=page,is_top_level_navigation=True,resource_type='document')
    with pytest.raises(AccessDenied):await policy.check(url)
    with pytest.raises(AccessDenied):await AccessPolicy(mission,profile,operation='download').check(url)
    with pytest.raises(AccessDenied):await policy.check_browser_request(url,top_level_url='https://unrelated.example/',is_top_level_navigation=False,resource_type='script')
    strict=AccessPolicy(mission,{**profile,'origins':['https://www.jacc.org']})
    with pytest.raises(AccessDenied):await strict.check_browser_request(url,top_level_url=page,is_top_level_navigation=False,resource_type='script')
    blocked=AccessPolicy({**mission,'source_policy':{'exclude':{'browser':['general_web']}}},profile)
    with pytest.raises(AccessDenied):await blocked.check_browser_request(url,top_level_url=page,is_top_level_navigation=False,resource_type='script')
    private=AccessPolicy({'allowed_origins':['https://www.jacc.org'],'scope':{'browser_support_origins':['http://127.0.0.1']}},profile)
    with pytest.raises(AccessDenied,match='Private'):await private.check_browser_request('http://127.0.0.1/x',top_level_url=page,is_top_level_navigation=False,resource_type='script')

@pytest.mark.asyncio
async def test_support_origin_cannot_reclassify_api_operation():
    mission={'allowed_origins':['https://www.jacc.org'],'scope':{'browser_support_origins':['https://api.elsevier.com']},'source_policy':{'exclude':{'search':['scopus']}}}
    policy=AccessPolicy(mission,{'allowed_hosts':['www.jacc.org','api.elsevier.com']})
    with pytest.raises(AccessDenied):await policy.check_browser_request('https://api.elsevier.com/content/search/scopus?query=x',top_level_url='https://www.jacc.org/toc',is_top_level_navigation=False,resource_type='fetch')

@pytest.mark.browser
@pytest.mark.asyncio
async def test_real_browser_loads_only_configured_support_script(tmp_path,site,browser_dependencies):
    class Support(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            body=b'document.body.dataset.verifiedDependency="loaded";'
            self.send_response(200)
            if self.path=='/download':self.send_header('Content-Disposition','attachment; filename=not-authorized.js')
            self.send_header('Content-Type','text/javascript');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    helper=ThreadingHTTPServer(('127.0.0.1',0),Support);thread=threading.Thread(target=helper.serve_forever,daemon=True);thread.start()
    host=f'http://127.0.0.1:{helper.server_port}'
    manager,store,_,job,mission=make_manager(tmp_path,site)
    try:
        mission['scope']={'browser_support_origins':[host]}
        session=await manager.create(job['id'],mission,{'id':'public','allow_private_network':True})
        await manager.action(session.id,'navigate',{'url':site,'screenshot':False})
        await session.page.add_script_tag(url=host+'/widget.js')
        assert await session.page.get_attribute('body','data-verified-dependency')=='loaded'
        assert not (await manager.summary(session))['network_diagnostics']['blocked_requests']
        # Popups have their own main frame and must not receive the iframe grant.
        async with session.context.expect_page() as opened:
            await session.page.evaluate("url => window.open(url)",host+'/widget.js')
        popup=await opened.value
        for _ in range(50):
            if session.request_denials:break
            await asyncio.sleep(.02)
        assert any(x['url']==host+'/widget.js' for x in session.request_denials)
        await popup.close()
        # A support iframe cannot turn its subresource permission into a file.
        await session.page.evaluate("url => {let f=document.createElement('iframe');f.src=url;document.body.appendChild(f)}",host+'/download')
        for _ in range(100):
            if session.downloads:break
            await asyncio.sleep(.02)
        assert session.downloads and session.downloads[-1]['status']=='failed'
        assert session.downloads[-1]['error']=='AccessDenied'
        assert not list((manager.settings.state_dir/'staging').iterdir())

        with pytest.raises(AccessDenied):await manager.action(session.id,'navigate',{'url':host+'/widget.js','epoch':session.epoch})
        # An unconfigured dependency is denied and the operator gets the reason.
        with pytest.raises(Exception):await session.page.add_script_tag(url=host.replace('127.0.0.1','localhost')+'/widget.js')
        denied=(await manager.summary(session))['network_diagnostics']['blocked_requests']
        assert denied[-1]['reason'].startswith('Origin outside')
    finally:
        await manager.close();store.close();helper.shutdown();thread.join();helper.server_close()
