"""Safe Amazon product-removal tests."""
from scripts.remove_amazon_products import _remove_from_payload


def _payload():
    fields = {
        '商品id': ['a', 'b', 'c'],
        '产品标题': ['A', 'B', 'C'],
        '产品描述': ['AD', 'BD', 'CD'],
        '产品图片链接': [
            ['https://img/a.jpg'],
            ['https://img/b.jpg'],
            ['https://img/c.jpg'],
        ],
        '变种图片链接': [[], [], []],
        'Bullet Point1': ['a1', 'b1', 'c1'],
        'Bullet Point2': ['a2', 'b2', 'c2'],
        'Bullet Point3': ['a3', 'b3', 'c3'],
        'Bullet Point4': ['a4', 'b4', 'c4'],
        'Bullet Point5': ['a5', 'b5', 'c5'],
        '关键词信息': ['ak', 'bk', 'ck'],
        '有问题的产品id': ['b', 'c'],
    }
    return fields


def test_remove_exact_product_keeps_all_columns_aligned():
    payload = _payload()

    removed = _remove_from_payload(payload, ['b'])

    assert payload['商品id'] == ['a', 'c']
    assert payload['产品标题'] == ['A', 'C']
    assert payload['Bullet Point5'] == ['a5', 'c5']
    assert payload['产品图片链接'] == [
        ['https://img/a.jpg'],
        ['https://img/c.jpg'],
    ]
    assert payload['有问题的产品id'] == ['c']
    assert removed[0]['row'] == 2


def test_remove_rejects_missing_product_id():
    payload = _payload()

    try:
        _remove_from_payload(payload, ['missing'])
    except ValueError as exc:
        assert '实际 0 行' in str(exc)
    else:
        raise AssertionError('missing ID should fail closed')
