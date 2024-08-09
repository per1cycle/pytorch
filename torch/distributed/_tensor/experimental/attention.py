# Copyright (c) Meta Platforms, Inc. and affiliates

import contextlib
import itertools
import logging
import types
import weakref
from abc import ABC, abstractmethod
from enum import auto, Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    Union,
)

import torch
import torch.distributed as dist
import torch.distributed._functional_collectives as ft_c
import torch.nn.functional as F
from torch import nn
from torch.distributed._tensor import distribute_module, DTensor, Replicate, Shard
from torch.distributed.device_mesh import DeviceMesh
from torch.distributed.tensor.parallel.style import ParallelStyle


class _CausalBehavior(Enum):
    SKIP = None
    NOT_IS_CAUSAL = False
    IS_CAUSAL = True


class _RotateMethod(Enum):
    ALL_TO_ALL = auto()
    ALL_GATHER = auto()
    SEND_RECV = auto()


aten = torch.ops.aten
logger = logging.getLogger(__name__)
# Whether to upcast parameters and gradients to float32 to avoid accumulation
# errors. It is likely this is always True but we currently keep this variable
# for the experimental purpose.
_convert_to_f32 = True
_enable_load_balance = True
_rotate_method = _RotateMethod.ALL_GATHER


def _is_causal_behavior(
    rank: int, world_size: int, i: int, is_causal: bool
) -> _CausalBehavior:
    """
    Calculate is_causal behavior for each KV block. The attention can either be
    calculated in full, not at all or with the causal mask applied.
    """
    if not is_causal:
        return _CausalBehavior.NOT_IS_CAUSAL

    if i == 0:
        return _CausalBehavior.IS_CAUSAL

    source_rank = (rank - i) % world_size
    if source_rank < rank or _enable_load_balance:
        return _CausalBehavior.NOT_IS_CAUSAL
    else:
        return _CausalBehavior.SKIP


def _maybe_wait(tensor: torch.Tensor) -> torch.Tensor:
    """
    When tracing the code, the result tensor is not an AsyncCollectiveTensor,
    so we cannot call ``wait()``.
    """
    if isinstance(tensor, ft_c.AsyncCollectiveTensor):
        return tensor.wait()
    return tensor


def _partial_update(
    original: torch.Tensor,
    new: torch.Tensor,
    dim: int,
    n_chunks: int,
    idx: int,
    add: bool,
) -> torch.Tensor:
    chunks = list(original.chunk(n_chunks, dim=dim))
    if add:
        chunks[idx] += new
    else:
        chunks[idx] = new
    return torch.cat(chunks, dim=dim)


class _SDPAMerger:
    """A class to help to merge the local SDPA result."""

    def __init__(self, convert_to_f32: bool, seq_dim: int):
        self._seq_dim = seq_dim
        self._out: Optional[torch.Tensor] = None
        self._lse: Optional[torch.Tensor] = None
        self._convert_to_f32 = convert_to_f32
        self._out_dtype = torch.float32
        self._lse_dtype = torch.float32

    def _merge_one(
        self, block_out: torch.Tensor, block_lse: torch.Tensor, partial: bool
    ) -> None:
        block_lse = block_lse.unsqueeze(dim=-1)
        if self._lse is None:
            self._lse = block_lse
            self._out = block_out
        else:
            assert self._lse is not None
            assert self._out is not None
            lse = self._lse.chunk(2, dim=self._seq_dim)[1] if partial else self._lse
            out = self._out.chunk(2, dim=self._seq_dim)[1] if partial else self._out

            # The algorithm from
            # github.com/zhuzilin/ring-flash-attention/pull/34#issuecomment-2076126795
            # gives a relatively stable result.
            new_lse = lse + torch.log(1 + torch.exp(block_lse - lse))
            out = (
                torch.exp(lse - new_lse) * out
                + torch.exp(block_lse - new_lse) * block_out
            )
            if partial:
                self._lse = _partial_update(self._lse, new_lse, 2, 2, 1, add=False)
                self._out = _partial_update(self._out, out, 2, 2, 1, add=False)
            else:
                self._lse = new_lse
                self._out = out

    def step(self, out: torch.Tensor, lse: torch.Tensor, partial: bool) -> None:
        self._out_dtype = out.dtype
        self._lse_dtype = lse.dtype

        if self._convert_to_f32:
            out = out.to(torch.float32)
            lse = lse.to(torch.float32)

        self._merge_one(out, lse, partial)

    def results(self) -> Tuple[torch.Tensor, torch.Tensor]:
        assert self._out is not None
        assert self._lse is not None
        out, lse = self._out, self._lse.squeeze(-1)
        return out.to(self._out_dtype), lse.to(self._lse_dtype)


def _scaled_dot_product_ring_flash_attention(
    mesh: DeviceMesh,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    dropout_p: float = 0.0,
    is_causal: bool = False,
    return_debug_mask: bool = False,
    *,
    scale: Optional[float] = None,
) -> Tuple[torch.Tensor, ...]:
    if return_debug_mask:
        raise NotImplementedError("return_debug_mask is not supported yet")

    return _templated_ring_attention(
        mesh,
        aten._scaled_dot_product_flash_attention,
        query=query,
        key=key,
        value=value,
        is_causal=is_causal,
        dropout_p=dropout_p,
        scale=scale,
    )


def _scaled_dot_product_ring_efficient_attention(
    mesh: DeviceMesh,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attn_bias: Optional[torch.Tensor] = None,
    compute_log_sumexp: bool = True,
    dropout_p: float = 0.0,
    is_causal: bool = False,
    *,
    scale: Optional[float] = None,
) -> Tuple[torch.Tensor, ...]:
    if attn_bias is not None:
        raise NotImplementedError("attn_bias is not supported yet")
    if not compute_log_sumexp:
        raise NotImplementedError("compute_log_sumexp must be set")

    return _templated_ring_attention(
        mesh,
        aten._scaled_dot_product_efficient_attention,
        query=query,
        key=key,
        value=value,
        is_causal=is_causal,
        attn_bias=attn_bias,
        dropout_p=dropout_p,
        scale=scale,
        compute_log_sumexp=compute_log_sumexp,
    )


class AttentionOp(Protocol):
    def __call__(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        **kwargs: object,
    ) -> Tuple[torch.Tensor, ...]:
        ...


class RingRotater(ABC):
    @abstractmethod
    def __init__(self, pg: dist.ProcessGroup, seq_dim: int) -> None:
        ...

    @abstractmethod
    def rotate(self, buffer: torch.Tensor, curr_idx: int) -> torch.Tensor:
        ...

    @abstractmethod
    def maybe_wait(self, tensor: torch.Tensor) -> torch.Tensor:
        ...


class AllToAllRotater(RingRotater):
    def __init__(self, pg: dist.ProcessGroup, seq_dim: int) -> None:
        self._pg = pg
        self._seq_dim = seq_dim

    def rotate(self, buffer: torch.Tensor, curr_idx: int) -> torch.Tensor:
        buffer = buffer.contiguous()
        size = dist.get_world_size(self._pg)
        dsts = list(range(1, size)) + [0]
        return ft_c.permute_tensor(buffer, dsts, self._pg)

    def maybe_wait(self, tensor: torch.Tensor) -> torch.Tensor:
        if isinstance(tensor, ft_c.AsyncCollectiveTensor):
            return tensor.wait()
        return tensor


class AllGatherRotater(RingRotater):
    def __init__(self, pg: dist.ProcessGroup, seq_dim: int) -> None:
        self._pg = pg
        self._seq_dim = seq_dim
        self._buffer = None
        self._idx = 0

    def rotate(self, buffer: torch.Tensor, curr_idx: int) -> torch.Tensor:
        self._idx = curr_idx
        if self._buffer is None:
            return ft_c.all_gather_tensor(
                buffer.contiguous(), gather_dim=0, group=self._pg
            )

        return self._buffer

    def maybe_wait(self, tensor: torch.Tensor) -> torch.Tensor:
        size = dist.get_world_size(self._pg)
        rank = dist.get_rank(self._pg)
        idx = rank - self._idx

        if self._buffer is None:
            if isinstance(tensor, ft_c.AsyncCollectiveTensor):
                tensor = tensor.wait()
            self._buffer = tensor
        else:
            assert tensor is self._buffer

        return self._buffer.chunk(dist.get_world_size(self._pg))[idx]


def _create_rotater(
    pg: dist.ProcessGroup, seq_dim: int, method: Optional[RingRotater] = None
):
    if method is None:
        method = _rotate_method
    if method == _RotateMethod.ALL_TO_ALL:
        return AllToAllRotater(pg, seq_dim)
    elif method == _RotateMethod.ALL_GATHER:
        return AllGatherRotater(pg, seq_dim)
    else:
        raise NotImplementedError


def _ring_rotate(
    block: torch.Tensor, pg: dist.ProcessGroup, send_to_next: bool
) -> torch.Tensor:
    block = block.contiguous()
    size = dist.get_world_size(pg)
    dsts = (
        list(range(1, size)) + [0]
        if send_to_next
        else [size - 1] + list(range(0, size - 1))
    )
    return ft_c.permute_tensor(block, dsts, pg)


def _templated_ring_attention(
    mesh: DeviceMesh,
    op: AttentionOp,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    is_causal: bool = False,
    **kwargs: object,
) -> Tuple[torch.Tensor, ...]:
    """
    This is a generalized ring attention implementation that can support multiple attention ops.

    Parameters
    ----------
    op:
        The attention op to use
    *args:
        additional args are passed to the op
    **kwargs:
        additional kwargs are passed to the op

    Returns
    -------
    out:
        The merged attention output
    softmax_lse:
        The logsumexp of the merged attention output
    """
    if is_causal and (query.size(2) != key.size(2)):
        raise NotImplementedError(
            "is_causal requires the same query and context sequence lengths"
        )

    if isinstance(mesh, dist.ProcessGroup):
        pg: Union[dist.ProcessGroup, List[dist.ProcessGroup]] = mesh
    else:
        pg = mesh.get_group()
    assert isinstance(pg, dist.ProcessGroup), "process group must be single dimension"
    rank = dist.get_rank(pg)
    size = dist.get_world_size(pg)

    next_kv = None

    # Without making key and value contiguous(), the lose curve is bad.
    # TODO(fegin): figure out why this is a requirement since SDPA does not have
    # this requirement.
    key = key.contiguous()
    value = value.contiguous()

    sdpa_merger = _SDPAMerger(_convert_to_f32, seq_dim=2)

    rest: List[Any]
    out: torch.Tensor
    logsumexp: torch.Tensor

    rotater = _create_rotater(pg, 2)

    for i in range(size):
        # overlap communication with compute
        if next_kv is not None:
            next_kv = rotater.maybe_wait(next_kv)
            key = next_kv[: key.numel()].reshape(key.shape)
            value = next_kv[key.numel() :].reshape(value.shape)

        if i < (size - 1):
            next_kv = torch.cat([key.flatten(), value.flatten()])
            next_kv = rotater.rotate(next_kv, i + 1)

        is_causal_behavior = _is_causal_behavior(
            rank=rank, world_size=size, i=i, is_causal=is_causal
        )

        if is_causal_behavior == _CausalBehavior.SKIP:
            continue

        if i == 0 or (not _enable_load_balance or not is_causal):
            q, k, v, partial = (query, key, value, False)
        elif i <= rank:
            q, k, v, partial = (
                query,
                key.chunk(2, dim=2)[0],
                value.chunk(2, dim=2)[0],
                False,
            )
        else:
            q, k, v, partial = query.chunk(2, dim=2)[1], key, value, True

        out, logsumexp, *rest = op(
            q,
            k,
            v,
            is_causal=is_causal_behavior.value,
            **kwargs,
        )
        sdpa_merger.step(out, logsumexp, partial)

    return *sdpa_merger.results(), *rest


def sdpa_handler(
    op_call: torch._ops.OpOverload,
    args: Tuple[object, ...],
    kwargs: Dict[str, object],
) -> object:
    # extract local tensor and sharding infos to a OpInfo
    op_info = DTensor._op_dispatcher.unwrap_to_op_info(op_call, args, kwargs)
    logger.debug("Dispatching op_call: %s", op_info.schema)

    # sharding propagation
    DTensor._op_dispatcher.sharding_propagator.propagate(op_info)
    output_sharding = op_info.output_sharding
    assert output_sharding is not None, "output sharding should not be None"
    assert not output_sharding.needs_redistribute, "inputs need to be redistributed"

    if op_call == aten._scaled_dot_product_flash_attention.default:
        local_results = _scaled_dot_product_ring_flash_attention(
            op_info.mesh,
            *op_info.local_args,  # type: ignore[arg-type]
            **op_info.local_kwargs,  # type: ignore[arg-type]
        )
    elif op_call == aten._scaled_dot_product_efficient_attention.default:
        local_results = _scaled_dot_product_ring_efficient_attention(
            op_info.mesh,
            *op_info.local_args,  # type: ignore[arg-type]
            **op_info.local_kwargs,  # type: ignore[arg-type]
        )
    else:
        raise NotImplementedError

    return DTensor._op_dispatcher.wrap(local_results, output_sharding.output_spec)


def sdpa_backward_handler(
    op_call: torch._ops.OpOverload,
    args: Tuple[object, ...],
    kwargs: Dict[str, object],
) -> object:
    # Redistribute grad_output tensor to the same placement as output tensor
    args = list(args)
    # assert isinstance(args[0], DTensor) and isinstance(args[4], DTensor)
    # args[0] = args[0].redistribute(args[4].device_mesh, args[4].placements)
    args = tuple(args)

    # extract local tensor and sharding infos to a OpInfo
    op_info = DTensor._op_dispatcher.unwrap_to_op_info(op_call, args, kwargs)
    logger.debug("Dispatching op_call: %s", op_info.schema)

    # sharding propagation
    DTensor._op_dispatcher.sharding_propagator.propagate(op_info)
    output_sharding = op_info.output_sharding
    assert output_sharding is not None, "output sharding should not be None"
    assert not output_sharding.needs_redistribute, "inputs need to be redistributed"

    if op_call == aten._scaled_dot_product_flash_attention_backward.default:
        local_results = _scaled_dot_product_ring_flash_attention_backward(
            op_info.mesh,
            *op_info.local_args,  # type: ignore[arg-type]
            **op_info.local_kwargs,  # type: ignore[arg-type]
        )
    elif op_call == aten._scaled_dot_product_efficient_attention_backward.default:
        local_results = _scaled_dot_product_ring_efficient_attention_backward(
            op_info.mesh,
            *op_info.local_args,  # type: ignore[arg-type]
            **op_info.local_kwargs,  # type: ignore[arg-type]
        )
    else:
        raise NotImplementedError(f"{op_call=}")

    return DTensor._op_dispatcher.wrap(local_results, output_sharding.output_spec)


def _templated_ring_attention_backward(
    mesh: DeviceMesh,
    op: AttentionOp,
    grad_out: torch.Tensor,
    grad_out_name: str,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    is_causal: bool,
    **kwargs: Any,
) -> Tuple[torch.Tensor, ...]:
    pg = mesh.get_group()
    assert isinstance(pg, dist.ProcessGroup), "must be single dimension"
    rank = dist.get_rank(pg)
    size = dist.get_world_size(pg)
    next_kv = None
    next_grad_kv = None
    rest: List[Any]
    grad_query_, grad_key_, grad_value_ = None, None, None

    accum_dtype = torch.float32 if _convert_to_f32 else query.dtype
    grad_query = torch.zeros_like(query, dtype=accum_dtype)
    grad_key = torch.zeros_like(key, dtype=accum_dtype)
    grad_value = torch.zeros_like(value, dtype=accum_dtype)

    key = key.contiguous()
    value = value.contiguous()
    kv_rotater = _create_rotater(pg, 2)
    dkv_rotater = _create_rotater(pg, 2, method=_RotateMethod.ALL_TO_ALL)
    for i in range(size):
        if next_kv is not None:
            buffer = kv_rotater.maybe_wait(next_kv)
            pointer = 0
            key = buffer[pointer : pointer + key.numel()].reshape(key.shape)
            pointer += key.numel()
            value = buffer[pointer : pointer + value.numel()].reshape(value.shape)
            pointer += value.numel()

        if i != size - 1:
            next_kv = torch.cat([key.flatten(), value.flatten()])
            next_kv = kv_rotater.rotate(next_kv, i + 1)

        is_causal_behavior = _is_causal_behavior(
            rank=rank, world_size=size, i=i, is_causal=is_causal
        )

        if is_causal_behavior != _CausalBehavior.SKIP:
            if i == 0 or (not _enable_load_balance or not is_causal):
                q, k, v, out_, dout, lse = (query, key, value, out, grad_out, logsumexp)
            elif i <= rank:
                q, k, v, out_, dout, lse = (
                    query,
                    key.chunk(2, dim=2)[0],
                    value.chunk(2, dim=2)[0],
                    out,
                    grad_out,
                    logsumexp,
                )
            else:
                q, k, v, out_, dout, lse = (
                    query.chunk(2, dim=2)[1],
                    key,
                    value,
                    out.chunk(2, dim=2)[1],
                    grad_out.chunk(2, dim=2)[1],
                    logsumexp.chunk(2, dim=2)[1].contiguous(),
                )

            kwargs[grad_out_name] = dout
            grad_query_, grad_key_, grad_value_, *rest = op(
                query=q,
                key=k,
                value=v,
                out=out_,
                logsumexp=lse,
                is_causal=is_causal_behavior.value,
                **kwargs,
            )
        else:
            grad_query_ = torch.zeros_like(query, dtype=accum_dtype)
            grad_key_ = torch.zeros_like(key, dtype=accum_dtype)
            grad_value_ = torch.zeros_like(value, dtype=accum_dtype)

        # Get the grad key and grad value for the i round.
        if i == 0:
            grad_key += grad_key_
            grad_value += grad_value_
        else:
            pointer = 0
            assert next_grad_kv is not None
            next_grad_kv = dkv_rotater.maybe_wait(next_grad_kv)
            grad_key = next_grad_kv[pointer : pointer + grad_key.numel()].reshape(
                grad_key.shape
            )
            pointer += grad_key.numel()
            grad_value = next_grad_kv[pointer : pointer + grad_value.numel()].reshape(
                grad_value.shape
            )

            if i <= rank and _enable_load_balance:
                grad_key = _partial_update(grad_key, grad_key_, 2, 2, 0, add=True)
                grad_value = _partial_update(grad_value, grad_value_, 2, 2, 0, add=True)
            else:
                grad_key += grad_key_
                grad_value += grad_value_

        # Send the key, value, grad key, and grad value to the next rank.
        next_grad_kv = torch.cat([grad_key.flatten(), grad_value.flatten()])
        next_grad_kv = dkv_rotater.rotate(next_grad_kv, i + 1)

        if i <= rank or not _enable_load_balance:
            grad_query += grad_query_
        else:
            grad_query = _partial_update(grad_query, grad_query_, 2, 2, 1, add=True)

    assert next_grad_kv is not None
    assert grad_key_ is not None
    assert grad_value_ is not None
    grad_query = grad_query.to(query.dtype)
    next_grad_kv = dkv_rotater.maybe_wait(next_grad_kv).to(key.dtype)
    grad_key = next_grad_kv[: grad_key.numel()].reshape(grad_key.shape)
    grad_value = next_grad_kv[grad_value.numel() :].reshape(grad_value.shape)
    return (
        grad_query,
        grad_key,
        grad_value,
        *rest,
    )


def _scaled_dot_product_ring_flash_attention_backward(
    mesh: DeviceMesh,
    grad_out: torch.Tensor,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    cum_seq_q: torch.Tensor,
    cum_seq_k: torch.Tensor,
    max_q: int,
    max_k: int,
    dropout_p: float,
    is_causal: bool,
    philox_seed: torch.Tensor,
    philox_offset: torch.Tensor,
    *,
    scale: Optional[float] = None,
) -> Tuple[torch.Tensor, ...]:
    return _templated_ring_attention_backward(
        mesh,
        aten._scaled_dot_product_flash_attention_backward.default,
        grad_out=grad_out,
        grad_out_name="grad_out",
        query=query,
        key=key,
        value=value,
        out=out,
        logsumexp=logsumexp,
        is_causal=is_causal,
        cum_seq_q=cum_seq_q,
        cum_seq_k=cum_seq_k,
        max_q=max_q,
        max_k=max_k,
        dropout_p=dropout_p,
        philox_seed=philox_seed,
        philox_offset=philox_offset,
        scale=scale,
    )


def _scaled_dot_product_ring_efficient_attention_backward(
    mesh: DeviceMesh,
    grad_out: torch.Tensor,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    bias: torch.Tensor,
    out: torch.Tensor,
    logsumexp: torch.Tensor,
    philox_seed: torch.Tensor,
    philox_offset: torch.Tensor,
    dropout_p: float,
    grad_input_mask: Tuple[bool, ...],
    is_causal: bool = False,
    *,
    scale: Optional[float] = None,
) -> Tuple[torch.Tensor, ...]:
    return _templated_ring_attention_backward(
        mesh,
        aten._scaled_dot_product_efficient_attention_backward.default,
        grad_out=grad_out,
        grad_out_name="grad_out_",
        query=query,
        key=key,
        value=value,
        attn_bias=bias,
        out=out,
        logsumexp=logsumexp,
        philox_seed=philox_seed,
        philox_offset=philox_offset,
        dropout_p=dropout_p,
        grad_input_mask=grad_input_mask,
        is_causal=is_causal,
        scale=scale,
    )


customized_ops = {
    aten._scaled_dot_product_flash_attention.default: sdpa_handler,
    aten._scaled_dot_product_flash_attention_backward.default: sdpa_backward_handler,
    aten._scaled_dot_product_efficient_attention.default: sdpa_handler,
    aten._scaled_dot_product_efficient_attention_backward.default: sdpa_backward_handler,
}


_replaced_functions: Dict[Callable, Tuple[str, Callable]] = {}


def _distribute_function(
    fn: Callable,
    fn_module: types.ModuleType,
    device_mesh: DeviceMesh,
    input_fn: Optional[Callable] = None,
    output_fn: Optional[Callable] = None,
) -> None:
    """
    ``distribute_function`` is an experimental API that allows users to "distribute"
    the inputs and outputs of a function. Similar to ``distribute_module``, this API
    installs hooks to the ``fn`` to convert the inputs and outputs. There are two
    major differences between ``distribute_function`` and ``distribute_module``.
    First, a function does not have parammeters and buffers, as a result,
    ``distribute_function`` itself won't convert any tensors but simply install the
    input and output hooks.  The tnesor conversion will happen in the hooks.
    Another difference is an nn.Module subclass can have several instances and each
    instance be fed into ``distribute_module`` independently with affecting other
    instance. On the other hand, function is a singleton object. So if a function
    is distributed by ``distribute_function`` all subsequent calls to the function
    will invoke the installed hooks.

    Args:
        fn (Callable): the function to be distributed.
        fn_module (types.ModuleType): the Python module that the function is declared.
            e.g., if ``fn`` is ``torch.nn.functional.scaled_dot_product_attention``,
            ``fn_module`` is ``torch.nn.functional``.
        device_mesh (:class:`DeviceMesh`): the device mesh that will be used by the
            input and output hooks to distribute the tensors.
        input_fn (Optioinal[Callable]): the hook to distribute or convert the input
            arguments of ``fn``.
        output_fn (Optioinal[Callable]): the hook to distribute or convert the output
            arguments of ``fn``.
    """

    def wrapper(
        target_fn: Callable, input_fn: Optional[Callable], output_fn: Optional[Callable]
    ) -> Callable:
        def inner_fn(*args: Tuple[Any, ...], **kwargs: Dict[str, Any]) -> Any:
            if input_fn is not None:
                args, kwargs = input_fn(device_mesh, *args, **kwargs)
            output = target_fn(*args, **kwargs)
            if output_fn is not None:
                output = output_fn(device_mesh, output)
            return output

        return inner_fn

    global _replaced_functions

    if fn in _replaced_functions:
        return

    wrapper_fn = wrapper(fn, input_fn, output_fn)
    setattr(fn_module, fn.__name__, wrapper_fn)
    _replaced_functions[wrapper_fn] = (fn.__name__, fn)


def _restore_function(fn: Callable, fn_module: types.ModuleType) -> None:
    """Restore the function that is replaced by _distribute_function."""
    global _original_functions
    global _wrapper_functions

    if fn not in _replaced_functions:
        return

    original_name, original_fn = _replaced_functions[fn]
    setattr(fn_module, original_name, original_fn)


@contextlib.contextmanager
def _enable_cp_dispatcher() -> Generator[None, None, None]:
    """Enables DTensor dispatcher to dispatch SDPA to CP."""
    old_handlers = DTensor._op_dispatcher._custom_op_handlers
    DTensor._op_dispatcher._custom_op_handlers = {**old_handlers, **customized_ops}

    yield

    DTensor._op_dispatcher._custom_op_handlers = old_handlers


class _AttentionContextParallel(ParallelStyle):
    """
    Applies context parallel optimizations to the attention layer.

    This will work for nn.MultiHeadedAttention and custom attention layers that
    call F.scaled_dotproduct_attention with a simliar signature.

    This expects the `forward` method consumes either:

    * a single tensor for self attention
    * one argument for each of: query, key, value

    This currently only supports ring attention and the
    SDPBackend.FLASH_ATTENTION backend. See sdpa_kernel.

    Non-flash attention backends will result in incorrect results.
    """

    # use a weakref dictionary to store context managers for each nn.Module
    _CONTEXT_MANAGERS: "weakref.WeakKeyDictionary[nn.Module, Any]" = (
        weakref.WeakKeyDictionary()
    )

    def _apply(self, module: nn.Module, device_mesh: DeviceMesh) -> nn.Module:
        if not isinstance(device_mesh, DeviceMesh):
            raise ValueError(
                f"{type(device_mesh)} is not supported by {type(self)} yet."
            )

        if not device_mesh.ndim == 1:
            raise ValueError

        return distribute_module(
            module,
            device_mesh,
            input_fn=self._input_fn,  # type: ignore[arg-type]
            output_fn=self._output_fn,  # type: ignore[arg-type]
        )

    @classmethod
    def _input_fn(
        cls,
        module: nn.Module,
        inputs: Tuple[Union[torch.Tensor, int, float], ...],
        device_mesh: DeviceMesh,
    ) -> Tuple[Union[torch.Tensor, int, float], ...]:
        # TODO(d4l3k); this should be Shard(2), need to fix Linear layer rules
        placement = [Replicate()]

        def backward_hook(grad: torch.Tensor) -> None:
            if module in cls._CONTEXT_MANAGERS:
                cls._CONTEXT_MANAGERS[module].__exit__(None, None, None)
                del cls._CONTEXT_MANAGERS[module]

        # convert inputs to DTensor
        inp = []
        for input in inputs:
            if isinstance(input, torch.Tensor) and not isinstance(input, DTensor):
                input = DTensor.from_local(
                    input.contiguous(), device_mesh, placement, run_check=False
                )

            if isinstance(input, torch.Tensor) and input.requires_grad:
                input.register_hook(backward_hook)

            inp.append(input)

        manager = _enable_cp_dispatcher()
        manager.__enter__()
        cls._CONTEXT_MANAGERS[module] = manager

        return tuple(inp)

    @classmethod
    def _output_fn(
        cls,
        module: nn.Module,
        outputs: Union[torch.Tensor, Tuple[Union[torch.Tensor, int, float], ...]],
        device_mesh: DeviceMesh,
    ) -> Union[
        Union[torch.Tensor, int, float], Tuple[Union[torch.Tensor, int, float], ...]
    ]:
        cls._CONTEXT_MANAGERS[module].__exit__(None, None, None)
        del cls._CONTEXT_MANAGERS[module]

        def backward_hook(grad: torch.Tensor) -> None:
            if module not in cls._CONTEXT_MANAGERS:
                manager = _enable_cp_dispatcher()
                manager.__enter__()
                cls._CONTEXT_MANAGERS[module] = manager

        # back to local tensor
        out = []
        for output in [outputs] if isinstance(outputs, torch.Tensor) else outputs:
            output = output.to_local() if isinstance(output, DTensor) else output

            if isinstance(output, torch.Tensor) and output.requires_grad:
                output.register_hook(backward_hook)

            out.append(output)

        if isinstance(outputs, torch.Tensor):
            return out[0]

        return tuple(out)


@contextlib.contextmanager
def _context_parallel(seq_dim: int, mesh: DeviceMesh) -> Generator[None, None, None]:
    """Replace SDPA with the CP-wrapped version and enable DTensor CP dispatcher."""

    def attention_input_fn(
        mesh: DeviceMesh, *args: Tuple[Any, ...], **kwargs: Dict[str, Any]
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        placement = [Shard(seq_dim)]
        all_args = []

        for arg in itertools.chain(args, kwargs.values()):
            if isinstance(arg, torch.Tensor) and not isinstance(arg, DTensor):
                arg = DTensor.from_local(arg, mesh, placement, run_check=False)

            all_args.append(arg)

        new_args = tuple(all_args[0 : len(args)])
        new_kwargs = dict(zip(kwargs.keys(), all_args[len(args) :]))
        return new_args, new_kwargs

    def attention_output_fn(mesh: DeviceMesh, outputs: Any) -> Any:
        new_outputs = []
        for output in [outputs] if isinstance(outputs, torch.Tensor) else outputs:
            output = output.to_local() if isinstance(output, DTensor) else output
            new_outputs.append(output)

        if isinstance(outputs, torch.Tensor):
            return new_outputs[0]

        return tuple(new_outputs)

    # TODO: provide a more robust way to replace SDPA.
    # Currently we use monkey patch to replace scaled_dot_product_attention with the
    # wrapped fn. This is okay if users do `import torch.nn.functional` but will not
    # work if users do `import torch.nn.functional.scaled_dot_product_attention`.
    _distribute_function(
        F.scaled_dot_product_attention,
        F,
        mesh,
        attention_input_fn,
        attention_output_fn,
    )

    with _enable_cp_dispatcher():
        yield

    _restore_function(F.scaled_dot_product_attention, F)


class _LoadBalancer(ABC):
    @classmethod
    @abstractmethod
    def shard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        ...

    @classmethod
    @abstractmethod
    def unshard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        ...


class EvenSharder(_LoadBalancer):
    @classmethod
    def shard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        assert buffer.size()[seq_dim] % mesh.size() == 0
        return buffer.chunk(mesh.size(), dim=seq_dim)[mesh.get_local_rank()]

    @classmethod
    def unshard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        buffer = buffer.contiguous()
        all_buffers = [torch.empty_like(buffer) for _ in range(mesh.size())]
        ft_c.all_gather_inplace(all_buffers, buffer, mesh)
        return torch.cat(all_buffers, dim=seq_dim)


class StripeLoadBalancer(_LoadBalancer):
    @classmethod
    def shard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        cp_world_size = mesh.size()
        cp_rank = mesh.get_local_rank()
        assert buffer.size()[seq_dim] % (cp_world_size * 2) == 0
        chunks = buffer.chunk(cp_world_size * 2, dim=seq_dim)
        return torch.cat(
            (chunks[cp_rank], chunks[cp_world_size * 2 - cp_rank - 1]),
            dim=seq_dim,
        )

    @classmethod
    def unshard(
        cls, buffer: torch.Tensor, mesh: DeviceMesh, seq_dim: int
    ) -> torch.Tensor:
        buffer = buffer.contiguous()
        cp_world_size = mesh.size()
        cp_rank = mesh.get_local_rank()

        all_buffers = [torch.empty_like(buffer) for _ in range(cp_world_size)]
        ft_c.all_gather_inplace(all_buffers, buffer, mesh)
        sliced_buffers = [sb for b in all_buffers for sb in b.chunk(2, dim=seq_dim)]
        ordered_buffers = list(sliced_buffers)
        for i, b in enumerate(sliced_buffers):
            if i % 2 == 0:
                ordered_buffers[i // 2] = b
            else:
                ordered_buffers[cp_world_size * 2 - (i // 2) - 1] = b
        return torch.cat(ordered_buffers, dim=seq_dim)


def _context_parallel_buffers(
    mesh: DeviceMesh,
    buffers: List[torch.Tensor],
    buffer_seq_dims: List[int],
) -> List[torch.Tensor]:
    """Shard the buffers along the sequence dimensions according to CP rules."""
    new_buffers = []
    sharder = StripeLoadBalancer if _enable_load_balance else EvenSharder
    for buffer, seq_dim in zip(buffers, buffer_seq_dims):
        new_buffers.append(sharder.shard(buffer, mesh, seq_dim))

    return new_buffers


@contextlib.contextmanager
@torch.no_grad()
def context_parallel(
    mesh: DeviceMesh,
    *,
    buffers: Optional[List[torch.Tensor]] = None,
    buffer_seq_dims: Optional[List[int]] = None,
    no_restore_buffers: Optional[Set[torch.Tensor]] = None,
) -> Generator[None, None, None]:
    """

    ``context_parallel`` is an experimental API to enable context
    parallelism (CP). This API performs two actions: 1) patch the SDPA
    (``torch.nn.functional.scaled_dot_product_attention``) with the CP-enabled
    one, 2) shard ``buffers`` along the sequence dimension and each rank will
    preserve the corresponding shard according ``mesh``.

    Args:
        mesh (:class:`DeviceMesh`): the device mesh for the context parallelism.
        buffers (Optional[List[torch.Tensor]]): buffers that the usage depend
            on the sequence dimension. Examples are input batch, labels and
            positional embedding buffers. These buffers must be sharded along
            the sequence dimension to ensure the accuracy. The sharding will
            happen in-place, the buffer's shape will change within the context.
            The buffers will be restored after the context finishes.
            ``no_restore_buffers`` can be used to specify which buffers don't
            need to be restored. Note that ``buffers`` should not contain any
            nn.Parameter.
        buffer_seq_dims (Optional[List[int]]): the sequence dimensions of ``buffers``.
        no_restore_buffers (Optional[Set[torch.Tensor]]): buffers in these set
            won't be restored after the context exists. This set must be a subset
            of ``buffers``.

    .. warning::
        `torch.distributed._tensor.experimental.attention.context_parall` is a
        prototype feature in PyTorch. The API is subject to change.
    """
    buffers = [] if buffers is None else buffers
    buffer_seq_dims = [] if buffer_seq_dims is None else buffer_seq_dims
    no_restore_buffers = set() if no_restore_buffers is None else no_restore_buffers

    if len(buffers) != len(buffer_seq_dims):
        raise ValueError(
            "`seq_dims` must have the same number of elements as `buffers`."
        )

    for buffer in no_restore_buffers:
        if buffer not in buffers:
            raise ValueError("`no_restore_buffers` must be a subset of `buffers`.")

    original_buffers = [None if b in no_restore_buffers else b.clone() for b in buffers]
    chunks = _context_parallel_buffers(mesh, buffers, buffer_seq_dims)
    for buffer, chunk in zip(buffers, chunks):
        chunk = chunk.clone()
        buffer.resize_(chunk.shape)
        buffer.copy_(chunk)

    with _context_parallel(seq_dim=2, mesh=mesh):
        yield

    for buffer, original_buffer in zip(buffers, original_buffers):
        if original_buffer is not None:
            buffer.resize_(original_buffer.shape)
            buffer.copy_(original_buffer)


@torch.no_grad()
def context_parallel_unshard(
    mesh: DeviceMesh,
    buffers: List[torch.Tensor],
    seq_dims: List[int],
) -> List[torch.Tensor]:
    """
    Unshard the tensors (e.g., output) that are sharded due to context parallelism.
    """
    sharder = StripeLoadBalancer if _enable_load_balance else EvenSharder
    return [sharder.unshard(b, mesh, dim) for b, dim in zip(buffers, seq_dims)]
