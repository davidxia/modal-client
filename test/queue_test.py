# Copyright Modal Labs 2022
import pytest
import queue
import time

from modal import Queue
from modal.exception import DeprecationError, InvalidError, NotFoundError
from modal_proto import api_pb2

from .supports.skip import skip_macos, skip_windows


def test_queue_named(servicer, client):
    name = "some-random-queue"
    q = Queue.from_name(name, create_if_missing=True)
    assert isinstance(q, Queue)
    assert q.name == name

    q.hydrate(client)

    info = q.info()
    assert info.name == name
    assert info.created_by == servicer.default_username

    assert q.len() == 0
    q.put(42)
    assert q.len() == 1
    assert q.get() == 42
    with pytest.raises(queue.Empty):
        q.get(timeout=0)
    assert q.len() == 0

    # test iter
    q.put_many([1, 2, 3])
    t0 = time.time()
    assert [v for v in q.iterate(item_poll_timeout=1.0)] == [1, 2, 3]
    assert 1.0 < time.time() - t0 < 2.0
    assert [v for v in q.iterate(item_poll_timeout=0.0)] == [1, 2, 3]

    Queue.delete("some-random-queue", client=client)
    with pytest.raises(NotFoundError):
        Queue.from_name("some-random-queue").hydrate(client)


def test_queue_ephemeral(servicer, client):
    with Queue.ephemeral(client=client, _heartbeat_sleep=1) as q:
        q.put("hello")
        assert q.len() == 1
        assert q.get() == "hello"
        time.sleep(1.5)  # enough to trigger two heartbeats

    assert servicer.n_queue_heartbeats == 2


@skip_macos("TODO(erikbern): this consistently fails on OSX. Unclear why.")
@skip_windows("TODO(Jonathon): figure out why timeouts don't occur on Windows.")
@pytest.mark.parametrize(
    ["put_timeout_secs", "min_queue_full_exc_count", "max_queue_full_exc_count"],
    [
        (0.02, 1, 100),  # a low timeout causes some exceptions
        (10.0, 0, 0),  # a high timeout causes zero exceptions
        (0.00, 1, 100),  # zero-len timeout causes some exceptions
        (None, 0, 0),  # no timeout causes zero exceptions
    ],
)
def test_queue_blocking_put(put_timeout_secs, min_queue_full_exc_count, max_queue_full_exc_count, servicer, client):
    import queue
    import threading

    producer_delay = 0.001
    consumer_delay = producer_delay * 5

    queue_full_exceptions = 0
    with Queue.ephemeral(client=client) as q:

        def producer():
            nonlocal queue_full_exceptions
            for i in range(servicer.queue_max_len * 2):
                item = f"Item {i}"
                try:
                    q.put(item, block=True, timeout=put_timeout_secs)  # type: ignore
                except queue.Full:
                    queue_full_exceptions += 1
                time.sleep(producer_delay)

        def consumer():
            while True:
                time.sleep(consumer_delay)
                item = q.get(block=True)  # type: ignore
                if item is None:
                    break  # Exit if a None item is received

        producer_thread = threading.Thread(target=producer)
        consumer_thread = threading.Thread(target=consumer)
        producer_thread.start()
        consumer_thread.start()
        producer_thread.join()
        # Stop the consumer by sending a None item
        q.put(None)  # type: ignore
        consumer_thread.join()

        assert queue_full_exceptions >= min_queue_full_exc_count
        assert queue_full_exceptions <= max_queue_full_exc_count


def test_queue_nonblocking_put(servicer, client):
    with Queue.ephemeral(client=client) as q:
        # Non-blocking PUTs don't tolerate a full queue and will raise exception.
        with pytest.raises(queue.Full) as excinfo:
            for i in range(servicer.queue_max_len + 1):
                q.put(i, block=False)  # type: ignore

    assert str(servicer.queue_max_len) in str(excinfo.value)
    assert i == servicer.queue_max_len


def test_queue_deploy(servicer, client):
    d = Queue.from_name("xyz", create_if_missing=True).hydrate(client)
    d.put(123)


def test_queue_lazy_hydrate_from_name(set_env_client):
    q = Queue.from_name("foo", create_if_missing=True)
    q.put(123)
    assert q.get() == 123


@pytest.mark.parametrize("name", ["has space", "has/slash", "a" * 65])
def test_invalid_name(name):
    with pytest.raises(InvalidError, match="Invalid Queue name"):
        Queue.from_name(name)


def test_queue_namespace_deprecated(servicer, client):
    # Test from_name with namespace parameter warns
    with pytest.warns(
        DeprecationError,
        match="The `namespace` parameter for `modal.Queue.from_name` is deprecated",
    ):
        Queue.from_name("test-queue", namespace=api_pb2.DEPLOYMENT_NAMESPACE_WORKSPACE)

    # Test that from_name without namespace parameter doesn't warn about namespace
    import warnings

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        Queue.from_name("test-queue")
    # Filter out any unrelated warnings
    namespace_warnings = [w for w in record if "namespace" in str(w.message).lower()]
    assert len(namespace_warnings) == 0
