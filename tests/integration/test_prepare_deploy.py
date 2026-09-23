"""The upload directory must contain committed runtime files and nothing else."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.prepare_deploy import FIXED_FILES, prepare_deploy


class PrepareDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ekt-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.output = self.directory / "deploy"
        self.git("init", "--quiet")
        for path in FIXED_FILES:
            self.write(path, f"committed {path}\n")
        self.write("src/ekt/__init__.py", "VERSION = 'committed'\n")
        self.write("apps/buyer_ui/client.py", "MODE = 'committed'\n")
        self.write("corporate-raw.zip", "never upload the corporate archive")
        self.write("src/raw.xlsx", "never upload data")
        self.write(".env", "never upload configuration secrets")
        self.write("var/state.sqlite", "never upload state")
        self.write("docs/notes.md", "not runtime")
        self.commit = self.commit_all()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True,
        ).stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit_all(self):
        # These Git mutations are confined to a temporary fixture repository.
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_exact_commit_excludes_working_changes_archives_secrets_and_state(self):
        self.write("src/ekt/__init__.py", "VERSION = 'uncommitted'\n")
        self.write("src/untracked.py", "uncommitted code")
        self.git("add", "src/ekt/__init__.py")
        commit, count, size = prepare_deploy(self.output, repo=self.repo)
        expected = FIXED_FILES | {"src/ekt/__init__.py", "apps/buyer_ui/client.py"}
        actual = {path.relative_to(self.output).as_posix()
                  for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected | {"DEPLOY_COMMIT"})
        self.assertEqual(commit, self.commit)
        self.assertEqual(count, len(expected))
        self.assertEqual(size, sum((self.output / path).stat().st_size for path in expected))
        self.assertEqual((self.output / "DEPLOY_COMMIT").read_text(), self.commit + "\n")
        self.assertEqual((self.output / "src/ekt/__init__.py").read_text(), "VERSION = 'committed'\n")
        self.assertEqual((self.repo / "src/ekt/__init__.py").read_text(), "VERSION = 'uncommitted'\n")

    def test_requested_old_commit_is_used_after_head_changes(self):
        self.write("apps/buyer_ui/client.py", "MODE = 'newer'\n")
        self.commit_all()
        prepare_deploy(self.output, self.commit, repo=self.repo)
        self.assertEqual((self.output / "apps/buyer_ui/client.py").read_text(), "MODE = 'committed'\n")

    def test_refuses_nonempty_output_without_modification(self):
        self.output.mkdir()
        sentinel = self.output / "keep-me.txt"
        sentinel.write_text("untouched")
        with self.assertRaisesRegex(RuntimeError, "new or empty"):
            prepare_deploy(self.output, repo=self.repo)
        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_empty_existing_output_is_allowed(self):
        self.output.mkdir()
        prepare_deploy(self.output, repo=self.repo)
        self.assertTrue((self.output / "Dockerfile").is_file())

    def test_rejects_committed_runtime_symlink_before_writing(self):
        (self.repo / "apps/buyer_ui/client.py").unlink()
        (self.repo / "apps/buyer_ui/client.py").symlink_to("../../.env")
        self.commit_all()
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            prepare_deploy(self.output, repo=self.repo)
        self.assertFalse(self.output.exists())

    def test_refuses_output_symlink(self):
        target = self.directory / "outside"
        target.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            prepare_deploy(self.output, repo=self.repo)
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_runtime_file_fails_before_writing(self):
        (self.repo / "Dockerfile").unlink()
        self.commit_all()
        with self.assertRaisesRegex(RuntimeError, "missing runtime files: Dockerfile"):
            prepare_deploy(self.output, repo=self.repo)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
