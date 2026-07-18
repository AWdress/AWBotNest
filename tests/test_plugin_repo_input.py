import unittest

from config.config import normalize_plugin_repo
from webui.github_import import parse_source


class PluginRepoInputTests(unittest.TestCase):
    def test_owner_repo_is_kept_for_settings(self):
        self.assertEqual(
            normalize_plugin_repo("AWdress/AWBotNest-Plugins"),
            "AWdress/AWBotNest-Plugins",
        )

    def test_github_url_is_normalized_for_settings(self):
        self.assertEqual(
            normalize_plugin_repo("https://github.com/AWdress/AWBotNest-Plugins.git"),
            "AWdress/AWBotNest-Plugins",
        )

    def test_repository_name_uses_standard_capitalization(self):
        self.assertEqual(
            normalize_plugin_repo("AWdress/awbotnest-plugins"),
            "AWdress/AWBotNest-Plugins",
        )

    def test_owner_repo_can_be_parsed_as_plugin_source(self):
        parsed = parse_source("AWdress/AWBotNest-Plugins")
        self.assertEqual(parsed["owner"], "AWdress")
        self.assertEqual(parsed["repo"], "AWBotNest-Plugins")

    def test_non_github_url_is_rejected(self):
        self.assertEqual(normalize_plugin_repo("https://example.com/owner/repo"), "")

    def test_other_repository_name_is_rejected(self):
        self.assertEqual(normalize_plugin_repo("AWdress/other-plugins"), "")


if __name__ == "__main__":
    unittest.main()
