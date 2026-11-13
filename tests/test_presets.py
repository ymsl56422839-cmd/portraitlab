"""Preset persistence and conflict-safe batch undo; no models or network needed."""
from copy import deepcopy
import json

import pytest

from portraitlab.engine import DEFAULT_PARAMS, validate_params
from portraitlab.presets import BUILTIN_IDS, MAX_CUSTOM_PRESETS, builtin_presets
from portraitlab.server import PortraitStore
from test_server import api, import_one


def reopen(store):
    return PortraitStore(store.data)


def close_store(store):
    store.model_setup.close()
    store.relight.close()
    store.queue.put(None)
    store.worker.join(timeout=5)


def test_builtin_recipes_are_distinct_complete_and_described(api):
    client, _ = api
    catalog = client.get('/api/presets').json()
    recipes = catalog['presets']
    assert len(recipes) == 22
    assert len({p['id'] for p in recipes}) == len(recipes)
    assert len({p['name'] for p in recipes}) == len(recipes)
    assert len({json.dumps(p['params'], sort_keys=True) for p in recipes}) == len(recipes)
    categories = {group['id'] for group in catalog['categories']}
    for recipe in recipes:
        assert recipe['builtin'] is True
        assert recipe['category'] in categories
        assert recipe['description'] and recipe['tags']
        assert set(recipe['params']) == set(DEFAULT_PARAMS)
        assert validate_params(recipe['params']) == recipe['params']
    # A reset does not leave latent beauty or color edits when strength changes.
    original = next(p for p in recipes if p['id'] == 'original')['params']
    assert all(original[key] == 0 for key in ('smooth', 'blemishes', 'even', 'texture', 'exposure', 'relight', 'warmth', 'opacity'))
    assert original['saturation'] == 1
    returned = builtin_presets()
    returned[0]['params']['smooth'] = 1
    assert builtin_presets()[0]['params']['smooth'] == .25
    boot = client.get('/api/bootstrap').json()
    assert boot['presets'] == recipes
    assert boot['preset_categories'] == catalog['categories']
    assert boot['preset_favorites'] == []


def test_custom_and_favorites_survive_reopen_and_delete(api):
    client, app = api
    response = client.post('/api/presets', json={'name': ' 我的棚拍 ', 'description': ' 偏暖，轻修 ',
                                               'params': {'smooth': .12, 'exposure': .2, 'warmth': .15}})
    assert response.status_code == 200
    created = response.json()['preset']
    assert created['name'] == '我的棚拍' and created['description'] == '偏暖，轻修'
    assert created['category'] == 'mine' and created['builtin'] is False
    assert created['id'].startswith('user_') and created['params']['exposure'] == .2
    for identifier in ('natural', created['id']):
        assert client.post(f'/api/presets/{identifier}/favorite', json={'favorite': True}).status_code == 200
    # Repeated favorite requests remain idempotent.
    assert client.post(f"/api/presets/{created['id']}/favorite", json={'favorite': True}).status_code == 200
    loaded = reopen(app.state.store)
    try:
        saved = loaded.preset_catalog()
        assert created in saved['presets']
        assert saved['favorites'] == ['natural', created['id']]
    finally:
        close_store(loaded)
    deleted = client.delete(f"/api/presets/{created['id']}")
    assert deleted.status_code == 200
    assert created['id'] not in [p['id'] for p in deleted.json()['presets']]
    assert deleted.json()['favorites'] == ['natural']
    assert client.delete('/api/presets/natural').status_code == 403
    assert client.delete(f"/api/presets/{created['id']}").status_code == 404


def test_legacy_workspace_without_preset_fields_loads(api):
    _, app = api
    state = app.state.store.state
    state.pop('preset_favorites')
    state.pop('presets')
    app.state.store.persist()
    loaded = reopen(app.state.store)
    try:
        assert loaded.preset_catalog()['favorites'] == []
        assert len(loaded.preset_catalog()['presets']) == 22
    finally:
        close_store(loaded)


@pytest.mark.parametrize('payload', [
    {'params': {}}, {'name': '', 'params': {}}, {'name': '  ', 'params': {}},
    {'name': 'x' * 41, 'params': {}}, {'name': 'bad\nname', 'params': {}},
    {'name': 'bad\u202ename', 'params': {}}, {'name': True, 'params': {}},
    {'name': 'x', 'description': 'x' * 181, 'params': {}},
    {'name': 'x', 'description': [], 'params': {}}, {'name': 'x'},
    {'name': 'x', 'params': []}, {'name': 'x', 'params': {'smooth': True}},
    {'name': 'x', 'params': {'warmth': 3}}, {'name': 'x', 'params': {'unknown': 1}},
    {'name': 'x', 'params': {}, 'builtin': True},
])
def test_invalid_custom_presets_do_not_mutate_state(api, payload):
    client, app = api
    before = deepcopy(app.state.store.state)
    assert client.post('/api/presets', json=payload).status_code == 400
    assert app.state.store.state == before


def test_preset_size_limits_duplicates_and_authentication(api):
    client, app = api
    assert client.post('/api/presets', content=b' ' * (64 * 1024 + 1), headers={'content-type': 'application/json'}).status_code == 413
    first = client.post('/api/presets', json={'name': 'A', 'params': {}}).json()['preset']
    assert client.post('/api/presets', json={'name': 'a', 'params': {}}).status_code == 409
    for flag in (1, 'true', None):
        assert client.post('/api/presets/natural/favorite', json={'favorite': flag}).status_code == 400
    assert client.post('/api/presets/missing/favorite', json={'favorite': True}).status_code == 404
    token = client.headers.pop('X-PortraitLab-Token')
    assert client.post('/api/presets', json={'name': 'Unauthorized', 'params': {}}).status_code == 401
    assert client.post('/api/presets/natural/favorite', json={'favorite': True}).status_code == 401
    assert client.delete(f"/api/presets/{first['id']}").status_code == 401
    client.headers['X-PortraitLab-Token'] = token
    app.state.store.state['presets'] = {
        f'user_{i:032x}': {**deepcopy(first), 'id': f'user_{i:032x}', 'name': str(i)}
        for i in range(MAX_CUSTOM_PRESETS)
    }
    assert client.post('/api/presets', json={'name': 'too many', 'params': {}}).status_code == 400
    assert len(app.state.store.state['presets']) == MAX_CUSTOM_PRESETS


def test_batch_restore_is_atomic_and_retains_later_brush_edits(api):
    client, app = api
    photos = [import_one(client), import_one(client)]
    ids = [p['id'] for p in photos]
    first_params = [{**DEFAULT_PARAMS, 'exposure': .1}, {**DEFAULT_PARAMS, 'exposure': -.2}]
    for identifier, params in zip(ids, first_params):
        assert client.post('/api/settings/apply', json={'ids': [identifier], 'params': params}).status_code == 200
    batch = client.post('/api/settings/apply', json={'ids': ids, 'params': {'smooth': .5, 'exposure': .4}}).json()['photos']
    entries = [{'id': p['id'], 'params': old, 'expected_params': p['params']} for p, old in zip(batch, first_params)]
    # The brush is independently edited after the batch; undo must not replace it.
    stroke = {'mode': 'protect', 'radius': .03, 'points': [[.2, .3]]}
    assert client.post(f'/api/photos/{ids[1]}/mask', json={'strokes': [stroke]}).status_code == 200
    undo = client.post('/api/settings/restore', json={'photos': entries})
    assert undo.status_code == 200
    assert undo.json()['ids'] == ids
    assert [p['params'] for p in undo.json()['photos']] == first_params
    assert undo.json()['photos'][0]['strokes'] == []
    assert undo.json()['photos'][1]['strokes'] == [stroke]
    saved = json.loads(app.state.store.state_path.read_text(encoding='utf-8'))
    assert [saved['photos'][i]['params'] for i in ids] == first_params


def test_batch_restore_conflict_or_invalid_entry_changes_nothing(api):
    client, app = api
    photos = [import_one(client), import_one(client)]
    ids = [p['id'] for p in photos]
    changed = client.post('/api/settings/apply', json={'ids': ids, 'params': {'exposure': .4}}).json()['photos']
    entries = [{'id': p['id'], 'params': DEFAULT_PARAMS, 'expected_params': p['params']} for p in changed]
    client.post('/api/settings/apply', json={'ids': [ids[1]], 'params': {'exposure': .5}})
    before = deepcopy(app.state.store.state['photos'])
    assert client.post('/api/settings/restore', json={'photos': entries}).status_code == 409
    assert app.state.store.state['photos'] == before
    bad = deepcopy(entries)
    bad[1]['params'] = {'exposure': 999}
    assert client.post('/api/settings/restore', json={'photos': bad}).status_code == 400
    assert app.state.store.state['photos'] == before
    bad[1] = {**entries[1], 'id': 'missing'}
    assert client.post('/api/settings/restore', json={'photos': bad}).status_code == 404
    assert app.state.store.state['photos'] == before
    assert client.post('/api/settings/restore', json={'photos': [entries[0], entries[0]]}).status_code == 400
    assert app.state.store.state['photos'] == before


@pytest.mark.parametrize('payload', [{}, {'photos': []}, {'photos': [{}]}, {'photos': [True]},
                                      {'photos': 'wrong'}, {'photos': [{}] * 501},
                                      {'photos': [], 'strokes': []}])
def test_restore_rejects_invalid_batch_shapes(api, payload):
    client, _ = api
    assert client.post('/api/settings/restore', json=payload).status_code == 400
