from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sats.chat_artifacts import save_json_artifact, save_markdown_artifact, validate_chat_artifact_path


class ChatArtifactsTest(unittest.TestCase):
    def test_artifact_helpers_write_under_chat_scoped_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md = save_markdown_artifact(
                project_root=Path(tmp),
                session_id="chat_a",
                turn_id="turn_a",
                title="研究报告",
                content="结论",
            )
            js = save_json_artifact(
                project_root=Path(tmp),
                session_id="chat_a",
                turn_id="turn_a",
                title="payload",
                payload={"ok": True},
            )

            self.assertTrue(md.path.exists())
            self.assertTrue(js.path.exists())
            self.assertIn("/reports/chat/chat_a/turn_a/", str(md.path))
            self.assertIn("/artifacts/chat/chat_a/turn_a/", str(js.path))
            self.assertEqual(validate_chat_artifact_path(Path(tmp), md.path), md.path)

    def test_validate_chat_artifact_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                validate_chat_artifact_path(Path(tmp), Path(tmp) / "reports" / "other.md")


if __name__ == "__main__":
    unittest.main()
