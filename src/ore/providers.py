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
    async def decide(self,instructions,prompt,images=None):
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
                response=await client.post(self.endpoint+'/v1/responses',headers=headers,json=body)
                response.raise_for_status();result=response.json()
                text=''.join(c.get('text','') for x in result.get('output',[]) if x.get('type')=='message' for c in x.get('content',[]) if c.get('type')=='output_text')
                decision=json.loads(text)
                # Keep only public decision content, not hidden reasoning or opaque provider state.
                self.messages.append({'role':'assistant','content':[{'type':'output_text','text':text}]})
                return decision,result.get('usage',{})
            content=[{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':x}} for x in images or []]
            self.messages.append({'role':'user','content':content})
            response=await client.post(self.endpoint+'/v1/chat/completions',headers=headers,json={
                'model':self.model,'messages':[{'role':'system','content':instructions},*self.messages],
                'response_format':{'type':'json_schema','json_schema':{'name':'ore_decision','strict':True,'schema':DECISION_SCHEMA}}})
            response.raise_for_status();result=response.json();message=result['choices'][0]['message']
            self.messages.append(message)
            return json.loads(message['content']),result.get('usage',{})
