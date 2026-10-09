from pathlib import Path
import importlib.util


def load_scanner():
    path = Path(__file__).resolve().parents[1]/"scripts/check_release.py"
    spec = importlib.util.spec_from_file_location("release_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_tree_hygiene():
    root = Path(__file__).resolve().parents[1]
    assert not load_scanner().check(root)["findings"]


def test_scanner_flags_literal_secret_without_echoing_it(tmp_path):
    value = "ab" * 20
    (tmp_path/"example.py").write_text("SERVICE_API_KEY = '"+value+"'\n")
    result = load_scanner().check(tmp_path)
    assert result["findings"][0]["rule"] == "literal_credential"
    assert value not in str(result)


def test_scanner_rejects_env_and_font_files(tmp_path):
    (tmp_path/".env").write_text("KEY=")
    (tmp_path/"font.ttf").write_bytes(b"not-a-real-font")
    result = load_scanner().check(tmp_path)
    assert {x["rule"] for x in result["findings"]} == {"private_env_file", "forbidden_distribution_file"}


def test_strict_scan_does_not_hide_private_files_in_bundle(tmp_path):
    private = tmp_path / "configs" / "output" / "job"
    private.mkdir(parents=True)
    (private / "config.json").write_text('{}')
    (tmp_path / "configs" / "local-personal.yaml").write_text('version: 2')
    assert not load_scanner().check(tmp_path)["findings"]
    strict = load_scanner().check(tmp_path, strict=True)
    assert len(strict["findings"]) == 2
    assert all(x["rule"] == "private_or_generated_bundle_file" for x in strict["findings"])


def test_current_scene_ir_audit_accepts_id_field(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "audit_outputs.py"
    spec = importlib.util.spec_from_file_location("audit_outputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "scene_plan.json"
    plan.write_text('{"scenes":[{"id":"INTRO","start":0,"end":1},{"id":"A","start":1,"end":2},{"id":"OUTRO","start":2,"end":3}]}')
    assert module.scene_info(plan, ["INTRO", "A", "OUTRO"])["canonical_labels"]
