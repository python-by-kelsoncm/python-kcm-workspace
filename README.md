# kcm-workspace

Classe base para os scripts de workspace baseados em `docker compose`
(`clone`, `setup`, `launch`, `shell`, `manage`, `git` e outros).

Cada projeto tem, em `<projeto>/workspace/<projeto>`, um script que estende
`Workspace` e informa no construtor o que é específico dele.

## Uso

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["kcm-workspace>=0.2.0"]
# ///
from kcm_workspace import GitClone, Workspace

GITBASE = "git@github.com:minha-org"


class Projeto(Workspace):
    def __init__(self):
        super().__init__(
            __file__,
            git_clones=[
                GitClone("app", f"{GITBASE}/app.git"),
                GitClone("lib", f"{GITBASE}/lib.git", "dev"),
            ],
            mk_volumes=["db"],
            hosts=("PROJETO", "127.0.0.1    projeto.local"),
            cli_dict={"db": "psql"},
            shell_dict={"db": "sh"},
        )

    def collectstatic(self) -> int:
        """Coleta os estáticos. Aplica-se a: $services_names$"""
        return self._execute("docker compose exec web python manage.py collectstatic")


if __name__ == "__main__":
    Projeto.main()
```

- `GitClone.folder` relativo é resolvido a partir da pasta do projeto (pai de `workspace`).
- `GitClone.branch` vazio clona a branch padrão do remoto.
- Após clonar, roda `pre-commit install` nos repositórios com `.pre-commit-config.yaml`.
- `hosts=(nome, linha)` adiciona a linha ao arquivo de hosts no `setup`.
- `compress_plugins` grava em `plugins_build_dir` (relativo ao projeto; padrão `workspace/build/plugins`)
  com o nome `plugin_package_name` (padrão `{plugin}-{version}.zip`).
- Um método vira comando quando a docstring termina com `Aplica-se a: <escopos>`.
- Comandos desconhecidos são repassados para `docker compose`.
- No Windows, use um wrapper `<projeto>.ps1`:

```powershell
$nomeSemExtensao = (Get-Item $PSCommandPath).BaseName
uv run --script "$PSScriptRoot\$nomeSemExtensao" @args
```

## Atributos disponíveis

`project_name`, `project_dir`, `workspace_dir`, `volumes_dir`, `home`, `argv`,
`git_clones`, `mk_volumes`, `moodle_plugins`, `cli_dict`, `shell_dict`, `hosts`,
`plugins_build_dir`, `plugin_package_name`.

## Métodos auxiliares

`_execute(cmd, *args)`, `_service_is_running(svc)`, `_service_is_healthy(svc)`,
`_prefixed_envs(prefix)`, `_resolve_django_service_and_args()`, `_verb`.

## Desenvolvimento

```bash
uv pip install -e ".[dev]"
pytest --cov=kcm_workspace
```
