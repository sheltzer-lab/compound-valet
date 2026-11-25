import pytest
from unittest.mock import MagicMock, patch
from core.context import Context


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Per-test SQLite isolation.

    `core/db.py` reads `DATABASE_URL` at module load and caches its engine in
    a module-global `_ENGINE` singleton. Without isolation, every test in a
    pytest session shares the *same* SQLite file at `data/compound_valet.db`
    and the *same* engine instance. Two consequences:

      1. Tests can see each other's rows (state pollution).
      2. SQLite's single-writer lock surfaces as `database is locked`
         errors when tests are reordered (e.g. by pytest-cov's import-time
         instrumentation).

    This fixture gives each test its own SQLite file under `tmp_path` and
    forces `init_db()` to rebuild the engine against the new URL. It also
    disposes the engine after the test so connections close cleanly before
    pytest blows the tmp dir away.

    Tests that need a specific database should set `DATABASE_URL`
    explicitly in their own fixture;
    that wins because they set it AFTER this fixture's monkeypatch.
    """
    import core.db

    db_path = tmp_path / "test_compound_valet.db"
    monkeypatch.setattr(core.db, "DATABASE_URL", f"sqlite:///{db_path}")

    # Snapshot the global engine and force init_db() to rebuild for this test.
    saved_engine = core.db._ENGINE
    core.db._ENGINE = None

    # Eagerly initialize: creates the engine + tables BEFORE any test code
    # runs. The reviewer / specialist pipeline uses a ThreadPoolExecutor and
    # if multiple threads hit `init_db()` concurrently while _ENGINE is None,
    # they can race in `Base.metadata.create_all` — one thread sees the table
    # half-created and SQLite raises "no such table". Pre-initializing here
    # makes the engine present from the first byte of the test, so the
    # `if _ENGINE: return` fast-path short-circuits all subsequent calls.
    core.db.init_db()

    try:
        yield
    finally:
        # Dispose any engine the test created so its connections close before
        # pytest unlinks the tmp_path SQLite file.
        if core.db._ENGINE is not None:
            try:
                core.db._ENGINE.dispose()
            except Exception:
                pass
        core.db._ENGINE = saved_engine


@pytest.fixture
def mock_gemini_client():
    """
    Patches get_client (the auth/factory entry point) to return a mock client.
    Returns the mock so tests can configure responses on
    `models.generate_content` and `batches.*`.

    We patch get_client rather than google.genai.Client because client_factory
    raises ValueError before constructing the Client when neither
    GOOGLE_API_KEY nor GOOGLE_CLOUD_PROJECT is set (e.g. on CI). Patching the
    constructor would never get reached in that case.
    """
    mock_instance = MagicMock()

    # Default mock response structure
    mock_response = MagicMock()
    mock_response.text = "Mock Gemini Response"
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50
    mock_response.usage_metadata.total_token_count = 150
    mock_instance.models.generate_content.return_value = mock_response

    # Configure batches.create, get, etc for batch testing
    mock_batch_job = MagicMock()
    mock_batch_job.name = "mock_job_123"
    mock_batch_job.state = "JOB_STATE_SUCCEEDED"
    mock_batch_job.dest.file_name = "output.jsonl"
    mock_instance.batches.create.return_value = mock_batch_job
    mock_instance.batches.get.return_value = mock_batch_job

    # Patch at every site that imports get_client by name. core.llm binds the
    # name at module import; core.target_matching imports it inside the
    # function, so patching the source module covers it.
    with patch("core.client_factory.get_client", return_value=mock_instance), \
         patch("core.llm.get_client", return_value=mock_instance), \
         patch("google.genai.Client", return_value=mock_instance):
        yield mock_instance

@pytest.fixture
def sample_context():
    """Returns a populated Context object for testing."""
    return Context(
        drug_name="TestDrug",
        relevant_papers=[
            {"title": "Paper 1", "year": "2023", "authors": ["Alice", "Bob"], "url": "http://example.com/1"},
        ],
        usage_metadata={"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    )
