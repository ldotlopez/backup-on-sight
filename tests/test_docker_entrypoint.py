import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).parent.parent / "docker-entrypoint.sh"
PATH = {"PATH": "/usr/bin:/bin"}


PRINT_VARS = 'echo "$BACKUP_ON_SIGHT_STATE_DIR $BACKUP_ON_SIGHT_CONFIG_FILE $HOME"'


def run_entrypoint(
    env: dict[str, str], command: tuple[str, ...] = ("echo", "ran")
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", ENTRYPOINT, *command],
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


def test_paths_fall_back_to_container_defaults() -> None:
    result = run_entrypoint({}, ("sh", "-c", PRINT_VARS))
    assert result.stdout == "/state /config.yaml /state/.home\n"

    result = run_entrypoint(
        {"BACKUP_ON_SIGHT_STATE_DIR": "", "BACKUP_ON_SIGHT_CONFIG_FILE": ""},
        ("sh", "-c", PRINT_VARS),
    )
    assert result.stdout == "/state /config.yaml /state/.home\n"


def test_paths_keep_defined_values() -> None:
    result = run_entrypoint(
        {
            "BACKUP_ON_SIGHT_STATE_DIR": "/srv/s",
            "BACKUP_ON_SIGHT_CONFIG_FILE": "/etc/c.yaml",
        },
        ("sh", "-c", PRINT_VARS),
    )
    assert result.stdout == "/srv/s /etc/c.yaml /srv/s/.home\n"


def test_rejects_unsafe_state_dir() -> None:
    for state_dir in ("data/state", "/"):
        result = run_entrypoint({"BACKUP_ON_SIGHT_STATE_DIR": state_dir})
        assert result.returncode == 1, state_dir
        assert "ran" not in result.stdout
