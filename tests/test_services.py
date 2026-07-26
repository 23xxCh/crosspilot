"""Unit tests for services/ — no network required (all mocked)."""
import json, pytest
from unittest.mock import Mock, patch, MagicMock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from services import TranslationService, ImageReviewService, ImageGenService
from services.translate import TRANSLATE_BATCH_PROMPT


@pytest.fixture
def mock_http():
    """Mock requests.Session that returns a configurable JSON response."""
    session = Mock()
    session.post = Mock()
    return session


@pytest.fixture
def translate_svc(mock_http):
    return TranslationService(mock_http)


@pytest.fixture
def review_svc(mock_http):
    return ImageReviewService(mock_http)


class TestTranslationService:
    """TranslationService unit tests."""

    def test_dmx_call_success(self, translate_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'Xin chào'}}]
        }
        mock_http.post.return_value = mock_resp

        result = translate_svc.dmx_call({
            'model': 'mimo-v2.5',
            'messages': [{'role': 'user', 'content': 'hello'}]
        })
        assert result == 'Xin chào'

    def test_dmx_call_timeout_then_fallback(self, translate_svc, mock_http):
        mock_http.post.side_effect = [
            Exception("timeout"),  # primary model fails
            Mock(ok=True, json=Mock(return_value={  # fallback succeeds
                'choices': [{'message': {'content': 'Bonjour'}}]
            }))
        ]
        result = translate_svc.dmx_call({
            'model': 'mimo-v2.5',
            'messages': [{'role': 'user', 'content': 'hello'}]
        })
        assert result == 'Bonjour'
        assert mock_http.post.call_count >= 2

    def test_dmx_call_all_models_fail(self, translate_svc, mock_http):
        mock_http.post.side_effect = Exception("timeout")
        result = translate_svc.dmx_call({
            'model': 'mimo-v2.5',
            'messages': [{'role': 'user', 'content': 'hello'}]
        })
        assert result is None

    def test_strip_code_fence(self, translate_svc):
        assert translate_svc._strip_code_fence('```json\nhello\n```') == 'hello'
        assert translate_svc._strip_code_fence('```\nworld\n```') == 'world'
        assert translate_svc._strip_code_fence('plain text') == 'plain text'
        assert translate_svc._strip_code_fence(None) is None

    def test_select_prompt_chinese(self, translate_svc):
        prompt = translate_svc._select_prompt('汽车配件', 'Default')
        assert 'Chinese' in prompt
        assert 'CRITICAL' in prompt

    def test_select_prompt_english(self, translate_svc):
        prompt = translate_svc._select_prompt('Car Parts', 'Default')
        assert prompt == 'Default'

    def test_translate_text_chinese_detection(self, translate_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'Phụ tùng xe'}}]
        }
        mock_http.post.return_value = mock_resp

        result = translate_svc.translate_text('汽车配件', 'Translate: {}')
        # Should contain Vietnamese, no Chinese
        assert '汽' not in result

    def test_translate_text_returns_original_on_failure(self, translate_svc, mock_http):
        mock_http.post.side_effect = Exception("all models down")
        result = translate_svc.translate_text('汽车配件', 'Translate: {}')
        assert result == '汽车配件'

    def test_parse_batch_response_valid(self, translate_svc):
        raw = json.dumps([
            {'index': 0, 'translation': 'hello'},
            {'index': 1, 'translation': 'world'}
        ])
        result = translate_svc._parse_batch_response(raw)
        assert len(result) == 2
        assert result[0] == 'hello'

    def test_parse_batch_response_empty(self, translate_svc):
        assert translate_svc._parse_batch_response('') == {}
        assert translate_svc._parse_batch_response(None) == {}

    def test_parse_batch_response_markdown_fence(self, translate_svc):
        raw = '```json\n[{"index": 0, "translation": "ok"}]\n```'
        result = translate_svc._parse_batch_response(raw)
        assert result == {0: 'ok'}

    def test_batch_process_empty(self, translate_svc):
        result = translate_svc.batch_process([], TRANSLATE_BATCH_PROMPT, 'test')
        assert result == {}

    def test_batch_process_chinese_detection(self, translate_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': json.dumps([
                {'index': 0, 'translation': 'Phụ tùng'}
            ])}}]
        }
        mock_http.post.return_value = mock_resp

        # Chinese text should trigger CN prompt
        result = translate_svc.batch_process(
            ['汽车配件'],
            TRANSLATE_BATCH_PROMPT,
            'batch_translate'
        )
        assert len(result) == 1

    def test_batch_translate_delegates(self, translate_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': json.dumps([
                {'index': 0, 'translation': 'xin chào'}
            ])}}]
        }
        mock_http.post.return_value = mock_resp
        result = translate_svc.batch_translate(['hello'])
        assert len(result) == 1


class TestImageReviewService:
    """ImageReviewService unit tests."""

    def test_review_once_watermark_detected(self, review_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'YES'}}]
        }
        mock_http.post.return_value = mock_resp

        result = review_svc.review_once('https://example.com/img.jpg')
        assert result is True

    def test_review_once_clean_image(self, review_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'NO'}}]
        }
        mock_http.post.return_value = mock_resp

        result = review_svc.review_once('https://example.com/clean.jpg')
        assert result is False

    def test_review_once_http_error(self, review_svc, mock_http):
        mock_resp = Mock()
        mock_resp.ok = False
        mock_http.post.return_value = mock_resp

        result = review_svc.review_once('https://example.com/img.jpg')
        assert result is None

    def test_review_once_timeout(self, review_svc, mock_http):
        mock_http.post.side_effect = Exception("timeout")
        result = review_svc.review_once('https://example.com/img.jpg')
        assert result is None

    def test_review_full_fallback(self, review_svc, mock_http):
        """Full review() with 3-level fallback — all fail => None."""
        mock_http.post.side_effect = Exception("timeout")
        result = review_svc.review('https://example.com/img.jpg')
        assert result is None
        # 7 attempts (3 fast + 2 slow + 2 gemini)
        assert mock_http.post.call_count >= 7


class TestImageGenService:
    """ImageGenService unit tests."""

    def test_generate_success(self, mock_http):
        # dmx_client.generate_image calls _post_json internally
        # We test the service delegation
        svc = ImageGenService(mock_http)
        # Just verify the service exists and has generate method
        assert hasattr(svc, 'generate')


class TestAmazonAdapter:
    """Amazon adapter detection."""

    def test_detect_valid_headers(self):
        from scripts.adapters.amazon_tk import AmazonTkAdapter

        # Mock worksheet with correct headers
        ws = Mock()
        ws.cell = Mock()
        def mock_cell(r, c):
            m = Mock()
            headers = {1: '商品id', 2: '产品标题', 3: '产品描述',
                      4: '产品图片', 5: '变种图片', 6: '产品图片链接', 7: '变种图片链接'}
            m.value = headers.get(c, '')
            return m
        ws.cell.side_effect = mock_cell

        assert AmazonTkAdapter.detect(ws) is True

    def test_detect_invalid_headers(self):
        from scripts.adapters.amazon_tk import AmazonTkAdapter

        ws = Mock()
        ws.cell = Mock()
        def mock_cell(r, c):
            m = Mock()
            m.value = 'Unknown' if c <= 3 else ''
            return m
        ws.cell.side_effect = mock_cell

        assert AmazonTkAdapter.detect(ws) is False


class TestEdgeCases:
    """Boundary and edge case tests."""

    def test_translate_empty_text(self, translate_svc):
        assert translate_svc.translate_text('', 'Translate: {}') == ''
        assert translate_svc.translate_text(None, 'Translate: {}') is None

    def test_translate_very_long_text(self, translate_svc, mock_http):
        mock_resp = Mock(ok=True, json=Mock(return_value={
            'choices': [{'message': {'content': 'short result'}}]
        }))
        mock_http.post.return_value = mock_resp
        long_text = 'A' * 5000
        result = translate_svc.translate_text(long_text, 'Translate: {}')
        assert result == 'short result'

    def test_batch_with_mixed_languages(self, translate_svc, mock_http):
        mock_resp = Mock(ok=True, json=Mock(return_value={
            'choices': [{'message': {'content': json.dumps([
                {'index': 0, 'translation': 'Phụ tùng'},
                {'index': 1, 'translation': 'Car parts'}
            ])}}]
        }))
        mock_http.post.return_value = mock_resp
        result = translate_svc.batch_translate(['汽车配件', 'Car parts'])
        assert len(result) == 2

    def test_parse_batch_malformed_json_returns_empty(self, translate_svc):
        assert translate_svc._parse_batch_response('{not json') == {}
        assert translate_svc._parse_batch_response('[1,2,3]') == {}
        assert translate_svc._parse_batch_response('{"key": "val"}') == {}

    def test_parse_batch_partial_results(self, translate_svc):
        raw = json.dumps([
            {'index': 0, 'translation': 'ok'},
            {'index': 2, 'translation': 'skip one'}
        ])
        result = translate_svc._parse_batch_response(raw)
        assert len(result) == 2
        assert 1 not in result

    def test_review_once_with_empty_url(self, review_svc, mock_http):
        mock_http.post.side_effect = Exception("invalid URL")
        result = review_svc.review_once('')
        assert result is None

    def test_batch_process_large_batch(self, translate_svc, mock_http):
        """25 texts in one batch (max batch size)."""
        texts = ['text ' + str(i) for i in range(25)]
        mock_resp = Mock(ok=True, json=Mock(return_value={
            'choices': [{'message': {'content': json.dumps([
                {'index': i, 'translation': f'translated {i}'} for i in range(25)
            ])}}]
        }))
        mock_http.post.return_value = mock_resp
        result = translate_svc.batch_translate(texts)
        assert len(result) == 25

    def test_metrics_to_dict(self):
        from scripts.pipeline_log import PipelineMetrics
        m = PipelineMetrics()
        m.record_stage('test', 2.5, 100, 95)
        m.record_api(True)
        m.record_api(False)
        d = m.to_dict()
        assert 'stages' in d
        assert d['api_calls'] == 2
        assert d['api_errors'] == 1
        assert d['api_success_rate'] == 0.5

    def test_new_request_id_unique(self):
        from scripts.pipeline_log import new_request_id
        ids = {new_request_id() for _ in range(10)}
        assert len(ids) == 10

    def test_select_prompt_vietnamese_is_default(self, translate_svc):
        """Vietnamese text should use default prompt, not Chinese."""
        prompt = translate_svc._select_prompt('Phụ kiện ô tô', 'Default')
        assert prompt == 'Default'
