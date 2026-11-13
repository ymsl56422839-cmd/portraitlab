"""Standard-library worker contract checks; no model download or AI imports."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portraitlab import relight


class RelightContractTests(unittest.TestCase):
    def test_unsafe_parameters_rejected(self):
        for values in ({'steps': True}, {'denoise': float('nan')}, {'width': 513},
                       {'cfg': float('inf')}, {'low_vram': 'false'},
                       {'device': 'unknown'}, {'direction': 'front'}, {'prompt': ''}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                relight.validate(values)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            original, replacement = folder / 'image.png', folder / 'temporary.png'
            original.write_bytes(b'original')
            replacement.write_bytes(b'replacement')
            with self.assertRaises(FileExistsError):
                relight.publish_new_file(replacement, original)
            self.assertEqual(original.read_bytes(), b'original')

    def test_filesystem_without_hardlinks_can_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / 'temporary.png', folder / 'image.png'
            source.write_bytes(b'new image')
            with patch.object(relight.os, 'link', side_effect=OSError('hardlinks unsupported')):
                relight.publish_new_file(source, output)
            self.assertEqual(output.read_bytes(), b'new image')

    def test_failed_fallback_does_not_leave_partial_image(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / 'temporary.png', folder / 'image.png'
            source.write_bytes(b'new image')
            with patch.object(relight.os, 'link', side_effect=OSError('hardlinks unsupported')):
                with patch.object(relight.shutil, 'copyfileobj', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        relight.publish_new_file(source, output)
            self.assertFalse(output.exists())

    def test_optional_storage_root_blocks_outside_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.dict(os.environ, {'PORTRAITLAB_STORAGE_ROOT': str(root)}):
                self.assertEqual(relight.destination(root / 'allowed.png'), root / 'allowed.png')
                with self.assertRaises(ValueError):
                    relight.destination(root.parent / 'outside.png')

    def test_missing_models_are_reported_without_network_or_ai_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(relight.urllib.request, 'urlopen', side_effect=AssertionError('network access')):
                result = relight.model_status(Path(directory))
            self.assertFalse(result['ready'])
            self.assertGreater(result['estimated_bytes'], 0)
            self.assertTrue(all(not file['hash_verified'] for file in result['files']))

    def test_corrupt_model_is_rejected_before_optional_ai_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, mask, bad_model = folder / 'input.png', folder / 'mask.png', folder / 'model.safetensors'
            source.write_bytes(b'source')
            mask.write_bytes(b'mask')
            bad_model.write_bytes(b'corrupted')
            status = {'ready': True, 'files': [{'path': str(bad_model), 'sha256': '0' * 64, 'relative_path': bad_model.name}]}
            with patch.object(relight, 'model_status', return_value=status):
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    relight.generate({'input_path': str(source), 'mask_path': str(mask),
                                      'output_path': str(folder / 'output.png')}, folder,
                                     relight.Progress())


if __name__ == '__main__':
    unittest.main()
