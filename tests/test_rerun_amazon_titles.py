from __future__ import annotations

import json

from scripts import rerun_amazon_titles


def _payload() -> dict:
    return {
        "商品id": ["p1", "p2"],
        "产品标题": [
            "Generic Switch for BMW",
            "Universal Cable",
        ],
        "产品描述": ["Description 1", "Description 2"],
        "产品图片链接": [
            ["https://img/1.jpg"],
            ["https://img/2.jpg"],
        ],
        "变种图片链接": [[], []],
        "Bullet Point1": ["1", "1"],
        "Bullet Point2": ["2", "2"],
        "Bullet Point3": ["3", "3"],
        "Bullet Point4": ["4", "4"],
        "Bullet Point5": ["5", "5"],
        "关键词信息": ["a,b", "c,d"],
        "有问题的产品id": [],
    }


def test_rerun_titles_backs_up_and_preserves_non_title_fields(
    tmp_path,
    monkeypatch,
) -> None:
    formal = tmp_path / "formal.json"
    original = _payload()
    formal.write_text(
        json.dumps(original, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_stage(rows, provider_getter):
        assert provider_getter() is provider
        rows[0]["title"] = "Generic Dashboard Switch for BMW"
        rows[1]["title"] = "Universal Replacement Cable"
        return rows

    class Registry:
        def metadata(self, _prompt_id):
            return {"signature": "test"}

    class Provider:
        def metrics_snapshot(self):
            return {"api_calls": 2}

    provider = Provider()
    monkeypatch.setattr(
        rerun_amazon_titles,
        "optimize_titles",
        fake_stage,
    )
    monkeypatch.setattr(
        rerun_amazon_titles,
        "get_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        rerun_amazon_titles,
        "reload_provider",
        lambda: None,
    )
    monkeypatch.setattr(
        rerun_amazon_titles,
        "get_prompt_registry",
        lambda: Registry(),
    )

    result = rerun_amazon_titles.rerun_titles(formal)

    updated = json.loads(formal.read_text(encoding="utf-8"))
    assert updated["产品标题"] == [
        "Generic Dashboard Switch for BMW",
        "Universal Replacement Cable",
    ]
    for field, values in original.items():
        if field != "产品标题":
            assert updated[field] == values
    assert result["summary"]["changed"] == 2
    assert result["summary"]["max_length"] <= 75
    assert result["backup"]
