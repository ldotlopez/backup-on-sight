import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).parent.parent / "docker-entrypoint.sh"
PATH = {"PATH": "/usr/bin:/bin"}


def run_entrypoint(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", ENTRYPOINT, "echo", "ran"],
        env=PATH | env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_rejects_non_numeric_ids() -> None:
    for env in ({"EUID": "abc"}, {"EUID": "1000", "EGID": "1;x"}, {"EUID": ""}):
        result = run_entrypoint(env)
        assert result.returncode == 1, env
        assert "ran" not in result.stdout


# Dropping privileges needs root, so that path is only covered by running the image.
def test_runs_command_without_ids() -> None:
    result = run_entrypoint({})
    assert result.returncode == 0
    assert result.stdout == "ran\n"
