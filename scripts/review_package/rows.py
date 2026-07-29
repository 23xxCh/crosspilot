"""Build the final-review product and image data model."""
from __future__ import annotations

def _row_images(
    payload: dict,
    mapping: dict[str, dict],
    index: int,
    audit_images: list[dict] | None = None,
) -> list[dict]:
    audit_images = audit_images or []

    def audit_for(url: str, role_key: str) -> dict:
        candidates = [
            item
            for item in audit_images
            if item.get('url') == url
            and item.get('role') == role_key
        ]
        if not candidates:
            return {}
        return candidates[-1]

    def image_record(
        *,
        role: str,
        role_key: str,
        url: str,
        position: int,
    ) -> dict:
        audit = audit_for(url, role_key)
        assessment = audit.get('assessment') or {}
        return {
            'role': role,
            'role_key': role_key,
            'position': position,
            'url': url,
            'local_path': (mapping.get(url) or {}).get('path', ''),
            'download_ok': bool((mapping.get(url) or {}).get('ok')),
            'source': audit.get('source') or 'source',
            'source_url': audit.get('source_url') or '',
            'source_local_path': (
                (mapping.get(audit.get('source_url')) or {}).get(
                    'path',
                    '',
                )
                if audit.get('source_url') else ''
            ),
            'assessment': assessment,
            'decision': audit.get('decision') or '',
            'evidence': (
                audit.get('evidence')
                or assessment.get('evidence')
                or ''
            ),
        }

    images = []
    for position, url in enumerate(payload['产品图片链接'][index]):
        role = '主图' if position == 0 else f'附图 {position}'
        images.append(image_record(
            role=role,
            role_key='main' if position == 0 else 'attachment',
            url=url,
            position=position,
        ))
    for position, url in enumerate(
        payload['变种图片链接'][index],
        start=1,
    ):
        images.append(image_record(
            role=f'变种图 {position}',
            role_key='variant',
            url=url,
            position=position,
        ))
    return images


def build_review_rows(
    payload: dict,
    translations: list[dict],
    mapping: dict[str, dict],
    audit_by_product: dict[str, list[dict]] | None = None,
) -> list[dict]:
    audit_by_product = audit_by_product or {}
    return [
        {
            'row': index + 1,
            'product_id': payload['商品id'][index],
            **translations[index],
            'images': _row_images(
                payload,
                mapping,
                index,
                audit_by_product.get(
                    str(payload['商品id'][index]),
                    [],
                ),
            ),
            'quarantined': False,
            'quarantine_reasons': [],
        }
        for index in range(len(payload['商品id']))
    ]


def build_quarantine_rows(
    quarantine_products: list[dict],
    mapping: dict[str, dict],
    *,
    row_offset: int,
) -> list[dict]:
    rows = []
    for index, item in enumerate(quarantine_products):
        source_row = item.get('source_row') or {}
        images = []
        for position, image in enumerate(item.get('images') or []):
            url = str(image.get('url') or '')
            if not url:
                continue
            assessment = image.get('assessment') or {}
            images.append({
                'role': {
                    'main': '主图',
                    'variant': '变种图',
                    'attachment': '附图',
                }.get(image.get('role'), str(image.get('role') or '图片')),
                'role_key': image.get('role') or 'attachment',
                'position': position,
                'url': url,
                'local_path': (mapping.get(url) or {}).get('path', ''),
                'download_ok': bool((mapping.get(url) or {}).get('ok')),
                'source': image.get('source') or 'source',
                'source_url': image.get('source_url') or '',
                'source_local_path': (
                    (
                        mapping.get(image.get('source_url')) or {}
                    ).get('path', '')
                    if image.get('source_url') else ''
                ),
                'assessment': assessment,
                'decision': image.get('decision') or '',
                'evidence': (
                    image.get('evidence')
                    or assessment.get('evidence')
                    or ''
                ),
            })
        bullets = source_row.get('bullets') or []
        rows.append({
            'row': row_offset + index + 1,
            'product_id': str(item.get('product_id') or ''),
            'title': str(
                source_row.get('title')
                or item.get('title')
                or ''
            ),
            'description': str(source_row.get('description') or ''),
            'bullets': [
                str(bullets[i] or '') if i < len(bullets) else ''
                for i in range(5)
            ],
            'keywords': str(source_row.get('keywords') or ''),
            'images': images,
            'quarantined': True,
            'quarantine_reasons': item.get('reasons') or [],
        })
    return rows



__all__ = ["build_quarantine_rows", "build_review_rows"]
