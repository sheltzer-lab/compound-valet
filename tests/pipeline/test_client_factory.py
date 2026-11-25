"""Tests for core/client_factory.py"""

import pytest
from unittest.mock import patch, MagicMock


class TestGetClient:
    @patch("core.client_factory.genai.Client")
    @patch("core.client_factory.USE_VERTEX_AI", True)
    @patch("core.client_factory.PROJECT_ID", "my-project")
    @patch("core.client_factory.LOCATION", "us-central1")
    def test_vertex_ai_mode(self, mock_client_cls):
        from core.client_factory import get_client, RETRY_OPTIONS
        client = get_client()
        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1",
            http_options={"retry_options": RETRY_OPTIONS},
        )

    @patch("core.client_factory.genai.Client")
    @patch("core.client_factory.USE_VERTEX_AI", False)
    def test_api_key_mode(self, mock_client_cls):
        from core.client_factory import get_client, RETRY_OPTIONS
        client = get_client(api_key="test-key-123")
        mock_client_cls.assert_called_once_with(api_key="test-key-123")

    @patch("core.client_factory.genai.Client")
    @patch("core.client_factory.USE_VERTEX_AI", False)
    @patch.dict("os.environ", {"GOOGLE_API_KEY": "env-key-456"})
    def test_env_var_fallback(self, mock_client_cls):
        from core.client_factory import get_client, RETRY_OPTIONS
        client = get_client()
        mock_client_cls.assert_called_once_with(api_key="env-key-456")

    @patch("core.client_factory.USE_VERTEX_AI", False)
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_key_raises(self):
        # Remove GOOGLE_API_KEY from env if present
        import os
        env_backup = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            from core.client_factory import get_client, RETRY_OPTIONS
            with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
                get_client()
        finally:
            if env_backup is not None:
                os.environ["GOOGLE_API_KEY"] = env_backup

    @patch("core.client_factory.genai.Client")
    @patch("core.client_factory.USE_VERTEX_AI", True)
    @patch("core.client_factory.PROJECT_ID", "proj")
    @patch("core.client_factory.LOCATION", "europe-west1")
    def test_vertex_ignores_api_key(self, mock_client_cls):
        from core.client_factory import get_client, RETRY_OPTIONS
        client = get_client(api_key="should-be-ignored")
        mock_client_cls.assert_called_once_with(
            vertexai=True, project="proj", location="europe-west1",
            http_options={"retry_options": RETRY_OPTIONS},
        )


class TestRetryOptionsAreEffective:
    """The bug this guards: with retry_options unset the SDK does not retry at all, so a
    single transient Vertex failure reached core/llm.py and paused every worker for 180s.
    Asserting the kwarg is not enough -- assert the SDK turns it into a real policy."""

    def test_absent_retry_options_means_never_retry(self):
        from google.genai._api_client import retry_args
        import tenacity
        args = retry_args(None)
        assert args["stop"].max_attempt_number == 1     # i.e. no retries

    def test_our_config_yields_a_real_retry_policy(self):
        from google.genai import types
        from google.genai._api_client import retry_args
        from core.client_factory import RETRY_OPTIONS
        opts = types.HttpOptions(retry_options=RETRY_OPTIONS).retry_options
        args = retry_args(opts)
        assert args["stop"].max_attempt_number == 5
        for code in (429, 503, 504):
            assert code in opts.http_status_codes


def _client_error(code, message, status="X"):
    from google.genai import errors
    return errors.ClientError(code, {"error": {"code": code, "message": message, "status": status}})


class TestCheckGeminiSetup:
    """check_gemini_setup stops a run before any work when Gemini cannot be used, and
    never stops it for a passing problem."""

    def _check(self, count_tokens=None, vertex=True, location="global", get_client=None):
        from core import client_factory as cf
        client = MagicMock()
        if count_tokens is not None:
            client.models.count_tokens.side_effect = count_tokens
        with patch.object(cf, "USE_VERTEX_AI", vertex), patch.object(cf, "PROJECT_ID", "proj"), \
             patch.object(cf, "LOCATION", location), \
             patch.object(cf, "get_client", get_client or MagicMock(return_value=client)):
            cf.check_gemini_setup(["model-a", "model-b", "model-a"])
        return client

    def test_passes_and_checks_each_model_once(self):
        client = self._check()
        assert [c.kwargs["model"] for c in client.models.count_tokens.call_args_list] == ["model-a", "model-b"]

    @patch.dict("os.environ", {}, clear=True)
    def test_no_settings(self):
        from core.client_factory import GeminiSetupError
        with pytest.raises(GeminiSetupError, match="GOOGLE_CLOUD_PROJECT .* or GOOGLE_API_KEY"):
            self._check(vertex=False)

    def test_no_login(self):
        import google.auth.exceptions
        from core.client_factory import GeminiSetupError
        no_login = google.auth.exceptions.DefaultCredentialsError("none")
        with pytest.raises(GeminiSetupError, match="gcloud auth application-default login"):
            self._check(get_client=MagicMock(side_effect=no_login))

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "bad"})
    def test_key_not_valid(self):
        from core.client_factory import GeminiSetupError
        with pytest.raises(GeminiSetupError, match="GOOGLE_API_KEY is not valid"):
            self._check(_client_error(400, "API key not valid. Please pass a valid API key."), vertex=False)

    def test_access_refused(self):
        from core.client_factory import GeminiSetupError
        with pytest.raises(GeminiSetupError, match="Vertex AI API is enabled"):
            self._check(_client_error(403, "Permission denied"))

    def test_model_not_available_names_the_model(self):
        from core.client_factory import GeminiSetupError
        with pytest.raises(GeminiSetupError, match="model-a is not available.*GOOGLE_CLOUD_LOCATION=global"):
            self._check(_client_error(404, "not found"), location="us-central1")

    def test_passing_problems_do_not_stop_the_run(self):
        from google.genai import errors
        self._check(errors.ServerError(503, {"error": {"code": 503, "message": "unavailable"}}))
        self._check(_client_error(429, "quota"))
        self._check(ConnectionError("network down"))
