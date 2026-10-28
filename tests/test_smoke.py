"""Import every package module, including the command-line entry point."""

import importlib
import pkgutil

import pytest

import culprit


MODULES = [culprit.__name__] + [
    module.name for module in pkgutil.walk_packages(culprit.__path__, "culprit.")
]


@pytest.mark.parametrize("name", MODULES)
def test_import_every_module(name, monkeypatch, capsys):
    if name == "culprit.__main__":
        monkeypatch.setattr("sys.argv", ["culprit", "--help"])
        with pytest.raises(SystemExit) as exc:
            importlib.import_module(name)
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "usage: culprit" in output
        assert all(command in output for command in ("doctor", "demo-repo", "demo", "hunt", "report"))
    else:
        importlib.import_module(name)
