"""Optional API-backed agent decisions; never selected as an automatic billing fallback."""
from __future__ import annotations
import json
import httpx
from .codex import BackendError

DECISION_SCHEMA = {
    'type':'object',
    'properties':{'tool':{'type':'string'},'arguments':{'type':'string'},'reason':{'type':'string'}},
    'required':['tool','arguments','reason'],'additionalProperties':False,
}

class APIBackend:
    def __init__(self,kind,model,endpoint,key=None,effort=None):
        self.kind=kind;self.model=model;self.endpoint=endpoint.rstrip('/');self.key=key;self.effort=effort
        self.messages=[]
    async def decide(self,instructions,prompt,images=None, *, on_text_delta=None):
        async with httpx.AsyncClient(timeout=180) as client:
            if self.kind=='anthropic':
                content=[{'type':'text','text':prompt}]
                for image in images or []:
                    prefix,data=image.split(',',1)
                    content.append({'type':'image','source':{'type':'base64','media_type':prefix[5:].split(';')[0],'data':data}})
                self.messages.append({'role':'user','content':content})
                body={'model':self.model,'max_tokens':4096,'system':instructions,'messages':self.messages,
                    'tools':[{'name':'ore_decision','description':'Select the next allowed ORE action.','input_schema':DECISION_SCHEMA}],
                    'tool_choice':{'type':'tool','name':'ore_decision'}}
                if on_text_delta is not None:
                    return await self._stream(client, '/v1/messages', {'x-api-key':self.key or '', 'anthropic-version':'2023-06-01'}, body, on_text_delta)
                response=await client.post(self.endpoint+'/v1/messages',headers={'x-api-key':self.key or '', 'anthropic-version':'2023-06-01'},json=body)
                response.raise_for_status();result=response.json()
                tool=next(x for x in result['content'] if x['type']=='tool_use')
                self.messages.append({'role':'assistant','content':result['content']})
                self.messages.append({'role':'user','content':[{'type':'tool_result','tool_use_id':tool['id'],'content':'Decision received by ORE; the next observation will contain its execution result.'}]})
                return tool['input'],result.get('usage',{})
            headers={'Authorization':f'Bearer {self.key}'} if self.key else {}
            if self.kind=='openai':
                content=[{'type':'input_text','text':prompt}]+[{'type':'input_image','image_url':x} for x in images or []]
                self.messages.append({'role':'user','content':content})
                body={'model':self.model,'instructions':instructions,'input':self.messages,'store':False,
                      'text':{'format':{'type':'json_schema','name':'ore_decision','strict':True,'schema':DECISION_SCHEMA}}}
                if self.effort:body['reasoning']={'effort':self.effort}
                if on_text_delta is not None:
                    return await self._stream(client, '/v1/responses', headers, body, on_text_delta)
                response=await client.post(self.endpoint+'/v1/responses',headers=headers,json=body)
                response.raise_for_status();result=response.json()
                text=''.join(c.get('text','') for x in result.get('output',[]) if x.get('type')=='message' for c in x.get('content',[]) if c.get('type')=='output_text')
                decision=json.loads(text)
                # Keep only public decision content, not hidden reasoning or opaque provider state.
                self.messages.append({'role':'assistant','content':[{'type':'output_text','text':text}]})
                return decision,result.get('usage',{})
            content=[{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':x}} for x in images or []]
            self.messages.append({'role':'user','content':content})
            body={'model':self.model,'messages':[{'role':'system','content':instructions},*self.messages],
                  'response_format':{'type':'json_schema','json_schema':{'name':'ore_decision','strict':True,'schema':DECISION_SCHEMA}}}
            if on_text_delta is not None:
                return await self._stream(client, '/v1/chat/completions', headers, body, on_text_delta)
            response=await client.post(self.endpoint+'/v1/chat/completions',headers=headers,json=body)
            response.raise_for_status();result=response.json();message=result['choices'][0]['message']
            self.messages.append(message)
            return json.loads(message['content']),result.get('usage',{})


    async def _stream(self, client, path, headers, body, callback):
        """Stream only final decision text; provider reasoning is never forwarded."""
        text='';usage={};tool_id=None;tool_index=None;completed=False
        async with client.stream('POST',self.endpoint+path,headers=headers,json={**body,'stream':True}) as response:
            response.raise_for_status()
            async for event in _sse_events(response):
                if event.get('type') in ('error','response.failed') or event.get('error'):
                    raise BackendError('Provider stream failed')
                delta=''
                if self.kind=='openai':
                    if event.get('type')=='response.output_text.delta':delta=event.get('delta','')
                    if event.get('type')=='response.completed':
                        completed=True;usage=event.get('response',{}).get('usage',{})
                elif self.kind=='anthropic':
                    kind=event.get('type')
                    if kind=='content_block_start':
                        block=event.get('content_block',{})
                        if block.get('type')=='tool_use' and block.get('name')=='ore_decision':
                            tool_id=block.get('id');tool_index=event.get('index')
                    if kind=='content_block_delta' and event.get('index')==tool_index and event.get('delta',{}).get('type')=='input_json_delta':
                        delta=event['delta'].get('partial_json','')
                    if kind=='message_delta':
                        if event.get('delta',{}).get('stop_reason') in ('max_tokens','refusal'):raise BackendError('Provider stream stopped before a complete decision')
                        usage.update(event.get('usage',{}))
                    if kind=='message_start':usage.update(event.get('message',{}).get('usage',{}))
                    if kind=='message_stop':completed=True
                else:
                    for choice in event.get('choices',[]):
                        delta+=choice.get('delta',{}).get('content') or ''
                        if choice.get('finish_reason') is not None:
                            if choice['finish_reason']!='stop':raise BackendError('Provider stream stopped before a complete decision')
                            completed=True
                    usage.update(event.get('usage') or {})
                if delta:
                    text+=delta
                    if len(text.encode('utf-8'))>2_000_000:raise BackendError('Provider decision exceeded the stream limit')
                    callback(delta)
        if not completed:raise BackendError('Provider stream ended before completion')
        decision=json.loads(text)
        if self.kind=='anthropic':
            self.messages.append({'role':'assistant','content':[{'type':'tool_use','id':tool_id,'name':'ore_decision','input':decision}]})
            self.messages.append({'role':'user','content':[{'type':'tool_result','tool_use_id':tool_id,'content':'Decision received by ORE; the next observation will contain its execution result.'}]})
        elif self.kind=='openai':self.messages.append({'role':'assistant','content':[{'type':'output_text','text':text}]})
        else:self.messages.append({'role':'assistant','content':text})
        return decision,usage


async def _sse_events(response):
    """Parse SSE framing across arbitrary network/UTF-8 chunk boundaries."""
    data=[]
    async for line in response.aiter_lines():
        if line.startswith('data:'):data.append(line[5:].lstrip(' '))
        elif not line and data:
            value='\n'.join(data);data=[]
            if value!='[DONE]':yield json.loads(value)
    if data:
        value='\n'.join(data)
        if value!='[DONE]':yield json.loads(value)
