const status=document.querySelector('#status'),code=document.querySelector('#code');
async function send(action){const result=await chrome.runtime.sendMessage({action,code:action==='connect'?code.value:undefined});status.textContent=result.error||result.status;if(!result.error&&action==='connect')code.value='';}
document.querySelector('#connect').onclick=()=>void send('connect').catch(e=>status.textContent=e.message);
document.querySelector('#disconnect').onclick=()=>void send('disconnect');
void send('status');

setInterval(()=>void send('status').catch(()=>{}),1000);
