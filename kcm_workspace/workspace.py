import glob
import inspect
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .color import Color

HOME = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~"))
WINDOWS_HOSTS = Path(r"C:\Windows\System32\drivers\etc\hosts")
POSIX_HOSTS = Path("/etc/hosts")

NATIVES = {
    "build": "Build or rebuild services.",
    "convert": "Converts the compose file to platform's canonical format.",
    "cp": "Copy files/folders between a service container and the local filesystem.",
    "create": "Creates containers for a service.",
    "down": "Stop and remove containers: networks.",
    "events": "Receive real time events from containers.",
    "exec": "Execute a command in a running container.",
    "images": "List images used by the created containers.",
    "kill": "Force stop service containers.",
    "logs": "View output from containers.",
    "ls": "List running compose projects.",
    "pause": "Pause services.",
    "port": "Print the public port for a port binding.",
    "ps": "List containers.",
    "pull": "Pull service images.",
    "push": "Push service images.",
    "restart": "Restart service containers.",
    "rm": "Removes stopped service containers.",
    "run": "Run a one-off command on a service.",
    "start": "Start services.",
    "stop": "Stop services.",
    "top": "Display the running processes.",
    "unpause": "Unpause services.,",
    "up": "Create and start containers.",
    "version": "Show the Docker Compose version information.",
}


@dataclass
class GitClone:
    """Repositório a ser clonado. ``folder`` relativo é resolvido a partir da pasta do projeto."""

    folder: str | Path
    url: str
    branch: str | None = None
    """Branch a clonar; ``None`` usa a branch padrão do remoto."""


class Workspace:
    """Base dos scripts de workspace.

    Cada projeto estende esta classe e informa no construtor o que é específico dele.
    Um método vira comando quando a docstring termina com ``Aplica-se a: <escopos>``.
    """

    def __init__(
        self,
        script_file: str | Path,
        git_clones: list[GitClone] | None = None,
        mk_volumes: list[str] | None = None,
        moodle_plugins: list[str] | None = None,
        cli_dict: dict[str, str] | None = None,
        shell_dict: dict[str, str] | None = None,
        hosts: tuple[str, str] | None = None,
        plugins_build_dir: str | Path | None = None,
        plugin_package_name: str = "{plugin}-{version}.zip",
        argv: list[str] | None = None,
    ):
        self.script = Path(script_file).resolve()
        self.project_name = self.script.stem
        self.workspace_dir = self.script.parent
        self.project_dir = self.workspace_dir.parent
        self.volumes_dir = Path(os.environ.get("VOLUMES_DIR", self.project_dir / "volumes"))
        self.home = HOME

        self.git_clones = [GitClone(self.project_dir / g.folder, g.url, g.branch) for g in (git_clones or [])]
        self.mk_volumes = mk_volumes or []
        self.moodle_plugins = moodle_plugins or []
        self.cli_dict = cli_dict or {}
        self.shell_dict = shell_dict or {}
        # (nome, linha): ex. ("IFRN-AVA", "127.0.0.1    moodle")
        self.hosts = hosts
        self.plugins_build_dir = self.project_dir / (plugins_build_dir or Path("workspace") / "build" / "plugins")
        self.plugin_package_name = plugin_package_name

        self.argv = list(sys.argv if argv is None else argv)
        self._command_name = self.argv[1] if len(self.argv) > 1 else "help"
        self._scope_name = self.argv[2] if len(self.argv) >= 3 else "env"
        self._service_name = self._scope_name if self._scope_name != "env" else ""

        self._services: list[str] = []
        self._repositories: set[str] = set()
        self._commands: dict = {}
        self._scopes: dict[str, list[str]] = {}
        self._descriptions: dict[str, str] = {}

    # ------------------------------------------------------------------ execução

    def run(self) -> int:
        """Lê .env e docker compose, carrega os comandos e executa o solicitado."""
        try:
            os.chdir(self.workspace_dir)
            print(f"Trabalhando em {Color.c(self.workspace_dir)}")
            self._read_envs()
            self._read_compose()
            self._load_commands()
            if len(self.argv) == 1:
                return self.help()
            if self._command_name not in self._commands:
                print(f"{Color.g('docker compose')} {Color.b(self._command_name)} {Color.m(' '.join(self.argv[2:]))}")
                return self._execute("docker", "compose", *self.argv[1:])
            return self._commands[self._command_name]() or 0
        except KeyboardInterrupt:
            print(f"\n{Color.r('Interrompido pelo usuário')}")
            return 130

    @classmethod
    def main(cls) -> None:
        """Ponto de entrada: ``if __name__ == "__main__": MeuProjeto.main()``."""
        sys.exit(cls().run())

    # ------------------------------------------------------------------ internos

    def _read_envs(self) -> None:
        env_file = self.workspace_dir / ".env"
        if not env_file.exists():
            return
        try:
            content = env_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = env_file.read_text()
        for line in content.splitlines():
            if line.startswith("#") or line.strip() == "" or "=" not in line:
                continue
            varname, varval = line.split("=", 1)
            if varname == "COMPOSE_PROJECT_NAME" and varval != self.project_name:
                raise ValueError(f"No arquivo .env está como {line}, mas devia ser {varname}={self.project_name}")
            os.environ[varname] = varval

    def _read_compose(self) -> None:
        result = subprocess.run(  # nosec B603 B607
            ["docker", "compose", "config"], capture_output=True, text=True, cwd=self.workspace_dir
        )
        self._services = []
        repositories = [""]
        section = ""
        for line in result.stdout.splitlines():
            top_level = re.search(r"^(\w+):", line)
            if top_level:
                section = top_level.group(1)
            # só entradas da seção services: (volumes/networks têm a mesma indentação)
            service = re.search(r"^  ([\w-]*):$", line)
            if service and section == "services":
                self._services.append(service.group(1))
            image = re.search(r"^(    image: )(.*)$", line)
            if image:
                name = image.group(2)
                repositories.append(name.split("/")[0] if name.count("/") > 1 else "")
        self._repositories = set(repositories)

    def _load_commands(self) -> None:
        for name, func in inspect.getmembers(type(self), inspect.isfunction):
            match = re.search(r"^(.*)(Aplica-se a: )(.*)$", func.__doc__ or "", re.MULTILINE)
            if match is None:
                continue
            scopes = match.group(3)
            scopes = scopes.replace("$services_names$", ",".join(self._services))
            scopes = scopes.replace("$restore_dirs$", ",".join(self._prefixed_envs("RESTORE_DIR_").keys()))
            scopes = scopes.replace("$download_files$", ",".join(self._prefixed_envs("DOWNLOAD_FILE").keys()))
            self._commands[name] = getattr(self, name)
            self._scopes[name] = scopes.split(",")
            self._descriptions[name] = match.group(1) + match.group(2)

    def _container_lines(self) -> list[str]:
        result = subprocess.run(["docker", "container", "ps"], capture_output=True, text=True)  # nosec B603 B607
        return result.stdout.splitlines()

    def _service_is_running(self, service_name: str) -> bool:
        pattern = f".*{self.project_name}-{service_name}-1.*"
        return any(re.match(pattern, line) for line in self._container_lines())

    def _service_is_healthy(self, service_name: str) -> bool:
        pattern = f".*{self.project_name}-{service_name}-1.*"
        return any(re.match(pattern, line) and "healthy" in line for line in self._container_lines())

    def _execute(self, executable_file: str, *args: str) -> int:
        command = " ".join([executable_file, *args])
        print(Color.g("Executando: "), Color.b(command))
        p = subprocess.Popen(  # nosec B602
            command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stdout, shell=True, cwd=self.workspace_dir
        )
        return p.wait()

    def _write_shell_file(self, filename: str) -> None:
        shell_file = self.home / filename
        if shell_file.exists() and str(self.workspace_dir) not in shell_file.read_text(encoding="utf-8"):
            with shell_file.open("a", encoding="utf-8") as f:
                f.write(f"\nPATH=$PATH:{self.workspace_dir}\n")
            print("Lembre-se de executar o comando abaixo:")
            print(Color.g(f"source ~/{filename}"), "\n")

    @staticmethod
    def _prefixed_envs(prefix: str) -> dict[str, str]:
        return {k[len(prefix) :].lower(): v for k, v in os.environ.items() if k.startswith(prefix) and v.strip()}

    @property
    def _verb(self) -> str:
        return "exec" if self._service_is_running(self._service_name) else "run --rm"

    @property
    def _default_django(self) -> str:
        django_svc = os.environ.get("DEFAULT_DJANGO", "web")
        return django_svc if django_svc in self._services else "web"

    def _resolve_django_service_and_args(self) -> tuple[str, str, list[str]]:
        if self._service_name in self._services:
            service, args = self._service_name, self.argv[3:]
        else:
            service, args = self._default_django, self.argv[2:]
        verb = "exec" if self._service_is_running(service) else "run --rm"
        return service, verb, args

    def _write_hosts(self) -> None:
        if self.hosts is None:
            return
        hosts_name, hosts_value = self.hosts
        hosts_file = WINDOWS_HOSTS if sys.platform == "win32" else POSIX_HOSTS
        try:
            content = hosts_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(Color.y(f"Arquivo de hosts ({hosts_file}) não encontrado."))
            return
        if hosts_value in content:
            return
        print(f"Adicionando este {hosts_value} em {hosts_file}:")
        nls = "#" * 70
        new_hosts = f"\n{nls}\n## {hosts_name}\n{nls}\n{hosts_value}\n"
        if sys.platform != "win32":
            self._execute(f"echo '{new_hosts}' | sudo tee -a {hosts_file}")
            return
        try:
            with hosts_file.open("a", encoding="utf-8") as f:
                f.write(new_hosts)
        except PermissionError:
            print(
                Color.r(
                    f"Permissão negada ao escrever em {hosts_file}. Execute o terminal como Administrador "
                    f"ou adicione manualmente a linha abaixo em {hosts_file}:\n{hosts_value}"
                )
            )

    def _mk_volumes(self) -> None:
        for mkvol in self.mk_volumes:
            vol = self.volumes_dir / mkvol
            print(vol)
            vol.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                self._execute(f"sudo chmod -R 777 {vol}")

    # ------------------------------------------------------------------ comandos

    def help(self) -> int:
        """Imprime esta ajuda. Aplica-se a: env"""
        print(f"""Uso:\n{Color.r(f'{self.project_name} [OPTIONS] COMMAND')}\n\nOptions:
    {Color.m('--ansi')} {Color.y('string')}                Control when to print ANSI control characters ("never"|"always"|"auto") (default "auto")
    {Color.m('--compatibility')}              Run compose in backward compatibility mode
    {Color.m('--env-file')} {Color.y('string')}            Specify an alternate environment file.
{Color.m('-f')}, {Color.m('--file')} {Color.y('stringArray')}           Compose configuration files
    {Color.m('--profile')} {Color.y('stringArray')}        Specify a profile to enable
    {Color.m('--project-directory')} {Color.y('string')}   Specify an alternate working directory (default: the path of the, first specified, Compose file)
    {Color.m('-p')}, {Color.m('--project-name')} {Color.y('string')}    Project name\nComandos:""")  # noqa: E501
        for m in list(NATIVES.keys()) + list(self._commands.keys()):
            fixed = Color.m(f"{m:22}")
            description = self._descriptions.get(m, NATIVES.get(m, " Sem descrição"))
            scopes = Color.c(", ".join(self._scopes.get(m, [" Ver `docker compose`"])))
            print(f"  {fixed} - {description}{scopes}")
        print(f"Execute '{self.script.name} COMMAND help' para imprimir a ajuda.")
        return 0

    def relaunch(self) -> int:
        """Para e depois inicia ambiente ou o serviço especificado, não faz lock do CLI. Aplica-se a: $services_names$,env"""  # noqa: E501
        self._execute(f"docker compose down {self._service_name}")
        return self._execute(f"docker compose up -d {self._service_name}")

    def launch(self) -> int:
        """Mostra o status ambiente ou o serviço especificado. Aplica-se a: $services_names$,env"""
        self.relaunch()
        return self._execute(f"docker compose logs -f {self._service_name}")

    def exec(self) -> int:
        """Executa um comando em um serviço que já esteja em execução. Aplica-se a: $services_names$,env"""
        return self._execute(f"docker compose {self._verb} {self._service_name} ", *self.argv[3:])

    def clone(self) -> int:
        """Clona todos os repositórios. Aplica-se a: env"""
        for g in self.git_clones:
            folder = Path(g.folder)
            folder.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
            if not folder.exists():
                branch = Color.m(g.branch or "padrão")
                print(f"Clonando {Color.c(folder)} do repositório {Color.y(g.url)} na branch {branch}")
                branch_arg = f"-b {g.branch} " if g.branch else ""
                self._execute(f'git clone {branch_arg}{g.url} "{folder}"')
            self._install_pre_commit(folder)
        return 0

    def _install_pre_commit(self, folder: Path) -> None:
        if not (folder / ".pre-commit-config.yaml").exists():
            return
        if shutil.which("pre-commit"):
            print(f"Configurando pre-commit em {Color.c(folder)}")
            self._execute(
                f'pre-commit install --install-hooks -t pre-commit -t pre-push -c "{folder}/.pre-commit-config.yaml"'
            )
        else:
            print(f"{Color.y('Aviso:')} 'pre-commit' não está instalado. Pulando configuração em {Color.c(folder)}.")

    def setup(self) -> int:
        """Executa o setup do ambiente, ou seja, clona todos os repositórios e configura o PATH e o hosts. Aplica-se a: env"""  # noqa: E501
        self._execute(f"docker compose down {self._service_name}")
        self.clone()
        self._write_hosts()
        self._mk_volumes()
        self._write_shell_file(".bashrc")
        self._write_shell_file(".zshrc")
        return 0

    def deploy(self) -> int:
        """Para tudo, constrói as imagens, iniciar os serviços background e acompanha o log. Aplica-se a: env"""
        self.setup()
        return self._execute("docker compose up -d")

    def undeploy(self) -> int:
        """Pára todo o ambiente e remove os volumes. Aplica-se a: $services_names$,env"""
        return self._execute("docker compose down -v")

    def git(self) -> int:
        """Executa o comando git em todos os repositórios git. Aplica-se a: env"""
        git_arguments = " ".join(self.argv[2:]) if len(self.argv) > 2 else "status"
        for folder in [self.workspace_dir, *(Path(g.folder) for g in self.git_clones)]:
            if not folder.exists():
                print(f"O projeto {folder} NÃO foi clonado ainda.")
                continue
            print(f"Executando {Color.b('git ' + git_arguments)} em {Color.m(folder)}")
            returncode = self._execute(f'git -C "{folder}" {git_arguments}')
            if returncode != 0:
                return returncode
        return 0

    def cli(self) -> int:
        """Executa a CLI padrão do serviço, pode ser php -a, shell_plus, psql ou redis-cli. Bash por padrão. Usará exec se possível, caso contrário será run. Aplica-se a: $services_names$"""  # noqa: E501
        cmd = self.cli_dict.get(self._service_name, "bash")
        return self._execute(f"docker compose {self._verb} {self._service_name} {cmd} ", *self.argv[3:])

    def shell(self) -> int:
        """Executa o bash ou sh de um serviço, usará exec se já estiver em execução, run caso contrário. Aplica-se a: $services_names$"""  # noqa: E501
        cmd = self.shell_dict.get(self._service_name, "bash")
        return self._execute(f"docker compose {self._verb} {self._service_name} {cmd} ", *self.argv[3:])

    def shell_root(self) -> int:
        """Executa o bash ou sh de um serviço como root, usará exec se já estiver em execução, run caso contrário. Aplica-se a: $services_names$"""  # noqa: E501
        cmd = self.shell_dict.get(self._service_name, "bash")
        return self._execute(f"docker compose {self._verb} -u root {self._service_name} {cmd} ", *self.argv[3:])

    def wait(self) -> int:
        """Aguarda até o banco de dados estar disponível. Aplica-se a: db"""
        while not self._service_is_healthy("db"):
            print(Color.y("Aguardando o banco de dados estar disponível..."))
            time.sleep(3)
        print(Color.g("Banco de dados disponível!"))
        return 0

    def manage(self) -> int:
        """Executa o manage.py do do serviço. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        return self._execute(f"docker compose {verb} {service} python3 manage.py ", *args)

    def makemigrations(self) -> int:
        """Criar as migrações pendentes do serviço. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        return self._execute(f"docker compose {verb} {service} python3 manage.py makemigrations", *args)

    def migrate(self) -> int:
        """Aplica as migrações pendentes do serviço. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        return self._execute(f"docker compose {verb} {service} python3 manage.py migrate", *args)

    def show_urls(self) -> int:
        """Mostra as URL do serviço. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        return self._execute(f"docker compose {verb} {service} python manage.py show_urls", *args)

    def test(self) -> int:
        """Executa os testes unitários do serviço. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        self._execute(f"docker compose {verb} {service} coverage run manage.py test", *args)
        return self._execute(f"docker compose {verb} {service} coverage report", *args)

    def lint(self) -> int:
        """Formata o código do serviço com black. Aplica-se a: $services_names$"""
        service, verb, args = self._resolve_django_service_and_args()
        return self._execute(f"docker compose {verb} {service} black --line-length 120 .", *args)

    def compress_plugins(self) -> int:
        """Comprime todos os plugins Moodle. Aplica-se a: env"""

        def zip_with_prefix(folder_path: Path, zip_path: Path, prefix: str) -> None:
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in folder_path.rglob("*"):
                    if not file_path.is_file() or {".git", ".idea"} & set(file_path.parts):
                        continue
                    zipf.write(file_path, Path(prefix) / file_path.relative_to(folder_path))

        build_dir = self.plugins_build_dir
        for plugin_fullname in self.moodle_plugins:
            plugin_name = plugin_fullname.split("_", 1)[1]
            plugin_dir = self.project_dir / plugin_fullname
            for line in (plugin_dir / "version.php").read_text(encoding="utf-8").splitlines():
                if not line.startswith("$plugin->version"):
                    continue
                plugin_version = line.split("=")[1].replace(";", "").split()[0]
                plugin_package = build_dir / self.plugin_package_name.format(
                    plugin=plugin_fullname, version=plugin_version
                )
                if plugin_package.exists():
                    print(f"Plugin {Color.c(plugin_package)} {Color.r('JÁ existe')}.")
                    continue
                print(f"Plugin {Color.c(plugin_package)} {Color.g('NÃO existe')}.")
                old_pattern = self.plugin_package_name.format(plugin=plugin_fullname, version="*")
                for old_zip in glob.glob(str(build_dir / old_pattern)):
                    try:
                        os.remove(old_zip)
                    except OSError:
                        pass
                zip_with_prefix(plugin_dir, plugin_package, plugin_name)
        return 0

    def publish(self) -> int:
        """Comprime os plugins Moodle e constrói e publica a imagem do serviço ava. Aplica-se a: env"""
        self.compress_plugins()
        return self._execute("docker compose build --progress plain --push ava")

    def code(self) -> int:
        """Abre o workspace no VS Code. Aplica-se a: env"""
        return self._execute(f'code "{self.workspace_dir / f"{self.project_name}.code-workspace"}"')
