"""Secondary content check for an explicitly registered native book source."""
from app.watch.native_queue import title_key


def verify_identity(baseline, book):
    pages = baseline.get('page_texts') or []
    if not isinstance(pages, list):
        raise ValueError('The native book did not provide front-matter identity evidence.')
    text = ' ' + title_key(' '.join(str(row.get('text', '')) for row in pages[:20]
                                   if isinstance(row, dict))) + ' '
    for field in ('title', 'author'):
        expected = [book.get(field, ''), *book.get(field + '_aliases', [])]
        if not any(title_key(value) and ' ' + title_key(value) + ' ' in text for value in expected):
            raise ValueError(f'The InDesign front matter does not confirm the registered book {field}. Review the source or register an explicit title/byline alias before processing.')
