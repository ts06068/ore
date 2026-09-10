import asyncio,json,os
from pathlib import Path
from playwright.async_api import async_playwright
async def main():
 root=Path('.ore').resolve();os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(root/'browsers')
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,env=dict(os.environ,LD_LIBRARY_PATH=str(root/'browser-libs/usr/lib/x86_64-linux-gnu')))
  for name,url in [('elsevier-register','https://dev.elsevier.com/apikey/manage'),('clarivate-register','https://developer.clarivate.com/signup')]:
   page=await browser.new_page()
   try:
    r=await page.goto(url,wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(2500)
    text=await page.locator('body').inner_text()
    elements=await page.locator('a,input,button,select').evaluate_all('els=>els.map(e=>({tag:e.tagName,text:(e.innerText||e.getAttribute("aria-label")||"").slice(0,200),name:e.name,type:e.type,url:e.href})).slice(0,100)')
    result={'name':name,'url':page.url.split('?')[0],'status':r.status,'text':text[:12000],'elements':elements}
    (root/'reports'/f'{name}.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
    await page.screenshot(path=root/'reports'/f'{name}.png')
   except Exception as e:print(name,type(e).__name__)
   await page.close()
  await browser.close()
asyncio.run(main())
