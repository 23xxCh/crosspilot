"""Contract tests for the image safety package Interface."""
from scripts.pipelines import amazon_image_safety
from scripts.pipelines.amazon_image_safety import assessment, cache, gate


def test_package_exports_stable_interface() -> None:
    assert (
        amazon_image_safety.current_cache_versions
        is cache.current_cache_versions
    )
    assert amazon_image_safety.is_current_assessment is cache.is_current_assessment
    assert amazon_image_safety.load_cache is cache.load_cache
    assert amazon_image_safety.save_cache is cache.save_cache
    assert amazon_image_safety.safe_assess is assessment.safe_assess
    assert amazon_image_safety.validate_image_url is assessment.validate_image_url
    assert (
        amazon_image_safety.run_structured_image_safety_gate
        is gate.run_structured_image_safety_gate
    )


def test_private_aliases_remain_compatible() -> None:
    assert (
        amazon_image_safety._is_current_assessment
        is amazon_image_safety.is_current_assessment
    )
    assert amazon_image_safety._load_cache is amazon_image_safety.load_cache
    assert amazon_image_safety._save_cache is amazon_image_safety.save_cache
    assert amazon_image_safety._safe_assess is amazon_image_safety.safe_assess
    assert (
        amazon_image_safety._validate_image_url
        is amazon_image_safety.validate_image_url
    )
