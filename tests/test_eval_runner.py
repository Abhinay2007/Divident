import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
evaluate = _module.evaluate


def test_offline_evaluation_is_reproducible_and_labels_come_from_fixture():
    result = evaluate()
    assert result["sources"] == 31
    assert result["deterministic_only"]["correct_mappings"] == 27
    assert result["assisted"] == result["deterministic_only"]
    assert result["validation_proposals"] == 6
    assert result["validation_rejections"] == 6
    assert result["llm_calls"] == 3
    assert result["cache_lookups"] == 2 and result["cache_hit_rate"] == 0
    assert "No live Groq run" in " ".join(result["limitations"])
