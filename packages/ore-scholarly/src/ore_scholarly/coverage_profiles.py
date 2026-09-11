"""Bundled structural extractors. Fail closed on unrecognized or partial pages.

These are reusable declarative adapters, not claims of live publisher validation.
Runtime seals require matching origin, template, links, pagination and identity.
"""
from copy import deepcopy

JOURNALS = {
 'jacc': (['www.jacc.org','jacc.org','www.sciencedirect.com'], '.issue-item, .js-article-list-item', '.issue-item__title a, .js-article-title a, a[data-doi]', r'/doi/|/science/article/', ['Original Investigation','Original Investigations','Original Research']),
 'circulation': (['www.ahajournals.org','ahajournals.org'], '.issue-item', '.issue-item__title a, a[data-doi]', r'/doi/', ['Original Research Article','Original Research','Original Article']),
 'ehj': (['academic.oup.com'], '.al-article-item, .article-list-item', '.al-title a, .article-title a', r'/eurheartj/article/', ['Clinical Research','Clinical research','Basic Science','Original Article','Original Research']),
 'jama-cardiology': (['jamanetwork.com'], '.issue-article, .article-listing, .article', '.article-title a, .article--title a, a[data-article-id]', r'/journals/jamacardiology/(fullarticle|article-abstract)/', ['Original Investigation','Brief Report','Original Research']),
 'hir': (['e-hir.org','www.e-hir.org','synapse.koreamed.org'], '.article-item, .article-list, .article', '.article-title a, .title a', r'/journal/view.php|/articles/', ['Original Article']),
 'plos-medicine': (['journals.plos.org'], '.article', '.article-name a, .article-title a, a.article-title', r'/plosmedicine/article\?id=', ['Research Article']),
}


def bundled_profiles():
    rows = []
    for journal, (origins, row, link, pattern, included) in JOURNALS.items():
        base = {'journal_id': journal, 'origins': origins, 'authority': 'journal', 'journal_is_publisher': journal != 'jacc', 'publisher_origins': ['www.sciencedirect.com'] if journal == 'jacc' else [], 'include_labels': included,
                'exclude_labels': ['Editorial','Review','Review Article','Systematic Review','Meta-analysis','Correction','Letter','Viewpoint'],
                'ambiguous_title_patterns': [r'\bsystematic (?:literature )?review\b', r'\bmeta[ -]analys[ie]s\b', r'\bscoping review\b'],
                'article_type_selector': '[data-article-type], .article-type, .articleType, .article-category, .content-type, meta[name="citation_article_type"]',
                'verification': 'structural_fixture_only_live_template_checks_required'}
        rows.append({**base, 'id': journal + '.issue.v1', 'kind': 'html_issue', 'container_selector': '.issue-toc, .table-of-content, .article-list, #issue-articles, main',
            'row_selector': row, 'link_selector': link, 'article_url_pattern': pattern,
            'complete_selector': '.issue-toc, .table-of-content, #issue-articles, main',
            'next_selector': 'a[rel="next"], .pagination a.next',
            'pending_selector': 'button.load-more:not([disabled]), [aria-busy="true"], .loading-spinner',
            'count_selector': '[data-total-articles]', 'count_attribute': 'data-total-articles'})
        rows.append({**base, 'id': journal + '.article.v1', 'kind': 'html_article',
            'main_selector': 'meta[name="citation_pdf_url"], a[href*="/doi/pdf/"], a[href*="/doi/epdf/"], a.article-pdf, a.pdf-link, a[href*="type=printable"], a[href*="/pdfft"]',
            'supplement_selector': '.supplementary-material a[href], .supplementary-materials a[href], .supplemental-material a[href], #supplementary-material a[href], .supplement a[href], .supplementary-material a[href], a[href*="/suppl/"], a[href*="/suppinfo/"], #supporting-information a[href], #supplemental-tab a[href], .supplementary-data a[href], a[href*="/article/file?"][href*=".s"]',
            'empty_supplements_selector': '[data-supplement-count="0"], meta[name="supplementary-materials"][content="none"]',
            'attachments_complete_selector': '.supplementary-material, .supplementary-materials, .supplemental-material, #supplementary-material, #supporting-information, #supplemental-tab, .supplementary-data',
            'supplement_url_pattern': r'/suppl/|/suppinfo/|\.s[0-9]{3}(?:[&.?]|$)|/supplement',
            'pending_selector': '[aria-busy="true"], .paywall, .access-denied',
            'final_version_from_official_pdf_link': True})
    rows.append({'id':'publisher.jats.article.v1','kind':'jats_article','authority':'publisher','origins':sorted({x for j in JOURNALS.values() for x in j[0]}),
        'include_labels':sorted({x for j in JOURNALS.values() for x in j[4]}|{'research-article','original-article'}),
        'ambiguous_title_patterns':[r'\bsystematic (?:literature )?review\b',r'\bmeta[ -]analys[ie]s\b',r'\bscoping review\b'],
        'exclude_labels':['review-article','editorial','correction','letter'],'final_version_from_official_pdf_link':True})
    return deepcopy(rows)
