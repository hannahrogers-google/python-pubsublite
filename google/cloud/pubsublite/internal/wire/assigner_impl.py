import asyncio
from typing import Optional, Set

from absl import logging
from google.cloud.pubsublite.internal.wire.assigner import Assigner
from google.cloud.pubsublite.internal.wire.retrying_connection import RetryingConnection, ConnectionFactory
from google.api_core.exceptions import FailedPrecondition, GoogleAPICallError
from google.cloud.pubsublite.internal.wire.connection_reinitializer import ConnectionReinitializer
from google.cloud.pubsublite.internal.wire.connection import Connection
from google.cloud.pubsublite.partition import Partition
from google.cloud.pubsublite_v1.types import PartitionAssignmentRequest, PartitionAssignment, \
  InitialPartitionAssignmentRequest, PartitionAssignmentAck

# Maximum bytes per batch at 3.5 MiB to avoid GRPC limit of 4 MiB
_MAX_BYTES = int(3.5 * 1024 * 1024)

# Maximum messages per batch at 1000
_MAX_MESSAGES = 1000


class AssignerImpl(Assigner, ConnectionReinitializer[PartitionAssignmentRequest, PartitionAssignment]):
  _initial: InitialPartitionAssignmentRequest
  _connection: RetryingConnection[PartitionAssignmentRequest, PartitionAssignment]

  _outstanding_assignment: bool

  _receiver: Optional[asyncio.Future]

  # A queue that may only hold one element with the next assignment.
  _new_assignment: 'asyncio.Queue[Set[Partition]]'

  def __init__(self, initial: InitialPartitionAssignmentRequest,
               factory: ConnectionFactory[PartitionAssignmentRequest, PartitionAssignment]):
    self._initial = initial
    self._connection = RetryingConnection(factory, self)
    self._outstanding_assignment = False
    self._receiver = None
    self._new_assignment = asyncio.Queue(maxsize=1)

  async def __aenter__(self):
    await self._connection.__aenter__()

  def _start_receiver(self):
    assert self._receiver is None
    self._receiver = asyncio.ensure_future(self._receive_loop())

  async def _stop_receiver(self):
    if self._receiver:
      self._receiver.cancel()
      await self._receiver
      self._receiver = None

  async def _receive_loop(self):
    try:
      while True:
        response = await self._connection.read()
        if self._outstanding_assignment or not self._new_assignment.empty():
          self._connection.fail(FailedPrecondition(
            "Received a duplicate assignment on the stream while one was outstanding."))
          return
        self._outstanding_assignment = True
        partitions = set()
        for partition in response.partitions:
          partitions.add(Partition(partition))
        self._new_assignment.put_nowait(partitions)
    except asyncio.CancelledError:
      return

  async def __aexit__(self, exc_type, exc_val, exc_tb):
    await self._connection.__aexit__(exc_type, exc_val, exc_tb)

  async def reinitialize(self, connection: Connection[PartitionAssignmentRequest, PartitionAssignment]):
    self._outstanding_assignment = False
    while not self._new_assignment.empty():
      self._new_assignment.get_nowait()
    await self._stop_receiver()
    await connection.write(PartitionAssignmentRequest(initial=self._initial))
    self._start_receiver()

  async def get_assignment(self) -> Set[Partition]:
    if self._outstanding_assignment:
      try:
        await self._connection.write(PartitionAssignmentRequest(ack=PartitionAssignmentAck()))
        self._outstanding_assignment = False
      except GoogleAPICallError as e:
        # If there is a failure to ack, keep going. The stream likely restarted.
        logging.debug(f"Assignment ack attempt failed due to stream failure: {e}")
    return await self._connection.await_unless_failed(self._new_assignment.get())
