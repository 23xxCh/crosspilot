---
name: amazon-json-processor
description: Process an Amazon column-oriented collection JSON into the fixed 14-field refill JSON and offline review package. Use when the user provides a concrete Amazon collection-table JSON path and asks to process, backfill, localize, review images, or generate the Amazon refill table.
---

# Amazon JSON Processor

Use this Skill only after the user explicitly asks to process a specific Amazon JSON file. The run makes paid DeepSeek text and vision requests.

## Required workflow

1. Resolve the supplied JSON path and confirm it exists. Never edit the input file.
2. From this Skill directory, run:

   ```powershell
   uv run python scripts/process_amazon_json.py "<absolute-input-path>"
   ```

3. Read the path printed after `AGENT_RESULT=`. Do not infer success from exit code alone.
4. Treat the result as successful only when its JSON contains `published: true` and `status: published`.
5. Report these absolute paths and counts:
   - `output_path`
   - `review_path`
   - `review_data_path`
   - `exception_path`, when non-empty
   - retained, quarantined and isolated product counts
   - elapsed time, attempts, request statistics and image statistics
6. If the result is `pending_review` or `failed`, report the exact blocker and result JSON path. Never claim that the formal refill table was updated.

## Fixed business rules

- Input is a 5/6-field column-oriented Amazon JSON. Missing marketplace defaults to US.
- Output field names and order are fixed by `amazon_processor.schema.AMAZON_JSON_OUTPUT_FIELDS` (14 fields).
- Text and image review use official DeepSeek only.
- Every unique image URL is reviewed. Risk or unresolved images are removed.
- A retained original source image is selected as main; the first product image is always main.
- A product without an eligible main image or without at least one product attachment is removed and added to `有问题的产品id`.
- Image generation is forbidden; generation request count must remain zero.
- Input hash must remain unchanged. Failed runs must not overwrite the previous formal output.

## Retry boundary

The runner performs a bounded retry for transient 429/5xx/network/timeout failures and reuses completed cache entries. Authentication, permission and quota failures stop immediately. Do not clear `.runtime/cache` to force progress.

## Configuration

- Secret: `.env` with `DEEPSEEK_KEY` only.
- Models and concurrency: `config/settings.json`.
- Prompts: `config/prompts/manifest.json` and registered `.txt` files.
- Default vision model: `deepseek-v4-flash-vision-exp`.
- Default image review concurrency: 15 batches, 3 images per batch.

## Verification commands

Run offline checks after code or Prompt changes:

```powershell
uv run python -m pytest -q
uv run ruff check amazon_processor scripts tests
uv run pyright
```

Do not run a real Provider smoke test unless the user explicitly authorizes paid calls.
