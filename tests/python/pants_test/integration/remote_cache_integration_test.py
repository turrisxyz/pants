# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import time

from pants.engine.fs import Digest, DigestContents, DigestEntries, FileDigest, FileEntry
from pants.engine.internals.native_engine import PyExecutor, PyStubCAS
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import Get, rule
from pants.option.global_options import RemoteCacheWarningsBehavior
from pants.testutil.pants_integration_test import run_pants
from pants.testutil.rule_runner import QueryRule, RuleRunner, logging
from pants.util.logging import LogLevel


def remote_cache_args(
    store_address: str,
    warnings_behavior: RemoteCacheWarningsBehavior = RemoteCacheWarningsBehavior.backoff,
) -> list[str]:
    # NB: Our options code expects `grpc://`, which it will then convert back to
    # `http://` before sending over FFI.
    store_address = store_address.replace("http://", "grpc://")
    return [
        "--remote-cache-read",
        "--remote-cache-write",
        f"--remote-cache-warnings={warnings_behavior.value}",
        f"--remote-store-address={store_address}",
    ]


def test_warns_on_remote_cache_errors() -> None:
    executor = PyExecutor(core_threads=2, max_threads=4)
    cas = PyStubCAS.builder().ac_always_errors().cas_always_errors().build(executor)

    def run(behavior: RemoteCacheWarningsBehavior) -> str:
        pants_run = run_pants(
            [
                "--backend-packages=['pants.backend.python']",
                "--no-dynamic-ui",
                *remote_cache_args(cas.address, behavior),
                "package",
                "testprojects/src/python/hello/main:main",
            ],
            use_pantsd=False,
        )
        pants_run.assert_success()
        return pants_run.stderr

    def read_err(i: int) -> str:
        return f"Failed to read from remote cache ({i} occurrences so far): Unavailable"

    def write_err(i: int) -> str:
        return (
            f'Failed to write to remote cache ({i} occurrences so far): InvalidArgument: "StubCAS is '
            f'configured to always fail"'
        )

    first_read_err = read_err(1)
    first_write_err = write_err(1)
    third_read_err = read_err(3)
    third_write_err = write_err(3)
    fourth_read_err = read_err(4)
    fourth_write_err = write_err(4)

    ignore_result = run(RemoteCacheWarningsBehavior.ignore)
    for err in [
        first_read_err,
        first_write_err,
        third_read_err,
        third_write_err,
        fourth_read_err,
        fourth_write_err,
    ]:
        assert err not in ignore_result

    first_only_result = run(RemoteCacheWarningsBehavior.first_only)
    for err in [first_read_err, first_write_err]:
        assert err in first_only_result, f"Not found in:\n{first_only_result}"
    for err in [third_read_err, third_write_err, fourth_read_err, fourth_write_err]:
        assert err not in first_only_result

    backoff_result = run(RemoteCacheWarningsBehavior.backoff)
    for err in [first_read_err, first_write_err, fourth_read_err, fourth_write_err]:
        assert err in backoff_result
    for err in [third_read_err, third_write_err]:
        assert err not in backoff_result


class ProcessOutputEntries(DigestEntries):
    pass


@rule
async def entries_from_process(process_result: ProcessResult) -> ProcessOutputEntries:
    # DigestEntries won't actually load file content, so we need to force it with DigestContents.
    _ = await Get(DigestContents, Digest, process_result.output_digest)
    return ProcessOutputEntries(await Get(DigestEntries, Digest, process_result.output_digest))


@logging
def test_lazy_fetch_backtracking() -> None:
    executor = PyExecutor(core_threads=2, max_threads=4)
    cas = PyStubCAS.builder().build(executor)

    def run() -> tuple[FileDigest, dict[str, int]]:
        # Use an isolated store to ensure that the only content is in the remote/stub cache.
        rule_runner = RuleRunner(
            rules=[entries_from_process, QueryRule(ProcessOutputEntries, [Process])],
            isolated_local_store=True,
            bootstrap_args=[
                "--no-remote-cache-eager-fetch",
                "--no-local-cache",
                *remote_cache_args(cas.address),
            ],
        )
        entries = rule_runner.request(
            ProcessOutputEntries,
            [
                Process(
                    ["/bin/bash", "-c", "sleep 1; echo content > file.txt"],
                    description="Create file.txt",
                    output_files=["file.txt"],
                    level=LogLevel.INFO,
                )
            ],
        )
        assert len(entries) == 1
        entry = entries[0]
        assert isinstance(entry, FileEntry)

        # Wait for any async cache writes to complete.
        time.sleep(1)
        return entry.file_digest, rule_runner.scheduler.get_metrics()

    # Run once to populate the remote cache, and validate that there is one entry afterwards.
    assert cas.action_cache_len() == 0
    file_digest1, metrics1 = run()
    assert cas.action_cache_len() == 1
    assert metrics1["remote_cache_requests"] == 1
    assert metrics1["remote_cache_requests_uncached"] == 1

    # Then, remove the content from the remote store and run again.
    assert cas.remove(file_digest1)
    file_digest2, metrics2 = run()
    assert file_digest1 == file_digest2
    # Validate both that we hit the cache, and that we backtracked to actually run the process.
    assert metrics2["remote_cache_requests"] == 1
    assert metrics2["remote_cache_requests_cached"] == 1
    assert metrics2["backtrack_attempts"] == 1
