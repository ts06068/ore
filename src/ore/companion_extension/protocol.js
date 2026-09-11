export function parsePair(text){
 const p=JSON.parse(text),u=new URL(p.server);
 if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash||!['','/'].includes(u.pathname))throw Error('Use the ORE server origin only.');
 if(u.protocol==='http:'&&!['127.0.0.1','localhost','[::1]'].includes(u.hostname))throw Error('Remote connections require HTTPS, or use a local SSH tunnel.');
 if(!/^[a-f0-9]{32}$/.test(p.pair_id)||!/^[-_a-zA-Z0-9]{40,80}$/.test(p.token)||!Array.isArray(p.origins)||!p.origins.length)throw Error('Invalid pairing code.');
 if(!Number.isFinite(p.expires_at)||p.expires_at*1000<=Date.now())throw Error('Pairing code expired. Generate a new code in ORE.');
 for(const origin of p.origins){const x=new URL(origin);if(!['http:','https:'].includes(x.protocol)||x.origin!==origin)throw Error('Invalid mission origin.');}
 return {...p,server:u.origin,websocket:u.origin.replace(/^http/,'ws')+'/v1/companion/socket'};
}
export function allowed(url,origins){try{const u=new URL(url);return !u.username&&!u.password&&origins.includes(u.origin);}catch{return false;}}
export function publicError(error){return String(error?.message||error).slice(0,180).replace(/https?:\/\/\S+/g,'[URL]');}
