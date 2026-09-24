"""Corrected-edition pages for the designer notes."""
from docproof.interior.verify import new_pages


def _snap(text, frames):
    return {'stories': [{'id': 's1', 'text': text, 'frames': frames}]}


BASE = 'Preface text. ' + 'x' * 20 + ' Chapter One. teh river bends.'


def test_edits_and_designer_notes_land_on_their_new_pages():
    edits = [{'id': 'e1', 'story_id': 's1', 'find': 'Preface', 'replacement': 'A Much Longer Preface'},
             {'id': 'e2', 'story_id': 's1', 'find': 'teh', 'replacement': 'the'}]
    final_text = BASE.replace('Preface', 'A Much Longer Preface').replace('teh', 'the')
    # After the insertion reflows, "Chapter One" starts on the third page.
    third = final_text.index('Chapter One')
    final = _snap(final_text, [
        {'start': 0, 'end': 20, 'page': 5, 'page_name': 'v'},
        {'start': 20, 'end': third, 'page': 6, 'page_name': '1'},
        {'start': third, 'end': len(final_text), 'page': 7, 'page_name': '2'}])
    plan = {'instructions': [
        {'id': 'i1', 'edit_ids': ['e1']}, {'id': 'i2', 'edit_ids': ['e2']},
        {'id': 'i3', 'edit_ids': [], 'disposition': 'designer',
         'locate_story_id': 's1', 'locate_text': 'Chapter One'}]}
    found = new_pages(_snap(BASE, []), final, edits, plan)
    assert found['edits'] == {'e1': [{'page': 5, 'page_name': 'v'}], 'e2': [{'page': 7, 'page_name': '2'}]}
    assert found['instructions'] == {'i1': [{'page': 5, 'page_name': 'v'}],
                                     'i2': [{'page': 7, 'page_name': '2'}],
                                     'i3': [{'page': 7, 'page_name': '2'}]}


def test_overset_or_unmapped_text_has_no_page():
    edits = [{'id': 'e2', 'story_id': 's1', 'find': 'teh', 'replacement': 'the'}]
    final_text = BASE.replace('teh', 'the')
    # The frame chain ends before the corrected word: it sits in overset.
    overset = _snap(final_text, [{'start': 0, 'end': 20, 'page': 1, 'page_name': '1'}])
    assert new_pages(_snap(BASE, []), overset, edits)['edits'] == {}
    # An older snapshot without a frame map places nothing rather than guessing.
    assert new_pages(_snap(BASE, []), _snap(final_text, None), edits)['edits'] == {}


def test_an_edit_in_the_last_frame_uses_that_frame():
    edits = [{'id': 'e', 'story_id': 's1', 'find': 'bends.', 'replacement': 'bends. The end.'}]
    final_text = BASE.replace('bends.', 'bends. The end.')
    final = _snap(final_text, [{'start': 0, 'end': len(final_text), 'page': 3, 'page_name': '3'}])
    assert new_pages(_snap(BASE, []), final, edits)['edits'] == {'e': [{'page': 3, 'page_name': '3'}]}
