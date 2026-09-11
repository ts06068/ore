import asyncio
import http.cookiejar
from pathlib import Path
import httpx
import pytest
from ore.policy import AccessPolicy,AccessDenied,ModelPolicy,RateLimiter,redact
from ore.config import Settings,SecretStore
from ore.engine import Engine
from ore.routes import rank_candidates
from ore.store import Store

async def test_private_addresses_and_profile_origins_are_real_constraints():
    with pytest.raises(AccessDenied):await AccessPolicy({}).check('http://127.0.0.1/private')
    with pytest.raises(AccessDenied):await AccessPolicy({}, {'origins':['https://example.org']}).check('https://example.com')
    assert await AccessPolicy({}, {'allowed_hosts':['127.0.0.1']}).check('http://127.0.0.1/fixture')

async def test_rate_penalty_survives_reconstruction(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'rates.db'));store.initialize()
    one=RateLimiter(store);await one.penalize('fixture',0.04)
    two=RateLimiter(Store('sqlite:///'+str(tmp_path/'rates.db')))
    started=asyncio.get_running_loop().time();await two.acquire('fixture',0)
    assert asyncio.get_running_loop().time()-started>=0.02
    store.close();two.store.close()

def test_no_egress_policy_rejects_remote_even_without_model_start(tmp_path):
    engine=Engine(Settings(state_dir=tmp_path,max_workers=0,auth_token='synthetic'))
    with pytest.raises(AccessDenied,match='egress'):engine.check_egress({'external_model_content':'none'},'codex')
    value=engine.model_observation({'external_model_content':'metadata'},{'result':{'text':'private full text','abstract':'private abstract','title':'Public title','image_url':'private-image','doi':'10.1/x'}})
    assert value=={'result':{'title':'Public title','doi':'10.1/x'}}
    engine.store.close()

def test_redaction_preserves_usage_but_hides_credentials():
    value=redact({'access_token':'synthetic-private','usage':{'total_tokens':17},'url':'https://example.org/file?token=synthetic&article=2'})
    assert value['access_token']=='[redacted]' and value['usage']['total_tokens']==17
    assert 'synthetic' not in value['url'] and 'article=2' in value['url']
    assert 'synthetic' not in redact('https://example.org/?__cf_chl_rt_tk=synthetic&article=2')

def test_routing_never_treats_wrong_version_as_cheaper_equivalent():
    values=rank_candidates([{'url':'a','source':'pmc','version':'accepted'},{'url':'b','source':'publisher','version':'published'}],{'scope':{'article_versions':['published']}})
    assert values[0]['url']=='b' and not values[1]['routing']['eligible']

def test_concurrent_secret_writers_preserve_entries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    Settings(state_dir=tmp_path,auth_token='synthetic').prepare();store=SecretStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda i:SecretStore(tmp_path).set(f'fixture/{i}',str(i)),range(20)))
    assert all(store.get(f'fixture/{i}')==str(i) for i in range(20))


@pytest.mark.parametrize('url', [
    'https://[probe].challenges.cloudflare.com',
    'https://[probe].dnstest.dev/private?token=fixture-secret',
    'http://[unfinished/private?token=fixture-secret',
])
def test_redaction_handles_synthetic_and_malformed_urls_without_losing_other_evidence(url):
    value = redact({'url_origin': url, 'diagnostic': {'expected': True, 'url_sha256': 'a' * 64},
                    'request': 'https://journal.test/paper?token=fixture-secret&article=2'})
    assert value['url_origin'] == '[invalid URL omitted]'
    assert value['diagnostic'] == {'expected': True, 'url_sha256': 'a' * 64}
    assert 'article=2' in value['request'] and 'fixture-secret' not in str(value)


def test_redaction_preserves_valid_ipv6_origins():
    assert redact('https://[2001:db8::1]/paper?token=fixture-secret&article=2').startswith('https://[2001:db8::1]/paper?')
    assert 'fixture-secret' not in redact('https://[2001:db8::1]/paper?token=fixture-secret&article=2')
