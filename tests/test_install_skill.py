from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_install_skill():
    spec = spec_from_file_location("install_skill", ROOT / "scripts" / "install_skill.py")
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_install_skill_copies_without_pip(tmp_path):
    module = _load_install_skill()
    dest = tmp_path / "skills"
    assert module.main([str(dest), "--skip-pip"]) == 0
    assert (dest / "qa-orchestrator" / "SKILL.md").is_file()
    assert (dest / "qa-test-design" / "SKILL.md").is_file()
    assert (dest / "qa-testcase-generator" / "SKILL.md").is_file()
