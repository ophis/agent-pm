import base64
import hashlib
import io
import runpy
import tarfile
import unittest
from pathlib import Path


class DockerCodexProbeTest(unittest.TestCase):
    def setUp(self):
        self.probe = runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "scripts/docker_codex_probe.py")
        )

    def test_integrity_mismatch_rejected(self):
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(b"expected").digest()).decode()
        with self.assertRaises(ValueError):
            self.probe["verify_integrity"](b"different", integrity)
        self.probe["verify_integrity"](b"expected", integrity)

    def test_archive_selects_only_exact_regular_native(self):
        path = "package/vendor/aarch64-apple-darwin/bin/codex"
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            unrelated = tarfile.TarInfo("package/../../outside")
            unrelated.size = 3
            archive.addfile(unrelated, io.BytesIO(b"bad"))
            entry = tarfile.TarInfo(path)
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"fake"))
        self.assertEqual(self.probe["native_bytes"](data.getvalue(), "darwin-arm64"), b"fake")

    def test_archive_link_and_duplicate_rejected(self):
        path = "package/vendor/x86_64-unknown-linux-musl/bin/codex"
        for count, kind in [(1, tarfile.SYMTYPE), (2, tarfile.REGTYPE)]:
            with self.subTest(count=count, kind=kind):
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:gz") as archive:
                    for _ in range(count):
                        entry = tarfile.TarInfo(path)
                        entry.type = kind
                        entry.linkname = "/outside"
                        archive.addfile(entry)
                with self.assertRaises(ValueError):
                    self.probe["native_bytes"](data.getvalue(), "linux-x64")

    def test_only_registry_origin_accepted(self):
        for url in [
            "https://registry.npmjs.org.evil.test/a",
            "https://user@registry.npmjs.org/a",
            "http://registry.npmjs.org/a",
            "https://registry.npmjs.org:444/a",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.probe["registry_url"](url)
        self.probe["registry_url"]("https://registry.npmjs.org/@openai/codex")
