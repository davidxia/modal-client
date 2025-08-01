# Copyright Modal Labs 2022

import asyncio
import dataclasses
import gc
import json
import logging
import os
import pathlib
import pickle
import pytest
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Optional
from unittest import mock
from unittest.mock import MagicMock

from grpclib import Status
from grpclib.exceptions import GRPCError

import modal
from modal import Client, Queue, Volume, is_local
from modal._container_entrypoint import UserException, main
from modal._runtime import asgi
from modal._runtime.container_io_manager import (
    ContainerIOManager,
    InputSlots,
    IOContext,
)
from modal._runtime.user_code_imports import FinalizedFunction
from modal._serialization import (
    deserialize,
    deserialize_data_format,
    serialize,
    serialize_data_format,
    serialize_proto_params,
)
from modal._utils import async_utils
from modal._utils.async_utils import synchronize_api
from modal._utils.blob_utils import (
    MAX_OBJECT_SIZE_BYTES,
    blob_download as _blob_download,
    blob_upload as _blob_upload,
)
from modal.app import _App
from modal.exception import InvalidError
from modal.partial_function import enter, method
from modal_proto import api_pb2

from .helpers import deploy_app_externally
from .supports.skip import skip_github_non_linux

EXTRA_TOLERANCE_DELAY = 2.0 if sys.platform == "linux" else 5.0
FUNCTION_CALL_ID = "fc-123"
SLEEP_DELAY = 0.1

blob_upload = synchronize_api(_blob_upload)
blob_download = synchronize_api(_blob_download)

DEFAULT_APP_LAYOUT_SENTINEL: Any = object()


def _get_inputs(
    args: tuple[tuple, dict] = ((42,), {}),
    n: int = 1,
    kill_switch=True,
    method_name: Optional[str] = None,
    upload_to_blob: bool = False,
    client: Optional[Client] = None,
) -> list[api_pb2.FunctionGetInputsResponse]:
    if upload_to_blob:
        args_blob_id = blob_upload(serialize(args), client.stub)
        input_pb = api_pb2.FunctionInput(
            args_blob_id=args_blob_id, data_format=api_pb2.DATA_FORMAT_PICKLE, method_name=method_name or ""
        )
    else:
        input_pb = api_pb2.FunctionInput(
            args=serialize(args), data_format=api_pb2.DATA_FORMAT_PICKLE, method_name=method_name or ""
        )
    inputs = [
        *(
            api_pb2.FunctionGetInputsItem(input_id=f"in-xyz{i}", function_call_id="fc-123", input=input_pb)
            for i in range(n)
        ),
        *([api_pb2.FunctionGetInputsItem(kill_switch=True)] if kill_switch else []),
    ]
    return [api_pb2.FunctionGetInputsResponse(inputs=[x]) for x in inputs]


def _get_inputs_batched(
    args_list: list[tuple[tuple, dict]],
    batch_max_size: int,
    kill_switch=True,
    method_name: Optional[str] = None,
):
    input_pbs = [
        api_pb2.FunctionInput(
            args=serialize(args), data_format=api_pb2.DATA_FORMAT_PICKLE, method_name=method_name or ""
        )
        for args in args_list
    ]
    inputs = [
        *(
            api_pb2.FunctionGetInputsItem(input_id=f"in-xyz{i}", function_call_id="fc-123", input=input_pb)
            for i, input_pb in enumerate(input_pbs)
        ),
        *([api_pb2.FunctionGetInputsItem(kill_switch=True)] if kill_switch else []),
    ]
    response_list = []
    current_batch: list[Any] = []
    while inputs:
        input = inputs.pop(0)
        if input.kill_switch:
            if len(current_batch) > 0:
                response_list.append(api_pb2.FunctionGetInputsResponse(inputs=current_batch))
            current_batch = [input]
            break
        if len(current_batch) > batch_max_size:
            response_list.append(api_pb2.FunctionGetInputsResponse(inputs=current_batch))
            current_batch = []
        current_batch.append(input)

    if len(current_batch) > 0:
        response_list.append(api_pb2.FunctionGetInputsResponse(inputs=current_batch))
    return response_list


def _get_multi_inputs(args: list[tuple[str, tuple, dict]] = []) -> list[api_pb2.FunctionGetInputsResponse]:
    responses = []
    for input_n, (method_name, input_args, input_kwargs) in enumerate(args):
        resp = api_pb2.FunctionGetInputsResponse(
            inputs=[
                api_pb2.FunctionGetInputsItem(
                    function_call_id="fc-123",
                    input_id=f"in-{input_n:03}",
                    input=api_pb2.FunctionInput(args=serialize((input_args, input_kwargs)), method_name=method_name),
                )
            ]
        )
        responses.append(resp)

    return responses + [api_pb2.FunctionGetInputsResponse(inputs=[api_pb2.FunctionGetInputsItem(kill_switch=True)])]


@dataclasses.dataclass
class ContainerResult:
    client: Client
    items: list[api_pb2.FunctionPutOutputsItem]
    data_chunks: list[api_pb2.DataChunk]
    task_result: api_pb2.GenericResult


def _get_multi_inputs_with_methods(args: list[tuple[str, tuple, dict]] = []) -> list[api_pb2.FunctionGetInputsResponse]:
    responses = []
    for input_n, (method_name, *input_args) in enumerate(args):
        resp = api_pb2.FunctionGetInputsResponse(
            inputs=[
                api_pb2.FunctionGetInputsItem(
                    input_id=f"in-{input_n:03}",
                    input=api_pb2.FunctionInput(args=serialize(input_args), method_name=method_name),
                )
            ]
        )
        responses.append(resp)

    return responses + [api_pb2.FunctionGetInputsResponse(inputs=[api_pb2.FunctionGetInputsItem(kill_switch=True)])]


def _container_args(
    module_name,
    function_name,
    function_type=api_pb2.Function.FUNCTION_TYPE_FUNCTION,
    webhook_type=api_pb2.WEBHOOK_TYPE_UNSPECIFIED,
    definition_type=api_pb2.Function.DEFINITION_TYPE_FILE,
    app_name: str = "",
    is_builder_function: bool = False,
    max_concurrent_inputs: Optional[int] = None,
    target_concurrent_inputs: Optional[int] = None,
    batch_max_size: Optional[int] = None,
    batch_wait_ms: Optional[int] = None,
    serialized_params: Optional[bytes] = None,
    is_checkpointing_function: bool = False,
    deps: list[str] = ["im-1"],
    volume_mounts: Optional[list[api_pb2.VolumeMount]] = None,
    is_auto_snapshot: bool = False,
    max_inputs: Optional[int] = None,
    is_class: bool = False,
    class_parameter_info=api_pb2.ClassParameterInfo(
        format=api_pb2.ClassParameterInfo.PARAM_SERIALIZATION_FORMAT_UNSPECIFIED, schema=[]
    ),
    app_id: str = "ap-1",
    app_layout: api_pb2.AppLayout = DEFAULT_APP_LAYOUT_SENTINEL,
    web_server_port: Optional[int] = None,
    web_server_startup_timeout: Optional[float] = None,
    function_serialized: Optional[bytes] = None,
    class_serialized: Optional[bytes] = None,
):
    if app_layout is DEFAULT_APP_LAYOUT_SENTINEL:
        app_layout = api_pb2.AppLayout(
            objects=[
                api_pb2.Object(object_id="im-1"),
                api_pb2.Object(
                    object_id="fu-123",
                    function_handle_metadata=api_pb2.FunctionHandleMetadata(
                        function_name=function_name,
                    ),
                ),
            ],
            function_ids={function_name: "fu-123"},
        )
        if is_class:
            app_layout.objects.append(
                api_pb2.Object(object_id="cs-123", class_handle_metadata=api_pb2.ClassHandleMetadata())
            )
            app_layout.class_ids[function_name.removesuffix(".*")] = "cs-123"

    if webhook_type:
        webhook_config = api_pb2.WebhookConfig(
            type=webhook_type,
            method="GET",
            async_mode=api_pb2.WEBHOOK_ASYNC_MODE_AUTO,
            web_server_port=web_server_port,
            web_server_startup_timeout=web_server_startup_timeout,
        )
    else:
        webhook_config = None
    function_def = api_pb2.Function(
        module_name=module_name,
        function_name=function_name,
        function_type=function_type,
        volume_mounts=volume_mounts,
        webhook_config=webhook_config,
        definition_type=definition_type,
        app_name=app_name or "",
        is_builder_function=is_builder_function,
        is_auto_snapshot=is_auto_snapshot,
        target_concurrent_inputs=target_concurrent_inputs,
        max_concurrent_inputs=max_concurrent_inputs,
        batch_max_size=batch_max_size,
        batch_linger_ms=batch_wait_ms,
        is_checkpointing_function=is_checkpointing_function,
        object_dependencies=[api_pb2.ObjectDependency(object_id=object_id) for object_id in deps],
        max_inputs=max_inputs,
        is_class=is_class,
        class_parameter_info=class_parameter_info,
        function_serialized=function_serialized,
        class_serialized=class_serialized,
    )

    return api_pb2.ContainerArguments(
        task_id="ta-123",
        function_id="fu-123",
        app_id=app_id,
        function_def=function_def,
        serialized_params=serialized_params,
        checkpoint_id=f"ch-{uuid.uuid4()}",
        app_layout=app_layout,
    )


def _flatten_outputs(outputs) -> list[api_pb2.FunctionPutOutputsItem]:
    items: list[api_pb2.FunctionPutOutputsItem] = []
    for req in outputs:
        items += list(req.outputs)
    return items


def _run_container(
    servicer,
    module_name,
    function_name,
    fail_get_inputs=False,
    inputs=None,
    function_type=api_pb2.Function.FUNCTION_TYPE_FUNCTION,
    webhook_type=api_pb2.WEBHOOK_TYPE_UNSPECIFIED,
    definition_type=api_pb2.Function.DEFINITION_TYPE_FILE,
    app_name: str = "",
    is_builder_function: bool = False,
    max_concurrent_inputs: Optional[int] = None,
    target_concurrent_inputs: Optional[int] = None,
    batch_max_size: int = 0,
    batch_wait_ms: int = 0,
    serialized_params: Optional[bytes] = None,
    is_checkpointing_function: bool = False,
    deps: list[str] = ["im-1"],
    volume_mounts: Optional[list[api_pb2.VolumeMount]] = None,
    is_auto_snapshot: bool = False,
    max_inputs: Optional[int] = None,
    is_class: bool = False,
    class_parameter_info=api_pb2.ClassParameterInfo(
        format=api_pb2.ClassParameterInfo.PARAM_SERIALIZATION_FORMAT_UNSPECIFIED, schema=[]
    ),
    app_layout=DEFAULT_APP_LAYOUT_SENTINEL,
    web_server_port: Optional[int] = None,
    web_server_startup_timeout: Optional[float] = None,
    function_serialized: Optional[bytes] = None,
    class_serialized: Optional[bytes] = None,
) -> ContainerResult:
    container_args = _container_args(
        module_name=module_name,
        function_name=function_name,
        function_type=function_type,
        webhook_type=webhook_type,
        definition_type=definition_type,
        app_name=app_name,
        is_builder_function=is_builder_function,
        max_concurrent_inputs=max_concurrent_inputs,
        target_concurrent_inputs=target_concurrent_inputs,
        batch_max_size=batch_max_size,
        batch_wait_ms=batch_wait_ms,
        serialized_params=serialized_params,
        is_checkpointing_function=is_checkpointing_function,
        deps=deps,
        volume_mounts=volume_mounts,
        is_auto_snapshot=is_auto_snapshot,
        max_inputs=max_inputs,
        is_class=is_class,
        class_parameter_info=class_parameter_info,
        app_layout=app_layout,
        web_server_port=web_server_port,
        web_server_startup_timeout=web_server_startup_timeout,
        function_serialized=function_serialized,
        class_serialized=class_serialized,
    )
    with Client(servicer.container_addr, api_pb2.CLIENT_TYPE_CONTAINER, None) as client:
        if inputs is None:
            servicer.container_inputs = _get_inputs()
        else:
            servicer.container_inputs = inputs
        first_function_call_id = servicer.container_inputs[0].inputs[0].function_call_id
        servicer.fail_get_inputs = fail_get_inputs

        if module_name in sys.modules:
            # Drop the module from sys.modules since some function code relies on the
            # assumption that that the app is created before the user code is imported.
            # This is really only an issue for tests.
            sys.modules.pop(module_name)

        env = os.environ.copy()
        temp_restore_file_path = tempfile.NamedTemporaryFile()
        if is_checkpointing_function:
            # State file is written to allow for a restore to happen.
            tmp_file_name = temp_restore_file_path.name
            with pathlib.Path(tmp_file_name).open("w") as target:
                json.dump({}, target)
            env["MODAL_RESTORE_STATE_PATH"] = tmp_file_name

            # Override server URL to reproduce restore behavior.
            env["MODAL_ENABLE_SNAP_RESTORE"] = "1"

        # These env vars are always present in containers
        env["MODAL_SERVER_URL"] = servicer.container_addr
        env["MODAL_TASK_ID"] = "ta-123"
        env["MODAL_IS_REMOTE"] = "1"

        # reset _App tracking state between runs
        _App._all_apps.clear()

        try:
            with mock.patch.dict(os.environ, env):
                main(container_args, client)
        except UserException:
            # Handle it gracefully
            pass
        finally:
            temp_restore_file_path.close()

        # Flatten outputs
        items = _flatten_outputs(servicer.container_outputs)

        data_chunks = servicer.get_data_chunks(first_function_call_id)

        return ContainerResult(client, items, data_chunks, servicer.task_result)


def _unwrap_scalar(ret: ContainerResult):
    assert len(ret.items) == 1
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    return deserialize(ret.items[0].result.data, ret.client)


def _unwrap_blob_scalar(ret: ContainerResult, client: Client):
    assert len(ret.items) == 1
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    data = blob_download(ret.items[0].result.data_blob_id, client.stub)
    return deserialize(data, ret.client)


def _unwrap_batch_scalar(ret: ContainerResult, batch_size):
    assert len(ret.items) == batch_size
    outputs = []
    for item in ret.items:
        assert item.result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
        outputs.append(deserialize(item.result.data, ret.client))
    assert len(outputs) == batch_size
    return outputs


def _unwrap_exception(ret: ContainerResult):
    assert len(ret.items) == 1
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "Traceback" in ret.items[0].result.traceback
    return deserialize(ret.items[0].result.data, ret.client)


def _unwrap_batch_exception(ret: ContainerResult, batch_size):
    assert len(ret.items) == batch_size
    outputs = []
    for item in ret.items:
        assert item.result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
        assert "Traceback" in item.result.traceback
        outputs.append(item.result.exception)
    assert len(outputs) == batch_size
    return outputs


def _unwrap_generator(ret: ContainerResult) -> tuple[list[Any], Optional[Exception]]:
    assert len(ret.items) == 1
    item = ret.items[0]

    values: list[Any] = [deserialize_data_format(chunk.data, chunk.data_format, None) for chunk in ret.data_chunks]

    if item.result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE:
        exc = deserialize(item.result.data, ret.client)
        return values, exc
    elif item.result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS:
        assert item.data_format == api_pb2.DATA_FORMAT_GENERATOR_DONE
        done: api_pb2.GeneratorDone = deserialize_data_format(item.result.data, item.data_format, None)
        assert done.items_total == len(values)
        return values, None
    else:
        raise RuntimeError("unknown result type")


def _unwrap_asgi(ret: ContainerResult):
    values, exc = _unwrap_generator(ret)
    assert exc is None, "web endpoint raised exception"
    return values


def _get_web_inputs(path="/", method_name=""):
    scope = {
        "method": "GET",
        "type": "http",
        "path": path,
        "headers": {},
        "query_string": b"arg=space",
        "http_version": "2",
    }
    return _get_inputs(((scope,), {}), method_name=method_name)


@skip_github_non_linux
def test_success(servicer):
    t0 = time.time()
    ret = _run_container(servicer, "test.supports.functions", "square")
    assert 0 <= time.time() - t0 < EXTRA_TOLERANCE_DELAY
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_generator_success(servicer, event_loop):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "gen_n",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
    )

    items, exc = _unwrap_generator(ret)
    assert items == [i**2 for i in range(42)]
    assert exc is None


@skip_github_non_linux
def test_generator_failure(servicer, capsys):
    inputs = _get_inputs(((10, 5), {}))
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "gen_n_fail_on_m",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
        inputs=inputs,
    )
    items, exc = _unwrap_generator(ret)
    assert items == [i**2 for i in range(5)]
    assert isinstance(exc, Exception)
    assert exc.args == ("bad",)
    assert 'raise Exception("bad")' in capsys.readouterr().err


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_generator_failure_async_cleanup(servicer, tmp_path, client):
    with _run_container_process(
        servicer,
        tmp_path,
        "test.supports.functions",
        "async_gen_n_fail_on_m",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
        inputs=[("", (10, 5), {})],
    ) as p:
        stdout, stderr = p.communicate()
        chunks = servicer.get_data_chunks("fc-123")  # hard coded ugly function call id...
        assert len(chunks) == 5
        container_stderr = stderr.decode("utf8")
        print(container_stderr)
        results = ContainerResult(
            client,
            items=_flatten_outputs(servicer.container_outputs),
            data_chunks=chunks,
            task_result=servicer.task_result,
        )
        items, exc = _unwrap_generator(results)
        assert items == [i**2 for i in range(5)]
        assert isinstance(exc, Exception)
        assert exc.args == ("bad",)
        # There shouldn't be additional garbage in the container output due to resource leaks, e.g.
        # "Task was destroyed but it is pending!"
        assert 'raise Exception("bad")' in container_stderr
        assert container_stderr.strip().endswith("Exception: bad")
        assert "Task was destroyed but it is pending" not in container_stderr


@skip_github_non_linux
def test_async(servicer):
    t0 = time.time()
    ret = _run_container(servicer, "test.supports.functions", "square_async")
    assert SLEEP_DELAY <= time.time() - t0 < SLEEP_DELAY + EXTRA_TOLERANCE_DELAY
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_failure(servicer, capsys):
    ret = _run_container(servicer, "test.supports.functions", "raises")
    exc = _unwrap_exception(ret)
    assert isinstance(exc, Exception)
    assert repr(exc) == "Exception('Failure!')"
    assert 'raise Exception("Failure!")' in capsys.readouterr().err  # traceback


@skip_github_non_linux
def test_raises_base_exception(servicer, capsys):
    ret = _run_container(servicer, "test.supports.functions", "raises_sysexit")
    exc = _unwrap_exception(ret)
    assert isinstance(exc, SystemExit)
    assert repr(exc) == "SystemExit(1)"
    assert "raise SystemExit(1)" in capsys.readouterr().err  # traceback


@skip_github_non_linux
def test_keyboardinterrupt(servicer):
    with pytest.raises(KeyboardInterrupt):
        _run_container(servicer, "test.supports.functions", "raises_keyboardinterrupt")


@skip_github_non_linux
def test_rate_limited(servicer, event_loop):
    t0 = time.time()
    servicer.rate_limit_sleep_duration = 0.25
    ret = _run_container(servicer, "test.supports.functions", "square")
    assert 0.25 <= time.time() - t0 < 0.25 + EXTRA_TOLERANCE_DELAY
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_grpc_failure(servicer, event_loop):
    # An error in "Modal code" should cause the entire container to fail
    with pytest.raises(GRPCError):
        _run_container(
            servicer,
            "test.supports.functions",
            "square",
            fail_get_inputs=True,
        )

    # assert servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    # assert "GRPCError" in servicer.task_result.exception


@skip_github_non_linux
def test_run_from_global_scope(servicer, capsys):
    _run_container(servicer, "test.supports.missing_main_conditional", "square")
    output = capsys.readouterr()
    assert "Can not run an app in global scope within a container" in output.err
    assert servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    exc = deserialize(servicer.task_result.data, None)
    assert isinstance(exc, InvalidError)


@skip_github_non_linux
def test_run_from_within_function(servicer, capsys):
    with servicer.intercept() as ctx:
        _run_container(servicer, "test.supports.modal_run_from_function", "run_other_app", inputs=_get_inputs(((), {})))

    inner_app_create: api_pb2.AppCreateRequest
    (inner_app_create,) = ctx.get_requests("AppCreate")
    assert inner_app_create.description == "app2"
    inner_function_call: api_pb2.FunctionMapRequest
    (inner_function_call,) = ctx.get_requests("FunctionMap")
    assert servicer.app_functions[inner_function_call.function_id].function_name == "foo"


@skip_github_non_linux
def test_startup_failure(servicer, capsys):
    _run_container(servicer, "test.supports.startup_failure", "f")

    assert servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE

    exc = deserialize(servicer.task_result.data, None)
    assert isinstance(exc, ImportError)
    assert "ModuleNotFoundError: No module named 'nonexistent_package'" in capsys.readouterr().err


@skip_github_non_linux
def test_from_local_python_packages_inside_container(servicer):
    """`from_local_python_packages` shouldn't actually collect modules inside the container, because it's possible
    that there are modules that were present locally for the user that didn't get mounted into
    all the containers."""
    ret = _run_container(servicer, "test.supports.package_mount", "dummy")
    assert _unwrap_scalar(ret) == 0


# needs to be synchronized so the asyncio.Queue gets used from the same event loop as the servicer
@async_utils.synchronize_api
async def _put_web_body(servicer, body: bytes):
    asgi = {"type": "http.request", "body": body, "more_body": False}
    data = serialize_data_format(asgi, api_pb2.DATA_FORMAT_ASGI)

    q = servicer.fc_data_in.setdefault("fc-123", asyncio.Queue())
    q.put_nowait(api_pb2.DataChunk(data_format=api_pb2.DATA_FORMAT_ASGI, data=data, index=1))


@skip_github_non_linux
def test_webhook(servicer):
    inputs = _get_web_inputs()
    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "webhook",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_FUNCTION,
    )
    items = _unwrap_asgi(ret)

    # There should be one message for the header, one for the body, one for the EOF
    first_message, second_message = items  # _unwrap_asgi ignores the eof

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == {"hello": "space"}


@skip_github_non_linux
def test_webhook_setup_failure(servicer):
    inputs = _get_web_inputs()
    _put_web_body(servicer, b"")
    with servicer.intercept() as ctx:
        ret = _run_container(
            servicer,
            "test.supports.functions",
            "error_in_asgi_setup",
            inputs=inputs,
            webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
        )

    task_result_request: api_pb2.TaskResultRequest
    (task_result_request,) = ctx.get_requests("TaskResult")
    assert task_result_request.result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "Error while setting up asgi app" in ret.task_result.exception
    assert ret.items == []
    # TODO: We should send some kind of 500 error back to modal-http here when the container can't start up


@skip_github_non_linux
def test_serialized_function(servicer):
    def triple(x):
        return 3 * x

    ret = _run_container(
        servicer,
        "",  # no module name
        "f",
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        function_serialized=serialize(triple),
    )
    assert _unwrap_scalar(ret) == 3 * 42


@skip_github_non_linux
def test_serialized_class_with_parameters(servicer):
    class SerializedClassWithParams:
        p: int = modal.parameter()

        @modal.method()
        def method(self):
            return "hello"

        # TODO: expand this test to check that self.other_method.remote() can be called
        #  this would require feeding the servicer with more information about the function
        #  since it would re-bind parameters to the class service function etc.

    app = modal.App()
    app.cls(serialized=True)(SerializedClassWithParams)  # gets rid of warning

    ret = _run_container(
        servicer,
        "",
        "SerializedClassWithParams.*",
        is_class=True,
        inputs=_get_inputs(((), {}), method_name="method"),
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        class_parameter_info=api_pb2.ClassParameterInfo(
            format=api_pb2.ClassParameterInfo.PARAM_SERIALIZATION_FORMAT_PROTO,
        ),
        serialized_params=serialize_proto_params({"p": 10}),
        app_layout=api_pb2.AppLayout(
            objects=[
                api_pb2.Object(
                    object_id="fu-123",
                    function_handle_metadata=api_pb2.FunctionHandleMetadata(
                        function_name="SerializedClassWithParams.*",
                        method_handle_metadata={
                            "method": api_pb2.FunctionHandleMetadata(),
                        },
                    ),
                )
            ],
            function_ids={"SerializedClassWithParams.*": "fu-123"},
            class_ids={"SerializedClassWithParams": "cs-123"},
        ),
        class_serialized=serialize(SerializedClassWithParams),
    )
    assert _unwrap_scalar(ret) == "hello"


@skip_github_non_linux
def test_webhook_serialized(servicer):
    inputs = _get_web_inputs()
    _put_web_body(servicer, b"")

    # Store a serialized webhook function on the servicer
    def webhook(arg="world"):
        return f"Hello, {arg}"

    ret = _run_container(
        servicer,
        "foo.bar.baz",
        "f",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_FUNCTION,
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        function_serialized=serialize(webhook),
    )

    _, second_message = _unwrap_asgi(ret)
    assert second_message["body"] == b'"Hello, space"'  # Note: JSON-encoded


@skip_github_non_linux
def test_function_returning_generator(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fun_returning_gen",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
    )
    items, exc = _unwrap_generator(ret)
    assert len(items) == 42


@skip_github_non_linux
def test_asgi(servicer):
    inputs = _get_web_inputs(path="/foo")
    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_app",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    )

    # There should be one message for the header, and one for the body
    first_message, second_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == {"hello": "space"}


@skip_github_non_linux
def test_non_blocking_web_server(servicer, monkeypatch):
    get_ip_address = MagicMock(wraps=asgi.get_ip_address)
    get_ip_address.return_value = "127.0.0.1"
    monkeypatch.setattr(asgi, "get_ip_address", get_ip_address)

    inputs = _get_web_inputs(path="/")
    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "non_blocking_web_server",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_WEB_SERVER,
        web_server_port=8765,
        web_server_startup_timeout=1,
    )
    first_message, second_message, _ = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"Content-Type"] == b"text/html; charset=utf-8"

    assert b"Directory listing" in second_message["body"]


@skip_github_non_linux
def test_asgi_lifespan(servicer):
    inputs = _get_web_inputs(path="/")

    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_app_with_lifespan",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    )

    # There should be one message for the header, and one for the body
    first_message, second_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == "this was set from state"

    from test.supports import functions

    assert ["enter", "foo", "exit"] == functions.lifespan_global_asgi_app_func


@skip_github_non_linux
def test_asgi_lifespan_startup_failure(servicer):
    inputs = _get_web_inputs(path="/")

    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_app_with_lifespan_failing_startup",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    )
    assert ret.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "ASGI lifespan startup failed" in ret.task_result.exception


@skip_github_non_linux
def test_asgi_lifespan_shutdown_failure(servicer):
    inputs = _get_web_inputs(path="/")

    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_app_with_lifespan_failing_shutdown",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    )
    assert ret.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "ASGI lifespan shutdown failed" in ret.task_result.exception


@skip_github_non_linux
def test_cls_web_asgi_with_lifespan(servicer):
    inputs = _get_web_inputs(method_name="my_app1")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_class_multiple_asgi_apps_lifespans.*",
        inputs=inputs,
        is_class=True,
    )

    # There should be one message for the header, and one for the body
    first_message, second_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == "foo1"

    from test.supports import functions

    assert functions.lifespan_global_asgi_app_cls == ["enter1", "enter2", "foo1", "exit1", "exit2", "exit"]


@skip_github_non_linux
@pytest.mark.filterwarnings("error")
def test_app_with_slow_lifespan_wind_down(servicer, caplog):
    inputs = _get_web_inputs()
    with caplog.at_level(logging.WARNING):
        ret = _run_container(
            servicer,
            "test.supports.functions",
            "asgi_app_with_slow_lifespan_wind_down",
            inputs=inputs,
            webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
        )
        asyncio.get_event_loop()
        # There should be one message for the header, and one for the body
        first_message, second_message = _unwrap_asgi(ret)
        # Check the headers
        assert first_message["status"] == 200
        # Check body
        assert json.loads(second_message["body"]) == {"some_result": "foo"}
        gc.collect()  # trigger potential "Task was destroyed but it is pending"

    for m in caplog.messages:
        assert "Task was destroyed" not in m


@skip_github_non_linux
def test_cls_web_asgi_with_lifespan_failure(servicer):
    inputs = _get_web_inputs(method_name="my_app1")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "fastapi_class_lifespan_shutdown_failure.*",
        inputs=inputs,
        is_class=True,
    )

    # There should be one message for the header, and one for the body
    first_message, second_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == "foo"

    from test.supports import functions

    assert ["enter", "foo", "lifecycle exit"] == functions.lifespan_global_asgi_app_cls_fail


@skip_github_non_linux
def test_non_lifespan_asgi(servicer):
    inputs = _get_web_inputs(path="/")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "non_lifespan_asgi",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    )

    # There should be one message for the header, and one for the body
    first_message, second_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"application/json"

    # Check body
    assert json.loads(second_message["body"]) == "foo"


@skip_github_non_linux
def test_wsgi(servicer):
    inputs = _get_web_inputs(path="/")
    _put_web_body(servicer, b"my wsgi body")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "basic_wsgi_app",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_WSGI_APP,
    )

    # There should be one message for headers, one for the body, and one for the end-of-body.
    first_message, second_message, third_message = _unwrap_asgi(ret)

    # Check the headers
    assert first_message["status"] == 200
    headers = dict(first_message["headers"])
    assert headers[b"content-type"] == b"text/plain; charset=utf-8"

    # Check body
    assert second_message["body"] == b"got body: my wsgi body"
    assert second_message.get("more_body", False) is True
    assert third_message["body"] == b""
    assert third_message.get("more_body", False) is False


@skip_github_non_linux
def test_webhook_streaming_sync(servicer):
    inputs = _get_web_inputs()
    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "webhook_streaming",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_FUNCTION,
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
    )
    data = _unwrap_asgi(ret)
    bodies = [d["body"].decode() for d in data if d.get("body")]
    assert bodies == [f"{i}..." for i in range(10)]


@skip_github_non_linux
def test_webhook_streaming_async(servicer):
    inputs = _get_web_inputs()
    _put_web_body(servicer, b"")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "webhook_streaming_async",
        inputs=inputs,
        webhook_type=api_pb2.WEBHOOK_TYPE_FUNCTION,
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
    )

    data = _unwrap_asgi(ret)
    bodies = [d["body"].decode() for d in data if d.get("body")]
    assert bodies == [f"{i}..." for i in range(10)]


@skip_github_non_linux
def test_cls_function(servicer):
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "NonParamCls.*",
        is_class=True,
        inputs=_get_inputs(method_name="f"),
    )
    assert _unwrap_scalar(ret) == 42 * 111


@skip_github_non_linux
def test_lifecycle_enter_sync(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "LifecycleCls.*",
        inputs=_get_inputs(((), {}), method_name="f_sync"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == ["enter_sync", "enter_async", "f_sync", "local"]


@skip_github_non_linux
def test_lifecycle_enter_async(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "LifecycleCls.*",
        inputs=_get_inputs(((), {}), method_name="f_async"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == ["enter_sync", "enter_async", "f_async", "local"]


@skip_github_non_linux
def test_param_cls_function(servicer):
    serialized_params = pickle.dumps(([111], {"y": "foo"}))
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "ParamCls.*",
        serialized_params=serialized_params,
        is_class=True,
        inputs=_get_inputs(method_name="f"),
    )
    assert _unwrap_scalar(ret) == "111 foo 42"


@skip_github_non_linux
def test_param_cls_function_strict_params(servicer):
    serialized_params = modal._serialization.serialize_proto_params({"x": 111, "y": "foo"})
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "ParamCls.*",
        serialized_params=serialized_params,
        is_class=True,
        inputs=_get_inputs(method_name="f"),
        class_parameter_info=api_pb2.ClassParameterInfo(
            format=api_pb2.ClassParameterInfo.PARAM_SERIALIZATION_FORMAT_PROTO,
        ),
    )
    assert _unwrap_scalar(ret) == "111 foo 42"


@skip_github_non_linux
def test_cls_web_endpoint(servicer):
    inputs = _get_web_inputs(method_name="web")
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "NonParamCls.*",
        inputs=inputs,
        is_class=True,
    )

    _, second_message = _unwrap_asgi(ret)
    assert json.loads(second_message["body"]) == {"ret": "space" * 111}


@skip_github_non_linux
def test_cls_web_asgi_construction(servicer):
    app_layout = api_pb2.AppLayout(
        objects=[
            api_pb2.Object(object_id="im-1"),
            # square function:
            api_pb2.Object(object_id="fu-2", function_handle_metadata=api_pb2.FunctionHandleMetadata()),
            # class service function:
            api_pb2.Object(object_id="fu-123", function_handle_metadata=api_pb2.FunctionHandleMetadata()),
            # class itself:
            api_pb2.Object(object_id="cs-123", class_handle_metadata=api_pb2.ClassHandleMetadata()),
        ],
        function_ids={
            "square": "fu-2",  # used to hydrate sibling function
            "NonParamCls.*": "fu-123",
        },
        class_ids={
            "NonParamCls": "cs-123",
        },
    )
    inputs = _get_web_inputs(method_name="asgi_web")
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "NonParamCls.*",
        inputs=inputs,
        is_class=True,
        app_layout=app_layout,
    )

    _, second_message = _unwrap_asgi(ret)
    return_dict = json.loads(second_message["body"])
    assert return_dict == {
        "arg": "space",
        "at_construction": 111,  # @enter should have run when the asgi app constructor is called
        "at_runtime": 111,
        "other_hydrated": True,
    }


@skip_github_non_linux
def test_serialized_cls(servicer):
    class Cls:
        @enter()
        def enter(self):
            self.power = 5

        @method()
        def method(self, x):
            return x**self.power

    app = modal.App()
    app.cls(serialized=True)(Cls)  # prevents warnings about not turning methods into functions
    ret = _run_container(
        servicer,
        "module.doesnt.matter",
        "function.doesnt.matter",
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        is_class=True,
        inputs=_get_inputs(method_name="method"),
        class_serialized=serialize(Cls),
    )
    assert _unwrap_scalar(ret) == 42**5


@skip_github_non_linux
def test_cls_generator(servicer):
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "NonParamCls.*",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
        is_class=True,
        inputs=_get_inputs(method_name="generator"),
    )
    items, exc = _unwrap_generator(ret)
    assert items == [42**3]
    assert exc is None


@skip_github_non_linux
def test_checkpointing_cls_function(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "SnapshottingCls.*",
        inputs=_get_inputs((("D",), {}), method_name="f"),
        is_checkpointing_function=True,
        is_class=True,
    )
    assert any(isinstance(request, api_pb2.ContainerCheckpointRequest) for request in servicer.requests)
    for request in servicer.requests:
        if isinstance(request, api_pb2.ContainerCheckpointRequest):
            assert request.checkpoint_id
    assert _unwrap_scalar(ret) == "ABCD"


@skip_github_non_linux
def test_cls_enter_uses_event_loop(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "EventLoopCls.*",
        inputs=_get_inputs(((), {}), method_name="f"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == True


@skip_github_non_linux
def test_cls_with_image(servicer):
    ret = _run_container(
        servicer,
        "test.supports.class_with_image",
        "ClassWithImage.*",
        inputs=_get_inputs(((), {}), method_name="image_is_hydrated"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == True


@skip_github_non_linux
def test_container_heartbeats(servicer):
    _run_container(servicer, "test.supports.functions", "square")
    assert any(isinstance(request, api_pb2.ContainerHeartbeatRequest) for request in servicer.requests)

    _run_container(servicer, "test.supports.functions", "snapshotting_square")
    assert any(isinstance(request, api_pb2.ContainerHeartbeatRequest) for request in servicer.requests)


@skip_github_non_linux
def test_cli(servicer, tmp_path, credentials):
    # This tests the container being invoked as a subprocess (the if __name__ == "__main__" block)

    # Build up payload we pass through sys args
    function_def = api_pb2.Function(
        module_name="test.supports.functions",
        function_name="square",
        function_type=api_pb2.Function.FUNCTION_TYPE_FUNCTION,
        definition_type=api_pb2.Function.DEFINITION_TYPE_FILE,
        object_dependencies=[api_pb2.ObjectDependency(object_id="im-123")],
    )
    container_args = api_pb2.ContainerArguments(
        task_id="ta-123",
        function_id="fu-123",
        app_id="ap-123",
        function_def=function_def,
        app_layout=api_pb2.AppLayout(
            objects=[
                api_pb2.Object(object_id="im-123"),
            ],
        ),
    )
    container_args_path = tmp_path / "container-args.bin"
    with container_args_path.open("wb") as f:
        f.write(container_args.SerializeToString())

    # Inputs that will be consumed by the container
    servicer.container_inputs = _get_inputs()

    # Launch subprocess
    token_id, token_secret = credentials
    env = {
        "MODAL_SERVER_URL": servicer.container_addr,
        "MODAL_TOKEN_ID": token_id,
        "MODAL_TOKEN_SECRET": token_secret,
        "MODAL_CONTAINER_ARGUMENTS_PATH": str(container_args_path),
    }
    lib_dir = pathlib.Path(__file__).parent.parent
    args: list[str] = [sys.executable, "-m", "modal._container_entrypoint"]
    ret = subprocess.run(args, cwd=lib_dir, env=env, capture_output=True)
    stdout = ret.stdout.decode()
    stderr = ret.stderr.decode()
    if ret.returncode != 0:
        raise Exception(f"Failed with {ret.returncode} stdout: {stdout} stderr: {stderr}")
    assert stdout == ""
    assert stderr == ""


@skip_github_non_linux
def test_function_sibling_hydration(servicer, credentials):
    # TODO: refactor this test to use its own source module/app instead of test.supports.functions (takes 7s to deploy)
    deploy_app_externally(servicer, credentials, "test.supports.sibling_hydration_app", "app", capture_output=False)
    app_layout = servicer.app_get_layout("ap-1")
    ret = _run_container(
        servicer, "test.supports.sibling_hydration_app", "check_sibling_hydration", app_layout=app_layout
    )
    assert _unwrap_scalar(ret) is None


@skip_github_non_linux
def test_multiapp(servicer, credentials, caplog):
    deploy_app_externally(servicer, credentials, "test.supports.multiapp", "a")
    app_layout = servicer.app_get_layout("ap-1")
    ret = _run_container(servicer, "test.supports.multiapp", "a_func", app_layout=app_layout)
    assert _unwrap_scalar(ret) is None
    assert len(caplog.messages) == 0
    # Note that the app can be inferred from the function, even though there are multiple
    # apps present in the file


@skip_github_non_linux
def test_multiapp_privately_decorated(servicer, caplog):
    # function handle does not override the original function, so we can't find the app
    # and the two apps are not named
    ret = _run_container(servicer, "test.supports.multiapp_privately_decorated", "foo")
    assert _unwrap_scalar(ret) == 1
    assert "You have more than one unnamed app." in caplog.text


@skip_github_non_linux
def test_multiapp_privately_decorated_named_app(servicer, caplog):
    # function handle does not override the original function, so we can't find the app
    # but we can use the names of the apps to determine the active app
    ret = _run_container(
        servicer,
        "test.supports.multiapp_privately_decorated_named_app",
        "foo",
        app_name="dummy",
    )
    assert _unwrap_scalar(ret) == 1
    assert len(caplog.messages) == 0  # no warnings, since target app is named


@skip_github_non_linux
def test_multiapp_same_name_warning(servicer, caplog, capsys):
    # function handle does not override the original function, so we can't find the app
    # two apps with the same name - warn since we won't know which one to hydrate
    ret = _run_container(
        servicer,
        "test.supports.multiapp_same_name",
        "foo",
        app_name="dummy",
    )
    assert _unwrap_scalar(ret) == 1
    assert "You have more than one app with the same name ('dummy')" in caplog.text
    capsys.readouterr()


@skip_github_non_linux
def test_multiapp_serialized_func(servicer, caplog):
    # serialized functions shouldn't warn about multiple/not finding apps, since
    # they shouldn't load the module to begin with
    def dummy(x):
        return x

    ret = _run_container(
        servicer,
        "test.supports.multiapp_serialized_func",
        "foo",
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        function_serialized=serialize(dummy),
    )
    assert _unwrap_scalar(ret) == 42
    assert len(caplog.messages) == 0


@skip_github_non_linux
def test_image_run_function_no_warn(servicer, caplog):
    # builder functions currently aren't tied to any modal app,
    # so they shouldn't need to warn if they can't determine which app to use
    ret = _run_container(
        servicer,
        "test.supports.image_run_function",
        "builder_function",
        inputs=_get_inputs(((), {})),
        is_builder_function=True,
    )
    assert _unwrap_scalar(ret) is None
    assert len(caplog.messages) == 0


SLEEP_TIME = 0.7


def _unwrap_concurrent_input_outputs(n_inputs: int, n_parallel: int, ret: ContainerResult):
    # Ensure that outputs align with expectation of running concurrent inputs

    # Each group of n_parallel inputs should start together of each other
    # and different groups should start SLEEP_TIME apart.
    assert len(ret.items) == n_inputs
    for i in range(1, len(ret.items)):
        diff = ret.items[i].input_started_at - ret.items[i - 1].input_started_at
        expected_diff = SLEEP_TIME if i % n_parallel == 0 else 0
        assert diff == pytest.approx(expected_diff, abs=0.3)

    outputs = []
    for item in ret.items:
        assert item.output_created_at - item.input_started_at == pytest.approx(SLEEP_TIME, abs=0.3)
        assert item.result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
        outputs.append(deserialize(item.result.data, ret.client))
    return outputs


@skip_github_non_linux
@pytest.mark.timeout(5)
def test_concurrent_inputs_sync_function(servicer):
    n_inputs = 18
    n_parallel = 6

    t0 = time.time()
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "sleep_700_sync",
        inputs=_get_inputs(n=n_inputs),
        max_concurrent_inputs=n_parallel,
    )

    expected_execution = n_inputs / n_parallel * SLEEP_TIME
    assert expected_execution <= time.time() - t0 < expected_execution + EXTRA_TOLERANCE_DELAY
    outputs = _unwrap_concurrent_input_outputs(n_inputs, n_parallel, ret)
    for i, (squared, input_id, function_call_id) in enumerate(outputs):
        assert squared == 42**2
        assert input_id and input_id != outputs[i - 1][1]
        assert function_call_id and function_call_id == outputs[i - 1][2]


@skip_github_non_linux
def test_concurrent_inputs_async_function(servicer):
    n_inputs = 18
    n_parallel = 6

    t0 = time.time()
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "sleep_700_async",
        inputs=_get_inputs(n=n_inputs),
        max_concurrent_inputs=n_parallel,
    )

    expected_execution = n_inputs / n_parallel * SLEEP_TIME
    assert expected_execution <= time.time() - t0 < expected_execution + EXTRA_TOLERANCE_DELAY
    outputs = _unwrap_concurrent_input_outputs(n_inputs, n_parallel, ret)
    for i, (squared, input_id, function_call_id) in enumerate(outputs):
        assert squared == 42**2
        assert input_id and input_id != outputs[i - 1][1]
        assert function_call_id and function_call_id == outputs[i - 1][2]


def _batch_function_test_helper(
    batch_func,
    servicer,
    args_list,
    expected_outputs,
    expected_status="success",
    batch_max_size=4,
):
    batch_wait_ms = 500
    inputs = _get_inputs_batched(args_list, batch_max_size)

    ret = _run_container(
        servicer,
        "test.supports.functions",
        batch_func,
        inputs=inputs,
        batch_max_size=batch_max_size,
        batch_wait_ms=batch_wait_ms,
    )
    if expected_status == "success":
        outputs = _unwrap_batch_scalar(ret, len(expected_outputs))
    else:
        outputs = _unwrap_batch_exception(ret, len(expected_outputs))
    assert outputs == expected_outputs


@skip_github_non_linux
def test_batch_sync_function_full_batched(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}) for _ in range(4)]
    expected_outputs = [2] * 4
    _batch_function_test_helper("batch_function_sync", servicer, inputs, expected_outputs)


@skip_github_non_linux
def test_batch_sync_function_partial_batched(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}) for _ in range(2)]
    expected_outputs = [2] * 2
    _batch_function_test_helper("batch_function_sync", servicer, inputs, expected_outputs)


@skip_github_non_linux
def test_batch_sync_function_keyword_args(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10,), {"y": 5}) for _ in range(4)]
    expected_outputs = [2] * 4
    _batch_function_test_helper("batch_function_sync", servicer, inputs, expected_outputs)


@skip_github_non_linux
def test_batch_sync_function_arg_len_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}), ((10, 5, 1), {})]
    _batch_function_test_helper(
        "batch_function_sync",
        servicer,
        inputs,
        [
            "InvalidError('Modal batched function batch_function_sync takes 2 positional arguments, but one invocation in the batch has 3.')"  # noqa
        ]
        * 2,
        expected_status="failure",
    )


@skip_github_non_linux
def test_batch_sync_function_keyword_arg_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}), ((10,), {"z": 5})]
    _batch_function_test_helper(
        "batch_function_sync",
        servicer,
        inputs,
        [
            "InvalidError('Modal batched function batch_function_sync got unexpected keyword argument z in one invocation in the batch.')"  # noqa
        ]
        * 2,
        expected_status="failure",
    )


@skip_github_non_linux
def test_batch_sync_function_multiple_args_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}), ((10,), {"x": 1})]
    _batch_function_test_helper(
        "batch_function_sync",
        servicer,
        inputs,
        [
            "InvalidError('Modal batched function batch_function_sync got multiple values for argument x in one invocation in the batch.')"  # noqa
        ]
        * 2,
        expected_status="failure",
    )


@skip_github_non_linux
def test_batch_sync_function_large_batch(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}) for _ in range(500)]
    expected_outputs = [2] * 500
    _batch_function_test_helper(
        "batch_function_sync_large_batch",
        servicer,
        inputs,
        expected_outputs,
        batch_max_size=500,
    )

    # Ensure that the outputs are pushed in small batches.
    for req in servicer.container_outputs:
        assert len(req.outputs) <= 20


@skip_github_non_linux
def test_batch_sync_function_outputs_list_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {})]
    _batch_function_test_helper(
        "batch_function_outputs_not_list",
        servicer,
        inputs,
        ["InvalidError('Output of batched function batch_function_outputs_not_list must be a list.')"] * 1,
        expected_status="failure",
    )


@skip_github_non_linux
def test_batch_sync_function_outputs_len_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {})]
    _batch_function_test_helper(
        "batch_function_outputs_wrong_len",
        servicer,
        inputs,
        [
            "InvalidError('Output of batched function batch_function_outputs_wrong_len must be a list of equal length as its inputs.')"  # noqa
        ]
        * 1,
        expected_status="failure",
    )


@skip_github_non_linux
def test_batch_sync_function_generic_error(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 0), {}) for _ in range(4)]
    expected_ouputs = ["ZeroDivisionError('division by zero')"] * 4
    _batch_function_test_helper("batch_function_sync", servicer, inputs, expected_ouputs, expected_status="failure")


@skip_github_non_linux
def test_batch_async_function(servicer):
    inputs: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((10, 5), {}) for _ in range(4)]
    expected_outputs = [2] * 4
    _batch_function_test_helper("batch_function_async", servicer, inputs, expected_outputs)


@skip_github_non_linux
def test_unassociated_function(servicer):
    ret = _run_container(servicer, "test.supports.functions", "unassociated_function")
    assert _unwrap_scalar(ret) == 58


@skip_github_non_linux
def test_param_cls_function_calling_local(servicer):
    serialized_params = pickle.dumps(([111], {"y": "foo"}))
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "ParamCls.*",
        serialized_params=serialized_params,
        inputs=_get_inputs(method_name="g"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == "111 foo 42"


@skip_github_non_linux
def test_derived_cls(servicer):
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "DerivedCls.*",
        inputs=_get_inputs(((3,), {}), method_name="run"),
        is_class=True,
    )
    assert _unwrap_scalar(ret) == 6


@skip_github_non_linux
def test_call_function_that_calls_function(servicer, credentials):
    deploy_app_externally(servicer, credentials, "test.supports.functions", "app")
    app_layout = servicer.app_get_layout("ap-1")
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "cube",
        inputs=_get_inputs(((42,), {})),
        app_layout=app_layout,
    )
    assert _unwrap_scalar(ret) == 42**3


@skip_github_non_linux
def test_call_function_that_calls_method(servicer, credentials, set_env_client):
    # TODO (elias): Remove set_env_client fixture dependency - shouldn't need an env client here?
    deploy_app_externally(servicer, credentials, "test.supports.sibling_hydration_app", "app")
    app_layout = servicer.app_get_layout("ap-1")
    ret = _run_container(
        servicer,
        "test.supports.sibling_hydration_app",
        "function_calling_method",
        inputs=_get_inputs(((42, "abc", 123), {})),
        app_layout=app_layout,
    )
    assert _unwrap_scalar(ret) == 123**2  # servicer's implementation of function calling


@skip_github_non_linux
def test_checkpoint_and_restore_success(servicer):
    """Functions send a checkpointing request and continue to execute normally,
    simulating a restore operation."""
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "square",
        is_checkpointing_function=True,
    )
    assert any(isinstance(request, api_pb2.ContainerCheckpointRequest) for request in servicer.requests)
    for request in servicer.requests:
        if isinstance(request, api_pb2.ContainerCheckpointRequest):
            assert request.checkpoint_id

    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_volume_commit_on_exit(servicer):
    volume_mounts = [
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-123", allow_background_commits=True),
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-456", allow_background_commits=True),
    ]
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "square",
        volume_mounts=volume_mounts,
    )
    volume_commit_rpcs = [r for r in servicer.requests if isinstance(r, api_pb2.VolumeCommitRequest)]
    assert volume_commit_rpcs
    assert {"vo-123", "vo-456"} == {r.volume_id for r in volume_commit_rpcs}
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_volume_commit_on_error(servicer, capsys):
    volume_mounts = [
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-foo", allow_background_commits=True),
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-bar", allow_background_commits=True),
    ]
    _run_container(
        servicer,
        "test.supports.functions",
        "raises",
        volume_mounts=volume_mounts,
    )
    volume_commit_rpcs = [r for r in servicer.requests if isinstance(r, api_pb2.VolumeCommitRequest)]
    assert {"vo-foo", "vo-bar"} == {r.volume_id for r in volume_commit_rpcs}
    assert 'raise Exception("Failure!")' in capsys.readouterr().err


@skip_github_non_linux
def test_no_volume_commit_on_exit(servicer):
    volume_mounts = [api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-999", allow_background_commits=False)]
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "square",
        volume_mounts=volume_mounts,
    )
    volume_commit_rpcs = [r for r in servicer.requests if isinstance(r, api_pb2.VolumeCommitRequest)]
    assert not volume_commit_rpcs  # No volume commit on exit for legacy volumes
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
def test_volume_commit_on_exit_doesnt_fail_container(servicer):
    volume_mounts = [
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vo-999", allow_background_commits=True),
        api_pb2.VolumeMount(
            mount_path="/var/foo",
            volume_id="BAD-ID-FOR-VOL",
            allow_background_commits=True,
        ),
        api_pb2.VolumeMount(mount_path="/var/foo", volume_id="vol-111", allow_background_commits=True),
    ]
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "square",
        volume_mounts=volume_mounts,
    )
    volume_commit_rpcs = [r for r in servicer.requests if isinstance(r, api_pb2.VolumeCommitRequest)]
    assert len(volume_commit_rpcs) == 3
    assert _unwrap_scalar(ret) == 42**2


@skip_github_non_linux
@pytest.mark.timeout(10.0)
def test_function_io_doesnt_inspect_args_or_return_values(monkeypatch, servicer):
    synchronizer = async_utils.synchronizer

    # set up spys to track synchronicity calls to _translate_scalar_in/out
    translate_in_spy = MagicMock(wraps=synchronizer._translate_scalar_in)
    monkeypatch.setattr(synchronizer, "_translate_scalar_in", translate_in_spy)
    translate_out_spy = MagicMock(wraps=synchronizer._translate_scalar_out)
    monkeypatch.setattr(synchronizer, "_translate_scalar_out", translate_out_spy)

    # don't do blobbing for this test
    monkeypatch.setattr("modal._runtime.container_io_manager.MAX_OBJECT_SIZE_BYTES", 1e100)

    large_data_list = list(range(int(1e6)))  # large data set

    t0 = time.perf_counter()
    # pr = cProfile.Profile()
    # pr.enable()
    _run_container(
        servicer,
        "test.supports.functions",
        "ident",
        inputs=_get_inputs(((large_data_list,), {})),
    )
    # pr.disable()
    # pr.print_stats()
    duration = time.perf_counter() - t0
    assert duration < 5.0  # TODO (elias): might be able to get this down significantly more by improving serialization

    # function_io_manager.serialize(large_data_list)
    in_translations = []
    out_translations = []
    for call in translate_in_spy.call_args_list:
        in_translations += list(call.args)
    for call in translate_out_spy.call_args_list:
        out_translations += list(call.args)

    assert len(in_translations) < 2000  # typically ~400 or something
    assert len(out_translations) < 2000


def _run_container_process(
    servicer,
    tmp_path,
    module_name,
    function_name,
    *,
    inputs: list[tuple[str, tuple, dict[str, Any]]],
    max_concurrent_inputs: Optional[int] = None,
    target_concurrent_inputs: Optional[int] = None,
    cls_params: tuple[tuple, dict[str, Any]] = ((), {}),
    _print=False,  # for debugging - print directly to stdout/stderr instead of pipeing
    env={},
    is_class=False,
    function_type: "api_pb2.Function.FunctionType.ValueType" = api_pb2.Function.FUNCTION_TYPE_FUNCTION,
) -> subprocess.Popen:
    container_args = _container_args(
        module_name,
        function_name,
        max_concurrent_inputs=max_concurrent_inputs,
        target_concurrent_inputs=target_concurrent_inputs,
        serialized_params=serialize(cls_params),
        is_class=is_class,
        function_type=function_type,
    )

    # These env vars are always present in containers
    env["MODAL_TASK_ID"] = "ta-123"
    env["MODAL_IS_REMOTE"] = "1"

    container_args_path = tmp_path / "container-arguments.bin"
    with container_args_path.open("wb") as f:
        f.write(container_args.SerializeToString())
    env["MODAL_CONTAINER_ARGUMENTS_PATH"] = str(container_args_path)

    servicer.container_inputs = _get_multi_inputs(inputs)

    return subprocess.Popen(
        [sys.executable, "-m", "modal._container_entrypoint"],
        env={**os.environ, **env},
        stdout=subprocess.PIPE if not _print else None,
        stderr=subprocess.PIPE if not _print else None,
    )


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
@pytest.mark.parametrize(
    ["function_name", "input_args", "cancelled_input_ids", "expected_container_output", "live_cancellations"],
    [
        # We use None to indicate that we expect a terminated output.
        # the 10 second inputs here are to be cancelled:
        ("delay", [0.01, 20, 0.02], ["in-001"], [0.01, None, 0.02], 1),  # cancel second input
        ("delay_async", [0.01, 20, 0.02], ["in-001"], [0.01, None, 0.02], 1),  # async variant
        # cancel first input, but it has already been processed, so all three should come through:
        ("delay", [0.01, 0.5, 0.03], ["in-000"], [0.01, 0.5, 0.03], 0),
        ("delay_async", [0.01, 0.5, 0.03], ["in-000"], [0.01, 0.5, 0.03], 0),
    ],
)
def test_cancellation_aborts_current_input_on_match(
    tmp_path, servicer, function_name, input_args, cancelled_input_ids, expected_container_output, live_cancellations
):
    # NOTE: for a cancellation to actually happen in this test, it needs to be
    #    triggered while the relevant input is being processed. A future input
    #    would not be cancelled, since those are expected to be handled by
    #    the backend
    with servicer.input_lockstep() as input_lock:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            function_name,
            inputs=[("", (arg,), {}) for arg in input_args],
        )
        time.sleep(1)
        input_lock.wait()
        input_lock.wait()
        # second input has been sent to container here
    time.sleep(0.05)  # give it a little time to start processing

    # now let container receive container heartbeat indicating there is a cancellation
    t0 = time.monotonic()
    num_prior_outputs = len(_flatten_outputs(servicer.container_outputs))
    assert num_prior_outputs == 1  # the second input shouldn't have completed yet

    servicer.container_heartbeat_return_now(
        api_pb2.ContainerHeartbeatResponse(cancel_input_event=api_pb2.CancelInputEvent(input_ids=cancelled_input_ids))
    )
    stdout, stderr = container_process.communicate()
    assert stderr.decode().count("Successfully canceled input") == live_cancellations
    assert "Traceback" not in stderr.decode()
    assert container_process.returncode == 0  # wait for container to exit
    duration = time.monotonic() - t0  # time from heartbeat to container exit

    items = _flatten_outputs(servicer.container_outputs)
    assert len(items) == len(expected_container_output)
    for i, item in enumerate(items):
        if item.result.status == api_pb2.GenericResult.GENERIC_STATUS_TERMINATED:
            assert not expected_container_output[i]
        else:
            data = deserialize(item.result.data, client=None)
            assert data == expected_container_output[i]

    # should never run for ~20s, which is what the input would take if the sleep isn't interrupted
    assert duration < 10  # should typically be < 1s, but for some reason in gh actions, it takes a really long time!


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_cancellation_stops_subset_of_async_concurrent_inputs(servicer, tmp_path):
    num_inputs = 2
    with servicer.input_lockstep() as input_lock:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "delay_async",
            inputs=[("", (1,), {})] * num_inputs,
            max_concurrent_inputs=num_inputs,
        )
        input_lock.wait()
        input_lock.wait()

    time.sleep(0.05)  # let the container get and start processing the input
    servicer.container_heartbeat_return_now(
        api_pb2.ContainerHeartbeatResponse(cancel_input_event=api_pb2.CancelInputEvent(input_ids=["in-001"]))
    )
    # container should exit soon!
    exit_code = container_process.wait(5)
    items = _flatten_outputs(servicer.container_outputs)
    assert len(items) == num_inputs  # should not fail the outputs, as they would have been cancelled in backend already
    assert items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_TERMINATED
    assert deserialize(items[1].result.data, client=None) == 1

    container_stderr = container_process.stderr.read().decode("utf8")
    assert "Traceback" not in container_stderr
    assert exit_code == 0  # container should exit gracefully


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_sigint_concurrent_async_cancel_doesnt_reraise(servicer, tmp_path):
    with servicer.input_lockstep() as input_lock:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "async_cancel_doesnt_reraise",
            inputs=[("", (1,), {})] * 2,  # two inputs
            max_concurrent_inputs=2,
        )
        input_lock.wait()
        input_lock.wait()

    time.sleep(0.05)  # let the container get and start processing the input
    container_process.send_signal(signal.SIGINT)
    # container should exit soon!
    exit_code = container_process.wait(5)
    container_stderr = container_process.stderr.read().decode("utf8")
    assert "Traceback" not in container_stderr
    # TODO (elias): Make some assertions regarding what kind of output is recorded (if any) is recorded for these inputs
    assert exit_code == 0  # container should exit gracefully


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_cancellation_stops_task_with_concurrent_inputs(servicer, tmp_path):
    with servicer.input_lockstep() as input_lock:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "delay",
            inputs=[("", (20,), {})] * 2,  # two inputs
            max_concurrent_inputs=2,
        )
        input_lock.wait()
        input_lock.wait()

    time.sleep(0.05)  # let the container get and start processing the input
    servicer.container_heartbeat_return_now(
        api_pb2.ContainerHeartbeatResponse(cancel_input_event=api_pb2.CancelInputEvent(input_ids=["in-001"]))
    )
    # container should exit immediately, stopping execution of both inputs
    exit_code = container_process.wait(5)
    assert not servicer.container_outputs  # No terminated outputs as task should be killed by server anyway.

    container_stderr = container_process.stderr.read().decode("utf8")
    assert "Traceback" not in container_stderr
    assert exit_code == 0  # container should exit gracefully


@skip_github_non_linux
def test_inputs_outputs_with_blob_id(servicer, client, monkeypatch):
    monkeypatch.setattr("modal._runtime.container_io_manager.MAX_OBJECT_SIZE_BYTES", 0)
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "ident",
        inputs=_get_inputs(((42,), {}), upload_to_blob=True, client=client),
    )
    assert _unwrap_blob_scalar(ret, client) == 42


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_lifecycle_full(servicer, tmp_path):
    # Sync and async container lifecycle methods on a sync function.
    container_process = _run_container_process(
        servicer,
        tmp_path,
        "test.supports.functions",
        "LifecycleCls.*",
        inputs=[("f_sync", (), {})],
        cls_params=((), {"print_at_exit": 1}),
        is_class=True,
    )
    stdout, _ = container_process.communicate(timeout=5)
    assert container_process.returncode == 0
    assert "[events:enter_sync,enter_async,f_sync,local,exit_sync,exit_async]" in stdout.decode()

    # Sync and async container lifecycle methods on an async function.
    container_process = _run_container_process(
        servicer,
        tmp_path,
        "test.supports.functions",
        "LifecycleCls.*",
        inputs=[("f_async", (), {})],
        cls_params=((), {"print_at_exit": 1}),
        is_class=True,
    )
    stdout, _ = container_process.communicate(timeout=5)
    assert container_process.returncode == 0
    assert "[events:enter_sync,enter_async,f_async,local,exit_sync,exit_async]" in stdout.decode()


## modal.experimental functionality ##


@skip_github_non_linux
@pytest.mark.timeout(10)
def test_stop_fetching_inputs(servicer):
    ret = _run_container(
        servicer,
        "test.supports.experimental",
        "StopFetching.*",
        inputs=_get_inputs(((42,), {}), n=4, kill_switch=False, method_name="after_two"),
        is_class=True,
    )

    assert len(ret.items) == 2
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS


@skip_github_non_linux
def test_container_heartbeat_survives_grpc_deadlines(servicer, caplog, monkeypatch):
    monkeypatch.setattr("modal._runtime.container_io_manager.HEARTBEAT_INTERVAL", 0.01)
    num_heartbeats = 0

    async def heartbeat_responder(servicer, stream):
        nonlocal num_heartbeats
        num_heartbeats += 1
        await stream.recv_message()
        raise GRPCError(Status.DEADLINE_EXCEEDED)

    with servicer.intercept() as ctx:
        ctx.set_responder("ContainerHeartbeat", heartbeat_responder)
        ret = _run_container(
            servicer,
            "test.supports.functions",
            "delay",
            inputs=_get_inputs(((2,), {})),
        )
        assert ret.task_result is None  # should not cause a failure result
    loop_iteration_failures = caplog.text.count("Heartbeat attempt failed")
    assert "Traceback" not in caplog.text  # should not print a full traceback - don't scare users!
    assert (
        loop_iteration_failures > 1
    )  # one occurence per failing `retry_transient_errors()`, so fewer than the number of failing requests!
    assert loop_iteration_failures < num_heartbeats
    assert num_heartbeats > 4  # more than the default number of retries per heartbeat attempt + 1


@skip_github_non_linux
def test_container_heartbeat_survives_local_exceptions(servicer, caplog, monkeypatch):
    numcalls = 0

    async def custom_heartbeater(self):
        nonlocal numcalls
        numcalls += 1
        raise Exception("oops")

    monkeypatch.setattr("modal._runtime.container_io_manager.HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(
        "modal._runtime.container_io_manager._ContainerIOManager._heartbeat_handle_cancellations", custom_heartbeater
    )

    ret = _run_container(
        servicer,
        "test.supports.functions",
        "delay",
        inputs=_get_inputs(((0.5,), {})),
    )
    assert ret.task_result is None  # should not cause a failure result
    loop_iteration_failures = caplog.text.count("Heartbeat attempt failed")
    assert loop_iteration_failures > 5
    assert "error=Exception('oops')" in caplog.text
    assert "Traceback" not in caplog.text  # should not print a full traceback - don't scare users!


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_container_doesnt_send_large_exceptions(servicer):
    # Tests that large exception messages (>2mb are trimmed)
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "raise_large_unicode_exception",
        inputs=_get_inputs(((), {})),
    )

    assert len(ret.items) == 1
    assert len(ret.items[0].SerializeToString()) < MAX_OBJECT_SIZE_BYTES * 1.5
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "UnicodeDecodeError" in ret.items[0].result.exception
    assert servicer.task_result is None  # should not cause a failure result


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
def test_sigint_termination_input_concurrent(servicer, tmp_path):
    # Sync and async container lifecycle methods on a sync function.
    with servicer.input_lockstep() as input_barrier:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "LifecycleCls.*",
            inputs=[("delay", (10,), {})] * 3,
            cls_params=((), {"print_at_exit": 1}),
            max_concurrent_inputs=2,
            is_class=True,
        )
        input_barrier.wait()  # get one input
        input_barrier.wait()  # get one input
        time.sleep(0.5)
        # container won't be able to fetch next input
        signal_time = time.monotonic()
        os.kill(container_process.pid, signal.SIGINT)

    stdout, stderr = container_process.communicate(timeout=5)
    stop_duration = time.monotonic() - signal_time
    assert len(servicer.container_outputs) == 0
    assert (
        container_process.returncode == 0
    )  # container should catch and indicate successful termination by exiting cleanly when possible
    assert "[events:enter_sync,enter_async,delay,delay,exit_sync,exit_async]" in stdout.decode()
    assert "Traceback" not in stderr.decode()
    assert "Traceback" not in stdout.decode()
    assert stop_duration < 2.0  # if this would be ~4.5s, then the input isn't getting terminated
    assert servicer.task_result is None


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
@pytest.mark.parametrize("method", ["delay", "delay_async"])
def test_sigint_termination_input(servicer, tmp_path, method):
    # Sync and async container lifecycle methods on a sync function.
    with servicer.input_lockstep() as input_barrier:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "LifecycleCls.*",
            inputs=[(method, (5,), {})],
            cls_params=((), {"print_at_exit": 1}),
            is_class=True,
        )
        input_barrier.wait()  # get input
        time.sleep(0.5)
        signal_time = time.monotonic()
        os.kill(container_process.pid, signal.SIGINT)

    stdout, stderr = container_process.communicate(timeout=5)
    stop_duration = time.monotonic() - signal_time

    if method == "delay":
        assert len(servicer.container_outputs) == 0
    else:
        # We end up returning a terminated output for async task cancels, which is ignored by the worker anyway.
        items = _flatten_outputs(servicer.container_outputs)
        assert len(items) == 1
        assert items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_TERMINATED

    assert (
        container_process.returncode == 0
    )  # container should catch and indicate successful termination by exiting cleanly when possible
    assert f"[events:enter_sync,enter_async,{method},exit_sync,exit_async]" in stdout.decode()
    assert "Traceback" not in stderr.decode()
    assert stop_duration < 2.0  # if this would be ~4.5s, then the input isn't getting terminated
    assert servicer.task_result is None


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
@pytest.mark.parametrize("enter_type", ["sync_enter", "async_enter"])
@pytest.mark.parametrize("method", ["delay", "delay_async"])
def test_sigint_termination_enter_handler(servicer, tmp_path, method, enter_type):
    # Sync and async container lifecycle methods on a sync function.
    container_process = _run_container_process(
        servicer,
        tmp_path,
        "test.supports.functions",
        "LifecycleCls.*",
        inputs=[(method, (5,), {})],
        cls_params=((), {"print_at_exit": 1, f"{enter_type}_duration": 10}),
        is_class=True,
    )
    time.sleep(1)  # should be enough to start the enter method
    signal_time = time.monotonic()
    os.kill(container_process.pid, signal.SIGINT)
    stdout, stderr = container_process.communicate(timeout=5)
    stop_duration = time.monotonic() - signal_time
    assert len(servicer.container_outputs) == 0
    assert container_process.returncode == 0
    if enter_type == "sync_enter":
        assert "[events:enter_sync]" in stdout.decode()
    else:
        # enter_sync should run in 0s, and then we interrupt during the async enter
        assert "[events:enter_sync,enter_async]" in stdout.decode()

    assert "Traceback" not in stderr.decode()
    assert stop_duration < 2.0  # if this would be ~4.5s, then the task isn't being terminated timely
    assert servicer.task_result is None


@skip_github_non_linux
@pytest.mark.usefixtures("server_url_env")
@pytest.mark.parametrize("exit_type", ["sync_exit", "async_exit"])
def test_sigint_termination_exit_handler(servicer, tmp_path, exit_type):
    # Sync and async container lifecycle methods on a sync function.
    with servicer.output_lockstep() as outputs:
        container_process = _run_container_process(
            servicer,
            tmp_path,
            "test.supports.functions",
            "LifecycleCls.*",
            inputs=[("delay", (0,), {})],
            cls_params=((), {"print_at_exit": 1, f"{exit_type}_duration": 2}),
            is_class=True,
        )
        outputs.wait()  # wait for first output to be emitted
    time.sleep(1)  # give some time for container to end up in the exit handler
    os.kill(container_process.pid, signal.SIGINT)

    stdout, stderr = container_process.communicate(timeout=5)

    assert len(servicer.container_outputs) == 1
    assert container_process.returncode == 0
    assert "[events:enter_sync,enter_async,delay,exit_sync,exit_async]" in stdout.decode()
    assert "Traceback" not in stderr.decode()
    assert servicer.task_result is None


@skip_github_non_linux
def test_sandbox(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.functions", "sandbox_f")
    assert _unwrap_scalar(ret) == "sb-123"


@skip_github_non_linux
def test_is_local(servicer, event_loop):
    assert is_local() == True

    ret = _run_container(servicer, "test.supports.functions", "is_local_f")
    assert _unwrap_scalar(ret) == False


class Foo:
    x: str = modal.parameter()

    @enter()
    def some_enter(self):
        self.x += "_enter"

    @method()
    def method_a(self, y):
        return self.x + f"_a_{y}"

    @method()
    def method_b(self, y):
        return self.x + f"_b_{y}"


@skip_github_non_linux
def test_class_as_service_serialized(servicer):
    # TODO(elias): refactor once the loading code is merged

    app = modal.App()
    app.cls()(Foo)  # avoid errors about methods not being turned into functions

    result = _run_container(
        servicer,
        "nomodule",
        "Foo.*",
        definition_type=api_pb2.Function.DEFINITION_TYPE_SERIALIZED,
        is_class=True,
        inputs=_get_multi_inputs_with_methods([("method_a", ("x",), {}), ("method_b", ("y",), {})]),
        serialized_params=serialize(((), {"x": "s"})),
        class_serialized=serialize(Foo),
    )
    assert len(result.items) == 2
    res_0 = result.items[0].result
    res_1 = result.items[1].result
    assert res_0.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert res_1.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert deserialize(res_0.data, result.client) == "s_enter_a_x"
    assert deserialize(res_1.data, result.client) == "s_enter_b_y"


@skip_github_non_linux
def test_function_lazy_hydration(servicer, credentials, set_env_client):
    # Deploy some global objects
    Volume.from_name("my-vol", create_if_missing=True).hydrate()
    Queue.from_name("my-queue", create_if_missing=True).hydrate()

    # Run container
    deploy_app_externally(servicer, credentials, "test.supports.lazy_hydration", "app", capture_output=False)
    app_layout = servicer.app_get_layout("ap-1")
    ret = _run_container(servicer, "test.supports.lazy_hydration", "f", deps=["im-2", "vo-0"], app_layout=app_layout)
    assert _unwrap_scalar(ret) is None


@skip_github_non_linux
def test_no_warn_on_remote_local_volume_mount(client, servicer, recwarn, set_env_client):
    _run_container(
        servicer,
        "test.supports.volume_local",
        "volume_func_outer",
        inputs=_get_inputs(((), {})),
    )

    warnings = len(recwarn)
    for w in range(warnings):
        warning = str(recwarn.pop().message)
        assert "and will not have access to the mounted Volume or NetworkFileSystem data" not in warning
    assert len(recwarn) == 0


@pytest.mark.parametrize("concurrency", [1, 2])
def test_container_io_manager_concurrency_tracking(client, servicer, concurrency):
    dummy_container_args = api_pb2.ContainerArguments(
        function_id="fu-123", function_def=api_pb2.Function(target_concurrent_inputs=concurrency)
    )
    from modal._utils.async_utils import synchronizer

    io_manager = ContainerIOManager(dummy_container_args, client)
    _io_manager = synchronizer._translate_in(io_manager)

    async def _func(x):
        await asyncio.sleep(x)

    fin_func = FinalizedFunction(_func, is_async=True, is_generator=False, data_format=api_pb2.DATA_FORMAT_PICKLE)

    total_inputs = 5
    servicer.container_inputs = _get_inputs(((42,), {}), n=total_inputs)
    active_inputs: list[IOContext] = []
    active_input_ids = set()
    processed_inputs = 0
    triggered_assertions = []
    peak_inputs = 0
    for io_context in io_manager.run_inputs_outputs(
        finalized_functions={"": fin_func},
    ):
        assert len(io_context.input_ids) == 1  # no batching in this test
        assert _io_manager.current_input_id == io_context.input_ids[0]
        active_inputs += [io_context]
        peak_inputs = max(peak_inputs, len(active_inputs))
        active_input_ids |= set(io_context.input_ids)
        processed_inputs += len(io_context.input_ids)

        while active_inputs and (len(active_inputs) == concurrency or processed_inputs == total_inputs):
            input_to_process = active_inputs.pop(0)
            send_failure = processed_inputs % 2 == 1
            # return values for inputs
            with io_manager.handle_input_exception(input_to_process, time.time()):
                try:
                    # can't raise assertions in here, since they are caught and forwarded as input exceptions
                    assert set(_io_manager.current_inputs.keys()) == set(active_input_ids)
                except AssertionError as assertion:
                    triggered_assertions.append(assertion)
                    raise

                active_input_ids -= set(input_to_process.input_ids)

                if send_failure:
                    # trigger some errors
                    raise Exception("Blah")
                else:
                    # and some successes
                    io_manager.push_outputs(input_to_process, 0, None, fin_func.data_format)
    assert not triggered_assertions


@pytest.mark.asyncio
async def test_input_slots():
    slots = InputSlots(10)

    async def acquire_for(cm, secs):
        await cm.acquire()
        await asyncio.sleep(secs)
        cm.release()

    tasks1 = asyncio.gather(*[acquire_for(slots, 0.1) for _ in range(4)])
    tasks2 = asyncio.gather(*[acquire_for(slots, 0.2) for _ in range(4)])
    await asyncio.sleep(0.01)

    slots.set_value(1)
    assert slots.value == 1
    assert slots.active == 8
    await tasks1
    assert slots.active == 4

    slots.set_value(2)
    assert slots.active == 4

    slots.set_value(10)
    await tasks2
    assert slots.active == 0

    await slots.close()
    assert slots.active == 10
    assert slots.value == 10


@skip_github_non_linux
def test_max_concurrency(servicer):
    n_inputs = 5
    target_concurrency = 2
    max_concurrency = 10

    ret = _run_container(
        servicer,
        "test.supports.functions",
        "get_input_concurrency",
        inputs=_get_inputs(((1,), {}), n=n_inputs),
        max_concurrent_inputs=max_concurrency,
        target_concurrent_inputs=target_concurrency,
    )

    outputs = [deserialize(item.result.data, ret.client) for item in ret.items]
    assert n_inputs in outputs


@skip_github_non_linux
def test_set_local_input_concurrency(servicer):
    n_inputs = 6
    target_concurrency = 3
    max_concurrency = 6

    now = time.time()
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "set_input_concurrency",
        inputs=_get_inputs(((now,), {}), n=n_inputs),
        max_concurrent_inputs=max_concurrency,
        target_concurrent_inputs=target_concurrency,
    )

    outputs = [int(deserialize(item.result.data, ret.client)) for item in ret.items]
    assert outputs == [1] * 3 + [2] * 3


@skip_github_non_linux
def test_sandbox_infers_app(servicer, event_loop):
    _run_container(servicer, "test.supports.sandbox", "spawn_sandbox")
    assert servicer.sandbox_app_id == "ap-1"


@skip_github_non_linux
def test_deserialization_error_returns_exception(servicer, client):
    inputs = [
        api_pb2.FunctionGetInputsResponse(
            inputs=[
                api_pb2.FunctionGetInputsItem(
                    input_id="in-xyz0",
                    function_call_id="fc-123",
                    input=api_pb2.FunctionInput(
                        args=b"\x80\x04\x95(\x00\x00\x00\x00\x00\x00\x00\x8c\x17",
                        data_format=api_pb2.DATA_FORMAT_PICKLE,
                        method_name="",
                    ),
                ),
            ]
        ),
        *_get_inputs(((2,), {})),
    ]
    ret = _run_container(
        servicer,
        "test.supports.functions",
        "square",
        inputs=inputs,
    )
    assert len(ret.items) == 2
    assert ret.items[0].result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert "DeserializationError" in ret.items[0].result.exception

    assert ret.items[1].result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert int(deserialize(ret.items[1].result.data, ret.client)) == 4


@skip_github_non_linux
def test_cls_self_doesnt_call_bind(servicer, credentials, set_env_client):
    # first populate app objects, so they can be fetched by AppGetObjects
    deploy_app_externally(servicer, credentials, "test.supports.user_code_import_samples.cls")
    app_layout = servicer.app_get_layout("ap-1")

    with servicer.intercept() as ctx:
        ret = _run_container(
            servicer,
            "test.supports.user_code_import_samples.cls",
            "C.*",
            is_class=True,
            inputs=_get_inputs(args=((3,), {}), method_name="calls_f_remote"),
            app_layout=app_layout,
        )
        assert _unwrap_scalar(ret) == 9  # implies successful container run (.remote will use dummy servicer function)

        # Using self should never have to call function bind params, since the object
        # is already specified and the instance servicer function should already be
        # hydrated:
        assert not ctx.get_requests("FunctionBindParams")

        # TODO: add test for using self.keep_warm()


@skip_github_non_linux
def test_container_app_zero_matching(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.function_without_app", "f")
    assert _unwrap_scalar(ret) == 123


@skip_github_non_linux
def test_container_app_one_matching(servicer, event_loop):
    _run_container(servicer, "test.supports.functions", "check_container_app")


@skip_github_non_linux
def test_no_event_loop(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.functions", "get_running_loop")
    exc = _unwrap_exception(ret)
    assert isinstance(exc, RuntimeError)
    assert repr(exc) == "RuntimeError('no running event loop')"


@skip_github_non_linux
def test_is_main_thread_sync(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.functions", "is_main_thread_sync")
    assert _unwrap_scalar(ret) is True


@skip_github_non_linux
def test_is_main_thread_async(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.functions", "is_main_thread_async")
    assert _unwrap_scalar(ret) is True


@skip_github_non_linux
def test_import_thread_is_main_thread(servicer, event_loop):
    ret = _run_container(servicer, "test.supports.functions", "import_thread_is_main_thread")
    assert _unwrap_scalar(ret) is True


@skip_github_non_linux
def test_custom_exception(servicer, capsys):
    ret = _run_container(servicer, "test.supports.functions", "raises_custom_exception")
    exc = _unwrap_exception(ret)
    assert isinstance(exc, Exception)
    assert repr(exc) == "CustomException('Failure!')"
