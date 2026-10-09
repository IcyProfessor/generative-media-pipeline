import json
from types import SimpleNamespace
import pytest
import httpx
from genmedia.providers.suno import SunoAudio, SunoAlignment, public_https
from genmedia.providers.suno_schema import song_text
from genmedia.providers.gemini import GeminiText
from genmedia.providers.base import AlignmentRequest
from genmedia.providers.factory import validate_selection
from genmedia.models import Candidate, Script, ProviderSpec, SunoConfig
from genmedia.errors import ProviderError, SubmissionUncertain, PipelineError
from genmedia.providers.suno_schema import normalize_suno

def make_client(cfg, handler):
    cfg.suno.callback_url = "https://owned.example/callback"

    return SunoAudio(
        cfg.suno,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )

@pytest.mark.parametrize("field",["audioUrl","audio_url"])
def test_suno_response_key_versions(cfg,field):
    provider = make_client(cfg,lambda r:httpx.Response(200,json={"code":200,"data":{
        "status":"SUCCESS","response":{"sunoData":[{"id":"one",field:"https://cdn.example/audio.mp3"}]}}}))
    assert provider.poll("task")[0].id == "one"
    provider.close()


@pytest.mark.parametrize("status",["PENDING","TEXT_SUCCESS","FIRST_SUCCESS"])
def test_pending_status(cfg,status):
    provider = make_client(cfg,lambda r:httpx.Response(200,json={"code":200,"data":{"status":status}}))
    assert provider.poll("task") is None
    provider.close()


@pytest.mark.parametrize("status",["CREATE_TASK_FAILED","GENERATE_AUDIO_FAILED","CALLBACK_EXCEPTION","SENSITIVE_WORD_ERROR","UNKNOWN"])
def test_terminal_status(cfg,status):
    provider = make_client(cfg,lambda r:httpx.Response(200,json={"code":200,"data":{"status":status}}))
    with pytest.raises(ProviderError): provider.poll("task")
    provider.close()


def test_paid_post_is_not_retried(cfg,script):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated",request=request)
    provider = make_client(cfg,handler)
    with pytest.raises(SubmissionUncertain): provider.submit(script,cfg.audio)
    assert len(calls) == 1
    provider.close()


def test_business_error_not_http_success(cfg):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={"code":429,"msg":"no credits","data":{}})
    provider=make_client(cfg,handler)
    with pytest.raises(ProviderError,match="429"): provider.poll("task")
    assert len(calls)==1
    provider.close()


def test_safe_get_bounded_retries(cfg):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    provider=make_client(cfg,handler)
    with pytest.raises(ProviderError): provider.poll("task")
    assert len(calls)==3
    provider.close()


def test_documented_prompt_payload_default(cfg,script):
    payloads=[]
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,json={"code":200,"data":{"taskId":"new-task"}})
    provider=make_client(cfg,handler)
    assert provider.submit(script,cfg.audio)=="new-task"
    assert payloads[0]["prompt"]==song_text(script)
    assert "lyrics" not in payloads[0] and "duration" not in payloads[0]
    assert payloads[0]["customMode"] and payloads[0]["instrumental"] is False
    provider.close()


def test_undocumented_legacy_payload_requires_explicit_optin(cfg,script):
    cfg.suno.request_format="legacy_lyrics"
    cfg.suno.model="V6"
    payloads=[]
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,json={"code":200,"data":{"taskId":"legacy-task"}})
    provider=make_client(cfg,handler)
    provider.submit(script,cfg.audio)
    assert payloads[0]["lyrics"]==song_text(script)
    assert payloads[0]["duration"]==cfg.audio.duration_seconds
    assert "prompt" not in payloads[0]
    provider.close()


def test_model_name_never_silently_changes_payload_schema(cfg,script):
    cfg.suno.model="V6"
    sent=[]
    def handler(r):
        sent.append(json.loads(r.content))
        return httpx.Response(200,json={"code":200,"data":{"taskId":"task"}})
    p=make_client(cfg,handler)
    p.submit(script,cfg.audio)
    assert "prompt" in sent[0] and "lyrics" not in sent[0]
    p.close()


def test_download_does_not_forward_api_auth_even_after_redirect(cfg,tmp_path):
    requests=[]
    def handler(r):
        requests.append(r)
        return httpx.Response(302,headers={"location":"https://other.example/song.mp3"}) if len(requests)==1 else httpx.Response(200,content=b"fixture")
    provider=SunoAudio(cfg.suno,
        client=httpx.Client(headers={"Authorization":"Bearer test-only"},transport=httpx.MockTransport(handler)),
        download_client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.download(Candidate(id="one",url="https://cdn.example/song.mp3"),tmp_path/"audio.mp3")
    assert len(requests)==2 and all("authorization" not in r.headers for r in requests)
    provider.close()


@pytest.mark.parametrize("url",["http://cdn.example/file","https://127.0.0.1/x","https://localhost/x", "https://localhost./x", "https://name:pass@cdn.example/x", "https://[::1]/x"])
def test_download_url_guard(url):
    with pytest.raises(ProviderError): public_https(url)


def test_gemini_structured_request(cfg,catalog,recipe,script):
    observed=[]
    def generate_content(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(parsed=script,text=script.model_dump_json(),usage_metadata=None)
    client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),close=lambda:None)
    provider=GeminiText(cfg.gemini.model,client=client)
    assert provider.generate(recipe.script_prompt(catalog,42),Script)==script

    request_schema = observed[0]["config"]["response_json_schema"]

    assert "response_schema" not in observed[0]["config"]

    assert request_schema["properties"]["version"]["enum"] == [1]

    schema_json = json.dumps(request_schema)
    assert '"const"' not in schema_json
    assert '"default"' not in schema_json
    assert '"pattern"' not in schema_json
    assert '"minLength"' not in schema_json
    assert '"maxLength"' not in schema_json
    assert '"minItems"' not in schema_json
    assert '"maxItems"' not in schema_json
    assert "D:/" not in observed[0]["contents"]
    provider.close()


def test_gemini_errors_are_sanitized(cfg,catalog,recipe):
    def generate_content(**kwargs): raise RuntimeError("url?key=do-not-print-this")
    client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),close=lambda:None)
    provider=GeminiText(cfg.gemini.model,client=client)
    with pytest.raises(ProviderError) as error:
        provider.generate(recipe.script_prompt(catalog,42),Script)
    assert "do-not-print-this" not in str(error.value)
    provider.close()


def test_alignment_normalized_at_adapter_boundary(cfg,script,raw,tmp_path):
    sent=[]
    def handler(r):
        sent.append(json.loads(r.content))
        return httpx.Response(200,json={"code":200,"data":{"alignedWords":raw}})
    p=SunoAlignment(cfg.suno,client=httpx.Client(transport=httpx.MockTransport(handler)))
    t=p.align(AlignmentRequest("task",Candidate(id="candidate",url="https://cdn.example/a.mp3"),tmp_path/"a.mp3",script))
    assert t.provider=="suno" and t.spans[0].text==raw[0]["word"]
    assert sent==[{"taskId":"task","audioId":"candidate"}]
    p.close()


def test_alignment_post_not_retried(cfg,script,tmp_path):
    requests=[]
    def handler(r):
        requests.append(r)
        return httpx.Response(503)
    p=SunoAlignment(cfg.suno,client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError):
        p.align(AlignmentRequest("task",Candidate(id="x",url="https://cdn.example/a.mp3"),tmp_path/"a.mp3",script))
    assert len(requests)==1
    p.close()


def test_synthetic_alignment_cannot_validate_real_audio(cfg):
    cfg.providers=ProviderSpec(text="fixture",audio="suno",alignment="fixture")
    with pytest.raises(PipelineError,match="Synthetic"): validate_selection(cfg)


def test_unknown_provider_rejected_before_job(cfg):
    cfg.providers.audio="unregistered"
    with pytest.raises(PipelineError,match="Unknown"): validate_selection(cfg)


def test_duration_is_optin_and_only_documented_for_v55(cfg, script):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "duration-task"}})
    cfg.suno.include_duration = True
    p = make_client(cfg, handler)
    try:
        with pytest.raises(ProviderError, match="V5_5"):
            p.preflight_submit()
        assert not sent
        cfg.suno.model = "V5_5"
        p.submit(script, cfg.audio)
        assert sent[0]["duration"] == cfg.audio.duration_seconds
        assert "prompt" in sent[0] and "lyrics" not in sent[0]
    finally:
        p.close()

def test_normalize_suno_documented_snake_case_timestamps():
    transcript = normalize_suno([
        {
            "word": "[Verse]\nApple",
            "success": True,
            "start_s": 1.36,
            "end_s": 1.79,
            "p_align": 0,
        },
        {
            "word": "balloon",
            "success": True,
            "start_s": 1.80,
            "end_s": 2.30,
            "p_align": 0,
        },
    ])

    assert transcript.provider == "suno"
    assert len(transcript.spans) == 2

    assert transcript.spans[0].text == "Apple"
    assert transcript.spans[0].start == 1.36
    assert transcript.spans[0].end == 1.79

    assert transcript.spans[1].text == "balloon"

def test_normalize_suno_legacy_camel_case_timestamps():
    transcript = normalize_suno([
        {
            "word": "legacy",
            "success": True,
            "startS": 2.0,
            "endS": 2.5,
        }
    ])

    assert transcript.spans[0].text == "legacy"
    assert transcript.spans[0].start == 2.0
    assert transcript.spans[0].end == 2.5

def test_normalize_suno_repairs_zero_duration_word():
    transcript = normalize_suno([
        {
            "word": "a",
            "success": True,
            "startS": 37.021,
            "endS": 37.021,
            "palign": 0,
        }
    ])

    span = transcript.spans[0]

    assert span.text == "a"
    assert span.start == 37.021
    assert span.end > span.start
    assert span.end - span.start < 0.002