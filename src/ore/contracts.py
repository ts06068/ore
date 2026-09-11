"""Closed action contracts: model output never becomes arbitrary Python or shell code."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, create_model

class Action(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

def schema(name,**fields):return create_model(name,__base__=Action,**fields)

ProviderFieldBinding=schema('ProviderFieldBinding',target=(int,Field(ge=0,le=511)),value_ref=(Literal['username','password','mfa_code','given_name','family_name','affiliation','application_name','website'],...))

ACTION_MODELS={
 'state':schema('State'),
 'browser_open':schema('BrowserOpen',url=(str|None,None)),
 'browser_observe':schema('BrowserObserve',session_id=(str,...)),
 'browser_action':schema('BrowserAction',session_id=(str,...),action=(Literal['navigate','click','type','key','scroll','wait','tab','back'],...),epoch=(int,...),url=(str|None,None),target=(int|None,None),selector=(str|None,None),x=(float|int|None,None),y=(float|int|None,None),text=(str|None,None),key=(str|None,None),deltaY=(float|int|None,None),deltaX=(float|int|None,None),index=(int|None,None),seconds=(float|int|None,None)),
 'provider_form':schema('ProviderForm',operation=(Literal['inspect','fill','propose_fill','propose_click','capture_key'],...),session_id=(str,...),epoch=(int,Field(ge=1)),url=(str|None,None),form_fingerprint=(str|None,None),fields=(list[ProviderFieldBinding]|None,Field(default=None,min_length=1,max_length=16)),target=(int|None,Field(default=None,ge=0))),
 'search':schema('Search',source=(str,...),query=(str,...),query_mode=(Literal['plain','native']|None,None),year_from=(int|None,None),year_to=(int|None,None),journals=(list[str]|None,None),limit=(int,Field(default=20,ge=1,le=200)),cursor=(str|None,None)),
 'resolve':schema('Resolve',source=(Literal['pmc','unpaywall'],...),identifier=(str,...)),
 'fetch':schema('Fetch',url=(str,...)),
 'resource':schema('ResourceAction',id=(str|None,None),title=(str,...),url=(str|None,None),doi=(str|None,None),canonical_id=(str|None,None),classification=(Literal['included','excluded','needs_review'], 'needs_review'),article_type=(str|None,None),version=(str|None,None),supplement_status=(Literal['present','source_declares_none','not_listed_after_checks','unknown'],'unknown'),supplement_evidence=(str|None,None),expected_supplements=(int|None,Field(default=None,ge=0)),reason=(str|None,None),abstract=(str|None,None),excerpt=(str|None,None)),
 'download':schema('Download',url=(str,...),resource_id=(str,...),role=(Literal['main_pdf','supplement','full_text','attachment','full_text_xml','full_text_text','media'],...),filename=(str|None,None),doi=(str|None,None),title=(str|None,None),version=(str|None,None),session_id=(str|None,None),candidate_id=(str|None,None),requirement_id=(str|None,None)),
 'artifact_commit':schema('ArtifactCommit',session_id=(str,...),download_index=(int,Field(ge=0)),resource_id=(str,...),role=(str,...),doi=(str|None,None),title=(str|None,None),version=(str|None,None),candidate_id=(str|None,None),requirement_id=(str|None,None)),
 'archive_expand':schema('ArchiveExpand',artifact_id=(str,...)),
 'extract':schema('Extract',artifact_id=(str,...)),
 'page_extract':schema('PageExtract',session_id=(str,...),epoch=(int,...),selector=(str,...),resource_id=(str,...)),
 'seal_issue':schema('SealIssue',issue_id=(str,...),snapshot_ids=(list[str],Field(min_length=1)),profile_id=(str,...)),
 'seal_article':schema('SealArticle',article_key=(str,...),snapshot_ids=(list[str],Field(min_length=1)),profile_id=(str,...)),
 'inventory':schema('Inventory',expected_resources=(int,Field(ge=0)),enumeration_complete=(bool,...),official_toc_evidence=(list[str],...),gaps=(list[str],Field(default_factory=list))),
 'delegate':schema('Delegate',goal=(str,...),key=(str,...),kind=(Literal['plan','retrieve','extract','classify'],'retrieve'),urls=(list[str],Field(default_factory=list))),
 'challenge':schema('Challenge',session_id=(str,...),resolved=(bool,False)),
 'handoff':schema('Handoff',session_id=(str,...),reason=(str,...)),
 'finish':schema('Finish',summary=(str,...)),
}

def validate_action(name,args):
    if name not in ACTION_MODELS:raise ValueError('Unknown ORE action')
    value=ACTION_MODELS[name].model_validate(args).model_dump(exclude_none=True)
    if name in ('download','artifact_commit'):
        ids=[value.get('candidate_id'),value.get('requirement_id')]
        if any(item is not None for item in ids) and not all(isinstance(item,str) and item.strip() for item in ids):
            raise ValueError('candidate_id and requirement_id must be supplied together as nonempty sealed coverage IDs. '
                'IDs returned by resolve are not sealed IDs; for bounded retrieval omit both IDs and use the observed URL.')
    return value
