import unittest
from unittest.mock import patch

from app.config import ENV_FILE, get_settings


class SettingsTests(unittest.TestCase):
    def test_example_api_key_is_treated_as_unconfigured(self):
        with (
            patch("app.config.load_dotenv"),
            patch("app.config.dotenv_values", return_value={"LLM_API_KEY": None}) as values,
            patch.dict("os.environ", {"LLM_API_KEY": "your_groq_api_key_here"}, clear=False),
        ):
            settings = get_settings()

        self.assertIsNone(settings.api_key)
        values.assert_called_once_with(ENV_FILE)

    def test_api_key_is_trimmed(self):
        with (
            patch("app.config.load_dotenv"),
            patch("app.config.dotenv_values") as values,
            patch.dict("os.environ", {"LLM_API_KEY": "  valid-key  "}, clear=False),
        ):
            settings = get_settings()

        self.assertEqual(settings.api_key, "valid-key")
        values.assert_not_called()

    def test_empty_environment_value_falls_back_to_dotenv(self):
        with (
            patch("app.config.load_dotenv"),
            patch("app.config.dotenv_values", return_value={"LLM_API_KEY": "dotenv-key"}),
            patch.dict("os.environ", {"LLM_API_KEY": ""}, clear=False),
        ):
            settings = get_settings()

        self.assertEqual(settings.api_key, "dotenv-key")

    def test_dotenv_key_replaces_example_environment_value(self):
        with (
            patch("app.config.load_dotenv"),
            patch("app.config.dotenv_values", return_value={"LLM_API_KEY": "dotenv-key"}),
            patch.dict("os.environ", {"LLM_API_KEY": "your_groq_api_key_here"}, clear=False),
        ):
            settings = get_settings()

        self.assertEqual(settings.api_key, "dotenv-key")

    def test_nonempty_environment_value_takes_precedence(self):
        with (
            patch("app.config.load_dotenv"),
            patch("app.config.dotenv_values", return_value={"LLM_API_KEY": "dotenv-key"}) as values,
            patch.dict("os.environ", {"LLM_API_KEY": "environment-key"}, clear=False),
        ):
            settings = get_settings()

        self.assertEqual(settings.api_key, "environment-key")
        values.assert_not_called()


if __name__ == "__main__":
    unittest.main()
