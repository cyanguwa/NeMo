import shutil
from contextlib import contextmanager
from time import time
from typing import Any, Callable, Dict, Optional, Tuple, cast

from lightning_fabric.plugins import CheckpointIO
from lightning_fabric.utilities.cloud_io import get_filesystem
from lightning_fabric.utilities.types import _PATH
from megatron.core import dist_checkpointing
from megatron.core.dist_checkpointing.strategies import tensorstore
from pytorch_lightning.plugins.io.wrapper import _WrappingCheckpointIO

from nemo.utils import logging
from nemo.utils.callbacks.torch_dist_async import AsyncCallsQueue, AsyncRequest, TorchDistAsyncSaveShardedStrategy


@contextmanager
def debug_time(name: str):
    start = time()
    try:
        yield
    finally:
        logging.debug(f'{name} took {time() - start:.3f}s')


class AsyncFinalizableCheckpointIO(_WrappingCheckpointIO):
    """
    Requires the underlying checkpoint_io.save_checkpoint to return save_fn, save_args, finalize_fn.
    """

    def __init__(self, checkpoint_io: Optional['CheckpointIO'] = None) -> None:
        super().__init__(checkpoint_io)
        self.async_calls_queue = AsyncCallsQueue()

    def save_checkpoint(self, checkpoint: Dict[str, Any], path: _PATH, storage_options: Optional[Any] = None) -> None:
        """
        Requires the underlying checkpoint_io.save_checkpoint to return save_fn, save_args, finalize_fn.

        Applies underlying checkpoint_io finalize callback first, then the external one (postfix order).
        """
        external_finalize_fn = storage_options.pop('finalize_fn', None)
        assert self.checkpoint_io is not None
        ret = self.checkpoint_io.save_checkpoint(checkpoint, path, storage_options)
        save_fn, save_args, finalize_fn = cast(AsyncRequest, ret)
        if external_finalize_fn is not None:
            finalize_fn = self._merge_finalize_callbacks(finalize_fn, external_finalize_fn)
        call_idx = self.async_calls_queue.schedule_async_call(save_fn, save_args, finalize_fn)
        logging.debug(f'Scheduled an async call #{call_idx}')

    def _merge_finalize_callbacks(self, *callbacks: Callable) -> Callable:
        def apply_all_finalizations():
            for callback_idx, callback in enumerate(callbacks):
                logging.debug(f'Applying finalize callback idx {callback_idx}')
                callback()

        return apply_all_finalizations

    @debug_time('AsyncFinalizableCheckpointIO.maybe_finalize_save_checkpoint')
    def maybe_finalize_save_checkpoint(self, blocking: bool = False):
        call_idx_finalized = self.async_calls_queue.maybe_finalize_async_calls(blocking)
        if call_idx_finalized:
            logging.debug(f'Finalized async calls: {[f"#{idx}" for idx in call_idx_finalized]}')
        return len(call_idx_finalized) > 0

    def on_train_batch_end(self, trainer: "pl.Trainer", *args, **kwargs) -> None:
        print('HERE' * 1000)

    def teardown(self) -> None:
        super().teardown()
        self.maybe_finalize_save_checkpoint(blocking=True)


class DistributedCheckpointIO(CheckpointIO):
    """ CheckpointIO for a distributed checkpoint format.

    Args:
        save_ckpt_format (str): Distributed checkpoint format to use for checkpoint saving.
    """

    def __init__(
        self, save_ckpt_format: str, async_save: bool = False,
    ):
        super().__init__()
        self.save_ckpt_format = save_ckpt_format
        self.async_save = async_save
        self.async_calls_queue = AsyncCallsQueue() if self.async_save else None
        self.save_sharded_strategy = self._determine_dist_ckpt_save_strategy()

    @debug_time('DistributedCheckpointIO.save_checkpoint')
    def save_checkpoint(
        self, checkpoint: Dict[str, Any], path: _PATH, storage_options: Optional[Any] = None
    ) -> Optional[Tuple]:
        """ Saves a distributed checkpoint. Creates the checkpoint root directory if doesn't exist.

        Args:
            checkpoint (Dict[str, Any]): sharded state dict to save
            path (_PATH): checkpoint directory
            storage_options (Any, optional): Optional parameters when saving the checkpoint
        """
        fs = get_filesystem(path)
        fs.makedirs(path, exist_ok=True)

        dist_checkpointing.save(
            sharded_state_dict=checkpoint, checkpoint_dir=path, sharded_strategy=self.save_sharded_strategy
        )
        if not self.async_save:
            return
        assert self.save_sharded_strategy.save_and_finalize_callbacks is not None
        save_fn, save_args, finalize_fn = self.save_sharded_strategy.save_and_finalize_callbacks
        self.save_sharded_strategy.save_and_finalize_callbacks = None
        return save_fn, save_args, finalize_fn

    @debug_time('DistributedCheckpointIO.load_checkpoint')
    def load_checkpoint(
        self, path: _PATH, map_location: Optional[Any] = None, sharded_state_dict: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """ Loads a distributed checkpoint.

        Args:
            path (_PATH): checkpoint directory
            map_location (Any, optional): required to be None in this implementation
            sharded_state_dict (Dict[str, Any], optional): state dict which
                defines the loading procedure for the distributed checkpoint.
                Defaults to None to comply with the CheckpointIO interface,
                but it's a required argument.

        Returns:
            Dist[str, Any]: loaded checkpoint.
        """
        if sharded_state_dict is None:
            raise ValueError('DistributedCheckpointIO requires passing sharded_state_dict argument to load_checkpoint')
        if map_location is not None:
            raise ValueError('DistributedCheckpointIO doesnt handle map_location argument')

        if self.save_ckpt_format == 'zarr':
            sharded_strategy = tensorstore.TensorStoreLoadShardedStrategy(load_directly_on_device=True)
        else:
            sharded_strategy = None

        return dist_checkpointing.load(
            sharded_state_dict=sharded_state_dict, checkpoint_dir=path, sharded_strategy=sharded_strategy
        )

    @debug_time('DistributedCheckpointIO.remove_checkpoint')
    def remove_checkpoint(self, path: _PATH) -> None:
        """ Remove a distributed checkpoint.

        Due to potentially large number of files, the implementation remove the whole directory at once.
        """
        shutil.rmtree(path, ignore_errors=True)

    def _determine_dist_ckpt_save_strategy(self):
        """ Determine the saving strategy based on storage config.

        For now only decides the checkpoint format.
        """
        save_strategy = (self.save_ckpt_format, 1)
        if self.async_save:
            if save_strategy[0] != 'torch_dist':
                raise ValueError('Async dist-ckpt save supported only for torch_dist format')
            save_strategy = TorchDistAsyncSaveShardedStrategy('torch_dist', 1)

        logging.info(f'Using {save_strategy} dist-ckpt save strategy.')
        return save_strategy
