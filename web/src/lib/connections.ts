/** Only explicit setup commands bypass model configuration. Collection stays with the planner. */
export function isConnectionSetupMessage(value:string):boolean{
 const text=value.trim();
 if(!text||text.length>2000)return false;
 if(/\b(?:collect\w*|retriev\w*|download\w*|scrap(?:e|ing))\b|\bfind\b[\s\S]{0,80}\b(?:articles?|papers?|studies)\b|\bsearch\s+for\b|수집|검색해|검색하|찾아|다운로드/i.test(text))return false;
 const provider=/(?<![a-z0-9_])(?:codex|chatgpt|claude(?:[ -]code)?|openai|anthropic|scopus|elsevier|pubmed|ncbi|cross[ -]?ref|clarivate|wos|web of science)(?![a-z0-9_])|코덱스|챗지피티|클로드|오픈에이아이|앤트로픽|스코퍼스|엘스비어|펍메드|퍼브메드|크로스레프|클래리베이트/i;
 const setup=/\b(?:connect|login|log[ -]?in|sign[ -]?in|setup|set up|register|enroll|authenticate)\b|\b(?:create|request|issue|obtain|get|generate|add|save|enter|apply for|open)\b[\s\S]{0,45}\b(?:account|api[ -]?key|access[ -]?key|token)\b|로그인|연결|설정|가입|발급|신청|등록|입력/i;
 return provider.test(text)&&setup.test(text);
}
