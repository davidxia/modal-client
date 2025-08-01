# Copyright Modal Labs 2022
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from google.protobuf.message import Message
from grpclib import GRPCError
from synchronicity.async_wrap import asynccontextmanager

from modal_proto import api_pb2

from ._object import EPHEMERAL_OBJECT_HEARTBEAT_SLEEP, _get_environment_name, _Object, live_method, live_method_gen
from ._resolver import Resolver
from ._serialization import deserialize, serialize
from ._utils.async_utils import TaskContext, synchronize_api
from ._utils.deprecation import deprecation_warning, warn_if_passing_namespace
from ._utils.grpc_utils import retry_transient_errors
from ._utils.name_utils import check_object_name
from ._utils.time_utils import timestamp_to_localized_dt
from .client import _Client
from .config import logger
from .exception import RequestSizeError


def _serialize_dict(data):
    return [api_pb2.DictEntry(key=serialize(k), value=serialize(v)) for k, v in data.items()]


@dataclass
class DictInfo:
    """Information about the Dict object."""

    # This dataclass should be limited to information that is unchanging over the lifetime of the Dict,
    # since it is transmitted from the server when the object is hydrated and could be stale when accessed.

    name: Optional[str]
    created_at: datetime
    created_by: Optional[str]


class _Dict(_Object, type_prefix="di"):
    """Distributed dictionary for storage in Modal apps.

    Dict contents can be essentially any object so long as they can be serialized by
    `cloudpickle`. This includes other Modal objects. If writing and reading in different
    environments (eg., writing locally and reading remotely), it's necessary to have the
    library defining the data type installed, with compatible versions, on both sides.
    Additionally, cloudpickle serialization is not guaranteed to be deterministic, so it is
    generally recommended to use primitive types for keys.

    **Lifetime of a Dict and its items**

    An individual Dict entry will expire after 7 days of inactivity (no reads or writes). The
    Dict entries are written to durable storage.

    Legacy Dicts (created before 2025-05-20) will still have entries expire 30 days after being
    last added. Additionally, contents are stored in memory on the Modal server and could be lost
    due to unexpected server restarts. Eventually, these Dicts will be fully sunset.

    **Usage**

    ```python
    from modal import Dict

    my_dict = Dict.from_name("my-persisted_dict", create_if_missing=True)

    my_dict["some key"] = "some value"
    my_dict[123] = 456

    assert my_dict["some key"] == "some value"
    assert my_dict[123] == 456
    ```

    The `Dict` class offers a few methods for operations that are usually accomplished
    in Python with operators, such as `Dict.put` and `Dict.contains`. The advantage of
    these methods is that they can be safely called in an asynchronous context by using
    the `.aio` suffix on the method, whereas their operator-based analogues will always
    run synchronously and block the event loop.

    For more examples, see the [guide](https://modal.com/docs/guide/dicts-and-queues#modal-dicts).
    """

    _name: Optional[str] = None
    _metadata: Optional[api_pb2.DictMetadata] = None

    def __init__(self, data={}):
        """mdmd:hidden"""
        raise RuntimeError(
            "`Dict(...)` constructor is not allowed. Please use `Dict.from_name` or `Dict.ephemeral` instead"
        )

    @property
    def name(self) -> Optional[str]:
        return self._name

    def _hydrate_metadata(self, metadata: Optional[Message]):
        if metadata:
            assert isinstance(metadata, api_pb2.DictMetadata)
            self._metadata = metadata
            self._name = metadata.name

    def _get_metadata(self) -> api_pb2.DictMetadata:
        assert self._metadata
        return self._metadata

    @classmethod
    @asynccontextmanager
    async def ephemeral(
        cls: type["_Dict"],
        data: Optional[dict] = None,  # DEPRECATED
        client: Optional[_Client] = None,
        environment_name: Optional[str] = None,
        _heartbeat_sleep: float = EPHEMERAL_OBJECT_HEARTBEAT_SLEEP,
    ) -> AsyncIterator["_Dict"]:
        """Creates a new ephemeral Dict within a context manager:

        Usage:
        ```python
        from modal import Dict

        with Dict.ephemeral() as d:
            d["foo"] = "bar"
        ```

        ```python notest
        async with Dict.ephemeral() as d:
            await d.put.aio("foo", "bar")
        ```
        """
        if client is None:
            client = await _Client.from_env()
        if data:
            deprecation_warning(
                (2025, 5, 6),
                "Passing data to `modal.Dict.ephemeral` is deprecated and will stop working in a future release.",
            )
        serialized = _serialize_dict(data if data is not None else {})
        request = api_pb2.DictGetOrCreateRequest(
            object_creation_type=api_pb2.OBJECT_CREATION_TYPE_EPHEMERAL,
            environment_name=_get_environment_name(environment_name),
            data=serialized,
        )
        response = await retry_transient_errors(client.stub.DictGetOrCreate, request, total_timeout=10.0)
        async with TaskContext() as tc:
            request = api_pb2.DictHeartbeatRequest(dict_id=response.dict_id)
            tc.infinite_loop(lambda: client.stub.DictHeartbeat(request), sleep=_heartbeat_sleep)
            yield cls._new_hydrated(response.dict_id, client, response.metadata, is_another_app=True)

    @staticmethod
    def from_name(
        name: str,
        data: Optional[dict] = None,  # DEPRECATED, mdmd:line-hidden
        *,
        namespace=None,  # mdmd:line-hidden
        environment_name: Optional[str] = None,
        create_if_missing: bool = False,
    ) -> "_Dict":
        """Reference a named Dict, creating if necessary.

        This is a lazy method that defers hydrating the local
        object with metadata from Modal servers until the first
        time it is actually used.

        ```python
        d = modal.Dict.from_name("my-dict", create_if_missing=True)
        d[123] = 456
        ```
        """
        check_object_name(name, "Dict")
        warn_if_passing_namespace(namespace, "modal.Dict.from_name")

        if data:
            deprecation_warning(
                (2025, 5, 6),
                "Passing data to `modal.Dict.from_name` is deprecated and will stop working in a future release.",
            )

        async def _load(self: _Dict, resolver: Resolver, existing_object_id: Optional[str]):
            serialized = _serialize_dict(data if data is not None else {})
            req = api_pb2.DictGetOrCreateRequest(
                deployment_name=name,
                environment_name=_get_environment_name(environment_name, resolver),
                object_creation_type=(api_pb2.OBJECT_CREATION_TYPE_CREATE_IF_MISSING if create_if_missing else None),
                data=serialized,
            )
            response = await resolver.client.stub.DictGetOrCreate(req)
            logger.debug(f"Created dict with id {response.dict_id}")
            self._hydrate(response.dict_id, resolver.client, response.metadata)

        return _Dict._from_loader(_load, "Dict()", is_another_app=True, hydrate_lazily=True, name=name)

    @staticmethod
    async def lookup(
        name: str,
        data: Optional[dict] = None,
        namespace=None,  # mdmd:line-hidden
        client: Optional[_Client] = None,
        environment_name: Optional[str] = None,
        create_if_missing: bool = False,
    ) -> "_Dict":
        """mdmd:hidden
        Lookup a named Dict.

        DEPRECATED: This method is deprecated in favor of `modal.Dict.from_name`.

        In contrast to `modal.Dict.from_name`, this is an eager method
        that will hydrate the local object with metadata from Modal servers.

        ```python
        d = modal.Dict.from_name("my-dict")
        d["xyz"] = 123
        ```
        """
        deprecation_warning(
            (2025, 1, 27),
            "`modal.Dict.lookup` is deprecated and will be removed in a future release."
            " It can be replaced with `modal.Dict.from_name`."
            "\n\nSee https://modal.com/docs/guide/modal-1-0-migration for more information.",
        )
        warn_if_passing_namespace(namespace, "modal.Dict.lookup")
        obj = _Dict.from_name(
            name,
            data=data,
            environment_name=environment_name,
            create_if_missing=create_if_missing,
        )
        if client is None:
            client = await _Client.from_env()
        resolver = Resolver(client=client)
        await resolver.load(obj)
        return obj

    @staticmethod
    async def delete(
        name: str,
        *,
        client: Optional[_Client] = None,
        environment_name: Optional[str] = None,
    ):
        obj = await _Dict.from_name(name, environment_name=environment_name).hydrate(client)
        req = api_pb2.DictDeleteRequest(dict_id=obj.object_id)
        await retry_transient_errors(obj._client.stub.DictDelete, req)

    @live_method
    async def info(self) -> DictInfo:
        """Return information about the Dict object."""
        metadata = self._get_metadata()
        creation_info = metadata.creation_info
        return DictInfo(
            name=metadata.name or None,
            created_at=timestamp_to_localized_dt(creation_info.created_at),
            created_by=creation_info.created_by or None,
        )

    @live_method
    async def clear(self) -> None:
        """Remove all items from the Dict."""
        req = api_pb2.DictClearRequest(dict_id=self.object_id)
        await retry_transient_errors(self._client.stub.DictClear, req)

    @live_method
    async def get(self, key: Any, default: Optional[Any] = None) -> Any:
        """Get the value associated with a key.

        Returns `default` if key does not exist.
        """
        req = api_pb2.DictGetRequest(dict_id=self.object_id, key=serialize(key))
        resp = await retry_transient_errors(self._client.stub.DictGet, req)
        if not resp.found:
            return default
        return deserialize(resp.value, self._client)

    @live_method
    async def contains(self, key: Any) -> bool:
        """Return if a key is present."""
        req = api_pb2.DictContainsRequest(dict_id=self.object_id, key=serialize(key))
        resp = await retry_transient_errors(self._client.stub.DictContains, req)
        return resp.found

    @live_method
    async def len(self) -> int:
        """Return the length of the Dict.

        Note: This is an expensive operation and will return at most 100,000.
        """
        req = api_pb2.DictLenRequest(dict_id=self.object_id)
        resp = await retry_transient_errors(self._client.stub.DictLen, req)
        return resp.len

    @live_method
    async def __getitem__(self, key: Any) -> Any:
        """Get the value associated with a key.

        Note: this function will block the event loop when called in an async context.
        """
        NOT_FOUND = object()
        value = await self.get(key, NOT_FOUND)
        if value is NOT_FOUND:
            raise KeyError(f"{key} not in dict {self.object_id}")

        return value

    @live_method
    async def update(self, other: Optional[Mapping] = None, /, **kwargs) -> None:
        """Update the Dict with additional items."""
        # Support the Python dict.update API
        # https://docs.python.org/3/library/stdtypes.html#dict.update
        contents = {}
        if other:
            contents.update({k: other[k] for k in other.keys()})
        if kwargs:
            contents.update(kwargs)
        serialized = _serialize_dict(contents)
        req = api_pb2.DictUpdateRequest(dict_id=self.object_id, updates=serialized)
        try:
            await retry_transient_errors(self._client.stub.DictUpdate, req)
        except GRPCError as exc:
            if "status = '413'" in exc.message:
                raise RequestSizeError("Dict.update request is too large") from exc
            else:
                raise exc

    @live_method
    async def put(self, key: Any, value: Any, *, skip_if_exists: bool = False) -> bool:
        """Add a specific key-value pair to the Dict.

        Returns True if the key-value pair was added and False if it wasn't because the key already existed and
        `skip_if_exists` was set.
        """
        updates = {key: value}
        serialized = _serialize_dict(updates)
        req = api_pb2.DictUpdateRequest(dict_id=self.object_id, updates=serialized, if_not_exists=skip_if_exists)
        try:
            resp = await retry_transient_errors(self._client.stub.DictUpdate, req)
            return resp.created
        except GRPCError as exc:
            if "status = '413'" in exc.message:
                raise RequestSizeError("Dict.put request is too large") from exc
            else:
                raise exc

    @live_method
    async def __setitem__(self, key: Any, value: Any) -> None:
        """Set a specific key-value pair to the Dict.

        Note: this function will block the event loop when called in an async context.
        """
        return await self.put(key, value)

    @live_method
    async def pop(self, key: Any) -> Any:
        """Remove a key from the Dict, returning the value if it exists."""
        req = api_pb2.DictPopRequest(dict_id=self.object_id, key=serialize(key))
        resp = await retry_transient_errors(self._client.stub.DictPop, req)
        if not resp.found:
            raise KeyError(f"{key} not in dict {self.object_id}")
        return deserialize(resp.value, self._client)

    @live_method
    async def __delitem__(self, key: Any) -> Any:
        """Delete a key from the Dict.

        Note: this function will block the event loop when called in an async context.
        """
        return await self.pop(key)

    @live_method
    async def __contains__(self, key: Any) -> bool:
        """Return if a key is present.

        Note: this function will block the event loop when called in an async context.
        """
        return await self.contains(key)

    @live_method_gen
    async def keys(self) -> AsyncIterator[Any]:
        """Return an iterator over the keys in this Dict.

        Note that (unlike with Python dicts) the return value is a simple iterator,
        and results are unordered.
        """
        req = api_pb2.DictContentsRequest(dict_id=self.object_id, keys=True)
        async for resp in self._client.stub.DictContents.unary_stream(req):
            yield deserialize(resp.key, self._client)

    @live_method_gen
    async def values(self) -> AsyncIterator[Any]:
        """Return an iterator over the values in this Dict.

        Note that (unlike with Python dicts) the return value is a simple iterator,
        and results are unordered.
        """
        req = api_pb2.DictContentsRequest(dict_id=self.object_id, values=True)
        async for resp in self._client.stub.DictContents.unary_stream(req):
            yield deserialize(resp.value, self._client)

    @live_method_gen
    async def items(self) -> AsyncIterator[tuple[Any, Any]]:
        """Return an iterator over the (key, value) tuples in this Dict.

        Note that (unlike with Python dicts) the return value is a simple iterator,
        and results are unordered.
        """
        req = api_pb2.DictContentsRequest(dict_id=self.object_id, keys=True, values=True)
        async for resp in self._client.stub.DictContents.unary_stream(req):
            yield (deserialize(resp.key, self._client), deserialize(resp.value, self._client))


Dict = synchronize_api(_Dict)
