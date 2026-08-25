# Amazon Processor Agent Rules

## Scope

This repository is the `amazon-json-processor` Skill. It supports one production chain only: Amazon columnar JSON → DeepSeek image review → marketplace copy → fixed 14-field refill JSON → offline review package.

Do not add Windows server workers, task APIs, inbox polling, web configuration pages, Agnes, Ollama, GPT Image, or image generation.

## Before editing

1. Run `git status --short` and preserve unrelated user files.
2. If `.codegraph/` exists, use `codegraph explore` before broad text search.
3. Read `docs/Agent维护与排障指南.md` for the affected area.
4. Never edit `.env`, input tables, formal output, or `.runtime` unless the user explicitly requests it.

## Stable contracts

- `amazon_processor.process_json(input_path) -> RunResult`
- `python -m amazon_processor run <input.json>`
- `uv run python scripts/process_amazon_json.py <input.json>`
- Input is 5/6-field columnar JSON; missing marketplace defaults to US.
- Output is exactly `AMAZON_JSON_OUTPUT_FIELDS` in order (14 fields).
- First product image is main. Every released product also needs at least one product attachment.
- `有问题的产品id` records missing source product content, no eligible main, or no product attachment after image cleanup.
- Input is read-only. Formal output uses staging and atomic publish.
- Failure must preserve the previous formal output.
- Image generation requests must remain zero.

## Configuration

- Secret: ignored `.env`, only `DEEPSEEK_KEY`.
- Models, Endpoint and concurrency: `config/settings.json`.
- Prompt registry: `config/prompts/manifest.json`.
- Prompt text: `config/prompts/**/*.txt`.
- Do not hide AI instructions in Python. Model and Prompt changes must affect cache signatures.

## Verification

Write or update an offline regression test first. Then run:

```powershell
uv run python -m pytest tests/<relevant>.py -q
uv run ruff check amazon_processor scripts tests
uv run pyright
uv run python -m pytest -q
```

Do not run network or paid Provider tests without explicit user authorization. Do not declare success from exit code alone; inspect the Agent result JSON, formal file, row count, field order and exception list.

## Git

Use `codex/` branch names. Never commit `.env`, `.runtime`, input tables, formal outputs, local models, or unrelated user artifacts.
