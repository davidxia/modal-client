# Copyright Modal Labs 2024
import asyncio
import pytest
import time

from grpclib import Status
from grpclib.exceptions import GRPCError

from modal import enable_output
from modal._utils.async_utils import aclosing, sync_or_async_iter
from modal.io_streams import StreamReader
from modal_proto import api_pb2


def test_stream_reader(servicer, client):
    """Tests that the stream reader works with clean inputs."""
    lines = ["foo\n", "bar\n", "baz\n"]

    async def sandbox_get_logs(servicer, stream):
        await stream.recv_message()

        for line in lines:
            log = api_pb2.TaskLogs(data=line, file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT)
            await stream.send_message(api_pb2.TaskLogsBatch(entry_id=line, items=[log]))

        # send EOF
        await stream.send_message(api_pb2.TaskLogsBatch(eof=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("SandboxGetLogs", sandbox_get_logs)

        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="sb-123",
                object_type="sandbox",
                client=client,
            )

            out = []
            for line in stdout:
                out.append(line)

            assert out == lines


def test_stream_reader_processed(servicer, client):
    """Tests that the stream reader with logs by line works with clean inputs."""
    lines = ["foo\n", "bar\n", "baz\n"]

    async def sandbox_get_logs(servicer, stream):
        await stream.recv_message()

        for line in lines:
            log = api_pb2.TaskLogs(data=line, file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT)
            await stream.send_message(api_pb2.TaskLogsBatch(entry_id=line, items=[log]))

        # send EOF
        await stream.send_message(api_pb2.TaskLogsBatch(eof=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("SandboxGetLogs", sandbox_get_logs)

        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="sb-123",
                object_type="sandbox",
                client=client,
                by_line=True,
            )

            out = []
            for line in stdout:
                out.append(line)

            assert out == lines


def test_stream_reader_processed_multiple(servicer, client):
    """Tests that the stream reader with logs by line splits multiple lines."""

    async def sandbox_get_logs(servicer, stream):
        await stream.recv_message()

        log = api_pb2.TaskLogs(
            data="foo\nbar\nbaz",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="0", items=[log]))

        # send EOF
        await stream.send_message(api_pb2.TaskLogsBatch(eof=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("SandboxGetLogs", sandbox_get_logs)

        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="sb-123",
                object_type="sandbox",
                client=client,
                by_line=True,
            )

            out = []
            for line in stdout:
                out.append(line)

            assert out == ["foo\n", "bar\n", "baz"]


def test_stream_reader_processed_partial_lines(servicer, client):
    """Test that the stream reader with logs by line joins partial lines together."""

    async def sandbox_get_logs(servicer, stream):
        await stream.recv_message()

        log1 = api_pb2.TaskLogs(
            data="foo",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="0", items=[log1]))

        log2 = api_pb2.TaskLogs(
            data="bar\n",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="1", items=[log2]))

        log3 = api_pb2.TaskLogs(
            data="baz",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="2", items=[log3]))

        # send EOF
        await stream.send_message(api_pb2.TaskLogsBatch(eof=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("SandboxGetLogs", sandbox_get_logs)

        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="sb-123",
                object_type="sandbox",
                client=client,
                by_line=True,
            )

            out = []
            for line in stdout:
                out.append(line)

            assert out == ["foobar\n", "baz"]


@pytest.mark.asyncio
async def test_stream_reader_bytes_mode(servicer, client):
    """Test that the stream reader works in bytes mode."""

    async def container_exec_get_output(servicer, stream):
        await stream.recv_message()

        await stream.send_message(
            api_pb2.RuntimeOutputBatch(batch_index=0, items=[api_pb2.RuntimeOutputMessage(message_bytes=b"foo\n")])
        )

        await stream.send_message(api_pb2.RuntimeOutputBatch(exit_code=0))

    with servicer.intercept() as ctx:
        ctx.set_responder("ContainerExecGetOutput", container_exec_get_output)

        with enable_output():
            stdout: StreamReader[bytes] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="tp-123",
                object_type="container_process",
                client=client,
                text=False,
            )

            assert await stdout.read.aio() == b"foo\n"


def test_stream_reader_line_buffered_bytes(servicer, client):
    """Test that using line-buffering with bytes mode fails."""

    with pytest.raises(ValueError):
        StreamReader(
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
            object_id="tp-123",
            object_type="container_process",
            client=client,
            by_line=True,
            text=False,
        )


@pytest.mark.asyncio
async def test_stream_reader_async_iter(servicer, client):
    """Test that StreamReader behaves as a proper async iterator."""

    async def sandbox_get_logs(servicer, stream):
        await stream.recv_message()

        log1 = api_pb2.TaskLogs(
            data="foo",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="0", items=[log1]))

        log2 = api_pb2.TaskLogs(
            data="bar",
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
        )
        await stream.send_message(api_pb2.TaskLogsBatch(entry_id="1", items=[log2]))

        # send EOF
        await stream.send_message(api_pb2.TaskLogsBatch(eof=True))

    with servicer.intercept() as ctx:
        ctx.set_responder("SandboxGetLogs", sandbox_get_logs)

        expected = "foobar"

        stdout: StreamReader[str] = StreamReader(
            file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
            object_id="sb-123",
            object_type="sandbox",
            client=client,
            by_line=True,
        )

        out = ""
        async with aclosing(sync_or_async_iter(stdout)) as stream:
            async for line in stream:
                out += line

        assert out == expected


@pytest.mark.asyncio
async def test_stream_reader_container_process_retry(servicer, client):
    """Test that StreamReader handles container process stream failures and retries."""

    batch_idx = 0

    async def container_exec_get_output(servicer, stream):
        nonlocal batch_idx
        await stream.recv_message()

        for _ in range(3):
            await stream.send_message(
                api_pb2.RuntimeOutputBatch(
                    batch_index=batch_idx,
                    items=[api_pb2.RuntimeOutputMessage(message_bytes=f"msg{batch_idx}\n".encode())],
                )
            )
            batch_idx += 1

        # Simulate failure on the first connection
        if batch_idx == 3:
            raise GRPCError(Status.INTERNAL, "internal error")

        await stream.send_message(api_pb2.RuntimeOutputBatch(exit_code=0))

    with servicer.intercept() as ctx:
        ctx.set_responder("ContainerExecGetOutput", container_exec_get_output)

        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="tp-123",
                object_type="container_process",
                client=client,
                by_line=True,
            )

            output = []
            async for line in stdout:
                output.append(line)

            assert output == [f"msg{i}\n" for i in range(6)]


@pytest.mark.asyncio
async def test_stream_reader_timeout(servicer, client):
    """Test that StreamReader stops reading messages after the given deadline, and that
    messages are received within the deadline"""

    time_first_send = 0.0

    async def container_exec_get_output(servicer, stream):
        nonlocal time_first_send
        await stream.recv_message()
        # Send three messages, third one heavily delayed
        for i in range(3):
            if i == 2:
                await asyncio.sleep(3)
            await stream.send_message(
                api_pb2.RuntimeOutputBatch(
                    batch_index=i,
                    items=[api_pb2.RuntimeOutputMessage(message_bytes=f"msg{i}\n".encode())],
                )
            )
            if i == 0:
                time_first_send = time.monotonic()
        await stream.send_message(api_pb2.RuntimeOutputBatch(exit_code=0))

    with servicer.intercept() as ctx:
        ctx.set_responder("ContainerExecGetOutput", container_exec_get_output)
        with enable_output():
            stdout: StreamReader[str] = StreamReader(
                file_descriptor=api_pb2.FILE_DESCRIPTOR_STDOUT,
                object_id="tp-123",
                object_type="container_process",
                client=client,
                by_line=True,
                deadline=time.monotonic() + 2,  # use a 2-second timeout
            )
            output: list[str] = []
            async for line in stdout:
                output.append(line)
            # message 3 should not be received, due to the timeout
            assert output == [f"msg{i}\n" for i in range(2)]
            assert time.monotonic() - time_first_send <= 4
