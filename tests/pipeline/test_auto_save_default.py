"""run_analysis_pipeline must default to auto_save=False."""
import inspect

from core.pipeline import run_analysis_pipeline


def test_pipeline_defaults_to_auto_save_false():
    """Forgetting the parameter must never silently write to the database."""
    param = inspect.signature(run_analysis_pipeline).parameters.get("auto_save")
    assert param is not None, "run_analysis_pipeline should have an auto_save param"
    assert param.default is False, (
        f"auto_save should default to False (safe default), got {param.default}"
    )
