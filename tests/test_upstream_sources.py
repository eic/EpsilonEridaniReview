"""The reviewer workspace stages every direct dependency, not only Mathlib."""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import cli  # noqa: E402
import review  # noqa: E402


MANIFEST = {"packages": [
    {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4",
     "rev": "a" * 40, "inherited": False},
    {"name": "Physlib", "url": "https://github.com/leanprover-community/physlib",
     "rev": "b" * 40, "inherited": False},
    {"name": "TauCeti", "url": "https://github.com/TauCetiProject/TauCeti",
     "rev": "c" * 40, "inherited": False},
    {"name": "batteries", "url": "https://github.com/leanprover-community/batteries",
     "rev": "d" * 40, "inherited": True},
    {"name": "broken", "inherited": False},
]}


def test_direct_dependencies_are_the_non_inherited_well_formed_packages():
    deps = cli.direct_dependencies(json.dumps(MANIFEST))
    assert [d["name"] for d in deps] == ["mathlib", "Physlib", "TauCeti"]
    assert deps[1]["url"].endswith("/physlib") and deps[1]["rev"] == "b" * 40


def test_direct_dependencies_tolerate_garbage():
    assert cli.direct_dependencies("not json") == []
    assert cli.direct_dependencies(json.dumps({"packages": "nope"})) == []
    assert cli.direct_dependencies(json.dumps({"packages": [{"name": "«x»", "url": "u",
                                                            "rev": "r", "inherited": False}]})) \
        == [{"name": "x", "url": "u", "rev": "r"}]


def test_upstream_context_names_the_libraries_each_tree_provides():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for lib in ("Physlib", "PhyslibAlpha", "QuantumInfo"):
            (root / "upstream/Physlib" / lib).mkdir(parents=True)
            (root / "upstream/Physlib" / f"{lib}.lean").touch()
        (root / "upstream/Physlib/scripts").mkdir()          # no root module: not a library
        (root / "upstream/mathlib/Mathlib").mkdir(parents=True)
        (root / "upstream/mathlib/Mathlib.lean").touch()
        text = review.upstream_sources_context(
            ["mathlib=upstream/mathlib", "Physlib=upstream/Physlib", "TauCeti=upstream/TauCeti"],
            tmp)
    lines = text.splitlines()
    assert lines[0] == ("- Mathlib source (libraries `Mathlib`): `./upstream/mathlib` "
                        "(grep before claiming a declaration exists).")
    assert lines[1] == ("- Physlib source (libraries `Physlib`, `PhyslibAlpha`, `QuantumInfo`): "
                        "`./upstream/Physlib` (grep before claiming a declaration exists).")
    assert lines[2].startswith("- TauCeti source: `./upstream/TauCeti`")   # not staged: no libraries


def test_upstream_context_rejects_a_spec_without_a_path():
    try:
        review.upstream_sources_context(["mathlib"], ".")
    except SystemExit as e:
        assert "NAME=PATH" in str(e)
    else:
        raise AssertionError("accepted a malformed --upstream-path")


def test_workflows_stage_every_direct_dependency_from_the_base_manifest():
    for name in ("review.yml", "shadow-review.yml"):
        wf = (pathlib.Path(__file__).resolve().parent.parent / ".github/workflows" / name).read_text()
        assert 'select(.inherited == false)' in wf, name
        assert "--upstream-path $n=upstream/$n" in wf, name
        assert "--mathlib-path" not in wf and "mathlib4" not in wf, name
