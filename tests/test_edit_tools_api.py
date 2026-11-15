"""Edit-stack persistence, protected source coordinates and render consistency."""
from copy import deepcopy
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from portraitlab import lighting
from portraitlab.face_shape import map_points, forward_points
from test_server import api, wait_job
from test_preset_pipeline_api import reopened
from test_lighting_service import configured, open_client, prepare, config


SHAPE = {"faces": [], "strokes": [{"mode": "push", "x": .53, "y": .5,
          "dx": .035, "dy": .008, "radius": .22, "strength": .8}]}
REPAIR = {"mode": "sampled", "x": .55, "y": .48, "source_x": .23, "source_y": .48,
          "radius": .07, "strength": .8, "hardness": .5,
          "points": [[.55, .48], [.56, .54], [.57, .59]]}


def import_pattern(client):
    yy, xx = np.indices((120, 200))
    pixels = np.stack((90 + xx // 2, 80 + yy, 105 + (xx % 13) * 3), axis=-1).astype(np.uint8)
    pixels[55:65,106:116] = [60, 30, 20]
    data = io.BytesIO(); Image.fromarray(pixels).save(data, 'PNG')
    response = client.post('/api/import', files={'files': ('人像工具.png', data.getvalue(), 'image/png')})
    assert response.status_code == 200, response.text
    return response.json()['photos'][0], pixels


def post_preview(client, photo, **params):
    response = client.post(f"/api/photos/{photo['id']}/preview", json=params)
    assert response.status_code == 200, response.text
    return response.json()


def read_preview(client, photo):
    response = client.get(photo['preview_url']); assert response.status_code == 200
    return np.asarray(Image.open(io.BytesIO(response.content)).convert('RGB'))


def export(client, photo):
    response = client.post('/api/jobs', json={'ids':[photo['id']], 'format':'png', 'max_side':0})
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()['job']['id'])
    assert job['status'] == 'completed', job
    item = job['items'][0]
    return np.asarray(Image.open(item['path']).convert('RGB')), json.loads(Path(item['sidecar']).read_text(encoding='utf-8'))


def test_shape_and_brush_survive_restart_and_export_matches_preview(api):
    client, app = api
    original, source = import_pattern(client)
    rendered = post_preview(client, original, face_shape=SHAPE, spot_repairs=[REPAIR])
    photo = rendered['photo']
    assert photo['shape_geometry']['face_shape'] == photo['face_shape']
    assert photo['shape_geometry']['operations']
    assert photo['spot_repairs'][0]['points'] == REPAIR['points']
    actual = read_preview(client, photo)
    assert np.any(actual != source)
    result, sidecar = export(client, photo)
    np.testing.assert_array_equal(actual, result)
    assert sidecar['face_shape'] == photo['face_shape']
    assert sidecar['spot_repairs'] == photo['spot_repairs']
    with reopened(app.state.store) as (again, _):
        saved = next(p for p in again.get('/api/photos').json()['photos'] if p['id'] == photo['id'])
        assert saved['face_shape'] == photo['face_shape']
        result = post_preview(again, saved)['photo']
        np.testing.assert_array_equal(read_preview(again, result), actual)
        reset = post_preview(again, saved, face_shape=None, spot_repairs=[])['photo']
        np.testing.assert_array_equal(read_preview(again, reset), source)


def test_layout_is_readonly_and_maps_screen_pick_back_to_source(api):
    client, app = api
    photo, _ = import_pattern(client)
    path = app.state.store.state_path
    before = path.read_bytes()
    layout = client.post(f"/api/photos/{photo['id']}/shape-layout", json={'face_shape':SHAPE})
    assert layout.status_code == 200, layout.text
    data = layout.json()
    assert data['operations'] and data['face_shape']['strokes']
    assert path.read_bytes() == before
    points = np.array([[.55,.48],[.57,.59],[.1,.1]], dtype=float)
    screen = forward_points(points, data['operations'])
    restored = map_points(screen, data['operations'])
    np.testing.assert_allclose(restored, points, atol=2e-5)
    get = client.get(f"/api/photos/{photo['id']}/face-layout")
    assert get.status_code == 200 and not get.json()['operations']
    assert path.read_bytes() == before


@pytest.mark.parametrize('bad', [{"faces": 'bad'}, {"strokes":[{**SHAPE['strokes'][0], 'radius':-1}]}, {'unknown':1}])
def test_bad_shape_is_atomic_and_does_not_discard_existing_repairs(api, bad):
    client, app = api
    photo, _ = import_pattern(client)
    photo = post_preview(client, photo, spot_repairs=[REPAIR])['photo']
    before = deepcopy(app.state.store.photo(photo['id']))
    disk = app.state.store.state_path.read_bytes()
    response = client.post(f"/api/photos/{photo['id']}/preview", json={'face_shape':bad, 'spot_repairs':[]})
    assert response.status_code == 400, response.text
    assert app.state.store.photo(photo['id']) == before
    assert app.state.store.state_path.read_bytes() == disk


def test_shaped_lighting_session_uses_same_geometry_as_saved_preview_and_export(configured):
    client, store = open_client(configured/'shape-lighting')
    with client:
        photo, _ = import_pattern(client)
        photo = post_preview(client, photo, face_shape=SHAPE, spot_repairs=[REPAIR])['photo']
        job = prepare(client, photo['id'])
        prepared = job['result']
        assets = {}
        for key in ('base','normal','depth','mask'):
            response = client.get(prepared[key+'_url']); assert response.status_code == 200
            assets[key] = np.asarray(Image.open(io.BytesIO(response.content))).copy()
        settings = config()
        response = client.post(f"/api/photos/{photo['id']}/lighting", json={
            'lighting':settings, 'expected_version':prepared['version'], 'geometry_key':prepared['geometry_key']})
        assert response.status_code == 200, response.text
        saved = response.json()['photo']
        expected = lighting.render_lighting(assets['base'],assets['normal'],assets['depth'],assets['mask'],settings)
        np.testing.assert_array_equal(read_preview(client,saved), expected)
        exported, _ = export(client,saved)
        np.testing.assert_array_equal(exported, expected)
        with Image.open(store.lighting.geometry_directory(prepared['geometry_key'])/'mask.png') as im:
            original_mask=np.asarray(im)
        assert np.any(assets['mask'] != original_mask), 'Shape must also move the lighting scope boundary'
