import gc
from loguru import logger

from typing import Tuple, Dict, Union
from transformers import AutoModelForCausalLM, AutoConfig
import torch
import numpy as np


def load_model_from_hf(model_name: str, pretrained: bool, device: Union[str, torch.device]):
    """
    Load a model from HuggingFace.

    If pretrained is True, the model is loaded from HuggingFace.
    If pretrained is False, the model is loaded from the config file.
    """
    if pretrained:
        model = AutoModelForCausalLM.from_pretrained(model_name)
    else:
        model = AutoModelForCausalLM.from_config(AutoConfig.from_pretrained(model_name))
    return model.to(device)


def compute_loss(
    mock: bool,
    logits: torch.Tensor,
    targets: torch.Tensor,
    vocab_size: int,
    pad_token_id: int,
    pack: bool,
):
    """
    Compute the loss for the given logits and batch.

    Args:
        mock (bool): Whether to use mock mode
        logits (torch.Tensor): Model output logits of shape (batch_size, seq_len, vocab_size)
        targets (torch.Tensor): Input labels of shape (batch_size, seq_len)
        vocab_size (int): Vocabulary size
        pad_token_id (int): Padding token id
        pack (bool): Whether sample packing is used

    Returns:
        torch.Tensor: Computed loss
    """
    if mock:
        loss = logits.sum()
        return loss

    # Shift the logits and labels to compute the loss.
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = targets[..., 1:].contiguous()

    if not pack:
        # If sample packing is not used,
        # create a mask to indicate location of PAD tokens.
        # Note, PAD tokens are always set to EOS tokens,
        # For this reason, we want to ignore all but the
        # first EOS token (the real one)
        pad_mask = shift_labels == pad_token_id
        zeros = torch.zeros_like(shift_labels[..., :1])
        pad_mask = torch.cat((zeros, pad_mask[..., :-1]), dim=-1).bool()
        # Set all the padded labels to -100, since the
        # CrossEntropyLoss ignores -100 labels by default.
        shift_labels[pad_mask] = -100

    # Flatten the tokens
    loss_fct = torch.nn.CrossEntropyLoss()
    shift_logits = shift_logits.view(-1, vocab_size)
    shift_labels = shift_labels.view(-1)
    loss = loss_fct(shift_logits, shift_labels)

    return loss


def _quantize_tensor_uint8_uniform(x: torch.Tensor, clip_factor: float = 6.0) -> torch.Tensor:
    """
    Uniform INT8 quantisation with (µ ± kσ) clipping, per-tensor.

    Args:
        x (torch.Tensor): Input tensor to quantize
        clip_factor (float, optional): Factor for clipping range as multiples of standard deviation.
            Defaults to 6.0.

    Returns:
        tuple: Contains:
                - q_codes (torch.uint8): Integer codes (0-255)
                - meta (dict): Dictionary containing:
                    - 'scale' (torch.Tensor): Bucket width (float32)
                    - 'offset' (torch.Tensor): Lower bound of bucket 0 (float32)

    """

    # Remove casting to float32 if you want to use the same dtype as the input
    x = x.detach().to(torch.float32)
    mu = x.mean()
    # Using unbiased=False (Bessel's correction disabled) to match the population standard deviation
    # rather than sample standard deviation. This is appropriate for quantization since we're
    # interested in the actual distribution of all values in the tensor, not estimating the std
    # of a larger population from which x is a sample.
    std = x.std(unbiased=False)
    lo = mu - clip_factor * std
    hi = mu + clip_factor * std

    # 1. clip
    x_clipped = torch.clamp(x, lo, hi)

    # 2. uniform buckets
    scale = (hi - lo) / 255.0  # width of a bucket
    codes = torch.round((x_clipped - lo) / scale).to(torch.uint8)

    # meta = {"scale": scale, "offset": lo}

    # codes = torch.cat((codes, meta), dim=0)
    packed_tensor = pack_int8_with_meta(codes, scale, lo)

    return packed_tensor


def quantize_activations_uint8_uniform(activations: torch.Tensor, clip_factor: float = 6.0) -> torch.Tensor:
    """
    Quantize a tensor of activations to uint8 uniform. Given an activation tensor
    of shape (batch_size, seq_len, hidden_size), it will return a tensor of the same shape
    but with quantized and packed uint8 values.

    Args:
        activations (torch.Tensor): Tensor of activations to quantize
                                    shape: (batch_size, seq_len, hidden_size)
        clip_factor (float, optional): Factor for clipping range as multiples of standard deviation.
            Defaults to 6.0.

    Returns:
        torch.Tensor: Tensor of quantized activations
                        shape: (batch_size, seq_len, hidden_size)

    TODO: Vectorize this function and remove the loop. (pytorch vectorization or c++)
    """
    quantized_activations_list = []

    # First reshape the activations to (batch_size * seq_len, hidden_size)
    batch_size, seq_len, _ = activations.shape

    activations = activations.view(-1, activations.shape[-1]).detach()

    for activation in activations:
        quantized_tensor = _quantize_tensor_uint8_uniform(activation, clip_factor)
        quantized_activations_list.append(quantized_tensor)

    quantized_activations = torch.stack(quantized_activations_list)

    # Reshape back to (batch_size, seq_len, <remaining_dims>)
    quantized_activations = quantized_activations.view(batch_size, seq_len, quantized_activations.shape[-1])
    return quantized_activations


def quantize_nd_tensor_uint8_uniform(x: torch.Tensor, clip_factor: float = 6.0) -> torch.Tensor:
    """
    Quantize a 3D or 2D tensor to uint8. Given a tensor
    of shape (batch_size, H, W) or (H, W), it will return a tensor of the same shape
    but with quantized and packed uint8 values.

    Args:
        x (torch.Tensor): Tensor to quantize
                         shape: (batch_size, H, W) or (H, W)
        clip_factor (float, optional): Factor for clipping range as multiples of standard deviation.
            Defaults to 6.0.
    """
    # Detach and convert to float32
    x = x.detach().to(torch.float32)

    # Get original shape for reshaping later
    original_shape = x.shape

    # Reshape to 2D if there is a batch dimension: (batch_size * H, W)
    if len(original_shape) > 2:
        x_2d = x.view(-1, original_shape[-1])
    else:
        x_2d = x

    # Calculate statistics along hidden dimension (dim=1)
    mu = x_2d.mean(dim=1, keepdim=True)
    std = x_2d.std(dim=1, keepdim=True, unbiased=False)

    # Calculate clipping bounds
    lo = mu - clip_factor * std
    hi = mu + clip_factor * std

    # Clip values
    x_clipped = torch.clamp(x_2d, lo, hi)

    # Calculate scale for each row
    scale = (hi - lo) / 255.0

    # Quantize to uint8
    codes = torch.round((x_clipped - lo) / scale).to(torch.uint8)

    # Pack each row with its metadata
    quantized_rows = []
    for i in range(x_2d.shape[0]):
        packed = pack_int8_with_meta(codes[i], scale[i].squeeze(), lo[i].squeeze())
        quantized_rows.append(packed)

    # Stack and reshape back to original dimensions
    quantized_x = torch.stack(quantized_rows)
    quantized_x = quantized_x.view(*original_shape[:-1], quantized_x.shape[-1])

    return quantized_x


def _dequantize_tensor_uint8_uniform(
    codes: torch.Tensor,
    meta: Dict[str, torch.Tensor],
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """
    Reconstruct fp32 tensor from uint8 codes and meta data.

    Args:
        codes (torch.Tensor): Integer codes (0-255) of type uint8
        meta (Dict[str, torch.Tensor]): Dictionary containing scale, offset, and optionally codebook


    Returns:
        torch.Tensor: Reconstructed tensor in float32 format

    If `use_codebook` is False, it maps each code to its bucket center:
        x_hat = offset + (codes + 0.5) * scale
    """

    scale = meta["scale"]
    offset = meta["offset"]
    return (offset + (codes.float() + 0.5) * scale).to(dtype)


def dequantize_activations_uint8_uniform(
    activations: torch.Tensor,
    dequantized_shape: list[int],
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """
    Unpack and dequantize a tensor of quantized activations to the given dtype.

    Args:
        activations (torch.Tensor): Tensor of quantized activations
        codes_shape (list[int]): Shape of the original activations
        dtype (torch.dtype, optional): Data type of the dequantized activations.
            Defaults to torch.float32.

    Returns:
        torch.Tensor: Tensor of dequantized activations
                        shape: (batch_size, seq_len, hidden_size)

    TODO: Vectorize this function and remove the loop. (pytorch vectorization or c++)
    """
    dequantized_activations_list = []

    # First reshape the activations to (batch_size * seq_len, hidden_size)
    activations = activations.view(-1, activations.shape[-1]).detach()

    for activation in activations:
        # unpack the activation
        batch_size, seq_len, embedding_dim = dequantized_shape
        codes, meta = unpack_int8_with_meta(activation, codes_shape=[1, embedding_dim])

        # dequantize the activation
        dequantized_activations_list.append(_dequantize_tensor_uint8_uniform(codes, meta, dtype))

    # Reshape back to (batch_size, seq_len, hidden_size)
    dequantized_activations = torch.stack(dequantized_activations_list)

    # Reshape back to (batch_size, seq_len, hidden_size) and make contiguous
    dequantized_activations = dequantized_activations.view(batch_size, seq_len, embedding_dim).contiguous()

    return dequantized_activations


def pack_int8_with_meta(codes: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor) -> torch.Tensor:
    """
    Pack uint8 codes with scale and offset metadata into a single uint8 tensor.

    Args:
        codes: Tensor of dtype uint8 with any shape
        scale: Scalar tensor of dtype float32 representing quantization scale
        offset: Scalar tensor of dtype float32 representing quantization offset

    Returns:
        torch.Tensor: 1-D uint8 tensor containing flattened codes followed by metadata bytes
            in the format [codes_bytes | scale_bytes | offset_bytes]
    """
    assert codes.dtype == torch.uint8
    device = codes.device

    # meta scalars → 8 bytes  (=2× fp32 = 8 uint8 values)
    meta = torch.stack([scale, offset]).to(torch.float32)  # (2,)
    meta_bytes = meta.view(torch.uint8)  # (8,)

    packed = torch.cat([codes.view(torch.uint8).flatten(), meta_bytes.to(device)])  # N  # +8
    return packed


def unpack_int8_with_meta(packed: torch.Tensor, codes_shape: list[int]) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
    """
    Unpack a uint8 blob into codes and metadata.

    Args:
        packed: 1-D uint8 tensor containing flattened codes followed by metadata bytes
        codes_shape: Shape that must match the original INT8 tensor shape

    Returns:
        tuple: A tuple containing:
            - codes: Tensor of dtype uint8 with shape matching codes_shape
            - meta: Dictionary with keys:
                - 'scale': Scalar tensor of dtype float32 representing quantization scale
                - 'offset': Scalar tensor of dtype float32 representing quantization offset
    """
    assert packed.dtype == torch.uint8
    num_codes = packed.numel() - 8  # last 8 bytes = meta

    codes_flat = packed[:num_codes]
    meta_bytes = packed[num_codes:]  # (8,) uint8 → fp32[2]

    meta_float = meta_bytes.view(torch.float32)
    scale, offset = meta_float[0], meta_float[1]

    codes = codes_flat.view(codes_shape).contiguous()
    return codes, {"scale": scale, "offset": offset}


def dequantize_nd_tensor_uint8_uniform(x: torch.Tensor, dtype: torch.dtype = torch.float32) -> torch.Tensor:
    """
    Dequantize a tensor of uint8 values back to float32. Given a tensor
    of shape (batch_size, H, W) or (H, W), it will return a tensor of the same shape
    but with dequantized float32 values.

    Args:
        x (torch.Tensor): Tensor to dequantize
                         shape: (batch_size, H, W) or (H, W)
        dtype (torch.dtype, optional): Data type of the dequantized tensor.
            Defaults to torch.float32.
    """
    # Get original shape for reshaping later
    original_shape = x.shape

    # Reshape to 2D if there is a batch dimension: (batch_size * H, W)
    if len(original_shape) > 2:
        x_2d = x.view(-1, original_shape[-1])
    else:
        x_2d = x

    # Unpack each row to get codes and metadata
    num_codes = x_2d.shape[1] - 8  # last 8 bytes = meta
    codes_flat = x_2d[:, :num_codes]
    meta_bytes = x_2d[:, num_codes:]  # (N, 8) uint8 → fp32[2]

    # Convert metadata bytes to float32
    meta_float = meta_bytes.view(-1, 2, 4).view(torch.float32)  # (N, 2)
    scales = meta_float[:, 0]  # (N,)
    offsets = meta_float[:, 1]  # (N,)

    # Dequantize using vectorized operations
    dequantized = (offsets + (codes_flat.float() + 0.5) * scales).to(dtype)

    # Reshape back to original dimensions
    dequantized = dequantized.view(*original_shape[:-1], dequantized.shape[-1])

    return dequantized


if __name__ == "__main__":
    # Test the quantization and dequantization functions
    import time

    def run_test(batch_size=1, seq_len=122000, hidden_size=2048, device="cuda", num_runs=100):
        print(f"\nRunning test with shape: ({batch_size}, {seq_len}, {hidden_size})")
        print(f"Number of runs: {num_runs}")

        # Arrays to store metrics
        quant_times = []
        dequant_times = []
        mses = []

        for run in range(num_runs):
            # Create random input tensor
            x = torch.randn(batch_size, seq_len, hidden_size).to(device).to(torch.bfloat16)

            # Test quantization
            start_time = time.time()
            quantized_x = quantize_nd_tensor_uint8_uniform(x)
            quant_time = time.time() - start_time
            quant_times.append(quant_time)

            # Test dequantization
            start_time = time.time()
            dequantized_x = dequantize_nd_tensor_uint8_uniform(quantized_x)
            # dequantized_x = dequantize_activations_uint8_uniform(quantized_x, [batch_size, seq_len, hidden_size])
            dequant_time = time.time() - start_time
            dequant_times.append(dequant_time)

            # Verify reconstruction error
            mse = torch.mean((x - dequantized_x) ** 2).item()
            mses.append(mse)

            if (run + 1) % 10 == 0:
                print(f"Completed {run + 1} runs...")

        # Compute statistics
        avg_quant_time = np.mean(quant_times)
        std_quant_time = np.std(quant_times)
        avg_dequant_time = np.mean(dequant_times)
        std_dequant_time = np.std(dequant_times)
        avg_mse = np.mean(mses)
        std_mse = np.std(mses)

        print("\nResults:")
        print(f"Quantization: {avg_quant_time:.4f}s ± {std_quant_time:.4f}s")
        print(f"Dequantization: {avg_dequant_time:.4f}s ± {std_dequant_time:.4f}s")
        print(f"MSE: {avg_mse:.6f} ± {std_mse:.6f}")

        return avg_quant_time, avg_dequant_time, avg_mse

    # Run multiple tests with different shapes
    test_shapes = [
        (1, 800, 2048),  # Multiple batches
    ]

    results = []
    for shape in test_shapes:
        results.append(run_test(*shape, num_runs=100))

    # Print summary
    print("\nTest Summary:")
    print("Shape\t\tAvg Quant Time\tAvg Dequant Time\tAvg MSE")
    for shape, (quant_time, dequant_time, mse) in zip(test_shapes, results):
        print(f"{shape}\t{quant_time:.4f}s\t{dequant_time:.4f}s\t{mse:.6f}")


def convert_dtype_string(dtype_str: str):
    """
    Convert a string representation of a dtype to the corresponding torch.dtype.

    Args:
        dtype_str (str): String representation of a dtype

    Returns:
        torch.dtype: Corresponding torch.dtype
    """
    if dtype_str == "torch.bfloat16":
        return torch.bfloat16
    elif dtype_str == "torch.float32":
        return torch.float32
    elif dtype_str == "torch.float16":
        return torch.float16
    elif dtype_str == "torch.int32":
        return torch.int32
    elif dtype_str == "torch.int64":
        return torch.int64
    else:
        raise ValueError(f"Unexpected dtype: {dtype_str}")


def _clean_gpu_memory():
    """Force cleanup of GPU memory."""
    if torch.cuda.is_available():
        torch.cuda.synchronize()  # wait for all in-flight kernels
        torch.cuda.empty_cache()  # release unused cached blocks
        torch.cuda.synchronize()  # (optional) make sure the allocator work is finished

    gc.collect()
    logger.debug(f"Miner GPU memory cleaned. memory: {torch.cuda.memory_allocated() / 1024**3:.2f}GB")
