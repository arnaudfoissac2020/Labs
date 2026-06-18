import pytest
import requests
from unittest.mock import MagicMock, patch

import query_helper
from query_helper import run_graphql_query, fetch_defillama, DEFILLAMA_BASE_URL


class TestRunGraphqlQuery:
    def _mock_response(self, data: dict):
        mock = MagicMock()
        mock.json.return_value = data
        mock.raise_for_status.return_value = None
        return mock

    def test_returns_data_field(self):
        payload = {"data": {"markets": [{"id": "0x1"}]}}
        with patch("query_helper.requests.post", return_value=self._mock_response(payload)) as mock_post:
            result = run_graphql_query("https://example.com/subgraph", "{ markets { id } }")
        assert result == {"markets": [{"id": "0x1"}]}

    def test_raises_value_error_on_graphql_errors(self):
        payload = {"errors": [{"message": "field not found"}]}
        with patch("query_helper.requests.post", return_value=self._mock_response(payload)):
            with pytest.raises(ValueError, match="Erreur GraphQL"):
                run_graphql_query("https://example.com/subgraph", "{ bad }")

    def test_includes_variables_when_provided(self):
        payload = {"data": {}}
        with patch("query_helper.requests.post", return_value=self._mock_response(payload)) as mock_post:
            run_graphql_query("https://example.com/subgraph", "{ q }", variables={"id": "abc"})
        sent = mock_post.call_args.kwargs["json"]
        assert sent["variables"] == {"id": "abc"}

    def test_omits_variables_when_none(self):
        payload = {"data": {}}
        with patch("query_helper.requests.post", return_value=self._mock_response(payload)) as mock_post:
            run_graphql_query("https://example.com/subgraph", "{ q }")
        sent = mock_post.call_args.kwargs["json"]
        assert "variables" not in sent

    def test_propagates_http_error(self):
        mock = MagicMock()
        mock.raise_for_status.side_effect = requests.HTTPError("404")
        with patch("query_helper.requests.post", return_value=mock):
            with pytest.raises(requests.HTTPError):
                run_graphql_query("https://example.com/subgraph", "{ q }")


class TestFetchDefillama:
    def _mock_response(self, data: dict):
        mock = MagicMock()
        mock.json.return_value = data
        mock.raise_for_status.return_value = None
        return mock

    def test_constructs_correct_url(self):
        with patch("query_helper.requests.get", return_value=self._mock_response({})) as mock_get:
            fetch_defillama("/protocol/aave-v3")
        url = mock_get.call_args.args[0]
        assert url == f"{DEFILLAMA_BASE_URL}/protocol/aave-v3"

    def test_returns_json(self):
        payload = {"tvl": 1_000_000}
        with patch("query_helper.requests.get", return_value=self._mock_response(payload)):
            result = fetch_defillama("/protocol/aave-v3")
        assert result == payload

    def test_propagates_http_error(self):
        mock = MagicMock()
        mock.raise_for_status.side_effect = requests.HTTPError("503")
        with patch("query_helper.requests.get", return_value=mock):
            with pytest.raises(requests.HTTPError):
                fetch_defillama("/protocol/aave-v3")
