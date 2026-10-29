import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import kcm_workspace.workspace as ws_module
from kcm_workspace import Color, GitClone, Workspace

COMPOSE_CONFIG = """name: demo
services:
  db:
    image: postgres:16
  web:
    image: ghcr.io/org/web:latest
networks:
  default:
    name: demo_default
volumes:
  db_data:
    name: demo_db_data
"""


class Demo(Workspace):
    def __init__(self, script, argv, **kwargs):
        super().__init__(script, argv=argv, **kwargs)

    def extra(self) -> int:
        """Comando do projeto. Aplica-se a: $services_names$,env"""
        return self._execute("echo", "extra")

    def not_a_command(self):
        """Sem escopo."""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    workspace = tmp_path / "demo" / "workspace"
    workspace.mkdir(parents=True)
    script = workspace / "demo"
    script.write_text("#!/usr/bin/env python3\n")
    (workspace / "docker-compose.yml").write_text("services: {}\n")
    return script


@pytest.fixture
def calls(monkeypatch):
    """Captura comandos executados e simula docker."""

    class Executed(list):
        containers = {"value": ""}

    executed = Executed()
    executed.cwds = []
    containers = executed.containers

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "config"]:
            return SimpleNamespace(stdout=COMPOSE_CONFIG, returncode=0)
        if cmd[:3] == ["docker", "container", "ps"]:
            return SimpleNamespace(stdout=containers["value"], returncode=0)
        raise AssertionError(cmd)

    class FakePopen:
        def __init__(self, command, **kwargs):
            executed.append(command)
            executed.cwds.append(kwargs.get("cwd"))

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.chdir(os.getcwd())
    return executed


def make(project: Path, *args: str, **kwargs) -> Demo:
    return Demo(project, ["demo", *args], **kwargs)


def test_paths(project: Path, monkeypatch):
    monkeypatch.delenv("VOLUMES_DIR", raising=False)
    w = make(project, git_clones=[GitClone("app", "url"), GitClone(project.parent.parent / "abs", "u2", "dev")])
    assert w.project_name == "demo"
    assert w.workspace_dir == project.parent
    assert w.project_dir == project.parent.parent
    assert w.volumes_dir == project.parent.parent / "volumes"
    assert w.git_clones[0].folder == project.parent.parent / "app"
    assert w.git_clones[1].folder == project.parent.parent / "abs"
    assert w.git_clones[1].branch == "dev"


def test_help_without_args(project, calls, capsys):
    assert make(project).run() == 0
    out = capsys.readouterr().out
    assert "extra" in out and "relaunch" in out and "not_a_command" not in out
    assert "db_data" not in out
    assert "db,web,env" in out.replace(Color.ENDC, "").replace("\033[96m", "").replace(", ", ",")


def test_command_with_service(project, calls):
    assert make(project, "extra", "web").run() == 0
    assert calls == ["echo extra"]


def test_passthrough_to_compose(project, calls):
    make(project, "ps", "-a").run()
    assert calls == ["docker compose ps -a"]


def test_exec_uses_run_when_not_running(project, calls):
    make(project, "exec", "web", "ls").run()
    assert calls == ["docker compose run --rm web  ls"]


def test_exec_uses_exec_when_running(project, calls):
    calls.containers["value"] = "abc demo-web-1 Up"
    make(project, "exec", "web", "ls").run()
    assert calls == ["docker compose exec web  ls"]


def test_cli_and_shell(project, calls):
    make(project, "cli", "db", cli_dict={"db": "psql"}).run()
    make(project, "shell", "db", shell_dict={"db": "sh"}).run()
    make(project, "shell_root", "web").run()
    assert calls == [
        "docker compose run --rm db psql ",
        "docker compose run --rm db sh ",
        "docker compose run --rm -u root web bash ",
    ]


def test_django_commands(project, calls, monkeypatch):
    monkeypatch.delenv("DEFAULT_DJANGO", raising=False)
    for cmd in ("manage", "makemigrations", "migrate", "show_urls", "test", "lint"):
        make(project, cmd).run()
    make(project, "migrate", "db", "app").run()
    assert calls[0] == "docker compose run --rm web python3 manage.py "
    assert calls[2] == "docker compose run --rm web python3 manage.py migrate"
    assert "coverage report" in calls[5]
    assert calls[-1] == "docker compose run --rm db python3 manage.py migrate app"


def test_relaunch_launch_deploy_undeploy(project, calls, monkeypatch):
    monkeypatch.setattr(ws_module, "HOME", project.parent)
    make(project, "launch", "web").run()
    make(project, "undeploy").run()
    assert calls[:3] == ["docker compose down web", "docker compose up -d web", "docker compose logs -f web"]
    assert calls[3] == "docker compose down -v"


def test_setup(project, calls, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# vazio\n")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    monkeypatch.setattr(ws_module, "WINDOWS_HOSTS", hosts)
    monkeypatch.setattr(ws_module, "POSIX_HOSTS", hosts)
    monkeypatch.setattr(ws_module.sys, "platform", "win32")
    monkeypatch.setenv("VOLUMES_DIR", str(tmp_path / "vols"))
    w = make(
        project,
        "setup",
        git_clones=[GitClone("app", "git@x:app.git"), GitClone("lib", "git@x:lib.git", "dev")],
        mk_volumes=["db"],
        hosts=("demo", "127.0.0.1 demo.local"),
    )
    w.home = home
    assert w.run() == 0
    assert any(c.startswith("git clone git@x:app.git") for c in calls)
    assert any(c.startswith("git clone -b dev git@x:lib.git") for c in calls)
    assert "127.0.0.1 demo.local" in hosts.read_text()
    assert (tmp_path / "vols" / "db").is_dir()
    assert str(project.parent) in (home / ".bashrc").read_text()


def test_write_hosts_posix_and_missing(project, calls, monkeypatch, tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("")
    monkeypatch.setattr(ws_module, "POSIX_HOSTS", hosts)
    monkeypatch.setattr(ws_module.sys, "platform", "linux")
    w = make(project, hosts=("demo", "127.0.0.1 demo.local"))
    w._write_hosts()
    assert "sudo tee -a" in calls[-1]
    monkeypatch.setattr(ws_module, "POSIX_HOSTS", tmp_path / "nao-existe")
    w._write_hosts()
    w.mk_volumes = ["x"]
    w.volumes_dir = tmp_path / "v"
    w._mk_volumes()
    assert calls[-1].startswith("sudo chmod -R 777")


def test_write_hosts_permission_denied(project, calls, monkeypatch, tmp_path, capsys):
    hosts = tmp_path / "hosts"
    hosts.write_text("")
    monkeypatch.setattr(ws_module, "WINDOWS_HOSTS", hosts)
    monkeypatch.setattr(ws_module.sys, "platform", "win32")

    original_open = Path.open

    def deny(self, mode="r", *args, **kwargs):
        if "a" in mode:
            raise PermissionError
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    make(project, hosts=("demo", "127.0.0.1 demo.local"))._write_hosts()
    assert "Permissão negada" in capsys.readouterr().out


def test_read_envs(project, calls):
    (project.parent / ".env").write_text("# comentário\n\nKCM_TEST_VAR=a=b\nCOMPOSE_PROJECT_NAME=demo\n")
    make(project, "ps").run()
    assert os.environ["KCM_TEST_VAR"] == "a=b"
    (project.parent / ".env").write_text("COMPOSE_PROJECT_NAME=outro\n")
    with pytest.raises(ValueError, match="devia ser COMPOSE_PROJECT_NAME=demo"):
        make(project, "ps").run()


def test_git(project, calls, tmp_path):
    (project.parent.parent / "app").mkdir()
    make(project, "git", "pull", git_clones=[GitClone("app", "u"), GitClone("falta", "u")]).run()
    assert calls[0].endswith("pull") and len(calls) == 2


def test_git_continues_after_error(project, monkeypatch, calls, capsys):
    (project.parent.parent / "app").mkdir()
    (project.parent.parent / "lib").mkdir()
    monkeypatch.setattr(Demo, "_execute", lambda self, *a: calls.append(" ".join(a)) or 1)
    assert make(project, "git", "pull", git_clones=[GitClone("app", "u"), GitClone("lib", "u")]).run() == 1
    assert len(calls) == 3
    assert "Falhou em 3 repositório(s)" in capsys.readouterr().out


def test_git_pull_skips_detached_head(project, calls, capsys):
    tag = project.parent.parent / "tag"
    (tag / ".git").mkdir(parents=True)
    (tag / ".git" / "HEAD").write_text("0123456789abcdef\n")
    branch = project.parent.parent / "branch"
    (branch / ".git").mkdir(parents=True)
    (branch / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    clones = [GitClone("tag", "u"), GitClone("branch", "u")]
    assert make(project, "git", "pull", git_clones=clones).run() == 0
    assert not any(str(tag) in c for c in calls) and any(str(branch) in c for c in calls)
    assert "HEAD destacado" in capsys.readouterr().out
    make(project, "git", "status", git_clones=clones).run()
    assert any(str(tag) in c for c in calls)


def test_clone_installs_pre_commit(project, calls, monkeypatch, capsys):
    app = project.parent.parent / "app"
    app.mkdir()
    (app / ".pre-commit-config.yaml").write_text("repos: []\n")
    monkeypatch.setattr(ws_module.shutil, "which", lambda name: "/bin/pre-commit")
    make(project, "clone", git_clones=[GitClone("app", "u")]).run()
    assert calls[-1].startswith("pre-commit install")
    monkeypatch.setattr(ws_module.shutil, "which", lambda name: None)
    make(project, "clone", git_clones=[GitClone("app", "u")]).run()
    assert "não está instalado" in capsys.readouterr().out


def test_wait(project, calls, monkeypatch):
    states = iter(["", "x demo-db-1 Up (healthy)"])
    monkeypatch.setattr(Workspace, "_container_lines", lambda self: next(states).splitlines())
    monkeypatch.setattr(ws_module.time, "sleep", lambda s: None)
    assert make(project, "wait", "db").run() == 0


def test_compress_and_publish(project, calls):
    plugin = project.parent.parent / "local_demo"
    (plugin / ".git").mkdir(parents=True)
    (plugin / ".git" / "HEAD").write_text("x")
    (plugin / "version.php").write_text("<?php\n$plugin->version = 2026102800;\n")
    build = project.parent / "build" / "plugins"
    build.mkdir(parents=True)
    (build / "local_demo-1.zip").write_text("velho")
    make(project, "publish", moodle_plugins=["local_demo"]).run()
    package = build / "local_demo-2026102800.zip"
    assert package.exists() and not (build / "local_demo-1.zip").exists()
    with zipfile.ZipFile(package) as z:
        assert z.namelist() == ["demo/version.php"]
    make(project, "compress_plugins", moodle_plugins=["local_demo"]).run()
    assert calls[-1] == "docker compose build --progress plain --push ava"


def test_compress_custom_destination(project, calls):
    plugin = project.parent.parent / "tool_x_y"
    plugin.mkdir()
    (plugin / "version.php").write_text("$plugin->version = 7;\n")
    w = make(
        project,
        "compress_plugins",
        moodle_plugins=["tool_x_y"],
        plugins_build_dir="../infra/build",
        plugin_package_name="{plugin}_{version}.zip",
    )
    w.run()
    package = project.parent.parent.parent / "infra" / "build" / "tool_x_y_7.zip"
    with zipfile.ZipFile(package) as z:
        assert z.namelist() == ["x_y/version.php"]


def test_code(project, calls):
    make(project, "code").run()
    assert calls[0].startswith("code ") and calls[0].endswith('demo.code-workspace"')


def test_keyboard_interrupt(project, calls, monkeypatch):
    def boom(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Demo, "_read_envs", boom)
    assert make(project, "ps").run() == 130


def test_main_exits_with_code(project, calls, monkeypatch):
    class Main(Workspace):
        def __init__(self):
            super().__init__(project, argv=["demo", "ps"])

    with pytest.raises(SystemExit) as exc:
        Main.main()
    assert exc.value.code == 0


def test_help_no_duplicates_and_empty_scope(project, monkeypatch, calls, capsys):
    monkeypatch.setattr(ws_module, "COMPOSE_CONFIG", "", raising=False)
    w = make(project)
    w._read_compose = lambda: None
    w.run()
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "[95mexec" in line]
    assert len(lines) == 1
    assert ", env" not in out.replace(Color.ENDC, "").replace("[96m", "")


def test_main_line_buffering(project, calls, monkeypatch):
    seen = []

    class Stream:
        def reconfigure(self, **kwargs):
            seen.append(kwargs)

        def write(self, *a):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(ws_module.sys, "stdout", Stream())
    monkeypatch.setattr(ws_module.sys, "stderr", Stream())

    class Main(Workspace):
        def __init__(self):
            super().__init__(project, argv=["demo", "ps"])

    with pytest.raises(SystemExit):
        Main.main()
    assert seen == [{"line_buffering": True}, {"line_buffering": True}]


def test_without_compose_skips_docker(project, calls, tmp_path):
    (project.parent / "docker-compose.yml").unlink()
    w = make(project, "setup")
    w.home = tmp_path
    assert w.run() == 0
    assert not any(c.startswith("docker compose down") for c in calls)
    assert w._services == []


def test_pre_commit_via_uvx_in_repo_folder(project, calls, monkeypatch):
    app = project.parent.parent / "app"
    app.mkdir()
    (app / ".pre-commit-config.yaml").write_text("repos: []\n")
    monkeypatch.setattr(ws_module.shutil, "which", lambda name: "/bin/uvx" if name == "uvx" else None)
    make(project, "clone", git_clones=[GitClone("app", "u")]).run()
    assert calls[-1].startswith("uvx pre-commit install")
    assert calls.cwds[-1] == app
