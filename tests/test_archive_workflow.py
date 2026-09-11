import pytest
from ore.coverage import CoverageLedger, CoverageError
from ore.models import Mission
from ore.store import Store
from ore.scholarly_workflow import seal_archive

@pytest.fixture
def archive(tmp_path):
    store=Store('sqlite:///:memory:');store.initialize()
    job=store.create_job(Mission(goal='June original articles',artifact_roles=[],completeness='systematic').model_dump(mode='json'))
    ledger=CoverageLedger(store,tmp_path)
    ledger.register_profile({'id':'fixture.archive','kind':'html_archive','origins':['journal.example'],
        'container_selector':'main','row_selector':'.issue','link_selector':'a','date_selector':'time',
        'complete_selector':'footer[data-end]','range_selector':'main','issue_url_pattern':'/issue/',
        'count_selector':'main','count_attribute':'data-total-issues'},reviewer='offline-fixture')
    yield ledger,job['id']
    store.close()

def body(extra='',total=4):
    return f'<main data-from="2024-01-01" data-until-exclusive="2025-01-01" data-total-issues="{total}">'+''.join(
        f'<div class="issue"><a href="/issue/{i}">Issue {i}</a><time datetime="{day}"></time></div>'
        for i,day in enumerate(['2024-05-31','2024-06-04','2024-06-25','2024-07-01']))+extra+'</main><footer data-end></footer>'

def capture(ledger,job,html):
    return ledger.capture_snapshot(job,url='https://journal.example/archive/2024',content=html.encode())['id']

def test_archive_month_uses_issue_date_and_all_matching_issues(archive):
    ledger,job=archive
    result=seal_archive(ledger,job,[capture(ledger,job,body())],'fixture.archive','2024-06-01','2024-07-01')
    assert result['issue_ids']==['https://journal.example/issue/1','https://journal.example/issue/2']
    assert result['target_basis']=='verified_official_archive'
    assert result['selection_rule']['basis']=='issue_date'

@pytest.mark.parametrize('html,reason',[
    (body('<a rel="next" href="/archive/2024?page=2">Next</a>'),'pagination'),
    (body('<a href="/issue/missing">Missing</a>'),'Unaccounted'),
    (body(total=5),'count'),
    (body().replace('2025-01-01','2024-06-15'),'range'),
])
def test_archive_rejects_incomplete_denominators(archive,html,reason):
    ledger,job=archive
    with pytest.raises(CoverageError,match=reason):seal_archive(ledger,job,[capture(ledger,job,html)],'fixture.archive','2024-06-01','2024-07-01')
