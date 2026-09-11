"""Actual chunk decoders, privacy boundaries, Unicode and durable partial replies."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ore.codex import CodexBackend
from ore.providers import APIBackend
from ore.public_stream import PublicDecisionStream, public_text


def frame(tool, **args):
    return json.dumps({'tool':tool,'arguments':json.dumps(args,ensure_ascii=True),'reason':'PRIVATE_REASON_NEVER_PUBLIC'},ensure_ascii=True)


@pytest.mark.parametrize('tool,field',[('respond','content'),('ask','content'),('propose_plan','summary')])
@pytest.mark.parametrize('width',[1,2,7,31])
def test_stream_decodes_only_public_fields_with_split_escapes(tool,field,width):
    content='한국어 😀 and a quoted "value" survive every boundary.\nSecond line with enough words to stream before completion.'
    raw=frame(tool,**{field:content,'private':'PRIVATE_TOOL_ARGS'})
    chunks=[];decoder=PublicDecisionStream(lambda delta,*_:chunks.append(delta))
    for offset in range(0,len(raw),width):decoder.feed(raw[offset:offset+width])
    assert ''.join(chunks)==content
    assert len(chunks)>1
    assert all('PRIVATE_' not in text for text in chunks)


@pytest.mark.parametrize('tool',['inspect','pause_for_change','execute','approve_plan'])
def test_nonreply_tools_never_emit_arguments_or_reason(tool):
    emitted=[];decoder=PublicDecisionStream(lambda delta,*_:emitted.append(delta))
    for char in frame(tool,content='PRIVATE_TOOL_ARGS',summary='PRIVATE_SUMMARY'):decoder.feed(char)
    assert not emitted


def test_credentials_are_masked_across_every_character_boundary():
    content='Public explanation starts here. Authorization: Bearer SECRET123456789 password="PRIVATE PASSWORD" api_key=APISECRET987 and https://user:pass@example.org/file?token=URLSECRET&x=1 ordinary safe words finish the answer.'
    chunks=[];decoder=PublicDecisionStream(lambda delta,*_:chunks.append(delta))
    for char in frame('respond',content=content):
        decoder.feed(char)
        emitted=''.join(chunks)
        assert not any(secret in emitted for secret in ['SECRET123','PRIVATE','APISECRET','URLSECRET','user:pass'])
    assert ''.join(chunks)==public_text(content)
    assert '[redacted]' in ''.join(chunks)


def test_final_only_provider_is_not_simulated_as_streaming():
    chunks=[];decoder=PublicDecisionStream(lambda delta,*_:chunks.append(delta))
    decoder.finish(json.loads(frame('respond',content='Entire final response.')))
    assert chunks==[]


class BytesStream(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data;self.closed=False
    async def __aiter__(self):
        try:
            for offset in range(0,len(self.data),3):
                yield self.data[offset:offset+3]
                await asyncio.sleep(0)
        finally:self.closed=True


@pytest.mark.parametrize('kind',['openai','anthropic','local'])
async def test_api_actual_sse_stream_filters_reasoning_and_decodes_utf8(monkeypatch,kind):
    decision=json.dumps({'tool':'respond','arguments':json.dumps({'content':'한국어 public answer with sufficient words, published as real HTTP deltas.'},ensure_ascii=False),'reason':'PRIVATE_REASON_NEVER_PUBLIC'},ensure_ascii=False)
    pieces=[decision[:45],decision[45:95],decision[95:]]
    events=[]
    if kind=='openai':
        events=[{'type':'response.reasoning_text.delta','delta':'PROVIDER_COT_DISTINCT'}]
        events += [{'type':'response.output_text.delta','delta':piece} for piece in pieces]
        events += [{'type':'response.completed','response':{'usage':{'output_tokens':12}}}]
    elif kind=='anthropic':
        events=[{'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'PROVIDER_COT_DISTINCT'}},
                {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','name':'ore_decision','id':'call1'}}]
        events += [{'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':piece}} for piece in pieces]
        events += [{'type':'message_stop'}]
    else:
        events=[{'choices':[{'delta':{'reasoning_content':'PROVIDER_COT_DISTINCT'}}]}]
        events += [{'choices':[{'delta':{'content':piece}}]} for piece in pieces]
        events += [{'choices':[{'delta':{},'finish_reason':'stop'}]}]
    stream=BytesStream(''.join('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events).encode())
    def transport(request):
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200,stream=stream,headers={'content-type':'text/event-stream'})
    client_class=httpx.AsyncClient
    monkeypatch.setattr('ore.providers.httpx.AsyncClient',lambda **kwargs:client_class(transport=httpx.MockTransport(transport),**kwargs))
    chunks=[];backend=APIBackend(kind,'model','https://fixture.test','fixture-key')
    def receive(chunk):
        assert not stream.closed
        chunks.append(chunk)
    result,_=await backend.decide('instructions','prompt',on_text_delta=receive)
    assert result==json.loads(decision)
    assert ''.join(chunks)==decision
    assert 'PROVIDER_COT_DISTINCT' not in ''.join(chunks)
    public=[];decoder=PublicDecisionStream(lambda delta,*_:public.append(delta))
    for chunk in chunks:decoder.feed(chunk)
    assert 'PRIVATE_REASON' not in ''.join(public) and '한국어' in ''.join(public)


async def test_codex_transport_routes_final_text_only_and_preserves_completion():
    backend=CodexBackend(binary='/not/executed')
    reader=asyncio.StreamReader();backend.process=SimpleNamespace(stdout=reader)
    chunks=[];backend._text_handlers['thread']=chunks.append
    future=asyncio.get_running_loop().create_future();backend.turns['thread']=future
    events=[('item/started',{'item':{'id':'comment','type':'agentMessage','phase':'commentary'}}),
            ('item/agentMessage/delta',{'itemId':'comment','delta':'PRIVATE_COMMENTARY'}),
            ('item/reasoning/textDelta',{'delta':'PROVIDER_COT_DISTINCT'}),
            ('item/started',{'item':{'id':'final','type':'agentMessage','phase':'final_answer'}}),
            ('item/agentMessage/delta',{'itemId':'final','delta':'public-final'}),
            ('turn/completed',{'turn':{'id':'turn1','status':'completed'}})]
    for method,params in events:reader.feed_data((json.dumps({'method':method,'params':{'threadId':'thread',**params}})+'\n').encode())
    reader.feed_eof();await backend._read()
    assert chunks==['public-final'] and future.done()


async def test_api_interruption_closes_underlying_response_stream(monkeypatch):
    entered=asyncio.Event();closed=asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b'data: {"type":"response.output_text.delta","delta":"{\\"tool\\": \\"respond\\""}\n\n'
                entered.set();await asyncio.Event().wait()
            finally:closed.set()
    client_class=httpx.AsyncClient
    monkeypatch.setattr('ore.providers.httpx.AsyncClient',lambda **kwargs:client_class(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=SlowStream())),**kwargs))
    backend=APIBackend('openai','model','https://fixture.test','fixture')
    task=asyncio.create_task(backend.decide('instructions','prompt',on_text_delta=lambda _:None))
    await asyncio.wait_for(entered.wait(),2);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert closed.is_set()


async def test_api_truncated_stream_cannot_be_a_completed_decision(monkeypatch):
    payload='data: '+json.dumps({'type':'response.output_text.delta','delta':frame('respond',content='Apparently complete text.')})+'\n\n'
    client_class=httpx.AsyncClient
    monkeypatch.setattr('ore.providers.httpx.AsyncClient',lambda **kwargs:client_class(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=BytesStream(payload.encode()))),**kwargs))
    backend=APIBackend('openai','model','https://fixture.test','fixture')
    from ore.codex import BackendError
    with pytest.raises(BackendError,match='before completion'):
        await backend.decide('instructions','prompt',on_text_delta=lambda _:None)
