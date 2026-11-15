import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from bench_gateway.cloud_evaluation import extract, resolve_scenes

SCENE = '2026-08-20_10-49-30_HLX34B161T1300638'


class CloudEvaluationTests(unittest.TestCase):
    def test_exact_scene_in_direct_and_export_date_layouts(self):
        for suffix in ('', '2026-09-11'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                gt = root/suffix/SCENE/'dataset'
                gt.mkdir(parents=True)
                self.assertEqual(resolve_scenes([root], [SCENE]), {SCENE: str(gt.resolve())})

    def test_same_scene_under_multiple_versions_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in ('2026-09-11', '2026-09-12'):
                (root/version/SCENE/'dataset').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                resolve_scenes([root], [SCENE])

    def test_missing_scene_does_not_select_similar_scene(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/(SCENE+'0')/'dataset').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'missing'):
                resolve_scenes([root], [SCENE])

    def test_invalid_and_repeated_scene_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for scenes in ([], [SCENE, SCENE], ['../'+SCENE], [SCENE+'/dataset']):
                with self.subTest(scenes=scenes), self.assertRaises(ValueError):
                    resolve_scenes([folder], scenes)

    def test_archive_rejects_parent_traversal_and_symlink(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = root/'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '/outside'
                    tar.addfile(member)
                with self.assertRaises(ValueError):
                    extract(archive, root/'output')
                self.assertFalse((root/'escape').exists())

    def test_archive_preserves_selected_bytes_and_rejects_oversize(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root/'inputs.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('predictions/'+SCENE+'/1/pred_img_1.json')
                member.size = 2
                tar.addfile(member, io.BytesIO(b'{}'))
            with self.assertRaises(ValueError):
                extract(archive, root/'small', maximum=1)
            extract(archive, root/'output')
            self.assertEqual((root/'output'/'predictions'/SCENE/'1/pred_img_1.json').read_bytes(), b'{}')


if __name__ == '__main__':
    unittest.main()
