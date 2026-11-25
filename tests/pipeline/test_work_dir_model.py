"""A consensus work folder is pinned to the report model that started it (simple_batch.py)."""
import pytest

from simple_batch import check_work_dir_model


def test_same_model_continues_and_another_is_refused(tmp_path):
    check_work_dir_model(str(tmp_path), "model-a")      # first use: pins the folder
    check_work_dir_model(str(tmp_path), "model-a")      # a restart with the same model
    with pytest.raises(ValueError, match="GEMINI_MODEL_NAME=model-a"):
        check_work_dir_model(str(tmp_path), "model-b")
