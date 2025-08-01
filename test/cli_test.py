# Copyright Modal Labs 2022-2023
import asyncio
import contextlib
import json
import os
import platform
import pytest
import re
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from pickle import dumps
from unittest import mock
from unittest.mock import MagicMock

import click
import click.testing
import toml

from modal import App, Sandbox
from modal._serialization import serialize
from modal._utils.grpc_testing import InterceptionContext
from modal.cli.entry_point import entrypoint_cli
from modal.exception import InvalidError
from modal_proto import api_pb2

from . import helpers
from .supports.skip import skip_windows

dummy_app_file = """
import modal

import other_module

app = modal.App("my_app")

# Sanity check that the module is imported properly
import sys
mod = sys.modules[__name__]
assert mod.app == app
"""

dummy_other_module_file = "x = 42"


def _run(args: list[str], expected_exit_code: int = 0, expected_stderr: str = "", expected_error: str = ""):
    if sys.version_info < (3, 10):
        # mix_stderr was removed in Click 8.2 which also removed support for Python 3.9
        # The desired behavior is the same across verisons, but we need to explicitly enable it on Python 3.9
        runner = click.testing.CliRunner(mix_stderr=False)
    else:
        runner = click.testing.CliRunner()
    # DEBUGGING TIP: this runs the CLI in a separate subprocess, and output from it is not echoed by default,
    # including from the mock fixtures. Print res.stdout and res.stderr for debugging tests.
    with mock.patch.object(sys, "argv", args):
        res = runner.invoke(entrypoint_cli, args)
    if res.exit_code != expected_exit_code:
        print("stdout:", repr(res.stdout))
        print("stderr:", repr(res.stderr))
        traceback.print_tb(res.exc_info[2])
        print(res.exception, file=sys.stderr)
        assert res.exit_code == expected_exit_code
    if expected_stderr:
        assert re.search(expected_stderr, res.stderr), "stderr does not match expected string"
    if expected_error:
        assert re.search(expected_error, str(res.exception)), "exception message does not match expected string"
    return res


def test_app_deploy_success(servicer, mock_dir, set_env_client):
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        # Deploy as a script in cwd
        _run(["deploy", "myapp.py"])

        # Deploy as a module
        _run(["deploy", "-m", "myapp"])

        # Deploy as a script with an absolute path
        _run(["deploy", os.path.abspath("myapp.py")])

    app_names = {app_name for (_, app_name) in servicer.deployed_apps}
    assert "my_app" in app_names


def test_app_deploy_with_name(servicer, mock_dir, set_env_client):
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        _run(["deploy", "myapp.py", "--name", "my_app_foo"])

    app_names = {app_name for (_, app_name) in servicer.deployed_apps}
    assert "my_app_foo" in app_names


def test_secret_create_list_delete(servicer, set_env_client):
    # fail without any keys
    _run(["secret", "create", "foo"], 2, None)

    _run(["secret", "create", "foo", "VAR=foo"])
    assert "foo" in _run(["secret", "list"]).stdout

    # Creating the same one again should fail
    _run(["secret", "create", "foo", "VAR=foo"], expected_exit_code=1)

    # But it should succeed with --force
    _run(["secret", "create", "foo", "VAR=foo", "--force"])

    # Create a few more
    _run(["secret", "create", "bar", "VAR=bar"])
    _run(["secret", "create", "buz", "VAR=buz"])
    assert len(json.loads(_run(["secret", "list", "--json"]).stdout)) == 3

    # We can delete it
    _run(["secret", "delete", "foo", "--yes"])
    assert "foo" not in _run(["secret", "list"]).stdout


@pytest.mark.parametrize(
    ("env_content", "expected_exit_code", "expected_stderr"),
    [
        ("KEY1=VAL1\nKEY2=VAL2", 0, None),
        ("", 2, "You need to specify at least one key for your secret"),
        ("=VAL", 2, "You need to specify at least one key for your secret"),
        ("KEY=", 0, None),
        ("KEY=413", 0, None),  # dotenv reads everything as string...
        ("KEY", 2, "Non-string value"),  # ... except this, which is read as None
    ],
)
def test_secret_create_from_dotenv(
    servicer, set_env_client, tmp_path, env_content, expected_exit_code, expected_stderr
):
    env_file = tmp_path / ".env"
    env_file.write_text(env_content)
    _run(
        ["secret", "create", "foo", "--from-dotenv", env_file.as_posix()],
        expected_exit_code=expected_exit_code,
        expected_stderr=expected_stderr,
    )


@pytest.mark.parametrize(
    ("json_content", "expected_exit_code", "expected_stderr"),
    [
        ('{"KEY1": "VAL1",\n"KEY2": "VAL2"}', 0, None),
        ("", 2, "Could not parse JSON file"),
        ("{}", 2, "You need to specify at least one key for your secret"),
        ('{"": ""}', 2, "Invalid key"),
        ('{"KEY": ""}', 0, None),
        ('{"KEY": "413"}', 0, None),
        ('{"KEY": null}', 2, "Non-string value"),
        ('{"KEY": 413}', 2, "Non-string value"),
        ('{"KEY": {"NESTED": "val"}}', 2, "Non-string value"),
    ],
)
def test_secret_create_from_json(servicer, set_env_client, tmp_path, json_content, expected_exit_code, expected_stderr):
    json_file = tmp_path / "test.json"
    json_file.write_text(json_content)
    _run(
        ["secret", "create", "foo", "--from-json", json_file.as_posix()],
        expected_exit_code=expected_exit_code,
        expected_stderr=expected_stderr,
    )


def test_app_token_new(servicer, set_env_client, server_url_env, modal_config):
    servicer.required_creds = {"abc": "xyz"}
    with modal_config() as config_file_path:
        _run(["token", "new", "--profile", "_test"])
        assert "_test" in toml.load(config_file_path)


def test_token_env_var_warning(servicer, set_env_client, server_url_env, modal_config, monkeypatch):
    servicer.required_creds = {"abc": "xyz"}
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-123")
    with modal_config():
        res = _run(["token", "new"])
        assert "MODAL_TOKEN_ID environment variable is" in res.stdout

    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-xyz")
    with modal_config():
        res = _run(["token", "new"])
        assert "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET environment variables are" in res.stdout


def test_app_setup(servicer, set_env_client, server_url_env, modal_config):
    servicer.required_creds = {"abc": "xyz"}
    with modal_config() as config_file_path:
        _run(["setup", "--profile", "_test"])
        assert "_test" in toml.load(config_file_path)


app_file = Path("app_run_tests") / "default_app.py"
app_module = "app_run_tests.default_app"
file_with_entrypoint = Path("app_run_tests") / "local_entrypoint.py"


@pytest.mark.parametrize(
    ("run_command", "expected_exit_code", "expected_output"),
    [
        ([f"{app_file}"], 0, ""),
        ([f"{app_file}::app"], 0, ""),
        ([f"{app_file}::foo"], 0, ""),
        ([f"{app_file}::bar"], 1, ""),
        ([f"{file_with_entrypoint}"], 0, ""),
        ([f"{file_with_entrypoint}::main"], 0, ""),
        ([f"{file_with_entrypoint}::app.main"], 0, ""),
        ([f"{file_with_entrypoint}::foo"], 0, ""),
    ],
)
def test_run(servicer, set_env_client, supports_dir, monkeypatch, run_command, expected_exit_code, expected_output):
    monkeypatch.chdir(supports_dir)
    res = _run(["run"] + run_command, expected_exit_code=expected_exit_code)
    if expected_output:
        assert re.search(expected_output, res.stdout) or re.search(expected_output, res.stderr), (
            "output does not match expected string"
        )


def test_run_warns_without_module_flag(
    servicer,
    set_env_client,
    supports_dir,
    recwarn,
    monkeypatch,
):
    monkeypatch.chdir(supports_dir)
    _run(["run", "-m", f"{app_module}::foo"])
    assert not len(recwarn)

    with pytest.warns(match=" -m "):
        _run(["run", f"{app_module}::foo"])


def test_run_async(servicer, set_env_client, test_dir):
    sync_fn = test_dir / "supports" / "app_run_tests" / "local_entrypoint.py"
    res = _run(["run", sync_fn.as_posix()])
    assert "called locally" in res.stdout

    async_fn = test_dir / "supports" / "app_run_tests" / "local_entrypoint_async.py"
    res = _run(["run", async_fn.as_posix()])
    assert "called locally (async)" in res.stdout


def test_run_generator(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "generator.py"
    result = _run(["run", app_file.as_posix()], expected_exit_code=1)
    assert "generator functions" in str(result.exception)


def test_help_message_unspecified_function(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "app_with_multiple_functions.py"
    result = _run(["run", app_file.as_posix()], expected_exit_code=1, expected_stderr=None)

    # should suggest available functions on the app:
    assert "foo" in result.stderr
    assert "bar" in result.stderr

    result = _run(
        ["run", app_file.as_posix(), "--help"], expected_exit_code=1, expected_stderr=None
    )  # TODO: help should not return non-zero
    # help should also available functions on the app:
    assert "foo" in result.stderr
    assert "bar" in result.stderr


def test_run_states(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "default_app.py"
    _run(["run", app_file.as_posix()])
    assert servicer.app_state_history["ap-1"] == [
        api_pb2.APP_STATE_INITIALIZING,
        api_pb2.APP_STATE_EPHEMERAL,
        api_pb2.APP_STATE_STOPPED,
    ]


def test_run_detach(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "default_app.py"
    _run(["run", "--detach", app_file.as_posix()])
    assert servicer.app_state_history["ap-1"] == [api_pb2.APP_STATE_INITIALIZING, api_pb2.APP_STATE_DETACHED]


def test_run_quiet(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "default_app.py"
    # Just tests that the command runs without error for now (tests end up defaulting to `show_progress=False` anyway,
    # without a TTY).
    _run(["run", "--quiet", app_file.as_posix()])


def test_run_class_hierarchy(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "class_hierarchy.py"
    _run(["run", app_file.as_posix() + "::Wrapped.defined_on_base"])
    _run(["run", app_file.as_posix() + "::Wrapped.overridden_on_wrapped"])


def test_run_write_result(servicer, set_env_client, test_dir):
    # Note that this test only exercises local entrypoint functions,
    # because the servicer doesn't appear to mock remote execution faithfully?
    app_file = (test_dir / "supports" / "app_run_tests" / "returns_data.py").as_posix()

    with tempfile.TemporaryDirectory() as tmpdir:
        _run(["run", "--write-result", result_file := f"{tmpdir}/result.txt", f"{app_file}::returns_str"])
        with open(result_file, "rt") as f:
            assert f.read() == "Hello!"

        _run(["run", "-w", result_file := f"{tmpdir}/result.bin", f"{app_file}::returns_bytes"])
        with open(result_file, "rb") as f:
            assert f.read().decode("utf8") == "Hello!"

        _run(
            ["run", "-w", result_file := f"{tmpdir}/result.bin", f"{app_file}::returns_int"],
            expected_exit_code=1,
            expected_error="Function must return str or bytes when using `--write-result`; got int.",
        )


@pytest.mark.parametrize(
    ["args", "success", "expected_warning"],
    [
        (["--name=deployment_name", str(app_file)], True, ""),
        (["--name=deployment_name", app_module], True, f"modal deploy -m {app_module}"),
        (["--name=deployment_name", "-m", app_module], True, ""),
    ],
)
def test_deploy(servicer, set_env_client, supports_dir, monkeypatch, args, success, expected_warning, recwarn):
    monkeypatch.chdir(supports_dir)
    _run(["deploy"] + args, expected_exit_code=0 if success else 1)
    if success:
        assert servicer.app_state_history["ap-1"] == [api_pb2.APP_STATE_INITIALIZING, api_pb2.APP_STATE_DEPLOYED]
    else:
        assert api_pb2.APP_STATE_DEPLOYED not in servicer.app_state_history["ap-1"]
    if expected_warning:
        assert len(recwarn) == 1
        assert expected_warning in str(recwarn[0].message)


def test_run_custom_app(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "custom_app.py"
    res = _run(["run", app_file.as_posix() + "::app"], expected_exit_code=1, expected_stderr=None)
    assert "Specify a Modal Function or local entrypoint to run" in res.stderr
    assert "foo / my_app.foo" in res.stderr
    res = _run(["run", app_file.as_posix() + "::app.foo"], expected_exit_code=1, expected_stderr=None)
    assert "Specify a Modal Function or local entrypoint" in res.stderr
    assert "foo / my_app.foo" in res.stderr

    _run(["run", app_file.as_posix() + "::foo"])


def test_run_aiofunc(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "async_app.py"
    _run(["run", app_file.as_posix()])
    assert len(servicer.function_call_inputs) == 1


def test_run_local_entrypoint(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "local_entrypoint.py"

    res = _run(["run", app_file.as_posix() + "::app.main"])  # explicit name
    assert "called locally" in res.stdout
    assert len(servicer.function_call_inputs) == 2

    res = _run(["run", app_file.as_posix()])  # only one entry-point, no name needed
    assert "called locally" in res.stdout
    assert len(servicer.function_call_inputs) == 4


def test_run_local_entrypoint_error(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "local_entrypoint.py"
    _run(
        ["run", "-iq", app_file.as_posix()],
        expected_exit_code=1,
        expected_error="To use interactive mode, remove the --quiet flag",
    )


def test_run_function_error(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "default_app.py"

    _run(
        ["run", "-iq", app_file.as_posix()],
        expected_exit_code=1,
        expected_error="To use interactive mode, remove the --quiet flag",
    )


def test_run_cls_error(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "cls.py"

    _run(
        ["run", "-iq", f"{app_file.as_posix()}::AParametrized.some_method", "--x", "42", "--y", "1000"],
        expected_exit_code=1,
        expected_error="To use interactive mode, remove the --quiet flag",
    )


def test_run_local_entrypoint_invalid_with_app_run(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "local_entrypoint_invalid.py"

    res = _run(["run", app_file.as_posix()], expected_exit_code=1)
    assert "app is already running" in str(res.exception.__cause__).lower()
    assert "unreachable" not in res.stdout
    assert len(servicer.function_call_inputs) == 0


def test_run_parse_args_entrypoint(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "cli_args.py"
    res = _run(["run", app_file.as_posix()], expected_exit_code=1, expected_stderr=None)
    assert "Specify a Modal Function or local entrypoint to run" in res.stderr

    valid_call_args = [
        (
            [
                "run",
                f"{app_file.as_posix()}::app.dt_arg",
                "--dt",
                "2022-10-31",
            ],
            "the day is 31",
        ),
        (["run", f"{app_file.as_posix()}::dt_arg", "--dt=2022-10-31"], "the day is 31"),
        (["run", f"{app_file.as_posix()}::int_arg", "--i=200"], "200 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::default_arg"], "10 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::unannotated_arg", "--i=2022-10-31"], "'2022-10-31' <class 'str'>"),
        (["run", f"{app_file.as_posix()}::unannotated_default_arg"], "10 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::optional_arg", "--i=20"], "20 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::optional_arg"], "None <class 'NoneType'>"),
        (["run", f"{app_file.as_posix()}::optional_arg_postponed"], "None <class 'NoneType'>"),
    ]
    if sys.version_info >= (3, 10):
        valid_call_args.extend(
            [
                (["run", f"{app_file.as_posix()}::optional_arg_pep604", "--i=20"], "20 <class 'int'>"),
                (["run", f"{app_file.as_posix()}::optional_arg_pep604"], "None <class 'NoneType'>"),
            ]
        )
    for args, expected in valid_call_args:
        res = _run(args)
        assert expected in res.stdout
        assert len(servicer.function_call_inputs) == 0

    res = _run(["run", f"{app_file.as_posix()}::unparseable_annot", "--i=20"], expected_exit_code=1)
    assert "Parameter `i` has unparseable annotation: typing.Union[int, str]" in str(res.exception)

    res = _run(["run", f"{app_file.as_posix()}::unevaluatable_annot", "--i=20"], expected_exit_code=1)
    assert "Unable to generate command line interface" in str(res.exception)
    assert "no go" in str(res.exception)

    if sys.version_info <= (3, 10):
        res = _run(["run", f"{app_file.as_posix()}::optional_arg_pep604"], expected_exit_code=1)
        assert "Unable to generate command line interface for app entrypoint" in str(res.exception)
        assert "unsupported operand" in str(res.exception)


def test_run_parse_args_function(servicer, set_env_client, test_dir, recwarn):
    app_file = test_dir / "supports" / "app_run_tests" / "cli_args.py"
    res = _run(["run", app_file.as_posix()], expected_exit_code=1, expected_stderr=None)
    assert "Specify a Modal Function or local entrypoint to run" in res.stderr

    # HACK: all the tests use the same arg, i.
    @servicer.function_body
    def print_type(i):
        print(repr(i), type(i))

    valid_call_args = [
        (["run", f"{app_file.as_posix()}::int_arg_fn", "--i=200"], "200 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::ALifecycle.some_method", "--i=hello"], "'hello' <class 'str'>"),
        (["run", f"{app_file.as_posix()}::ALifecycle.some_method_int", "--i=42"], "42 <class 'int'>"),
        (["run", f"{app_file.as_posix()}::optional_arg_fn"], "None <class 'NoneType'>"),
    ]
    for args, expected in valid_call_args:
        res = _run(args)
        assert expected in res.stdout

    if len(recwarn):
        print("Unexpected warnings:", [str(w) for w in recwarn])
    assert len(recwarn) == 0


def test_run_user_script_exception(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "raises_error.py"
    res = _run(["run", app_file.as_posix()], expected_exit_code=1)
    assert res.exc_info[1].user_source == str(app_file.resolve())


@pytest.fixture
def fresh_main_thread_assertion_module(test_dir):
    modules_to_unload = [n for n in sys.modules.keys() if "main_thread_assertion" in n]
    assert len(modules_to_unload) <= 1
    for mod in modules_to_unload:
        sys.modules.pop(mod)
    yield test_dir / "supports" / "app_run_tests" / "main_thread_assertion.py"


def test_no_user_code_in_synchronicity_run(servicer, set_env_client, test_dir, fresh_main_thread_assertion_module):
    pytest._did_load_main_thread_assertion = False  # type: ignore
    _run(["run", fresh_main_thread_assertion_module.as_posix()])
    assert pytest._did_load_main_thread_assertion  # type: ignore
    print()


def test_no_user_code_in_synchronicity_deploy(servicer, set_env_client, test_dir, fresh_main_thread_assertion_module):
    pytest._did_load_main_thread_assertion = False  # type: ignore
    _run(["deploy", "--name", "foo", fresh_main_thread_assertion_module.as_posix()])
    assert pytest._did_load_main_thread_assertion  # type: ignore


def test_serve(servicer, set_env_client, server_url_env, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "webhook.py"
    _run(["serve", app_file.as_posix(), "--timeout", "3"], expected_exit_code=0)


@pytest.fixture
def mock_shell_pty(servicer):
    servicer.shell_prompt = b"TEST_PROMPT# "

    def mock_get_pty_info(shell: bool) -> api_pb2.PTYInfo:
        rows, cols = (64, 128)
        return api_pb2.PTYInfo(
            enabled=True,
            winsz_rows=rows,
            winsz_cols=cols,
            env_term=os.environ.get("TERM"),
            env_colorterm=os.environ.get("COLORTERM"),
            env_term_program=os.environ.get("TERM_PROGRAM"),
            pty_type=api_pb2.PTYInfo.PTY_TYPE_SHELL,
        )

    captured_out = []
    fake_stdin = [b"echo foo\n", b"exit\n"]

    async def write_to_fd(fd: int, data: bytes):
        nonlocal captured_out
        captured_out.append((fd, data))

    @contextlib.asynccontextmanager
    async def fake_stream_from_stdin(handle_input, use_raw_terminal=False):
        async def _write():
            message_index = 0
            while True:
                if message_index == len(fake_stdin):
                    break
                data = fake_stdin[message_index]
                await handle_input(data, message_index)
                message_index += 1

        write_task = asyncio.create_task(_write())
        yield
        write_task.cancel()

    with (
        mock.patch("rich.console.Console.is_terminal", True),
        mock.patch("modal.cli.container.get_pty_info", mock_get_pty_info),
        mock.patch("modal._pty.get_pty_info", mock_get_pty_info),
        mock.patch("modal.runner.get_pty_info", mock_get_pty_info),
        mock.patch("modal._utils.shell_utils.stream_from_stdin", fake_stream_from_stdin),
        mock.patch("modal.container_process.stream_from_stdin", fake_stream_from_stdin),
        mock.patch("modal.container_process.write_to_fd", write_to_fd),
    ):
        yield fake_stdin, captured_out


app_file = Path("app_run_tests") / "default_app.py"
app_file_as_module = "app_run_tests.default_app"
webhook_app_file = Path("app_run_tests") / "webhook.py"
cls_app_file = Path("app_run_tests") / "cls.py"


@skip_windows("modal shell is not supported on Windows.")
@pytest.mark.parametrize(
    ["flags", "rel_file", "suffix"],
    [
        ([], app_file, "::foo"),  # Function is explicitly specified
        (["-m"], app_file_as_module, "::foo"),  # Function is explicitly specified - module mode
        ([], webhook_app_file, "::foo"),  # Function is explicitly specified
        ([], webhook_app_file, ""),  # Function must be inferred
        # TODO: fix modal shell auto-detection of a single class, even if it has multiple methods
        # ([], cls_app_file, ""),  # Class must be inferred
        # ([], cls_app_file, "AParametrized"),  # class name
        ([], cls_app_file, "::AParametrized.some_method"),  # method name
    ],
)
def test_shell(servicer, set_env_client, mock_shell_pty, suffix, monkeypatch, supports_dir, rel_file, flags):
    monkeypatch.chdir(supports_dir)
    fake_stdin, captured_out = mock_shell_pty

    fake_stdin.clear()
    fake_stdin.extend([b'echo "Hello World"\n', b"exit\n"])

    shell_prompt = servicer.shell_prompt

    _run(["shell"] + flags + [str(rel_file) + suffix])

    # first captured message is the empty message the mock server sends
    assert captured_out == [(1, shell_prompt), (1, b"Hello World\n")]
    captured_out.clear()


@skip_windows("modal shell is not supported on Windows.")
def test_shell_cmd(servicer, set_env_client, test_dir, mock_shell_pty):
    app_file = test_dir / "supports" / "app_run_tests" / "default_app.py"
    _, captured_out = mock_shell_pty
    shell_prompt = servicer.shell_prompt
    _run(["shell", "--cmd", "pwd", app_file.as_posix() + "::foo"])
    expected_output = subprocess.run(["pwd"], capture_output=True, check=True).stdout
    assert captured_out == [(1, shell_prompt), (1, expected_output)]


@skip_windows("modal shell is not supported on Windows.")
def test_shell_preserve_token(servicer, set_env_client, mock_shell_pty, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "my-token-id")

    fake_stdin, captured_out = mock_shell_pty
    shell_prompt = servicer.shell_prompt

    fake_stdin.clear()
    fake_stdin.extend([b'echo "$MODAL_TOKEN_ID"\n', b"exit\n"])
    _run(["shell"])

    expected_output = b"my-token-id\n"
    assert captured_out == [(1, shell_prompt), (1, expected_output)]


def test_shell_unsuported_cmds_fails_on_windows(servicer, set_env_client, mock_shell_pty):
    expected_exit_code = 1 if platform.system() == "Windows" else 0
    res = _run(["shell"], expected_exit_code=expected_exit_code)

    if expected_exit_code != 0:
        assert re.search("Windows", str(res.exception)), "exception message does not match expected string"


def test_app_descriptions(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "prints_desc_app.py"
    _run(["run", "--detach", app_file.as_posix() + "::foo"])

    create_reqs = [s for s in servicer.requests if isinstance(s, api_pb2.AppCreateRequest)]
    assert len(create_reqs) == 1
    assert create_reqs[0].app_state == api_pb2.APP_STATE_DETACHED
    description = create_reqs[0].description
    assert "prints_desc_app.py::foo" in description
    assert "run --detach " not in description

    _run(["serve", "--timeout", "0.0", app_file.as_posix()])
    create_reqs = [s for s in servicer.requests if isinstance(s, api_pb2.AppCreateRequest)]
    assert len(create_reqs) == 2
    description = create_reqs[1].description
    assert "prints_desc_app.py" in description
    assert "serve" not in description
    assert "--timeout 0.0" not in description


def test_logs(servicer, server_url_env, set_env_client, mock_dir):
    async def app_done(self, stream):
        await stream.recv_message()
        log = api_pb2.TaskLogs(data="hello\n", file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT)
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="1", items=[log]))
        await stream.send_message(api_pb2.TaskLogsBatch(app_done=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("AppGetLogs", app_done)

        # TODO Fix the mock servicer to use "real" App IDs so this does not get misconstrued as a name
        # res = _run(["app", "logs", "ap-123"])
        # assert res.stdout == "hello\n"

        with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
            res = _run(["deploy", "myapp.py", "--name", "my-app", "--stream-logs"])
            assert res.stdout.endswith("hello\n")

        res = _run(["app", "logs", "my-app"])
        assert res.stdout == "hello\n"

    _run(
        ["app", "logs", "does-not-exist"],
        expected_exit_code=1,
        expected_error="Could not find a deployed app named 'does-not-exist'",
    )


def test_app_stop(servicer, mock_dir, set_env_client):
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        # Deploy as a module
        _run(["deploy", "-m", "myapp"])

    res = _run(["app", "list"])
    assert re.search("my_app .+ deployed", res.stdout)

    _run(["app", "stop", "my_app"])

    # Note that the mock servicer doesn't report "stopped" app statuses
    # so we just check that it's not reported as deployed
    res = _run(["app", "list"])
    assert not re.search("my_app .+ deployed", res.stdout)


def test_nfs_get(set_env_client, servicer):
    nfs_name = "my-shared-nfs"
    _run(["nfs", "create", nfs_name])
    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = os.path.join(tmpdir, "upload.txt")
        with open(upload_path, "w") as f:
            f.write("foo bar baz")
            f.flush()
        _run(["nfs", "put", nfs_name, upload_path, "test.txt"])

        _run(["nfs", "get", nfs_name, "test.txt", tmpdir])
        with open(os.path.join(tmpdir, "test.txt")) as f:
            assert f.read() == "foo bar baz"


def test_nfs_create_delete(servicer, server_url_env, set_env_client):
    name = "test-delete-nfs"
    _run(["nfs", "create", name])
    assert name in _run(["nfs", "list"]).stdout
    _run(["nfs", "delete", "--yes", name])
    assert name not in _run(["nfs", "list"]).stdout


def test_volume_cli(set_env_client):
    _run(["volume", "--help"])


def test_volume_get(servicer, set_env_client):
    vol_name = "my-test-vol"
    _run(["volume", "create", vol_name])
    file_path = "test.txt"
    file_contents = b"foo bar baz"
    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = os.path.join(tmpdir, "upload.txt")
        with open(upload_path, "wb") as f:
            f.write(file_contents)
            f.flush()
        _run(["volume", "put", vol_name, upload_path, file_path])

        _run(["volume", "get", vol_name, file_path, tmpdir])
        with open(os.path.join(tmpdir, file_path), "rb") as f:
            assert f.read() == file_contents

        download_path = os.path.join(tmpdir, "download.txt")
        _run(["volume", "get", vol_name, file_path, download_path])
        with open(download_path, "rb") as f:
            assert f.read() == file_contents

    with tempfile.TemporaryDirectory() as tmpdir2:
        _run(["volume", "get", vol_name, "/", tmpdir2])
        with open(os.path.join(tmpdir2, file_path), "rb") as f:
            assert f.read() == file_contents


def test_volume_put_force(servicer, set_env_client):
    vol_name = "my-test-vol"
    _run(["volume", "create", vol_name])
    file_path = "test.txt"
    file_contents = b"foo bar baz"
    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = os.path.join(tmpdir, "upload.txt")
        with open(upload_path, "wb") as f:
            f.write(file_contents)
            f.flush()

        # File upload
        _run(["volume", "put", vol_name, upload_path, file_path])  # Seed the volume
        with servicer.intercept() as ctx:
            _run(["volume", "put", vol_name, upload_path, file_path], expected_exit_code=2, expected_stderr=None)
            assert ctx.pop_request("VolumePutFiles").disallow_overwrite_existing_files

            _run(["volume", "put", vol_name, upload_path, file_path, "--force"])
            assert not ctx.pop_request("VolumePutFiles").disallow_overwrite_existing_files

        # Dir upload
        _run(["volume", "put", vol_name, tmpdir])  # Seed the volume
        with servicer.intercept() as ctx:
            _run(["volume", "put", vol_name, tmpdir], expected_exit_code=2, expected_stderr=None)
            assert ctx.pop_request("VolumePutFiles").disallow_overwrite_existing_files

            _run(["volume", "put", vol_name, tmpdir, "--force"])
            assert not ctx.pop_request("VolumePutFiles").disallow_overwrite_existing_files


def test_volume_rm(servicer, set_env_client):
    vol_name = "my-test-vol"
    _run(["volume", "create", vol_name])
    file_path = "test.txt"
    file_contents = b"foo bar baz"
    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = os.path.join(tmpdir, "upload.txt")
        with open(upload_path, "wb") as f:
            f.write(file_contents)
            f.flush()
        _run(["volume", "put", vol_name, upload_path, file_path])

        _run(["volume", "get", vol_name, file_path, tmpdir])
        with open(os.path.join(tmpdir, file_path), "rb") as f:
            assert f.read() == file_contents

        _run(["volume", "rm", vol_name, file_path])
        _run(["volume", "get", vol_name, file_path], expected_exit_code=1, expected_stderr=None)


def test_volume_ls(servicer, set_env_client):
    vol_name = "my-test-vol"
    _run(["volume", "create", vol_name])

    fnames = ["a", "b", "c"]
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname in fnames:
            src_path = os.path.join(tmpdir, f"{fname}.txt")
            with open(src_path, "w") as f:
                f.write(fname * 5)
            _run(["volume", "put", vol_name, src_path, f"data/{fname}.txt"])

    res = _run(["volume", "ls", vol_name])
    assert "data" in res.stdout

    res = _run(["volume", "ls", vol_name, "data"])
    for fname in fnames:
        assert f"{fname}.txt" in res.stdout

    res = _run(["volume", "ls", vol_name, "data", "--json"])
    res_dict = json.loads(res.stdout)
    assert len(res_dict) == len(fnames)
    for entry, fname in zip(res_dict, fnames):
        assert entry["Filename"] == f"data/{fname}.txt"
        assert entry["Type"] == "file"


def test_volume_create_delete(servicer, server_url_env, set_env_client):
    vol_name = "test-delete-vol"
    _run(["volume", "create", vol_name])
    assert vol_name in _run(["volume", "list"]).stdout
    _run(["volume", "delete", "--yes", vol_name])
    assert vol_name not in _run(["volume", "list"]).stdout


def test_volume_rename(servicer, server_url_env, set_env_client):
    old_name, new_name = "foo-vol", "bar-vol"
    _run(["volume", "create", old_name])
    _run(["volume", "rename", "--yes", old_name, new_name])
    assert new_name in _run(["volume", "list"]).stdout
    assert old_name not in _run(["volume", "list"]).stdout


@pytest.mark.parametrize("command", [["shell"]])
@pytest.mark.usefixtures("set_env_client", "mock_shell_pty")
@skip_windows("modal shell is not supported on Windows.")
def test_environment_flag(test_dir, servicer, command):
    @servicer.function_body
    def nothing(
        arg=None,
    ):  # hacky - compatible with both argless modal run and interactive mode which always sends an arg...
        pass

    app_file = test_dir / "supports" / "app_run_tests" / "app_with_lookups.py"
    with servicer.intercept() as ctx:
        ctx.add_response(
            "MountGetOrCreate",
            api_pb2.MountGetOrCreateResponse(
                mount_id="mo-123",
                handle_metadata=api_pb2.MountHandleMetadata(content_checksum_sha256_hex="abc123"),
            ),
            request_filter=lambda req: req.deployment_name.startswith("modal-client-mount")
            and req.namespace == api_pb2.DEPLOYMENT_NAMESPACE_GLOBAL,
        )  # built-in client lookup
        ctx.add_response(
            "SharedVolumeGetOrCreate",
            api_pb2.SharedVolumeGetOrCreateResponse(shared_volume_id="sv-123"),
            request_filter=lambda req: req.deployment_name == "volume_app" and req.environment_name == "staging",
        )
        _run(command + ["--env=staging", str(app_file)])

    app_create: api_pb2.AppCreateRequest = ctx.pop_request("AppCreate")
    assert app_create.environment_name == "staging"


@pytest.mark.parametrize("command", [["run"], ["deploy"], ["serve", "--timeout=1"], ["shell"]])
@pytest.mark.usefixtures("set_env_client", "mock_shell_pty")
@skip_windows("modal shell is not supported on Windows.")
def test_environment_noflag(test_dir, servicer, command, monkeypatch):
    monkeypatch.setenv("MODAL_ENVIRONMENT", "some_weird_default_env")

    @servicer.function_body
    def nothing(
        arg=None,
    ):  # hacky - compatible with both argless modal run and interactive mode which always sends an arg...
        pass

    app_file = test_dir / "supports" / "app_run_tests" / "app_with_lookups.py"
    with servicer.intercept() as ctx:
        ctx.add_response(
            "MountGetOrCreate",
            api_pb2.MountGetOrCreateResponse(
                mount_id="mo-123",
                handle_metadata=api_pb2.MountHandleMetadata(content_checksum_sha256_hex="abc123"),
            ),
            request_filter=lambda req: req.deployment_name.startswith("modal-client-mount")
            and req.namespace == api_pb2.DEPLOYMENT_NAMESPACE_GLOBAL,
        )  # built-in client lookup
        ctx.add_response(
            "SharedVolumeGetOrCreate",
            api_pb2.SharedVolumeGetOrCreateResponse(shared_volume_id="sv-123"),
            request_filter=lambda req: req.deployment_name == "volume_app"
            and req.environment_name == "some_weird_default_env",
        )
        _run(command + [str(app_file)])

    app_create: api_pb2.AppCreateRequest = ctx.pop_request("AppCreate")
    assert app_create.environment_name == "some_weird_default_env"


def test_cls(servicer, set_env_client, test_dir):
    app_file = test_dir / "supports" / "app_run_tests" / "cls.py"

    print(_run(["run", app_file.as_posix(), "--x", "42", "--y", "1000"]))
    _run(["run", f"{app_file.as_posix()}::AParametrized.some_method", "--x", "42", "--y", "1000"])


def test_profile_list(servicer, server_url_env, modal_config):
    config = """
    [test-profile]
    token_id = "ak-abc"
    token_secret = "as-xyz"

    [other-profile]
    token_id = "ak-123"
    token_secret = "as-789"
    active = true
    """

    with modal_config(config):
        servicer.required_creds = {"ak-abc": "as-xyz", "ak-123": "as-789"}
        res = _run(["profile", "list"])
        table_rows = res.stdout.split("\n")
        assert re.search("Profile .+ Workspace", table_rows[1])
        assert re.search("test-profile .+ test-username", table_rows[3])
        assert re.search("other-profile .+ test-username", table_rows[4])

        res = _run(["profile", "list", "--json"])
        json_data = json.loads(res.stdout)
        assert json_data[0]["name"] == "test-profile"
        assert json_data[0]["workspace"] == "test-username"
        assert json_data[1]["name"] == "other-profile"
        assert json_data[1]["workspace"] == "test-username"

        orig_env_token_id = os.environ.get("MODAL_TOKEN_ID")
        orig_env_token_secret = os.environ.get("MODAL_TOKEN_SECRET")
        os.environ["MODAL_TOKEN_ID"] = "ak-abc"
        os.environ["MODAL_TOKEN_SECRET"] = "as-xyz"
        servicer.required_creds = {"ak-abc": "as-xyz"}
        try:
            res = _run(["profile", "list"])
            assert "Using test-username workspace based on environment variables" in res.stdout
        finally:
            if orig_env_token_id:
                os.environ["MODAL_TOKEN_ID"] = orig_env_token_id
            else:
                del os.environ["MODAL_TOKEN_ID"]
            if orig_env_token_secret:
                os.environ["MODAL_TOKEN_SECRET"] = orig_env_token_secret
            else:
                del os.environ["MODAL_TOKEN_SECRET"]


def test_config_show(servicer, server_url_env, modal_config):
    config = """
    [test-profile]
    token_id = "ak-abc"
    token_secret = "as-xyz"
    active = true
    """
    with modal_config(config):
        res = _run(["config", "show"])
        assert "'token_id': 'ak-abc'" in res.stdout
        assert "'token_secret': '***'" in res.stdout


def test_app_list(servicer, mock_dir, set_env_client):
    res = _run(["app", "list"])
    assert "my_app_foo" not in res.stdout

    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        _run(["deploy", "myapp.py", "--name", "my_app_foo"])

    res = _run(["app", "list"])
    assert "my_app_foo" in res.stdout

    res = _run(["app", "list", "--json"])
    assert json.loads(res.stdout)

    _run(["volume", "create", "my-vol"])
    res = _run(["app", "list"])
    assert "my-vol" not in res.stdout


def test_app_history(servicer, mock_dir, set_env_client):
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        _run(["deploy", "myapp.py", "--name", "my_app_foo"])

    app_id = servicer.deployed_apps.get(("main", "my_app_foo"))

    servicer.app_deployment_history[app_id][-1]["commit_info"] = api_pb2.CommitInfo(
        vcs="git", branch="main", commit_hash="abc123"
    )

    # app should be deployed once it exists
    res = _run(["app", "history", "my_app_foo"])
    assert "v1" in res.stdout, res.stdout

    res = _run(["app", "history", "my_app_foo", "--json"])
    assert json.loads(res.stdout)

    # re-deploying an app should result in a new row in the history table
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        _run(["deploy", "myapp.py", "--name", "my_app_foo"])

    servicer.app_deployment_history[app_id][-1]["commit_info"] = api_pb2.CommitInfo(
        vcs="git", branch="main", commit_hash="def456", dirty=True
    )

    res = _run(["app", "history", "my_app_foo"])
    assert "v1" in res.stdout
    assert "v2" in res.stdout, f"{res.stdout=}"
    assert "abc123" in res.stdout
    assert "def456*" in res.stdout

    # can't fetch history for stopped apps
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        _run(["app", "stop", "my_app_foo"])

    res = _run(["app", "history", "my_app_foo", "--json"], expected_exit_code=1)


def test_app_rollback(servicer, mock_dir, set_env_client):
    with mock_dir({"myapp.py": dummy_app_file, "other_module.py": dummy_other_module_file}):
        # Deploy multiple times
        for _ in range(4):
            _run(["deploy", "myapp.py", "--name", "my_app"])
    _run(["app", "rollback", "my_app"])
    app_id = servicer.deployed_apps.get(("main", "my_app"))
    assert servicer.app_deployment_history[app_id][-1]["rollback_version"] == 3

    _run(["app", "rollback", "my_app", "v2"])
    app_id = servicer.deployed_apps.get(("main", "my_app"))
    assert servicer.app_deployment_history[app_id][-1]["rollback_version"] == 2

    _run(["app", "rollback", "my_app", "2"], expected_exit_code=2)


def test_dict_create_list_delete(servicer, server_url_env, set_env_client):
    _run(["dict", "create", "foo-dict"])
    _run(["dict", "create", "bar-dict"])
    res = _run(["dict", "list"])
    assert "foo-dict" in res.stdout
    assert "bar-dict" in res.stdout

    _run(["dict", "delete", "bar-dict", "--yes"])
    res = _run(["dict", "list"])
    assert "foo-dict" in res.stdout
    assert "bar-dict" not in res.stdout


def test_dict_show_get_clear(servicer, server_url_env, set_env_client):
    # Kind of hacky to be modifying the attributes on the servicer like this
    key = ("baz-dict", os.environ.get("MODAL_ENVIRONMENT", "main"))
    dict_id = "di-abc123"
    servicer.deployed_dicts[key] = dict_id
    servicer.dicts[dict_id] = {dumps("a"): dumps(123), dumps("b"): dumps("blah")}

    res = _run(["dict", "items", "baz-dict"])
    assert re.search(r" Key .+ Value", res.stdout)
    assert re.search(r" a .+ 123 ", res.stdout)
    assert re.search(r" b .+ blah ", res.stdout)

    res = _run(["dict", "items", "baz-dict", "1"])
    assert re.search(r"\.\.\. .+ \.\.\.", res.stdout)
    assert "blah" not in res.stdout

    res = _run(["dict", "items", "baz-dict", "2"])
    assert "..." not in res.stdout

    res = _run(["dict", "items", "baz-dict", "--json"])
    assert '"Key": "a"' in res.stdout
    assert '"Value": 123' in res.stdout
    assert "..." not in res.stdout

    assert _run(["dict", "get", "baz-dict", "a"]).stdout == "123\n"
    assert _run(["dict", "get", "baz-dict", "b"]).stdout == "blah\n"

    res = _run(["dict", "clear", "baz-dict", "--yes"])
    assert servicer.dicts[dict_id] == {}


def test_queue_create_list_delete(servicer, server_url_env, set_env_client):
    _run(["queue", "create", "foo-queue"])
    _run(["queue", "create", "bar-queue"])
    res = _run(["queue", "list"])
    assert "foo-queue" in res.stdout
    assert "bar-queue" in res.stdout

    _run(["queue", "delete", "bar-queue", "--yes"])

    res = _run(["queue", "list"])
    assert "foo-queue" in res.stdout
    assert "bar-queue" not in res.stdout


def test_queue_peek_len_clear(servicer, server_url_env, set_env_client):
    # Kind of hacky to be modifying the attributes on the servicer like this
    name = "queue-who"
    key = (name, os.environ.get("MODAL_ENVIRONMENT", "main"))
    queue_id = "qu-abc123"
    servicer.deployed_queues[key] = queue_id
    servicer.queue = {b"": [dumps("a"), dumps("b"), dumps("c")], b"alt": [dumps("x"), dumps("y")]}

    assert _run(["queue", "peek", name]).stdout == "a\n"
    assert _run(["queue", "peek", name, "-p", "alt"]).stdout == "x\n"
    assert _run(["queue", "peek", name, "3"]).stdout == "a\nb\nc\n"
    assert _run(["queue", "peek", name, "3", "--partition", "alt"]).stdout == "x\ny\n"

    assert _run(["queue", "len", name]).stdout == "3\n"
    assert _run(["queue", "len", name, "--partition", "alt"]).stdout == "2\n"
    assert _run(["queue", "len", name, "--total"]).stdout == "5\n"

    _run(["queue", "clear", name, "--yes"])
    assert _run(["queue", "len", name]).stdout == "0\n"
    assert _run(["queue", "peek", name, "--partition", "alt"]).stdout == "x\n"

    _run(["queue", "clear", name, "--all", "--yes"])
    assert _run(["queue", "len", name, "--total"]).stdout == "0\n"
    assert _run(["queue", "peek", name, "--partition", "alt"]).stdout == ""


@pytest.mark.parametrize("name", [".main", "_main", "'-main'", "main/main", "main:main"])
def test_create_environment_name_invalid(servicer, set_env_client, name):
    assert isinstance(
        _run(
            ["environment", "create", name],
            1,
        ).exception,
        InvalidError,
    )


@pytest.mark.parametrize("name", ["main", "main_-123."])
def test_create_environment_name_valid(servicer, set_env_client, name):
    assert (
        "Environment created"
        in _run(
            ["environment", "create", name],
            0,
        ).stdout
    )


@pytest.mark.parametrize(("name", "set_name"), (("main", "main/main"), ("main", "'-main'")))
def test_update_environment_name_invalid(servicer, set_env_client, name, set_name):
    assert isinstance(
        _run(
            ["environment", "update", name, "--set-name", set_name],
            1,
        ).exception,
        InvalidError,
    )


@pytest.mark.parametrize(("name", "set_name"), (("main", "main_-123."), ("main:main", "main2")))
def test_update_environment_name_valid(servicer, set_env_client, name, set_name):
    assert (
        "Environment updated"
        in _run(
            ["environment", "update", name, "--set-name", set_name],
            0,
        ).stdout
    )


def test_call_update_environment_suffix(servicer, set_env_client):
    _run(["environment", "update", "main", "--set-web-suffix", "_"])


def _run_subprocess(cli_cmd: list[str]) -> helpers.PopenWithCtrlC:
    p = helpers.PopenWithCtrlC(
        [sys.executable, "-m", "modal"] + cli_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf8"
    )
    return p


@pytest.mark.timeout(10)
def test_keyboard_interrupt_during_app_load(servicer, server_url_env, token_env, supports_dir):
    ctx: InterceptionContext
    creating_function = threading.Event()

    async def stalling_function_create(servicer, req):
        creating_function.set()
        await asyncio.sleep(10)

    with servicer.intercept() as ctx:
        ctx.set_responder("FunctionCreate", stalling_function_create)

        p = _run_subprocess(["run", f"{supports_dir / 'hello.py'}::hello"])
        creating_function.wait()
        p.send_ctrl_c()
        out, err = p.communicate(timeout=5)
        print(out)
        assert "Traceback" not in err
        assert "Aborting app initialization..." in out


@pytest.mark.timeout(10)
def test_keyboard_interrupt_during_app_run(servicer, server_url_env, token_env, supports_dir):
    ctx: InterceptionContext
    waiting_for_output = threading.Event()

    async def stalling_function_get_output(servicer, req):
        waiting_for_output.set()
        await asyncio.sleep(10)

    with servicer.intercept() as ctx:
        ctx.set_responder("FunctionGetOutputs", stalling_function_get_output)

        p = _run_subprocess(["run", f"{supports_dir / 'hello.py'}::hello"])
        waiting_for_output.wait()
        p.send_ctrl_c()
        out, err = p.communicate(timeout=5)
        assert "App aborted. View run at https://modaltest.com/apps/ap-123" in out
        assert "Traceback" not in err


@pytest.mark.timeout(10)
def test_keyboard_interrupt_during_app_run_detach(servicer, server_url_env, token_env, supports_dir):
    ctx: InterceptionContext
    waiting_for_output = threading.Event()

    async def stalling_function_get_output(servicer, req):
        waiting_for_output.set()
        await asyncio.sleep(10)

    with servicer.intercept() as ctx:
        ctx.set_responder("FunctionGetOutputs", stalling_function_get_output)

        p = _run_subprocess(["run", "--detach", f"{supports_dir / 'hello.py'}::hello"])
        waiting_for_output.wait()
        p.send_ctrl_c()
        out, err = p.communicate(timeout=5)
        print(out)
        assert "Shutting down Modal client." in out
        assert "The detached app keeps running. You can track its progress at:" in out
        assert "Traceback" not in err


@pytest.fixture
def app(client):
    app = App()
    with app.run(client=client):
        yield app


@skip_windows("modal shell is not supported on Windows.")
def test_container_exec(servicer, set_env_client, mock_shell_pty, app):
    sb = Sandbox.create("bash", "-c", "sleep 10000", app=app)

    fake_stdin, captured_out = mock_shell_pty

    fake_stdin.clear()
    fake_stdin.extend([b'echo "Hello World"\n', b"exit\n"])

    shell_prompt = servicer.shell_prompt

    _run(["container", "exec", "--pty", sb.object_id, "/bin/bash"])
    assert captured_out == [(1, shell_prompt), (1, b"Hello World\n")]
    captured_out.clear()

    sb.terminate()


def test_can_run_all_listed_functions_with_includes(supports_on_path, monkeypatch, set_env_client):
    monkeypatch.setenv("TERM", "dumb")  # prevents looking at ansi escape sequences

    res = _run(["run", "-m", "multifile_project.main"], expected_exit_code=1)
    print("err", res.stderr)
    # there are no runnables directly in the target module, so references need to go via the app
    func_listing = res.stderr.split("functions and local entrypoints:")[1]

    listed_runnables = set(re.findall(r"\b[\w.]+\b", func_listing))

    expected_runnables = {
        "app.a_func",
        "app.b_func",
        "app.c_func",
        "app.main_function",
        "main_function",
        "Cls.method_on_other_app_class",
        "other_app.Cls.method_on_other_app_class",
    }
    assert listed_runnables == expected_runnables

    for runnable in expected_runnables:
        assert runnable in res.stderr
        _run(["run", "-m", f"multifile_project.main::{runnable}"], expected_exit_code=0)


def test_modal_launch_vscode(monkeypatch, set_env_client, servicer):
    mock_open = MagicMock()
    monkeypatch.setattr("webbrowser.open", mock_open)
    with servicer.intercept() as ctx:
        ctx.add_response("QueueGet", api_pb2.QueueGetResponse(values=[serialize(("http://dummy", "tok"))]))
        ctx.add_response("QueueGet", api_pb2.QueueGetResponse(values=[serialize("done")]))
        _run(["launch", "vscode"])

    assert mock_open.call_count == 1


def test_run_file_with_global_lookups(servicer, set_env_client, supports_dir):
    # having module-global Function/Cls objects from .from_name constructors shouldn't
    # cause issues, and they shouldn't be runnable via CLI (for now)
    with servicer.intercept() as ctx:
        _run(["run", str(supports_dir / "app_run_tests" / "file_with_global_lookups.py")])

    (req,) = ctx.get_requests("FunctionCreate")
    assert req.function.function_name == "local_f"
    assert len(ctx.get_requests("FunctionMap")) == 1
    assert len(ctx.get_requests("FunctionGet")) == 0


def test_run_auto_infer_prefer_target_module(servicer, supports_dir, set_env_client, monkeypatch):
    monkeypatch.syspath_prepend(supports_dir / "app_run_tests")
    res = _run(["run", "-m", "multifile.util"])
    assert "ran util\nmain func" in res.stdout


@pytest.mark.parametrize("func", ["va_entrypoint", "va_function", "VaClass.va_method"])
def test_cli_run_variadic_args(servicer, set_env_client, test_dir, func):
    app_file = test_dir / "supports" / "app_run_tests" / "variadic_args.py"

    @servicer.function_body
    def print_args(*args):
        print(f"args: {args}")

    res = _run(["run", f"{app_file.as_posix()}::{func}"])
    assert "args: ()" in res.stdout

    res = _run(["run", f"{app_file.as_posix()}::{func}", "abc", "--foo=123", "--bar=456"])
    assert "args: ('abc', '--foo=123', '--bar=456')" in res.stdout

    _run(["run", f"{app_file.as_posix()}::{func}_invalid", "--foo=123"], expected_exit_code=1)


def test_server_warnings(servicer, set_env_client, supports_dir):
    res = _run(["run", f"{supports_dir / 'app_run_tests' / 'uses_experimental_options.py'}::gets_warning"])
    assert "You have been warned!" in res.stdout


def test_run_with_options(servicer, set_env_client, supports_dir):
    app_file = supports_dir / "app_run_tests" / "uses_with_options.py"
    _run(["run", f"{app_file.as_posix()}::C_with_gpu.f"])
