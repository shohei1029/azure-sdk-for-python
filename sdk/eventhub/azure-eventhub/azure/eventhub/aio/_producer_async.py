# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
import uuid
import asyncio
import logging
from typing import Iterable, Union, Optional, Any, AnyStr, List, TYPE_CHECKING
import time

from uamqp import types, constants, errors
from uamqp import SendClientAsync

from azure.core.tracing import AbstractSpan

from .._common import EventData, EventDataBatch
from ..exceptions import _error_handler, OperationTimeoutError
from .._producer import _set_partition_key, _set_trace_message
from .._utils import (
    create_properties,
    set_message_partition_key,
    trace_message,
    send_context_manager,
    transform_outbound_single_message,
)
from .._constants import TIMEOUT_SYMBOL
from ..amqp import AmqpAnnotatedMessage
from ._client_base_async import ConsumerProducerMixin
from ._async_utils import get_dict_with_loop_if_needed

if TYPE_CHECKING:
    from uamqp.authentication import JWTTokenAsync  # pylint: disable=ungrouped-imports
    from ._producer_client_async import EventHubProducerClient

_LOGGER = logging.getLogger(__name__)


class EventHubProducer(
    ConsumerProducerMixin
):  # pylint: disable=too-many-instance-attributes
    """A producer responsible for transmitting batches of EventData to a specific Event Hub.

    Depending on the options specified at creation, the producer may
    be created to allow event data to be automatically routed to an available partition or specific
    to a partition.

    Please use the method `_create_producer` on `EventHubClient` for creating `EventHubProducer`.

    :param client: The parent EventHubProducerClient.
    :type client: ~azure.eventhub.aio.EventHubProducerClient
    :param target: The URI of the EventHub to send to.
    :type target: str
    :keyword str partition: The specific partition ID to send to. Default is `None`, in which case the service
     will assign to all partitions using round-robin.
    :keyword float send_timeout: The timeout in seconds for an individual event to be sent from the time that it is
     queued. Default value is 60 seconds. If set to 0, there will be no timeout.
    :keyword int keep_alive: The time interval in seconds between pinging the connection to keep it alive during
     periods of inactivity. The default value is `None`, i.e. no keep alive pings.
    :keyword bool auto_reconnect: Whether to automatically reconnect the producer if a retryable error occurs.
     Default value is `True`.
    """

    def __init__(self, client: "EventHubProducerClient", target: str, **kwargs) -> None:
        super().__init__()
        partition = kwargs.get("partition", None)
        send_timeout = kwargs.get("send_timeout", 60)
        keep_alive = kwargs.get("keep_alive", None)
        auto_reconnect = kwargs.get("auto_reconnect", True)
        idle_timeout = kwargs.get("idle_timeout", None)

        self.running = False
        self.closed = False

        self._internal_kwargs = get_dict_with_loop_if_needed(kwargs.get("loop", None))
        self._max_message_size_on_link = None
        self._client = client
        self._target = target
        self._partition = partition
        self._keep_alive = keep_alive
        self._auto_reconnect = auto_reconnect
        self._timeout = send_timeout
        self._idle_timeout = (idle_timeout * 1000) if idle_timeout else None
        self._retry_policy = errors.ErrorPolicy(
            max_retries=self._client._config.max_retries,
            on_error=_error_handler,  # pylint:disable=protected-access
        )
        self._reconnect_backoff = 1
        self._name = "EHProducer-{}".format(uuid.uuid4())
        self._unsent_events = []  # type: List[Any]
        self._error = None
        if partition:
            self._target += "/Partitions/" + partition
            self._name += "-partition{}".format(partition)
        self._handler = None  # type: Optional[SendClientAsync]
        self._outcome = None  # type: Optional[constants.MessageSendResult]
        self._condition = None  # type: Optional[Exception]
        self._lock = asyncio.Lock(**self._internal_kwargs)
        self._link_properties = {
            types.AMQPSymbol(TIMEOUT_SYMBOL): types.AMQPLong(int(self._timeout * 1000))
        }

    def _create_handler(self, auth: "JWTTokenAsync") -> None:
        self._handler = SendClientAsync(
            self._target,
            auth=auth,
            debug=self._client._config.network_tracing,  # pylint:disable=protected-access
            msg_timeout=self._timeout * 1000,
            idle_timeout=self._idle_timeout,
            error_policy=self._retry_policy,
            keep_alive_interval=self._keep_alive,
            client_name=self._name,
            link_properties=self._link_properties,
            properties=create_properties(
                self._client._config.user_agent  # pylint:disable=protected-access
            ),
            **self._internal_kwargs
        )

    async def _open_with_retry(self) -> Any:
        return await self._do_retryable_operation(
            self._open, operation_need_param=False
        )

    def _set_msg_timeout(
        self, timeout_time: Optional[float], last_exception: Optional[Exception]
    ) -> None:
        if not timeout_time:
            return
        remaining_time = timeout_time - time.time()
        if remaining_time <= 0.0:
            if last_exception:
                error = last_exception
            else:
                error = OperationTimeoutError("Send operation timed out")
            _LOGGER.info("%r send operation timed out. (%r)", self._name, error)
            raise error
        self._handler._msg_timeout = remaining_time * 1000  # type: ignore  # pylint: disable=protected-access

    async def _send_event_data(
        self,
        timeout_time: Optional[float] = None,
        last_exception: Optional[Exception] = None,
    ) -> None:
        # TODO: Correct uAMQP type hints
        if self._unsent_events:
            await self._open()
            self._set_msg_timeout(timeout_time, last_exception)
            self._handler.queue_message(*self._unsent_events)  # type: ignore
            await self._handler.wait_async()  # type: ignore
            self._unsent_events = self._handler.pending_messages  # type: ignore
            if self._outcome != constants.MessageSendResult.Ok:
                if self._outcome == constants.MessageSendResult.Timeout:
                    self._condition = OperationTimeoutError("Send operation timed out")
                if self._condition:
                    raise self._condition

    async def _send_event_data_with_retry(
        self, timeout: Optional[float] = None
    ) -> None:
        await self._do_retryable_operation(self._send_event_data, timeout=timeout)

    def _on_outcome(
        self, outcome: constants.MessageSendResult, condition: Optional[Exception]
    ) -> None:
        """
        Called when the outcome is received for a delivery.

        :param outcome: The outcome of the message delivery - success or failure.
        :type outcome: ~uamqp.constants.MessageSendResult
        :param condition: Detail information of the outcome.

        """
        self._outcome = outcome
        self._condition = condition

    def _wrap_eventdata(
        self,
        event_data: Union[
            EventData, AmqpAnnotatedMessage, EventDataBatch, Iterable[EventData]
        ],
        span: Optional[AbstractSpan],
        partition_key: Optional[AnyStr],
    ) -> Union[EventData, EventDataBatch]:
        if isinstance(event_data, (EventData, AmqpAnnotatedMessage)):
            outgoing_event_data = transform_outbound_single_message(
                event_data, EventData
            )
            if partition_key:
                set_message_partition_key(outgoing_event_data.message, partition_key)
            wrapper_event_data = outgoing_event_data
            trace_message(wrapper_event_data, span)
        else:
            if isinstance(
                event_data, EventDataBatch
            ):  # The partition_key in the param will be omitted.
                if (
                    partition_key
                    and partition_key
                    != event_data._partition_key  # pylint: disable=protected-access
                ):
                    raise ValueError(
                        "The partition_key does not match the one of the EventDataBatch"
                    )
                for (
                    event
                ) in event_data.message._body_gen:  # pylint: disable=protected-access
                    trace_message(event, span)
                wrapper_event_data = event_data  # type:ignore
            else:
                if partition_key:
                    event_data = _set_partition_key(event_data, partition_key)
                event_data = _set_trace_message(event_data, span)
                wrapper_event_data = EventDataBatch._from_batch(event_data, partition_key)  # type: ignore  # pylint: disable=protected-access
        wrapper_event_data.message.on_send_complete = self._on_outcome
        return wrapper_event_data

    async def send(
        self,
        event_data: Union[
            EventData, AmqpAnnotatedMessage, EventDataBatch, Iterable[EventData]
        ],
        *,
        partition_key: Optional[AnyStr] = None,
        timeout: Optional[float] = None
    ) -> None:
        """
        Sends an event data and blocks until acknowledgement is
        received or operation times out.

        :param event_data: The event to be sent. It can be an EventData object, or iterable of EventData objects
        :type event_data: ~azure.eventhub.common.EventData, Iterator, Generator, list
        :param partition_key: With the given partition_key, event data will land to
         a particular partition of the Event Hub decided by the service. partition_key
         could be omitted if event_data is of type ~azure.eventhub.EventDataBatch.
        :type partition_key: str
        :param timeout: The maximum wait time to send the event data.
         If not specified, the default wait time specified when the producer was created will be used.
        :type timeout: float

        :raises: ~azure.eventhub.exceptions.AuthenticationError,
                 ~azure.eventhub.exceptions.ConnectError,
                 ~azure.eventhub.exceptions.ConnectionLostError,
                 ~azure.eventhub.exceptions.EventDataError,
                 ~azure.eventhub.exceptions.EventDataSendError,
                 ~azure.eventhub.exceptions.EventHubError
        :return: None
        :rtype: None
        """
        # Tracing code
        async with self._lock:
            with send_context_manager() as child:
                self._check_closed()
                wrapper_event_data = self._wrap_eventdata(
                    event_data, child, partition_key
                )
                self._unsent_events = [wrapper_event_data.message]

                if child:
                    self._client._add_span_request_attributes(  # pylint: disable=protected-access
                        child
                    )

                await self._send_event_data_with_retry(
                    timeout=timeout
                )  # pylint:disable=unexpected-keyword-arg

    async def close(self) -> None:
        """
        Close down the handler. If the handler has already closed,
        this will be a no op.
        """
        async with self._lock:
            await super(EventHubProducer, self).close()
