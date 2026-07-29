"""Final Amazon row acceptance rules."""
from __future__ import annotations

import re

from .listing import (
    dedupe_terms,
    is_weak_bullet,
    split_keywords,
)
from .rules import (
    BRAND_RE,
    META_TEXT_RE,
    OEM_RE,
    fingerprint_text,
    plain_text,
)


def validate_amazon_rows(
    rows,
    extra_issues=None,
    row_offset=0,
):
    """Validate final rows and return a bounded review result."""
    issues = list(extra_issues or [])
    for index, row in enumerate(rows, 1):
        row_number = row_offset + index
        label = f"第 {row_number} 行"
        title = str(row.get("title") or "").strip()
        description = plain_text(row.get("desc") or "")
        main_image = str(row.get("main_img") or "").strip()
        bullets = [
            str(item or "").strip()
            for item in list(row.get("bullets") or [])[:5]
        ]
        bullets.extend([""] * (5 - len(bullets)))
        keywords = str(row.get("keywords") or "").strip()
        keyword_terms = dedupe_terms(split_keywords(keywords))

        if (
            not title
            or len(title) > 75
            or META_TEXT_RE.search(title)
        ):
            issues.append(
                f"{label}标题为空、超过 75 字符或疑似模型说明文本"
            )
        if (
            not description
            or BRAND_RE.search(description)
            or OEM_RE.search(description)
            or META_TEXT_RE.search(description)
        ):
            issues.append(
                f"{label}描述为空、含品牌残留或疑似模型说明文本"
            )
        if not re.match(
            r"^https?://",
            main_image,
            re.IGNORECASE,
        ):
            issues.append(f"{label}主图 URL 无效")

        non_empty_bullets = [
            bullet
            for bullet in bullets
            if bullet
        ]
        if len(non_empty_bullets) < 5:
            issues.append(f"{label} Bullet 不足 5 条")
        bullet_fingerprints = [
            fingerprint_text(bullet)
            for bullet in non_empty_bullets
        ]
        if len(set(bullet_fingerprints)) < len(
            bullet_fingerprints
        ):
            issues.append(f"{label} Bullet 存在重复内容")
        if any(
            len(bullet) > 200
            for bullet in non_empty_bullets
        ):
            issues.append(f"{label} Bullet 超过 200 字符")
        if any(
            BRAND_RE.search(bullet)
            or OEM_RE.search(bullet)
            for bullet in non_empty_bullets
        ):
            issues.append(f"{label} Bullet 含品牌或 OEM 残留")
        if any(
            is_weak_bullet(bullet)
            for bullet in non_empty_bullets
        ):
            issues.append(
                f"{label} Bullet 过泛，缺少可检索的产品信息"
            )

        if len(keyword_terms) != 10:
            issues.append(f"{label}关键词需为 10 个有效搜索词")
        if keywords and len(keywords) > 250:
            issues.append(f"{label}关键词超过 250 字符")
        if (
            BRAND_RE.search(keywords)
            or len(keyword_terms) != len(
                split_keywords(keywords)
            )
        ):
            issues.append(
                f"{label}关键词为空、重复、过泛或含品牌"
            )
        if len(issues) >= 20:
            break
    return {
        "passed": not issues,
        "issues": issues[:20],
        "truncated": len(issues) >= 20,
    }


__all__ = ["validate_amazon_rows"]
