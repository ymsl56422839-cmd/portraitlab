"""Small fixture tests for share-payload boundaries; no models or downloads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
import sys

SPEC = importlib.util.spec_from_file_location("v2_share_builder", Path(__file__).resolve().parents[1] / "packaging/build_v2_share.py")
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


class ShareBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="v2-share-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def put(self, name, text="test", root=None):
        path = (root or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def plan(self):
        self.put("code.py", "print('portable')\n")
        plan = builder.Plan()
        plan.add(self.source, "code.py", "app/code.py", "source")
        plan.text("VERSION.txt", "0.7.0\n", "metadata")
        return plan

    def test_relative_paths_reject_escape_and_windows_ads(self):
        for value in ("../outside", "a/../b", "C:/Users/private", "/root", "a//b", "x:secret", "./a", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.relative_name(value)
        self.assertEqual(builder.relative_name("app\\code.py"), "app/code.py")

    def test_checksum_fails_before_creating_destination(self):
        self.put("weight.pt")
        plan = builder.Plan()
        plan.add(self.source, "weight.pt", "Data/models/weight.pt", "models", "0" * 64)
        destination = self.root / "release"
        with self.assertRaisesRegex(ValueError, "checksum"):
            builder.write_build(plan, destination)
        self.assertFalse(destination.exists())

    def test_no_overwrite_and_manifest_has_only_relative_paths(self):
        destination = self.root / "release"
        builder.write_build(self.plan(), destination)
        self.assertEqual(builder.verify(destination)["files"], 2)
        data = (destination / builder.MANIFEST).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), data)
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            builder.write_build(self.plan(), destination)
        (destination / "app/code.py").write_text("altered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            builder.verify(destination)

    def test_pristine_verification_detects_user_state(self):
        destination = self.root / "release"
        builder.write_build(self.plan(), destination)
        self.put("Data/state.json", "{}", destination)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            builder.verify(destination)
        builder.verify(destination, pristine=False)
        self.put("Data/models/unexpected.pt", "private", destination)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            builder.verify(destination, pristine=False)

    def test_record_allowlist_excludes_unrecorded_and_private_files(self):
        for name in builder.EMBEDDED_FILES:
            self.put(name)
        self.put("python312._pth", "C:/private\nimport malicious\n")
        site = self.source / "Lib/site-packages"
        record_files = ["approved/__init__.py", "approved-1.dist-info/METADATA", "approved-1.dist-info/RECORD",
                        "approved-1.dist-info/direct_url.json", "approved/__pycache__/x.pyc", "hook.pth", "../../../Scripts/tool.exe"]
        self.put("approved/__init__.py", "pass", site)
        self.put("approved-1.dist-info/METADATA", "Name: approved\nVersion: 1\n", site)
        self.put("approved-1.dist-info/RECORD", "\n".join(x + ",," for x in record_files), site)
        self.put("approved-1.dist-info/direct_url.json", '{"url":"file:///private"}', site)
        self.put("approved/unlisted-secret.txt", "secret", site)
        self.put("unrelated.py", "private", site)
        plan = builder.Plan()
        builder.runtime_entries(plan, self.source, "runtime", {"approved"})
        names = [x.target for x in plan.entries.values()]
        self.assertFalse(any("secret" in x or "unrelated" in x or "direct_url" in x or "hook.pth" in x for x in names))
        pth = plan.entries["runtime/python312._pth"]
        self.assertEqual(pth.content.decode(), builder.PTH)
        self.assertIsNone(pth.source)

    def test_source_allowlist_omits_research_photos_and_state(self):
        repo = self.source
        for module in builder.CORE_MODULES:
            self.put(f"portraitlab/{module}.py")
        for module in builder.V2_MODULES:
            self.put(f"experiments/portraitlab_v2/{module}.py")
        self.put("portraitlab/vendor/engine/code.py")
        self.put("portraitlab/vendor/engine/COPYING")
        for name in ("app.js", "index.html", "style.css", "workspace.js"):
            self.put("experiments/portraitlab_v2/web/" + name)
        for name in ("experiments/portraitlab_v2/presets_v2.json", "models/lighting-manifest.json", "LICENSE"):
            self.put(name)
        for name in ("build_v2_share.py", "V2Launcher.cs", "v2_build_launcher.py", "webview2.py", "webview2.lock.json"):
            self.put("packaging/" + name)
        for name in ("LICENSE.txt", "NOTICE.txt"):
            self.put("licenses/webview2/" + name)
        for name in ("acceptance-plan.json", "capability-plan.json", "personal.jpg", "state.json"):
            self.put("experiments/portraitlab_v2/" + name, "private")
        plan = builder.Plan()
        builder.source_entries(plan, repo)
        self.assertFalse(any("private" in e.source.read_text() for e in plan.entries.values()))

    def test_duplicate_case_insensitive_target_is_rejected(self):
        plan = builder.Plan()
        plan.text("app/CODE.py", "first", "source")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            plan.text("App/code.py", "second", "source")

    def test_vc_runtime_replaces_old_donor_dlls(self):
        vc = self.root / "vc"
        for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            self.put(name, "official-new-runtime", vc)
        self.put("unreviewed.dll", "excluded", vc)
        for name in builder.EMBEDDED_FILES:
            self.put(name, "old-runtime")
        (self.source / "Lib/site-packages").mkdir(parents=True)
        plan = builder.Plan()
        builder.runtime_entries(plan, self.source, "runtime", set(), vc)
        self.assertEqual(plan.entries["runtime/vcruntime140.dll"].source, vc / "vcruntime140.dll")
        self.assertIn("runtime/msvcp140.dll", plan.entries)
        self.assertNotIn("runtime/unreviewed.dll", plan.entries)

    def test_archives_restore_manifest_and_reject_overwrite(self):
        destination = self.root / "PortraitLab-fixture"
        builder.write_build(self.plan(), destination)
        archives = builder.archive(destination, self.root / "archives")
        zipped = self.root / "archives" / archives[0]["file"]
        self.assertEqual(builder.sha256(zipped), archives[0]["sha256"])
        with zipfile.ZipFile(zipped) as archive:
            self.assertIsNone(archive.testzip())
            archive.extractall(self.root / "extracted")
        builder.verify(self.root / "extracted" / destination.name)
        with self.assertRaisesRegex(ValueError, "already exists"):
            builder.archive(destination, self.root / "archives")
        with self.assertRaisesRegex(ValueError, "outside"):
            builder.archive(destination, destination / "archives")

    def test_github_parts_are_independent_archives_with_one_root(self):
        plan = builder.Plan()
        plan.text("app/a.py", "a" * 800_000, "source")
        plan.text("app/b.py", "b" * 800_000, "source")
        destination = self.root / "PortraitLab-fixture"
        builder.write_build(plan, destination)
        limit = 17 * 1024 * 1024
        parts = builder.archive(destination, self.root / "parts", limit)
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertLess(part["bytes"], limit)
            with zipfile.ZipFile(self.root / "parts" / part["file"]) as archive:
                self.assertTrue(all(name.startswith(destination.name + "/") for name in archive.namelist()))
                archive.extractall(self.root / "merged")
        builder.verify(self.root / "merged" / destination.name)


if __name__ == "__main__":
    unittest.main()
