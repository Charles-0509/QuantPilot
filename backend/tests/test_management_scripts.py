from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUAN = ROOT / "scripts" / "quan"
INSTALLER = ROOT / "scripts" / "install.sh"


def executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def run_script(path: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(path), *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=10,
    )


def fake_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == "inspect" ]]; then exit 1; fi
if [[ "$1" == "images" ]]; then exit 0; fi
exit 0
""",
    )
    executable(
        bin_dir / "sudo",
        "#!/usr/bin/env bash\nexec \"$@\"\n",
    )
    executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
output=""
previous=""
url=""
for argument in "$@"; do
  if [[ "$previous" == "-o" ]]; then output="$argument"; fi
  if [[ "$argument" == http* ]]; then url="$argument"; fi
  previous="$argument"
done
if [[ "$url" == *"/api/health" ]]; then
  printf '%s\n' "${FAKE_HEALTH_JSON:-{\"status\":\"ok\",\"paper\":true,\"version\":\"1.3.3\"}}"
elif [[ "$url" == *"api.github.com"* ]]; then
  printf '%s\n' "$FAKE_TAGS_JSON"
elif [[ "$url" == *"raw.githubusercontent.com"* && -n "$output" ]]; then
  cp "$FAKE_QUAN_SOURCE" "$output"
else
  exit 22
fi
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "LC_ALL": "C",
            "LANG": "C",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_TAGS_JSON": '[{"name":"v1.3.3"},{"name":"v1.3.2"}]',
            "FAKE_QUAN_SOURCE": str(QUAN),
        }
    )
    return environment, docker_log


def write_quan_config(
    path: Path, install_dir: Path, port: int = 10000, quan_bin: Path | None = None
) -> None:
    path.write_text(
        "\n".join(
            [
                f"QUANTPILOT_DIR={install_dir}",
                f"COMPOSE_FILE={install_dir / 'docker-compose.yml'}",
                f"QUANTPILOT_PORT={port}",
                "REPO_SLUG=Charles-0509/QuantPilot",
                "IMAGE=ghcr.io/charles-0509/quantpilot:latest",
                f"QUAN_BIN_PATH={quan_bin or install_dir / 'quan'}",
                "",
            ]
        )
    )


def test_quan_update_reports_nothing_to_do(tmp_path: Path) -> None:
    environment, _ = fake_environment(tmp_path)
    install_dir = tmp_path / "quantpilot"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").write_text("services: {}\n")
    config = tmp_path / "quan.conf"
    write_quan_config(config, install_dir)
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment["FAKE_HEALTH_JSON"] = '{"status":"ok","paper":true,"version":"1.3.3"}'

    result = run_script(QUAN, "update", env=environment)

    assert result.returncode == 0, result.stderr
    assert "Synchronizing QuantPilot package database" in result.stdout
    assert "Nothing to do" in result.stdout
    assert "quan upgrade" not in result.stdout


def test_quan_uses_chinese_for_chinese_locale_but_keeps_nothing_to_do(tmp_path: Path) -> None:
    environment, _ = fake_environment(tmp_path)
    install_dir = tmp_path / "quantpilot"
    install_dir.mkdir()
    config = tmp_path / "quan.conf"
    write_quan_config(config, install_dir)
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment.pop("LC_ALL", None)
    environment.pop("LC_MESSAGES", None)
    environment["LANG"] = "zh_CN.UTF-8"
    environment["FAKE_HEALTH_JSON"] = '{"status":"ok","paper":true,"version":"1.3.3"}'

    update = run_script(QUAN, "update", env=environment)
    help_result = run_script(QUAN, "help", env=environment)

    assert update.returncode == 0, update.stderr
    assert "正在同步 QuantPilot 软件包数据库" in update.stdout
    assert "Nothing to do" in update.stdout
    assert "QuantPilot 管理命令" in help_result.stdout
    assert "检查是否有新的稳定版本" in help_result.stdout


def test_quan_locale_priority_allows_lc_all_to_override_lang(tmp_path: Path) -> None:
    environment, _ = fake_environment(tmp_path)
    config = tmp_path / "quan.conf"
    write_quan_config(config, tmp_path / "quantpilot")
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment["LANG"] = "zh_CN.UTF-8"
    environment["LC_ALL"] = "C"

    result = run_script(QUAN, "help", env=environment)

    assert result.returncode == 0
    assert "QuantPilot management command" in result.stdout
    assert "QuantPilot 管理命令" not in result.stdout


def test_quan_update_reports_newer_version_and_ignores_prerelease(tmp_path: Path) -> None:
    environment, _ = fake_environment(tmp_path)
    install_dir = tmp_path / "quantpilot"
    install_dir.mkdir()
    config = tmp_path / "quan.conf"
    write_quan_config(config, install_dir)
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment["FAKE_HEALTH_JSON"] = '{"status":"ok","paper":true,"version":"1.3.1"}'
    environment["FAKE_TAGS_JSON"] = (
        '[{"name":"v1.4.0-beta.1"},{"name":"v1.3.3"},{"name":"not-a-version"}]'
    )

    result = run_script(QUAN, "update", env=environment)

    assert result.returncode == 0, result.stderr
    assert "quantpilot 1.3.1 -> 1.3.3" in result.stdout
    assert "Run 'quan upgrade' to update." in result.stdout


def test_quan_update_fails_when_container_is_unavailable(tmp_path: Path) -> None:
    environment, _ = fake_environment(tmp_path)
    config = tmp_path / "quan.conf"
    write_quan_config(config, tmp_path / "quantpilot")
    environment["QUAN_CONFIG_FILE"] = str(config)
    executable(
        Path(environment["PATH"].split(":", 1)[0]) / "curl",
        "#!/usr/bin/env bash\nexit 7\n",
    )

    result = run_script(QUAN, "update", env=environment)

    assert result.returncode != 0
    assert "not running" in result.stderr
    assert "Nothing to do" not in result.stdout


def test_quan_management_commands_use_configured_compose_file(tmp_path: Path) -> None:
    environment, docker_log = fake_environment(tmp_path)
    install_dir = tmp_path / "quantpilot"
    install_dir.mkdir()
    compose_file = install_dir / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    config = tmp_path / "quan.conf"
    write_quan_config(config, install_dir, port=19400)
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment["FAKE_HEALTH_JSON"] = '{"status":"ok","paper":true,"version":"1.3.3"}'

    for arguments in [("status",), ("start",), ("restart",), ("stop",), ("logs", "25")]:
        result = run_script(QUAN, *arguments, env=environment)
        assert result.returncode == 0, f"{arguments}: {result.stderr}"

    calls = docker_log.read_text()
    assert f"-f {compose_file}" in calls
    assert "up -d --remove-orphans" in calls
    assert "restart" in calls
    assert "stop" in calls
    assert "logs --tail=25 -f" in calls


def test_quan_upgrade_pulls_image_checks_version_and_updates_itself(tmp_path: Path) -> None:
    environment, docker_log = fake_environment(tmp_path)
    install_dir = tmp_path / "quantpilot"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").write_text("services: {}\n")
    config = tmp_path / "quan.conf"
    installed_quan = tmp_path / "installed" / "quan"
    installed_quan.parent.mkdir()
    write_quan_config(config, install_dir, quan_bin=installed_quan)
    environment["QUAN_CONFIG_FILE"] = str(config)
    environment["FAKE_HEALTH_JSON"] = '{"status":"ok","paper":true,"version":"1.3.3"}'

    result = run_script(QUAN, "upgrade", env=environment)

    assert result.returncode == 0, result.stderr
    assert "Upgrading QuantPilot to 1.3.3" in result.stdout
    assert installed_quan.read_text() == QUAN.read_text()
    assert installed_quan.stat().st_mode & 0o777 == 0o755
    calls = docker_log.read_text()
    assert "pull" in calls
    assert "up -d --remove-orphans" in calls


def installer_environment(tmp_path: Path, tty_text: str) -> tuple[dict[str, str], Path, Path, Path]:
    environment, docker_log = fake_environment(tmp_path)
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=debian\nVERSION_CODENAME=bookworm\n')
    tty_file = tmp_path / "tty-input"
    tty_file.write_text(tty_text)
    config = tmp_path / "etc" / "quan.conf"
    quan_bin = tmp_path / "usr" / "bin" / "quan"
    environment.update(
        {
            "QUANTPILOT_ALLOW_NON_ROOT": "1",
            "QUANTPILOT_SKIP_DOCKER_INSTALL": "1",
            "QUANTPILOT_OS_RELEASE": str(os_release),
            "QUANTPILOT_TTY": str(tty_file),
            "QUANTPILOT_CONFIG_FILE": str(config),
            "QUANTPILOT_QUAN_BIN": str(quan_bin),
            "QUANTPILOT_QUAN_SOURCE": str(QUAN),
        }
    )
    return environment, docker_log, config, quan_bin


def test_installer_supports_custom_directory_port_and_repair(tmp_path: Path) -> None:
    install_dir = tmp_path / "custom quantpilot"
    environment, docker_log, config, quan_bin = installer_environment(
        tmp_path, f"{install_dir}\n19321\n"
    )

    installed = run_script(INSTALLER, env=environment)

    assert installed.returncode == 0, installed.stderr
    assert (install_dir / "docker-compose.yml").exists()
    assert '0.0.0.0:19321:10000' in (install_dir / "docker-compose.yml").read_text()
    assert "build:" not in (install_dir / "docker-compose.yml").read_text()
    assert (install_dir / ".env").stat().st_mode & 0o777 == 0o600
    assert "QUANTPILOT_COOKIE_SECURE=false" in (install_dir / ".env").read_text()
    assert "QUANTPILOT_PORT=19321" in config.read_text()
    assert quan_bin.exists()
    (install_dir / "data" / "keep.db").write_text("persistent")
    original_env = (install_dir / ".env").read_text()

    repair_tty = tmp_path / "repair-input"
    repair_tty.write_text("1\n")
    environment["QUANTPILOT_TTY"] = str(repair_tty)
    repaired = run_script(INSTALLER, env=environment)

    assert repaired.returncode == 0, repaired.stderr
    assert (install_dir / "data" / "keep.db").read_text() == "persistent"
    assert (install_dir / ".env").read_text() == original_env
    assert "down --remove-orphans" in docker_log.read_text()


def test_installer_reprompts_when_port_is_occupied(tmp_path: Path) -> None:
    install_dir = tmp_path / "port-test"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied_port = listener.getsockname()[1]
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            available_port = candidate.getsockname()[1]
        environment, _, _, _ = installer_environment(
            tmp_path, f"{install_dir}\n{occupied_port}\n{available_port}\n"
        )
        result = run_script(INSTALLER, env=environment)

    assert result.returncode == 0, result.stderr
    assert f"Port {occupied_port} is already in use" in result.stderr
    assert f"0.0.0.0:{available_port}:10000" in (install_dir / "docker-compose.yml").read_text()


def test_uninstall_can_keep_or_delete_persistent_data(tmp_path: Path) -> None:
    keep_dir = tmp_path / "keep-install"
    environment, _, config, quan_bin = installer_environment(tmp_path, f"{keep_dir}\n19322\n")
    assert run_script(INSTALLER, env=environment).returncode == 0
    (keep_dir / "data" / "keep.db").write_text("persistent")

    keep_tty = tmp_path / "keep-input"
    keep_tty.write_text("2\n1\n")
    environment["QUANTPILOT_TTY"] = str(keep_tty)
    kept = run_script(INSTALLER, env=environment)
    assert kept.returncode == 0, kept.stderr
    assert (keep_dir / "data" / "keep.db").exists()
    assert not config.exists()
    assert not quan_bin.exists()

    delete_root = tmp_path / "delete-case"
    delete_root.mkdir()
    delete_dir = delete_root / "delete-install"
    delete_env, _, delete_config, _ = installer_environment(
        delete_root, f"{delete_dir}\n19323\n"
    )
    assert run_script(INSTALLER, env=delete_env).returncode == 0
    (delete_dir / "data" / "delete.db").write_text("remove")
    delete_tty = delete_root / "delete-input"
    delete_tty.write_text("2\n2\nDELETE\n")
    delete_env["QUANTPILOT_TTY"] = str(delete_tty)
    deleted = run_script(INSTALLER, env=delete_env)
    assert deleted.returncode == 0, deleted.stderr
    assert not (delete_dir / "data").exists()
    assert not delete_config.exists()


def test_installer_rejects_unsupported_system_and_missing_tty(tmp_path: Path) -> None:
    environment, _, _, _ = installer_environment(tmp_path, "")
    unsupported = tmp_path / "unsupported"
    unsupported.write_text("ID=arch\n")
    environment["QUANTPILOT_OS_RELEASE"] = str(unsupported)
    result = run_script(INSTALLER, env=environment)
    assert result.returncode != 0
    assert "Only Debian and Ubuntu" in result.stderr

    environment["QUANTPILOT_OS_RELEASE"] = str(tmp_path / "os-release")
    environment["QUANTPILOT_TTY"] = str(tmp_path / "missing-tty")
    result = run_script(INSTALLER, env=environment)
    assert result.returncode != 0
    assert "interactive terminal" in result.stderr
