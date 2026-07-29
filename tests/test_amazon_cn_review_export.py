"""Chinese Amazon review-package exporter tests."""
from scripts.export_amazon_cn_review import (
    _valid_translation,
    build_review_rows,
    render_html,
)


def _payload():
    return {
        '商品id': ['item-1'],
        '产品标题': ['English title'],
        '产品描述': ['English description'],
        '产品图片链接': [[
            'https://img/main.jpg',
            'https://img/extra.jpg',
        ]],
        '变种图片链接': [['https://img/variant.jpg']],
        'Bullet Point1': ['One'],
        'Bullet Point2': ['Two'],
        'Bullet Point3': ['Three'],
        'Bullet Point4': ['Four'],
        'Bullet Point5': ['Five'],
        '关键词信息': ['one, two'],
        '有问题的产品id': [],
    }


def _translation():
    return {
        'title': '中文标题',
        'description': '中文描述',
        'bullets': ['要点一', '要点二', '要点三', '要点四', '要点五'],
        'keywords': '关键词一，关键词二',
    }


def test_translation_requires_five_chinese_bullets():
    source = {
        'title': 'English title',
        'description': 'Description',
        'bullets': ['1', '2', '3', '4', '5'],
        'keywords': 'one, two',
    }

    assert _valid_translation(source, _translation()) is True
    invalid = _translation()
    invalid['bullets'] = invalid['bullets'][:4]
    assert _valid_translation(source, invalid) is False


def test_review_rows_include_all_image_roles_and_local_paths():
    mapping = {
        'https://img/main.jpg': {
            'ok': True,
            'path': '图片/main.jpg',
        },
        'https://img/extra.jpg': {
            'ok': True,
            'path': '图片/extra.jpg',
        },
        'https://img/variant.jpg': {
            'ok': True,
            'path': '图片/variant.jpg',
        },
    }

    rows = build_review_rows(_payload(), [_translation()], mapping)

    assert [image['role'] for image in rows[0]['images']] == [
        '主图',
        '附图 1',
        '变种图 1',
    ]
    assert all(image['download_ok'] for image in rows[0]['images'])


def test_html_contains_chinese_copy_and_every_image():
    mapping = {
        url: {'ok': True, 'path': f'图片/{index}.jpg'}
        for index, url in enumerate((
            'https://img/main.jpg',
            'https://img/extra.jpg',
            'https://img/variant.jpg',
        ))
    }
    rows = build_review_rows(_payload(), [_translation()], mapping)

    result = render_html(rows, {
        'products': 1,
        'image_occurrences': 3,
        'downloaded_unique_images': 3,
        'unique_images': 3,
    })

    assert '中文标题' in result
    assert result.count('<img ') == 3
    assert '主图' in result
    assert '附图 1' in result
    assert '变种图 1' in result
