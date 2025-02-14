# Copyright 2020 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helpers for applying Google Cloud Firestore changes in a transaction."""


import asyncio
import random

from google.api_core import gapic_v1
from google.api_core import retry_async as retries

from google.cloud.firestore_v1.base_transaction import (
    _BaseTransactional,
    BaseTransaction,
    MAX_ATTEMPTS,
    _CANT_BEGIN,
    _CANT_ROLLBACK,
    _CANT_COMMIT,
    _WRITE_READ_ONLY,
    _INITIAL_SLEEP,
    _MAX_SLEEP,
    _MULTIPLIER,
    _EXCEED_ATTEMPTS_TEMPLATE,
)

from google.api_core import exceptions
from google.cloud.firestore_v1 import async_batch
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1 import types

from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.async_document import DocumentSnapshot
from google.cloud.firestore_v1.async_query import AsyncQuery
from typing import Any, AsyncGenerator, Callable, Coroutine, Optional, Union

# Types needed only for Type Hints
from google.cloud.firestore_v1.client import Client


class AsyncTransaction(async_batch.AsyncWriteBatch, BaseTransaction):
    """Accumulate read-and-write operations to be sent in a transaction.

    Args:
        client (:class:`~google.cloud.firestore_v1.client.Client`):
            The client that created this transaction.
        max_attempts (Optional[int]): The maximum number of attempts for
            the transaction (i.e. allowing retries). Defaults to
            :attr:`~google.cloud.firestore_v1.transaction.MAX_ATTEMPTS`.
        read_only (Optional[bool]): Flag indicating if the transaction
            should be read-only or should allow writes. Defaults to
            :data:`False`.
    """

    def __init__(self, client, max_attempts=MAX_ATTEMPTS, read_only=False) -> None:
        super(AsyncTransaction, self).__init__(client)
        BaseTransaction.__init__(self, max_attempts, read_only)

    def _add_write_pbs(self, write_pbs: list) -> None:
        """Add `Write`` protobufs to this transaction.

        Args:
            write_pbs (List[google.cloud.proto.firestore.v1.\
                write.Write]): A list of write protobufs to be added.

        Raises:
            ValueError: If this transaction is read-only.
        """
        if self._read_only:
            raise ValueError(_WRITE_READ_ONLY)

        super(AsyncTransaction, self)._add_write_pbs(write_pbs)

    async def _begin(self, retry_id: Optional[bytes] = None) -> None:
        """Begin the transaction.

        Args:
            retry_id (Optional[bytes]): Transaction ID of a transaction to be
                retried.

        Raises:
            ValueError: If the current transaction has already begun.
        """
        if self.in_progress:
            msg = _CANT_BEGIN.format(self._id)
            raise ValueError(msg)

        transaction_response = await self._client._firestore_api.begin_transaction(
            request={
                "database": self._client._database_string,
                "options": self._options_protobuf(retry_id),
            },
            metadata=self._client._rpc_metadata,
        )
        self._id = transaction_response.transaction

    async def _rollback(self) -> None:
        """Roll back the transaction.

        Raises:
            ValueError: If no transaction is in progress.
            google.api_core.exceptions.GoogleAPICallError: If the rollback fails.
        """
        if not self.in_progress:
            raise ValueError(_CANT_ROLLBACK)

        try:
            # NOTE: The response is just ``google.protobuf.Empty``.
            await self._client._firestore_api.rollback(
                request={
                    "database": self._client._database_string,
                    "transaction": self._id,
                },
                metadata=self._client._rpc_metadata,
            )
        finally:
            # clean up, even if rollback fails
            self._clean_up()

    async def _commit(self) -> list:
        """Transactionally commit the changes accumulated.

        Returns:
            List[:class:`google.cloud.proto.firestore.v1.write.WriteResult`, ...]:
            The write results corresponding to the changes committed, returned
            in the same order as the changes were applied to this transaction.
            A write result contains an ``update_time`` field.

        Raises:
            ValueError: If no transaction is in progress.
        """
        if not self.in_progress:
            raise ValueError(_CANT_COMMIT)

        commit_response = await _commit_with_retry(
            self._client, self._write_pbs, self._id
        )

        self._clean_up()
        return list(commit_response.write_results)

    async def get_all(
        self,
        references: list[AsyncDocumentReference],
        retry: retries.AsyncRetry = gapic_v1.method.DEFAULT,
        timeout: Optional[float] = None,
    ) -> AsyncGenerator[DocumentSnapshot, Any]:
        """Retrieves multiple documents from Firestore.

        Args:
            references (List[.AsyncDocumentReference, ...]): Iterable of document
                references to be retrieved.
            retry (google.api_core.retry.Retry): Designation of what errors, if any,
                should be retried.  Defaults to a system-specified policy.
            timeout (float): The timeout for this request.  Defaults to a
                system-specified value.

        Yields:
            .DocumentSnapshot: The next document snapshot that fulfills the
            query, or :data:`None` if the document does not exist.
        """
        kwargs = _helpers.make_retry_timeout_kwargs(retry, timeout)
        return await self._client.get_all(references, transaction=self, **kwargs)

    async def get(
        self,
        ref_or_query: Union[AsyncDocumentReference, AsyncQuery],
        retry: retries.AsyncRetry = gapic_v1.method.DEFAULT,
        timeout: Optional[float] = None,
    ) -> AsyncGenerator[DocumentSnapshot, Any]:
        """
        Retrieve a document or a query result from the database.

        Args:
            ref_or_query The document references or query object to return.
            retry (google.api_core.retry.Retry): Designation of what errors, if any,
                should be retried.  Defaults to a system-specified policy.
            timeout (float): The timeout for this request.  Defaults to a
                system-specified value.

        Yields:
            .DocumentSnapshot: The next document snapshot that fulfills the
            query, or :data:`None` if the document does not exist.
        """
        kwargs = _helpers.make_retry_timeout_kwargs(retry, timeout)
        if isinstance(ref_or_query, AsyncDocumentReference):
            return await self._client.get_all(
                [ref_or_query], transaction=self, **kwargs
            )
        elif isinstance(ref_or_query, AsyncQuery):
            return await ref_or_query.stream(transaction=self, **kwargs)
        else:
            raise ValueError(
                'Value for argument "ref_or_query" must be a AsyncDocumentReference or a AsyncQuery.'
            )


class _AsyncTransactional(_BaseTransactional):
    """Provide a callable object to use as a transactional decorater.

    This is surfaced via
    :func:`~google.cloud.firestore_v1.async_transaction.transactional`.

    Args:
        to_wrap (Coroutine[[:class:`~google.cloud.firestore_v1.async_transaction.AsyncTransaction`, ...], Any]):
            A coroutine that should be run (and retried) in a transaction.
    """

    def __init__(self, to_wrap) -> None:
        super(_AsyncTransactional, self).__init__(to_wrap)

    async def _pre_commit(
        self, transaction: AsyncTransaction, *args, **kwargs
    ) -> Coroutine:
        """Begin transaction and call the wrapped coroutine.

        Args:
            transaction
                (:class:`~google.cloud.firestore_v1.async_transaction.AsyncTransaction`):
                A transaction to execute the coroutine within.
            args (Tuple[Any, ...]): The extra positional arguments to pass
                along to the wrapped coroutine.
            kwargs (Dict[str, Any]): The extra keyword arguments to pass
                along to the wrapped coroutine.

        Returns:
            Any: result of the wrapped coroutine.

        Raises:
            Exception: Any failure caused by ``to_wrap``.
        """
        # Force the ``transaction`` to be not "in progress".
        transaction._clean_up()
        await transaction._begin(retry_id=self.retry_id)

        # Update the stored transaction IDs.
        self.current_id = transaction._id
        if self.retry_id is None:
            self.retry_id = self.current_id
        return await self.to_wrap(transaction, *args, **kwargs)

    async def __call__(self, transaction, *args, **kwargs):
        """Execute the wrapped callable within a transaction.

        Args:
            transaction
                (:class:`~google.cloud.firestore_v1.transaction.Transaction`):
                A transaction to execute the callable within.
            args (Tuple[Any, ...]): The extra positional arguments to pass
                along to the wrapped callable.
            kwargs (Dict[str, Any]): The extra keyword arguments to pass
                along to the wrapped callable.

        Returns:
            Any: The result of the wrapped callable.

        Raises:
            ValueError: If the transaction does not succeed in
                ``max_attempts``.
        """
        self._reset()
        retryable_exceptions = (
            (exceptions.Aborted) if not transaction._read_only else ()
        )
        last_exc = None

        try:
            for attempt in range(transaction._max_attempts):
                result = await self._pre_commit(transaction, *args, **kwargs)
                try:
                    await transaction._commit()
                    return result
                except retryable_exceptions as exc:
                    last_exc = exc
                # Retry attempts that result in retryable exceptions
                # Subsequent requests will use the failed transaction ID as part of
                # the ``BeginTransactionRequest`` when restarting this transaction
                # (via ``options.retry_transaction``). This preserves the "spot in
                # line" of the transaction, so exponential backoff is not required
                # in this case.
            # retries exhausted
            # wrap the last exception in a ValueError before raising
            msg = _EXCEED_ATTEMPTS_TEMPLATE.format(transaction._max_attempts)
            raise ValueError(msg) from last_exc

        except BaseException:
            # rollback the transaction on any error
            # errors raised during _rollback will be chained to the original error through __context__
            await transaction._rollback()
            raise


def async_transactional(
    to_wrap: Callable[[AsyncTransaction], Any]
) -> _AsyncTransactional:
    """Decorate a callable so that it runs in a transaction.

    Args:
        to_wrap
            (Callable[[:class:`~google.cloud.firestore_v1.transaction.Transaction`, ...], Any]):
            A callable that should be run (and retried) in a transaction.

    Returns:
        Callable[[:class:`~google.cloud.firestore_v1.transaction.Transaction`, ...], Any]:
        the wrapped callable.
    """
    return _AsyncTransactional(to_wrap)


# TODO(crwilcox): this was 'coroutine' from pytype merge-pyi...
async def _commit_with_retry(
    client: Client, write_pbs: list, transaction_id: bytes
) -> types.CommitResponse:
    """Call ``Commit`` on the GAPIC client with retry / sleep.

    Retries the ``Commit`` RPC on Unavailable. Usually this RPC-level
    retry is handled by the underlying GAPICd client, but in this case it
    doesn't because ``Commit`` is not always idempotent. But here we know it
    is "idempotent"-like because it has a transaction ID. We also need to do
    our own retry to special-case the ``INVALID_ARGUMENT`` error.

    Args:
        client (:class:`~google.cloud.firestore_v1.client.Client`):
            A client with GAPIC client and configuration details.
        write_pbs (List[:class:`google.cloud.proto.firestore.v1.write.Write`, ...]):
            A ``Write`` protobuf instance to be committed.
        transaction_id (bytes):
            ID of an existing transaction that this commit will run in.

    Returns:
        :class:`google.cloud.firestore_v1.types.CommitResponse`:
        The protobuf response from ``Commit``.

    Raises:
        ~google.api_core.exceptions.GoogleAPICallError: If a non-retryable
            exception is encountered.
    """
    current_sleep = _INITIAL_SLEEP
    while True:
        try:
            return await client._firestore_api.commit(
                request={
                    "database": client._database_string,
                    "writes": write_pbs,
                    "transaction": transaction_id,
                },
                metadata=client._rpc_metadata,
            )
        except exceptions.ServiceUnavailable:
            # Retry
            pass

        current_sleep = await _sleep(current_sleep)


async def _sleep(
    current_sleep: float, max_sleep: float = _MAX_SLEEP, multiplier: float = _MULTIPLIER
) -> float:
    """Sleep and produce a new sleep time.

    .. _Exponential Backoff And Jitter: https://www.awsarchitectureblog.com/\
                                        2015/03/backoff.html

    Select a duration between zero and ``current_sleep``. It might seem
    counterintuitive to have so much jitter, but
    `Exponential Backoff And Jitter`_ argues that "full jitter" is
    the best strategy.

    Args:
        current_sleep (float): The current "max" for sleep interval.
        max_sleep (Optional[float]): Eventual "max" sleep time
        multiplier (Optional[float]): Multiplier for exponential backoff.

    Returns:
        float: Newly doubled ``current_sleep`` or ``max_sleep`` (whichever
        is smaller)
    """
    actual_sleep = random.uniform(0.0, current_sleep)
    await asyncio.sleep(actual_sleep)
    return min(multiplier * current_sleep, max_sleep)
