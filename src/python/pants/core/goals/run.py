# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
import logging
from abc import ABCMeta
from dataclasses import dataclass
from pathlib import PurePath
from typing import Iterable, Mapping, Optional, Tuple

from pants.base.build_root import BuildRoot
from pants.engine.environment import CompleteEnvironment
from pants.engine.fs import Digest, Workspace
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.process import InteractiveProcess, InteractiveProcessResult
from pants.engine.rules import Effect, Get, collect_rules, goal_rule
from pants.engine.target import (
    BoolField,
    FieldSet,
    NoApplicableTargetsBehavior,
    TargetRootsToFieldSets,
    TargetRootsToFieldSetsRequest,
    WrappedTarget,
    WrappedTargetRequest,
)
from pants.engine.unions import UnionMembership, union
from pants.option.global_options import GlobalOptions
from pants.option.option_types import ArgsListOption, BoolOption
from pants.util.contextutil import temporary_dir
from pants.util.frozendict import FrozenDict
from pants.util.meta import frozen_after_init
from pants.util.strutil import softwrap

logger = logging.getLogger(__name__)


@union
class RunFieldSet(FieldSet, metaclass=ABCMeta):
    """The fields necessary from a target to run a program/script."""


class RestartableField(BoolField):
    alias = "restartable"
    default = False
    help = softwrap(
        """
        If true, runs of this target with the `run` goal may be interrupted and
        restarted when its input files change.
        """
    )


@frozen_after_init
@dataclass(unsafe_hash=True)
class RunRequest:
    digest: Digest
    # Values in args and in env can contain the format specifier "{chroot}", which will
    # be substituted with the (absolute) chroot path.
    args: Tuple[str, ...]
    extra_env: FrozenDict[str, str]

    def __init__(
        self,
        *,
        digest: Digest,
        args: Iterable[str],
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.digest = digest
        self.args = tuple(args)
        self.extra_env = FrozenDict(extra_env or {})


class RunSubsystem(GoalSubsystem):
    name = "run"
    help = softwrap(
        """
        Runs a binary target.

        This goal propagates the return code of the underlying executable.

        If your application can safely be restarted while it is running, you can pass
        `restartable=True` on your binary target (for supported types), and the `run` goal
        will automatically restart them as all relevant files change. This can be particularly
        useful for server applications.
        """
    )

    @classmethod
    def activated(cls, union_membership: UnionMembership) -> bool:
        return RunFieldSet in union_membership

    args = ArgsListOption(
        example="val1 val2 --debug",
        tool_name="the executed target",
        passthrough=True,
    )
    cleanup = BoolOption(
        "--cleanup",
        default=True,
        help=softwrap(
            """
            Whether to clean up the temporary directory in which the binary is chrooted.
            Set this to false to retain the directory, e.g., for debugging.

            Note that setting the global --process-cleanup option to false will also conserve
            this directory, along with those of all other processes that Pants executes.
            This option is more selective and controls just the target binary's directory.
            """
        ),
    )


class Run(Goal):
    subsystem_cls = RunSubsystem


@goal_rule
async def run(
    run_subsystem: RunSubsystem,
    global_options: GlobalOptions,
    workspace: Workspace,
    build_root: BuildRoot,
    complete_env: CompleteEnvironment,
) -> Run:
    targets_to_valid_field_sets = await Get(
        TargetRootsToFieldSets,
        TargetRootsToFieldSetsRequest(
            RunFieldSet,
            goal_description="the `run` goal",
            no_applicable_targets_behavior=NoApplicableTargetsBehavior.error,
            expect_single_field_set=True,
        ),
    )
    field_set = targets_to_valid_field_sets.field_sets[0]
    request = await Get(RunRequest, RunFieldSet, field_set)
    wrapped_target = await Get(
        WrappedTarget, WrappedTargetRequest(field_set.address, description_of_origin="<infallible>")
    )
    restartable = wrapped_target.target.get(RestartableField).value
    # Cleanup is the default, so we want to preserve the chroot if either option is off.
    cleanup = run_subsystem.cleanup and global_options.process_cleanup

    with temporary_dir(root_dir=global_options.pants_workdir, cleanup=cleanup) as tmpdir:
        if not cleanup:
            logger.info(f"Preserving running binary chroot {tmpdir}")
        workspace.write_digest(
            request.digest,
            path_prefix=PurePath(tmpdir).relative_to(build_root.path).as_posix(),
            # We don't want to influence whether the InteractiveProcess is able to restart. Because
            # we're writing into a temp directory, we can safely mark this side_effecting=False.
            side_effecting=False,
        )

        args = (arg.format(chroot=tmpdir) for arg in request.args)
        env = {**complete_env, **{k: v.format(chroot=tmpdir) for k, v in request.extra_env.items()}}
        result = await Effect(
            InteractiveProcessResult,
            InteractiveProcess(
                argv=(*args, *run_subsystem.args),
                env=env,
                run_in_workspace=True,
                restartable=restartable,
            ),
        )
        exit_code = result.exit_code

    return Run(exit_code)


def rules():
    return collect_rules()
