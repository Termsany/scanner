import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "raw",
    [
        "https://integrate.api.nvidia.com/v1",
        "https://integrate.api.nvidia.com/v1/",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        " https://integrate.api.nvidia.com/v1/chat/completions/ ",
    ],
)
def test_llm_base_url_strips_endpoint_path(raw):
    assert Settings(llm_base_url=raw).llm_base_url == "https://integrate.api.nvidia.com/v1"


def test_llm_extra_body_parses_json_env(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}')
    assert Settings().llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
