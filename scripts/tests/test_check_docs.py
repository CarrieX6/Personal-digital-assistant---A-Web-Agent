"""Standard-library tests for the documentation checker; no app dependencies."""
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.check_docs import check_file


class DocumentationCheckerTests(unittest.TestCase):
    def check(self, content: str, *, exists: bool = True):
        with patch.object(Path, "read_text", return_value=content), patch.object(
            Path, "exists", return_value=exists
        ) as exists_check:
            count, errors = check_file(Path("docs/example.md"))
        return count, errors, exists_check.call_args_list

    def test_relative_link(self):
        count, errors, _ = self.check("[chapter](chapter.md)")
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])

    def test_missing_target(self):
        count, errors, _ = self.check("[missing](missing.md)", exists=False)
        self.assertEqual(count, 1)
        self.assertIn("missing relative target missing.md", errors[0])

    def test_external_links_and_same_file_fragments_are_skipped(self):
        count, errors, calls = self.check(
            "[web](https://example.org) [mail](mailto:a@example.org) [top](#top)"
        )
        self.assertEqual(count, 0)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [])

    def test_links_inside_code_fences_are_skipped(self):
        count, errors, _ = self.check("```md\n[example](missing.md)\n```", exists=False)
        self.assertEqual(count, 0)
        self.assertEqual(errors, [])

    def test_unclosed_fence_is_reported(self):
        _, errors, _ = self.check("~~~python\nprint('hello')")
        self.assertIn("unclosed code fence", errors[0])

    def test_angle_bracket_link_and_title(self):
        count, errors, _ = self.check('[file](<a%20b.md#part> "Title")')
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])

    def test_malformed_angle_bracket_link_is_reported(self):
        count, errors, _ = self.check("[broken](<chapter.md)")
        self.assertEqual(count, 0)
        self.assertIn("malformed angle-bracket link", errors[0])

    def test_longer_matching_fence_closes_block(self):
        count, errors, _ = self.check("```md\nignored\n````\n[chapter](chapter.md)")
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
