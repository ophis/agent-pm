"""One team's catalog, access policy and prompt composition from an owned config snapshot."""

import os
from collections.abc import Mapping, Sequence
from typing import overload

from agent_pm.delegate.composer import prompt, run, teams
from agent_pm.delegate.composer.types import (
    ConfigError,
    DelegateConfig,
    Intent,
    RunConfig,
    RunParams,
    Team,
)
from agent_pm.delegate.config import read_config
from agent_pm.utils import freeze, thaw


class Composer:
    def __init__(
        self,
        team_dirs: Sequence[str] | None = None,
        config: DelegateConfig | Mapping | None = None,
        *,
        team: Team | None = None,
    ):
        if config is None:
            try:
                config = read_config()
            except (OSError, ValueError) as error:
                raise ConfigError(f"config: {error}") from None
        self.config = config if isinstance(config, DelegateConfig) else DelegateConfig(config)
        if team is not None:
            self.team = team
        else:
            dirs = (
                teams.configured_dirs(thaw(self.config.values).get("team_dirs"))
                if team_dirs is None
                else tuple(team_dirs)
            )
            self.team = teams.team(dirs=dirs)

    def resolve(
        self,
        role: str,
        task: str | None = None,
        *,
        overlays: Sequence[Mapping] = (),
        label: tuple[str, str] = ("run overlay", "overlay"),
    ) -> RunConfig:
        return run.resolve(
            role,
            task,
            overlays=thaw(freeze(tuple(overlays))),
            label=label,
            team=self.team,
            cfg=thaw(self.config.values),
        )

    @overload
    def index(self, role: None = None) -> dict[str, dict[str, str]]: ...

    @overload
    def index(self, role: str) -> dict[str, str]: ...

    def index(self, role: str | None = None) -> dict[str, str] | dict[str, dict[str, str]]:
        cfg = thaw(self.config.values)
        if role is not None:
            return teams.index(role, self.team, cfg=cfg)
        teams.role_tables("config snapshot", "", cfg)
        return {name: teams.index(name, self.team, cfg=cfg) for name in teams.roles(self.team, cfg)}

    def trusted_dirs(self) -> frozenset[str]:
        return run.trusted_dirs(cfg=thaw(self.config.values))

    def catalog(self) -> dict[str, dict[str, str]]:
        return self.index()

    def render(self, config: RunConfig, params: RunParams) -> str:
        return prompt.render(config, params, team=self.team)

    def compose(
        self,
        role: str,
        task: str | None,
        input: str | RunParams,
        *,
        overlays: Sequence[Mapping] = (),
    ) -> str:
        params = (
            input
            if isinstance(input, RunParams)
            else RunParams(input=input, out="", workdir=os.getcwd())
        )
        return self.render(self.resolve(role, task, overlays=overlays), params)

    def access(
        self,
        config: RunConfig,
        workdir: str,
        *,
        cwd: str,
        project: bool,
        repo_dir: str | None = None,
    ) -> Intent:
        return run.access(
            config, workdir, cwd=cwd, project=project, repo_dir=repo_dir, team=self.team
        )

    def lookup(self, kind: str, name: str) -> str | None:
        return teams.lookup(self.team, kind, name)

    def path_for(self, kind: str, name: str) -> str:
        return teams.path_for(self.team, kind, name)

    def tasks_path(self, role: str) -> str:
        return teams.tasks_path(role, self.team)

    def check_all(self) -> None:
        teams.check_all(self.team, thaw(self.config.values))

    def check_team_dirs(
        self,
        *,
        writes: Sequence[tuple[str, str]],
        temp: Sequence[str] | None = None,
    ) -> None:
        teams.check_team_dirs(self.team.dirs, writes=writes, temp=temp)

    @staticmethod
    def check_overlap(shown: str, real: str, writes: Sequence[tuple[str, str]]) -> None:
        teams.check_overlap(shown, real, writes)

    @staticmethod
    def team_paths(directory: str) -> list[str]:
        return teams.team_paths(directory)
