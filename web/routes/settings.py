"""Settings + Templates + Stages + Version routes."""
import math, os, sys, re, glob as _glob
from fastapi import APIRouter, HTTPException
from web.context import ctx

router = APIRouter(tags=["settings"])


def _reload_runtime():
    """Reload prompt/config consumers after a settings or prompt change."""
    from crosspilot.prompt_registry import reload_prompt_registry

    reload_prompt_registry()
    reloaded = set()
    for module_name in ('model_provider', 'scripts.model_provider'):
        module = sys.modules.get(module_name)
        if (
            module
            and id(module) not in reloaded
            and hasattr(module, 'reload_provider')
        ):
            module.reload_provider()
            reloaded.add(id(module))
    for module_name in (
        'pipelines.ebay_shared', 'scripts.pipelines.ebay_shared',
        'process_amazon', 'scripts.process_amazon',
    ):
        module = sys.modules.get(module_name)
        if module and hasattr(module, 'reload_credentials'):
            module.reload_credentials()


def _prompt_error(exc):
    detail = exc.args[0] if exc.args else str(exc)
    return HTTPException(400, str(detail))


@router.get("/api/templates")
def list_templates():
    adir = os.path.join(ctx.root, 'scripts', 'adapters')
    tmpl = []
    try:
        for f in sorted(_glob.glob(os.path.join(adir, '*.py'))):
            if f.endswith('__init__.py') or f.endswith('base.py'):
                continue
            name = os.path.splitext(os.path.basename(f))[0]
            tmpl.append({'id': name, 'name': name.replace('_', '→').upper()})
    except Exception as e:
        print(f"[WARN] template scan failed: {e}", file=sys.stderr)
    return {'templates': tmpl}


@router.get("/api/stages")
def get_stages(pipeline: str = 'ebay'):
    if pipeline == 'amazon':
        from scripts.process_amazon import AMAZON_STAGES
        return {"stages": AMAZON_STAGES}
    from scripts.process_ebay_tk import StatusReporter
    return {"stages": StatusReporter.STAGES}


@router.get("/api/version")
def get_version():
    from web import __version__
    try:
        from web.updater import check_for_update
        update = check_for_update()
    except Exception as e:
        print(f"[WARN] update check failed: {e}", file=sys.stderr)
        update = None
    public_update = {'version': update['version']} if update else None
    return {'version': __version__, 'update': public_update}


@router.get("/api/settings")
def get_settings():
    """回显当前生效配置和 key 状态，不回传密钥明文。"""
    from crosspilot.config import load_config
    from crosspilot.model_registry import list_model_profiles

    cfg = load_config()
    fields = {
        'model_profile': 'MODEL_PROFILE',
        'prompt_profile': 'PROMPT_PROFILE',
        'text_provider': 'TEXT_PROVIDER',
        'vision_provider': 'VISION_PROVIDER',
        'image_gen_provider': 'IMAGE_PROVIDER',
        'deepseek_text_model': 'DEEPSEEK_TEXT_MODEL',
        'deepseek_text_fallback_model': (
            'DEEPSEEK_TEXT_FALLBACK_MODEL'
        ),
        'agnes_text_model': 'AGNES_TEXT_MODEL',
        'agnes_vision_model': 'AGNES_VISION_MODEL',
        'agnes_image_model': 'AGNES_IMAGE_MODEL',
        'agnes_image_fallback_model': (
            'AGNES_IMAGE_FALLBACK_MODEL'
        ),
        'gpt_image_model': 'GPT_IMAGE_MODEL',
        'agnes_503_retry_limit': 'AGNES_503_RETRY_LIMIT',
        'agnes_503_backoff_min_s': 'AGNES_503_BACKOFF_MIN_S',
        'agnes_503_backoff_max_s': 'AGNES_503_BACKOFF_MAX_S',
        'agnes_503_circuit_threshold': (
            'AGNES_503_CIRCUIT_THRESHOLD'
        ),
        'agnes_503_circuit_cooldown_s': (
            'AGNES_503_CIRCUIT_COOLDOWN_S'
        ),
    }
    locked_fields = [
        public_name
        for public_name, config_name in fields.items()
        if os.environ.get(f'CROSSPILOT_{config_name}')
    ]
    return {
        **{
            public_name: cfg.get(config_name, '')
            for public_name, config_name in fields.items()
        },
        'deepseek_key_set': bool(cfg.get('DEEPSEEK_KEY')),
        'agnes_key_set': bool(cfg.get('AGNES_KEY')),
        'gpt_image_key_set': bool(cfg.get('GPT_IMAGE_KEY')),
        'model_profiles': list(list_model_profiles()),
        'prompt_profiles': sorted({
            'production',
            'test',
            cfg.get('PROMPT_PROFILE', 'production'),
        }),
        'locked_fields': locked_fields,
    }


@router.post("/api/settings")
async def save_settings(payload: dict):
    """保存模型和密钥到统一 .env，并热重载全部 Provider。"""
    from crosspilot.config import (
        MODEL_CONFIG_KEYS,
        load_config,
        save_env_values,
    )
    from crosspilot.model_registry import (
        ModelConfigError,
        get_model_registry,
    )

    current = load_config()
    profile = payload.get('model_profile')
    profile_changed = False
    if profile is not None:
        if (
            not isinstance(profile, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', profile)
        ):
            raise HTTPException(400, 'model_profile 格式无效')
        try:
            get_model_registry(profile=profile)
        except ModelConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        profile_changed = profile != current.get('MODEL_PROFILE')

    provider_options = {
        'text_provider': {'deepseek', 'agnes'},
        'vision_provider': {'agnes'},
        'image_gen_provider': {'agnes', 'gpt'},
    }
    config_names = {
        'text_provider': 'TEXT_PROVIDER',
        'vision_provider': 'VISION_PROVIDER',
        'image_gen_provider': 'IMAGE_PROVIDER',
    }
    updates = {}
    if profile_changed:
        updates.update({
            key: ''
            for key in MODEL_CONFIG_KEYS
            if key != 'MODEL_PROFILE'
        })
    else:
        for field, allowed in provider_options.items():
            value = payload.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or value not in allowed:
                choices = ' / '.join(sorted(allowed))
                raise HTTPException(
                    400,
                    f"{field} 不支持 {value!r}，可选: {choices}",
                )
            updates[config_names[field]] = value

    key_fields = {
        'deepseek_key': 'DEEPSEEK_KEY',
        'agnes_key': 'AGNES_KEY',
        'gpt_image_key': 'GPT_IMAGE_KEY',
    }
    for field, config_name in key_fields.items():
        value = payload.get(field)
        if value is None or value == '':
            continue
        if not isinstance(value, str) or len(value) > 4096:
            raise HTTPException(400, f"{field} 格式无效")
        stripped = value.strip()
        if '\n' in stripped or '\r' in stripped:
            raise HTTPException(400, f"{field} 格式无效")
        updates[config_name] = stripped

    if profile is not None:
        updates['MODEL_PROFILE'] = profile

    prompt_profile = payload.get('prompt_profile')
    if prompt_profile is not None:
        if (
            not isinstance(prompt_profile, str)
            or not re.fullmatch(
                r'[A-Za-z0-9_.-]{1,64}',
                prompt_profile,
            )
        ):
            raise HTTPException(400, 'prompt_profile 格式无效')
        updates['PROMPT_PROFILE'] = prompt_profile

    model_fields = {
        'deepseek_text_model': 'DEEPSEEK_TEXT_MODEL',
        'deepseek_text_fallback_model': (
            'DEEPSEEK_TEXT_FALLBACK_MODEL'
        ),
        'agnes_text_model': 'AGNES_TEXT_MODEL',
        'agnes_vision_model': 'AGNES_VISION_MODEL',
        'agnes_image_model': 'AGNES_IMAGE_MODEL',
        'agnes_image_fallback_model': (
            'AGNES_IMAGE_FALLBACK_MODEL'
        ),
        'gpt_image_model': 'GPT_IMAGE_MODEL',
    }
    model_pattern = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}')
    if not profile_changed:
        for field, config_name in model_fields.items():
            value = payload.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not model_pattern.fullmatch(value):
                raise HTTPException(400, f'{field} 模型 ID 格式无效')
            updates[config_name] = value

    congestion_fields = {
        'agnes_503_retry_limit': (
            'AGNES_503_RETRY_LIMIT', 0, 3, True,
        ),
        'agnes_503_backoff_min_s': (
            'AGNES_503_BACKOFF_MIN_S', 0, 60, False,
        ),
        'agnes_503_backoff_max_s': (
            'AGNES_503_BACKOFF_MAX_S', 0, 60, False,
        ),
        'agnes_503_circuit_threshold': (
            'AGNES_503_CIRCUIT_THRESHOLD', 1, 20, True,
        ),
        'agnes_503_circuit_cooldown_s': (
            'AGNES_503_CIRCUIT_COOLDOWN_S', 10, 1800, False,
        ),
    }
    for field, (
        config_name,
        minimum,
        maximum,
        integer_only,
    ) in congestion_fields.items():
        value = payload.get(field)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f'{field} 必须是数字')
        if (
            isinstance(value, bool)
            or not math.isfinite(parsed)
            or parsed < minimum
            or parsed > maximum
            or (integer_only and not parsed.is_integer())
        ):
            raise HTTPException(
                400,
                f'{field} 必须在 {minimum}–{maximum} 范围内',
            )
        updates[config_name] = (
            str(int(parsed))
            if integer_only or parsed.is_integer()
            else format(parsed, 'g')
        )

    backoff_min = float(
        updates.get(
            'AGNES_503_BACKOFF_MIN_S',
            current.get('AGNES_503_BACKOFF_MIN_S', '3'),
        )
    )
    backoff_max = float(
        updates.get(
            'AGNES_503_BACKOFF_MAX_S',
            current.get('AGNES_503_BACKOFF_MAX_S', '8'),
        )
    )
    if backoff_min > backoff_max:
        raise HTTPException(400, 'Agnes 503 最短/最长等待区间无效')

    try:
        save_env_values(updates)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    _reload_runtime()
    return {'ok': True, 'settings': get_settings()}


@router.get("/api/prompts")
def list_prompts():
    from crosspilot.prompt_registry import get_prompt_registry

    registry = get_prompt_registry()
    return {
        'profile': registry.profile,
        'prompts': registry.list_prompts(),
    }


@router.get("/api/prompts/{prompt_id}")
def get_prompt(prompt_id: str):
    from crosspilot.prompt_registry import (
        PromptNotFoundError,
        get_prompt_registry,
    )

    try:
        return get_prompt_registry().metadata(
            prompt_id,
            include_content=True,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(404, exc.args[0]) from exc


@router.post("/api/prompts/{prompt_id}")
def save_prompt(prompt_id: str, payload: dict):
    from crosspilot.prompt_registry import (
        PromptNotFoundError,
        PromptValidationError,
        get_prompt_registry,
    )

    content = payload.get('content')
    reason = payload.get('reason', 'edit')
    if not isinstance(content, str):
        raise HTTPException(400, 'content 必须是文本')
    if not isinstance(reason, str) or len(reason) > 128:
        raise HTTPException(400, 'reason 格式无效')
    try:
        result = get_prompt_registry().save_override(
            prompt_id,
            content,
            reason=reason,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except PromptValidationError as exc:
        raise _prompt_error(exc) from exc
    _reload_runtime()
    return {'ok': True, 'prompt': result}


@router.get("/api/prompts/{prompt_id}/history")
def get_prompt_history(prompt_id: str):
    from crosspilot.prompt_registry import (
        PromptNotFoundError,
        PromptValidationError,
        get_prompt_registry,
    )

    try:
        registry = get_prompt_registry()
        return {
            'profile': registry.profile,
            'revisions': registry.history(prompt_id),
        }
    except PromptNotFoundError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except PromptValidationError as exc:
        raise _prompt_error(exc) from exc


@router.post("/api/prompts/{prompt_id}/rollback")
def rollback_prompt(prompt_id: str, payload: dict):
    from crosspilot.prompt_registry import (
        PromptNotFoundError,
        PromptValidationError,
        get_prompt_registry,
    )

    revision_id = payload.get('revision_id')
    if not isinstance(revision_id, str):
        raise HTTPException(400, 'revision_id 必须是文本')
    try:
        result = get_prompt_registry().rollback(
            prompt_id,
            revision_id,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except PromptValidationError as exc:
        raise _prompt_error(exc) from exc
    _reload_runtime()
    return {'ok': True, 'prompt': result}


@router.delete("/api/prompts/{prompt_id}/override")
def reset_prompt(prompt_id: str):
    from crosspilot.prompt_registry import (
        PromptNotFoundError,
        PromptValidationError,
        get_prompt_registry,
    )

    try:
        result = get_prompt_registry().reset_override(prompt_id)
    except PromptNotFoundError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except PromptValidationError as exc:
        raise _prompt_error(exc) from exc
    _reload_runtime()
    return {'ok': True, 'prompt': result}
