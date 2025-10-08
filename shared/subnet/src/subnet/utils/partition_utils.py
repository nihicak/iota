import asyncio
from datetime import datetime
import os
from typing import Literal

from common import settings as common_settings
from common.utils.exceptions import NanInfWarning
from common.utils.partitions import MinerPartition
from common.utils.s3_utils import filter_exceptions
from common.utils.partitions import get_start_and_end_indices
from pydantic import BaseModel
import torch


from loguru import logger
from subnet.utils.vector_utils import check_for_nans_and_infs
from subnet.utils.s3_torch import download_tensor


class MergingPartition(BaseModel):
    class Config:
        arbitrary_types_allowed = True

    new_partition: MinerPartition | None = None  # The new partition which the miner is building
    old_partition: MinerPartition | None = (
        None  # The old partition from which the miner needs to get the optimizer state from
    )
    pseudograds: list[
        torch.Tensor
    ] | None = None  # This is where the pseudograds from all the other miners (to be averaged) go
    old_optimizer_state: torch.Tensor | None = None  # This is where the optimizer state from the previous run goes
    new_optimizer_state: torch.Tensor | None = None  # This is where the optimizer state from the new run goes
    new_weights: torch.Tensor | None = None  # This is where the weights from the new run goes
    local_optimizer_state: torch.Tensor | None = None  # This is where the local optimizer state from the new run goes


def get_cosine_similarity(old_shard: torch.Tensor, new_shard: torch.Tensor) -> float:
    """Compute cosine similarity between two weight shards.

    Ensures both tensors are flattened, on CPU, and float32 for numerical stability.
    Returns a Python float for clean logging/formatting.
    """
    old_vec = old_shard.detach().to("cpu", dtype=torch.float32).flatten()
    new_vec = new_shard.detach().to("cpu", dtype=torch.float32).flatten()

    denom = old_vec.norm() * new_vec.norm()
    if denom == 0:
        return float("nan")

    similarity = torch.dot(old_vec, new_vec) / denom
    return float(similarity.item())


async def download_partition_optimizer(partition: MinerPartition) -> torch.Tensor:
    optimizer_state = await download_tensor(partition.optimizer_state_path, device="cpu", dtype=torch.bfloat16)
    return optimizer_state


async def download_merged_partitions(
    merged_partitions: list[MinerPartition],
    target_tensor: torch.Tensor,
    device: str,
    layer: int = None,
    num_partitions: int = None,
    download_type: Literal["weights", "optimizer_state", "local_optimizer_state"] = "weights",
) -> torch.Tensor | None:
    """Downloads the weights and optimizer state for a given list of partitions.

    Args:
        merged_partitions (list[MinerPartition]): The merged partitions to download weights and optimizer state for
        weights (torch.Tensor): The current weights to download weights and optimizer state for
        device (str): The device to download weights and optimizer state for

    Returns:
        torch.Tensor | None: The weights for the layer
    """
    logger.info(
        f"Downloading {download_type} for layer {layer}. Target tensor shape: {target_tensor.shape} with {num_partitions} partitions"
    )
    partition_download_error_counter: int = 0
    total_parts: int = len(merged_partitions) if merged_partitions else 0

    if not merged_partitions:
        logger.warning("No merged partitions found❗️")
        return None

    try:
        # Check for nans and infs in the weights
        check_for_nans_and_infs(target_tensor, "current weights", exception_type=NanInfWarning)

        total_tensors_downloaded: int = 0
        total_shard_elements_downloaded: int = 0

        BATCH_DOWNLOAD_SIZE = (
            common_settings.DOWNLOAD_BATCH_SIZE
            if len(merged_partitions) > common_settings.DOWNLOAD_BATCH_SIZE
            else len(merged_partitions)
        )

        logger.info(
            f"Starting batched download of {len(merged_partitions)} partitions in batches of {BATCH_DOWNLOAD_SIZE}"
        )

        # Batch the downloads
        for batch_start in range(0, len(merged_partitions), BATCH_DOWNLOAD_SIZE):
            batch_end = min(batch_start + BATCH_DOWNLOAD_SIZE, len(merged_partitions))
            batch_partitions = merged_partitions[batch_start:batch_end]

            logger.info(
                f"Processing batch {batch_start // BATCH_DOWNLOAD_SIZE + 1}/{(len(merged_partitions) + BATCH_DOWNLOAD_SIZE - 1) // BATCH_DOWNLOAD_SIZE}: partitions {batch_start} to {batch_end - 1}"
            )

            try:
                downloaded_tensors = await asyncio.gather(
                    *[
                        download_tensor(
                            (
                                partition.weight_path
                                if download_type == "weights"
                                else (
                                    partition.optimizer_state_path
                                    if download_type == "optimizer_state"
                                    else partition.local_optimizer_state_path
                                )
                            ),
                            device="cpu",
                            dtype=torch.bfloat16,
                        )
                        for partition in batch_partitions
                    ],
                    return_exceptions=True,
                )

                downloaded_tensors = filter_exceptions(downloaded_tensors)

                logger.info(
                    f"Percentage of downloaded tensors filtered: {len(downloaded_tensors) / len(batch_partitions) * 100}%"
                )

                # Process downloaded tensors and apply to model
                for tensor_shard, partition in zip(downloaded_tensors, batch_partitions):
                    # If either of the downloads fail, we want to discard the shards.
                    if isinstance(tensor_shard, Exception):
                        partition_download_error_counter += 1
                        continue

                    start_idx, end_idx = await get_start_and_end_indices(
                        tensor_length=target_tensor.numel(),
                        num_sections=num_partitions,
                        target_section=partition.chunk_number,
                    )

                    try:
                        cosine_similarity_tensor_shard = get_cosine_similarity(
                            old_shard=target_tensor[start_idx:end_idx].clone().to(device),
                            new_shard=tensor_shard,
                        )

                        logger.debug(
                            f"Cosine similarities for partition chunk {partition.chunk_number} for layer {layer} for {download_type} shard: {cosine_similarity_tensor_shard:.4f}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Error calculating cosine similarity for partition chunk {partition.chunk_number} for layer {layer} for {download_type} shard: {e}"
                        )

                    logger.debug(
                        f"{download_type} shard shape: {tensor_shard.shape}, expected start idx: {start_idx}, expected end idx: {end_idx}, range: {end_idx - start_idx}"
                    )
                    target_tensor[start_idx:end_idx] = tensor_shard

                    total_shard_elements_downloaded += tensor_shard.numel()
                    total_tensors_downloaded += 1

                    logger.debug(f"Applied partition {partition.chunk_number}: {download_type}[{start_idx}:{end_idx}]")

            except Exception as e:
                logger.warning(f"Error in batched download for batch {batch_start // BATCH_DOWNLOAD_SIZE + 1}: {e}")
                partition_download_error_counter += BATCH_DOWNLOAD_SIZE
                continue

        logger.debug(
            f"Downloaded {total_parts - partition_download_error_counter} / {total_parts} partitions inside download_partitions"
        )
        logger.info(
            f"download_partitions downloaded {total_shard_elements_downloaded} / {target_tensor.numel()} ({(total_shard_elements_downloaded/target_tensor.numel())*100}%) {download_type}"
        )

        # Cast the model weights and optimizer state to the correct device.
        target_tensor: torch.Tensor = target_tensor.to(device)
        return target_tensor

    except Exception:
        raise


def _model_suffix(hotkey: str, run_id: str, layer_idx: int) -> str:
    return f"{hotkey[:8]}_{run_id}_{layer_idx}"


def delete_saved_model_weights_and_optimizer_state(hotkey: str) -> None:
    """Deletes any saved model weights and optimizer state files for the given identity.

    This is useful to ensure we only keep a single snapshot per run/layer to avoid
    accumulating files across epochs.
    """
    try:
        if not os.path.exists("./weights"):
            return

        hotkey_prefix = hotkey[:8]
        for file in list(os.listdir("./weights")):
            # Delete any old snapshots for this hotkey regardless of prior run/layer to avoid disk bloat
            if file.startswith(f"current_model_weights_{hotkey_prefix}_") or file.startswith(
                f"current_model_optimizer_state_dict_{hotkey_prefix}_"
            ):
                try:
                    os.remove(f"./weights/{file}")
                except Exception as e:
                    logger.warning(f"Failed to delete file during cleanup {file}: {e}")
    except Exception as e:
        logger.warning(f"Error cleaning up saved model files: {e}")


def save_model_weights_and_optimizer_state(
    model_weights: torch.Tensor, optimizer_state_dict: dict, hotkey: str, run_id: str, layer_idx: int
):
    """Saves the model weights and optimizer state to the weights directory."""

    logger.debug(f"Saving model weights and optimizer state for hotkey {hotkey[:8]}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("./weights", exist_ok=True)

    model_suffix = _model_suffix(hotkey=hotkey, run_id=run_id, layer_idx=layer_idx)
    try:
        # Remove any previous snapshot for this exact run/layer to avoid disk bloat
        delete_saved_model_weights_and_optimizer_state(hotkey=hotkey)

        # First, save the new files
        new_weights_file = f"./weights/current_model_weights_{model_suffix}_{timestamp}.pt"
        new_optimizer_file = f"./weights/current_model_optimizer_state_dict_{model_suffix}_{timestamp}.pt"

        # Always persist on CPU to keep files portable and smaller
        cpu_weights = model_weights.detach().to("cpu") if isinstance(model_weights, torch.Tensor) else model_weights

        torch.save(cpu_weights, new_weights_file)
        torch.save(optimizer_state_dict, new_optimizer_file)

        # Verify the new files were saved successfully
        if not os.path.exists(new_weights_file):
            logger.warning(f"Model weights file not able to be saved: {new_weights_file}")
            # raise Exception(f"Model weights file not able to be saved: {new_weights_file}")
        if not os.path.exists(new_optimizer_file):
            logger.warning(f"Optimizer state file not able to be saved: {new_optimizer_file}")
            # raise Exception(f"Optimizer state file not able to be saved: {new_optimizer_file}")

        logger.debug(f"Model weights and optimizer state files after cleanup: {os.listdir('./weights')}")

    except Exception as e:
        logger.exception(f"Error saving model weights and optimizer state: {e}")


def load_model_weights(hotkey: str, run_id: str, layer_idx: int) -> torch.Tensor:
    """Loads the model weights from the weights directory and returns them on the CPU."""
    try:
        model_suffix = _model_suffix(hotkey=hotkey, run_id=run_id, layer_idx=layer_idx)

        logger.debug(f"All potential model weights files: {os.listdir('./weights')}")
        weight_path = [f for f in os.listdir("./weights") if f"current_model_weights_{model_suffix}" in f]

        if not weight_path:
            raise Exception(
                f"Model weights file not found for hotkey {hotkey[:8]} and run_id {run_id} and layer_idx {layer_idx}"
            )

        # Load the most recent snapshot (lexicographically by timestamp or by mtime)
        weight_path = sorted(weight_path, key=lambda f: os.path.getmtime(f"./weights/{f}"))
        latest_weight_file = weight_path[-1]
        logger.debug(f"Loading model weights from ./weights/{latest_weight_file}")
        model_weights = torch.load(f"./weights/{latest_weight_file}", map_location="cpu")

        if model_weights is None:
            raise Exception(f"Torch load failed for model weights file {latest_weight_file}")

        return model_weights

    except Exception as e:
        logger.error(f"Error loading model weights: {e}")
        return None


def load_model_weights_and_optimizer_state(hotkey: str, run_id: str, layer_idx: int) -> tuple[torch.Tensor, dict]:
    """Loads the model weights and optimizer state from the weights directory."""

    try:
        model_suffix = _model_suffix(hotkey=hotkey, run_id=run_id, layer_idx=layer_idx)
        weight_path = [f for f in os.listdir("./weights") if f"current_model_weights_{model_suffix}" in f]
        optimizer_state_path = [
            f for f in os.listdir("./weights") if f"current_model_optimizer_state_dict_{model_suffix}" in f
        ]
        if not weight_path or not optimizer_state_path:
            raise Exception(f"Model weights and optimizer state files not found for {model_suffix}")

        # Select the latest snapshot for both weights and optimizer state
        weight_path = sorted(weight_path, key=lambda f: os.path.getmtime(f"./weights/{f}"))
        optimizer_state_path = sorted(optimizer_state_path, key=lambda f: os.path.getmtime(f"./weights/{f}"))

        latest_weight_file = weight_path[-1]
        latest_optimizer_file = optimizer_state_path[-1]

        logger.debug(f"Loading model weights and optimizer state for hotkey {hotkey[:8]}")
        model_weights = torch.load(f"./weights/{latest_weight_file}", map_location="cpu")
        optimizer_state_dict = torch.load(f"./weights/{latest_optimizer_file}", map_location="cpu")

        return model_weights, optimizer_state_dict

    except Exception as e:
        if not os.path.exists("./weights"):
            logger.info("📂 Weights directory doesn't exist, creating it to save model weights and optimizer states!")
        else:
            logger.warning(
                f"❌ Attempted to load saved model weights and optimizer state, but it doesn't exist. If epoch is > 1, it's excepted that you have these saved. It's possible that you will diverge and lose incentive if you don't have these: {e}"
            )

        return None, None
