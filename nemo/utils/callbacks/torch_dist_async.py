from collections import deque
from logging import getLogger
from pathlib import Path
from time import time
from typing import Callable, List, NamedTuple, Optional, Tuple

import torch
from megatron.core.dist_checkpointing.mapping import ShardedStateDict
from megatron.core.dist_checkpointing.strategies.filesystem_async import FileSystemWriterAsync
from megatron.core.dist_checkpointing.strategies.state_dict_saver import (
    save_state_dict_async_finalize,
    save_state_dict_async_plan,
)
from megatron.core.dist_checkpointing.strategies.torch import (
    MCoreSavePlanner,
    TorchDistSaveShardedStrategy,
    _replace_state_dict_keys_with_sharded_keys,
    mcore_to_pyt_state_dict,
)
from torch import multiprocessing as mp

logger = getLogger(__name__)


class TorchDistAsyncSaveShardedStrategy(TorchDistSaveShardedStrategy):
    """Async save strategy for the PyT Distributed format. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_and_finalize_callbacks = None

    def save(self, sharded_state_dict: ShardedStateDict, checkpoint_dir: Path):
        """ Translates MCore ShardedTensors to PyT ShardedTensors and saves in PyT Distributed format.

        Args:
            sharded_state_dict (ShardedStateDict): sharded state dict to save
            checkpoint_dir (Path): checkpoint directory

        Returns: None
        """
        # Translate the state dict
        (sharded_state_dict, flat_mapping, rename_mapping,) = _replace_state_dict_keys_with_sharded_keys(
            sharded_state_dict, self.keep_only_main_replica
        )
        pyt_state_dict = mcore_to_pyt_state_dict(sharded_state_dict, False)
        # Use PyT saving mechanism
        writer = FileSystemWriterAsync(checkpoint_dir, thread_count=self.thread_count)

        save_state_dict_ret = save_state_dict_async_plan(
            pyt_state_dict,
            writer,
            None,
            planner=MCoreSavePlanner(dedup_replicated_tensors=not self.keep_only_main_replica),
        )
        self.save_and_finalize_callbacks = self._get_save_and_finalize_callbacks(writer, save_state_dict_ret)
        return self.save_and_finalize_callbacks

    def _get_save_and_finalize_callbacks(self, writer, save_state_dict_ret):
        save_fn_args = writer.get_save_function_and_args()
        if save_fn_args is None:  # this check can be removed with MCore v0.7
            save_fn_args = None, ()
        save_fn, save_args = save_fn_args

        def finalize_fn():
            save_state_dict_async_finalize(*save_state_dict_ret)
            torch.distributed.barrier()

        return save_fn, save_args, finalize_fn


class DistributedAsyncCaller:
    def __init__(self):
        self.process = None
        self.start_time = None

    def schedule_async_call(
        self, async_fn: Optional[Callable], save_args: Tuple,
    ):
        """ Spawn a saving process"""
        if async_fn is None:
            return  # nothing to do
        torch.cuda.synchronize()
        ctx = mp.get_context('fork')
        self.start_time = time()
        self.process = ctx.Process(target=async_fn, args=save_args,)
        self.process.start()

    def close(self):
        if self.process:
            self.process.join()

    def is_current_async_call_done(self, blocking=False, post_barrier=True):
        """ Check if async save is finished.

        Returns True for the first call of this method after an async save is done
        (or in progress if blocking=True).

        # We assume all ranks have AsyncWriter.
        """
        # The following takes the same overhead as torch.distributed.barrier (single integer all-reduce)
        if self.process is not None:
            is_alive = int(self.process.is_alive())
        else:
            is_alive = 0
        ten = torch.tensor([is_alive], dtype=torch.int, device=torch.cuda.current_device())
        logger.debug(f"rank: {torch.distributed.get_rank()}, {ten}")
        torch.distributed.all_reduce(ten)
        if ten[0] > 0 and not blocking:
            return False
        else:
            if self.process is not None:
                logger.debug(f"rank: {torch.distributed.get_rank()}, joining self.process")
                self.process.join()
                self.process = None

                logger.debug(
                    f"DistributedAsyncCaller: Async process join finished after {time() - self.start_time:.2f}s from forking"
                )
                self.start_time = None

            # This ensures no race condition on `if self.process` during next is_current_async_call_done call
            if post_barrier:
                torch.distributed.barrier()
            return True


class _AsyncCall(NamedTuple):
    idx: int
    async_caller: DistributedAsyncCaller
    finalize_callback: Callable[[], None]


class AsyncRequest(NamedTuple):
    async_fn: Callable
    async_fn_args: Tuple
    finalize_fn: Callable


class AsyncCallsQueue:
    def __init__(self, concurrent_calls: bool = True, max_unfinished_calls: int = 2):
        self.concurrent_calls = concurrent_calls
        self.max_unfinished_calls = max_unfinished_calls  # TODO: handle this

        self.async_calls = deque([])
        self.call_idx = -1

        if not self.concurrent_calls:
            raise NotImplementedError

    def schedule_async_call(self, async_fn: Optional[Callable], async_fn_args: Tuple, finalize_fn: Callable):
        if not self.concurrent_calls:
            raise NotImplementedError
        self.call_idx += 1
        async_caller = DistributedAsyncCaller()
        async_caller.schedule_async_call(async_fn, async_fn_args)
        self.async_calls.append(_AsyncCall(self.call_idx, async_caller, finalize_fn))
        return self.call_idx

    def maybe_finalize_async_calls(self, blocking=False) -> List[int]:
        """ Finalizes all available calls. """
        call_idx_finalized = []
        while self.async_calls:
            _this_blocking = blocking  # TODO: or len(self.async_calls) > self.max_unfinished_calls
            next_async_done = self.async_calls[0].async_caller.is_current_async_call_done(_this_blocking)
            if not next_async_done:
                break
            call_idx, _, finalize_fn = self.async_calls.popleft()
            # TODO: barrier here and check async calls consistency
            finalize_fn()
            call_idx_finalized.append(call_idx)
        return call_idx_finalized
