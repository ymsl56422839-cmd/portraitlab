"""Large-photo render consistency and lighting-session refresh after shaping."""
from copy import deepcopy
import io

import cv2
import numpy as np
from PIL import Image

from portraitlab import lighting
from portraitlab.lighting_service import LightingController
from test_edit_tools_api import SHAPE, post_preview, read_preview
from test_lighting_service import configured, config, open_client, prepare, save
from test_server import wait_job


def import_large_pattern(client):
    # Uneven dimensions exercise independent rounding in preview/export paths.
    height, width = 1283, 1921
    yy, xx = np.indices((height, width), dtype=np.int32)
    pixels = np.stack((90 + (xx % 29) * 3, 80 + (yy % 31) * 2,
                       100 + ((xx + yy) % 37) * 2), axis=-1).astype(np.uint8)
    pixels[(xx - round(.55 * (width - 1))) ** 2
           + (yy - round(.48 * (height - 1))) ** 2 <= 14 ** 2] = [40, 25, 20]
    data = io.BytesIO()
    Image.fromarray(pixels).save(data, 'PNG')
    response = client.post('/api/import', files={
        'files': ('large-edit-stack.png', data.getvalue(), 'image/png')})
    assert response.status_code == 200, response.text
    assert not response.json()['errors']
    return response.json()['photos'][0], pixels


def export_pixels(client, photo, max_side=0):
    response = client.post('/api/jobs', json={
        'ids': [photo['id']], 'format': 'png', 'max_side': max_side})
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()['job']['id'])
    assert job['status'] == 'completed', job
    with Image.open(job['items'][0]['path']) as image:
        return np.asarray(image.convert('RGB')).copy()


def read_assets(client, prepared):
    result = {}
    for name in ('base', 'normal', 'depth', 'mask'):
        response = client.get(prepared[name + '_url'])
        assert response.status_code == 200, response.text
        with Image.open(io.BytesIO(response.content)) as image:
            result[name] = np.asarray(image).copy()
    return result


def test_large_brush_and_shape_preview_matches_resampled_full_export(configured):
    client, store = open_client(configured / 'large-edit-stack')
    with client:
        photo, original = import_large_pattern(client)
        repair = {'mode': 'sampled', 'x': .55, 'y': .48,
                  'source_x': .23, 'source_y': .48, 'radius': .015,
                  'strength': .8, 'hardness': .5,
                  'points': [[.55, .48], [.56, .50], [.57, .52]]}
        photo = post_preview(client, photo, face_shape=SHAPE,
                             spot_repairs=[repair])['photo']
        preview = read_preview(client, photo)
        full = export_pixels(client, photo)
        reduced = export_pixels(client, photo, 1600)

        assert full.shape == original.shape == (1283, 1921, 3)
        assert preview.shape == (round(1283 * 1600 / 1921), 1600, 3)
        assert np.any(full != original), 'Edits must reach the full-size export'
        np.testing.assert_array_equal(full[:100, :100], original[:100, :100])
        np.testing.assert_array_equal(store.rgb(store.photo(photo['id'])), original)
        expected_preview = cv2.resize(full, (preview.shape[1], preview.shape[0]),
                                      interpolation=cv2.INTER_AREA)
        np.testing.assert_array_equal(preview, expected_preview)
        np.testing.assert_array_equal(reduced, preview)


def test_reprepare_after_shape_change_refreshes_assets_without_reinferring_geometry(
        configured, monkeypatch):
    # Spatially varying fields expose stale or double-warped geometry; constant
    # test normals/depth would hide those failures despite a changed subject mask.
    def patterned_geometry(self, input_path, output_dir, device, job):
        self.test_inferences = getattr(self, 'test_inferences', 0) + 1
        with Image.open(input_path) as image:
            width, height = image.size
        yy, xx = np.indices((height, width), dtype=np.float32)
        normal = np.stack((96 + 80 * xx / (width - 1),
                           96 + 80 * yy / (height - 1),
                           235 + 15 * xx / (width - 1)), axis=-1).astype(np.uint8)
        depth = (45 + 160 * xx / (width - 1)).astype(np.uint8)
        self.save_image(output_dir / 'normal.png', normal, 'PNG')
        self.save_image(output_dir / 'depth.png', Image.fromarray(depth), 'PNG')
        return {'device': 'test', 'width': width, 'height': height}

    monkeypatch.setattr(LightingController, '_infer_geometry', patterned_geometry)
    client, store = open_client(configured / 'shape-refresh')
    with client:
        photo, _ = import_large_pattern(client)
        photo = post_preview(client, photo, face_shape=SHAPE)['photo']
        first = prepare(client, photo['id'])['result']
        first_assets = read_assets(client, first)
        directory = store.lighting.geometry_directory(first['geometry_key'])
        raw_geometry = {name: (directory / (name + '.png')).read_bytes()
                        for name in ('normal', 'depth', 'mask')}
        settings = config()
        committed = save(client, photo, first, settings)
        assert committed.status_code == 200, committed.text

        changed_shape = deepcopy(SHAPE)
        changed_shape['strokes'][0].update(dx=-.035, dy=-.012)
        photo = post_preview(client, committed.json()['photo'],
                             face_shape=changed_shape)['photo']
        changed_preview = read_preview(client, photo)
        second = prepare(client, photo['id'])['result']
        second_assets = read_assets(client, second)

        assert second['version'] == photo['version'] > first['version']
        assert second['base_key'] != first['base_key']
        assert second['geometry_key'] == first['geometry_key']
        assert store.lighting.test_inferences == 1
        assert second['width'] == 1024 and second['height'] < 1024
        retained_assets = read_assets(client, first)
        for name in first_assets:
            assert np.any(second_assets[name] != first_assets[name]), name
            # A new editing session must not modify an older session's assets.
            np.testing.assert_array_equal(retained_assets[name], first_assets[name])
        for name, data in raw_geometry.items():
            assert (directory / (name + '.png')).read_bytes() == data

        expected = lighting.render_lighting(second_assets['base'], second_assets['normal'],
                                            second_assets['depth'], second_assets['mask'], settings)
        np.testing.assert_array_equal(changed_preview, expected)
        assert save(client, photo, first, settings).status_code == 409
        committed = save(client, photo, second, settings)
        assert committed.status_code == 200, committed.text
        saved = committed.json()['photo']
        np.testing.assert_array_equal(read_preview(client, saved), expected)
        np.testing.assert_array_equal(export_pixels(client, saved, 1024), expected)

        # Reverting shape must recover the exact original session, not apply
        # another warp to already-transformed normals/depth/mask.
        post_preview(client, saved, face_shape=SHAPE)
        reverted = prepare(client, photo['id'])['result']
        assert reverted['base_key'] == first['base_key']
        assert store.lighting.test_inferences == 1
        for name, pixels in read_assets(client, reverted).items():
            np.testing.assert_array_equal(pixels, first_assets[name])
