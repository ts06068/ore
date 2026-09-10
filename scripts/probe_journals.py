"""Small, non-authenticated browser probe; no bulk collection or credential logging."""
import asyncio,json,os
from pathlib import Path
from datetime import datetime,timezone
from playwright.async_api import async_playwright

async def main():
    root=Path('.ore').resolve();os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(root/'browsers')
    env=dict(os.environ,LD_LIBRARY_PATH=str(root/'browser-libs/usr/lib/x86_64-linux-gnu'))
    pages=[('jacc','https://www.jacc.org/loi/jacc'),('ehj','https://academic.oup.com/eurheartj/issue-archive'),('circulation','https://www.ahajournals.org/loi/circ'),('jama-cardiology','https://jamanetwork.com/journals/jamacardiology/currentissue'),('elsevier-onboarding','https://dev.elsevier.com/'),('clarivate-onboarding','https://developer.clarivate.com/')]
    out=root/'reports'/'journal-probe';out.mkdir(parents=True,exist_ok=True)
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,env=env)
        for ident,url in pages:
            context=await browser.new_context(viewport={'width':1280,'height':800});page=await context.new_page()
            try:
                response=await page.goto(url,wait_until='domcontentloaded',timeout=60000)
                await page.wait_for_timeout(3000)
                body=await page.locator('body').inner_text(timeout=10000)
                links=await page.locator('a').evaluate_all('els=>els.map(e=>({text:e.innerText.slice(0,150),url:e.href})).filter(e=>e.text).slice(0,100)')
                record={'id':ident,'url':url,'final_url':page.url,'http_status':response.status if response else None,'title':await page.title(),'text':body[:20000],'links':links,'time':datetime.now(timezone.utc).isoformat()}
                await page.screenshot(path=out/f'{ident}.png')
            except Exception as exc:record={'id':ident,'url':url,'error':type(exc).__name__,'message':str(exc)[:200]}
            results.append(record);(out/f'{ident}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
            print(json.dumps({k:v for k,v in record.items() if k not in ('text','links')}),flush=True)
            await context.close()
        await browser.close()
    (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
asyncio.run(main())
