"""Normalize explicit provider labels without guessing publication status."""
def normalize_article_version(value):
    key = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    return {'published': 'published_version', 'publishedversion': 'published_version', 'published_version': 'published_version',
            'version_of_record': 'published_version', 'vor': 'published_version',
            'accepted': 'accepted_manuscript', 'acceptedversion': 'accepted_manuscript', 'accepted_manuscript': 'accepted_manuscript',
            'author_manuscript': 'accepted_manuscript', 'submitted': 'submitted_manuscript',
            'submittedversion': 'submitted_manuscript', 'submitted_manuscript': 'submitted_manuscript', 'preprint': 'submitted_manuscript'}.get(key, 'unknown')
