# (c) Meta Platforms, Inc. and affiliates. Confidential and proprietary.

"""
This benchmark script is based on the benchmark code from:
https://triton-lang.org/main/getting-started/tutorials/06-fused-attention.html

It benchmarks the following FMHA kernels:

* Triton-Flash-V2: the triton version of FA-V2:

  https://triton-lang.org/main/getting-started/tutorials/06-fused-attention.html

* Flash-V2: the FA-V2 from //ai_codesign/gen_ai/flash_attention_v2:flash_attention_v2,
  which was imported from https://github.com/Dao-AILab/flash-attention

* Xformers: the memory-efficient attention from xformers:

  https://fburl.com/code/cuorcm9h

* [optional] Xformers-Splitk: the triton-splitk FMHA kernel from xformers:

  https://fburl.com/code/awt36vjj

  Disabled by default because it failed with some configs. Note that
  the relevant benchmark only works with causal = False at the moment.
  Known to work with "--batch=8 --n-heads=8 --xformers-splitk"
"""

import argparse
import math
import os
from typing import Callable, Optional

import numpy

import torch
import triton  # @manual=//triton:triton
import xformers  # @manual=//fair/xformers:xformers
import xformers.ops.fmha as xformers_fmha  # @manual=//fair/xformers:xformers

from torchbenchmark.util.kernels.triton_fused_attention import attention as triton_tutorial_FA2
from aikl.gpu.triton.tests.test_fmha_utils import (
    generate_qkv,
    make_packed_qkv,
    permute_qkv,
)
from flash_attn.flash_attn_interface import flash_attn_qkvpacked_func as flash_attn_func
from torch.nn.attention import SDPBackend, sdpa_kernel
from torch.nn.functional import scaled_dot_product_attention as sdpa
from triton.ops.flash_attention import attention as triton_op_FA2

try:
    # colfax Flash Attention V2 for Hopper
    # https://www.internalfb.com/code/fbsource/fbcode/ai_codesign/gen_ai/cutlass-kernels/src/fmha/README.md
    torch.ops.load_library("//ai_codesign/gen_ai/cutlass-kernels:fmha_forward_lib")
    colfax_cutlass_fmha = torch.ops.cutlass.fmha_forward
except (ImportError, IOError, AttributeError):
    colfax_cutlass_fmha = None

try:
    import h100_fwd as tk_fwd
    import h100_fwd_causal as tk_fwd_causal
except (ImportError, IOError, AttributeError):
    tk_fwd = None
    tk_fwd_causal = None

from typing import Any, Generator, List

import torch
import triton
import triton.language as tl
from torchbenchmark.util.input import input_filter

from torchbenchmark.util.triton_op import (
    BenchmarkOperator,
    BenchmarkOperatorMetrics,
    register_benchmark,
    register_metric,
    register_x_val,
)
from torchbenchmark.util.triton_op import Mode as BenchmarkMode


def parse_op_args(args: List[str]):
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=4, help="Batch size")
    parser.add_argument("--n-heads", type=int, default=48, help="Number of heads")
    parser.add_argument("--d-head", type=int, default=64, help="specify head dimension")
    parser.add_argument("--causal", action="store_true", help="enable causal")
    parser.add_argument(
        "--xformers-splitk", action="store_true", help="benchmark xformers-split impl"
    )
    return parser.parse_args(args)


class Operator(BenchmarkOperator):
    DEFAULT_PRECISION = "bf16"

    def __init__(self, mode: str, device: str, extra_args: Optional[List[str]]=None):
        # pass the framework level args (e.g., device, is_training, dtype) to the parent class
        super().__init__(mode=mode, device=device, extra_args=extra_args)
        args = parse_op_args(self.extra_args)
        self.BATCH = args.batch
        self.H = args.n_heads
        self.D_HEAD = args.d_head
        self.N_CTX = None
        self.causal = args.causal
        self.sm_scale = 1.3
        self.xformers_splitk = args.xformers_splitk

    @register_benchmark(baseline=True)
    def aten(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        def _inner():
            M = torch.tril(torch.ones((self.N_CTX, self.N_CTX), device=self.device))
            p = torch.matmul(q, k.transpose(2, 3)) * self.sm_scale
            if self.causal:
                p[:, :, M == 0] = float("-inf")
            p = torch.softmax(p.float(), dim=-1).to(q.dtype)
            # p = torch.exp(p)
            ref_out = torch.matmul(p, v)
            return ref_out

        return _inner

    @register_benchmark()
    def sdpa(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        def sdpa_flash_attention(q, k, v):
            with sdpa_kernel([SDPBackend.FLASH_ATTENTION]):
                return sdpa(
                    q,
                    k,
                    v,
                    is_causal=self.causal,
                    scale=self.sm_scale,
                )

        return lambda: sdpa_flash_attention(
            q,
            k,
            v,
        )

    @register_benchmark()
    def flash_v2(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        qkv = make_packed_qkv(q, k, v)
        fn = lambda: flash_attn_func(
            qkv, softmax_scale=self.sm_scale, causal=self.causal
        )
        return fn

    @register_benchmark()
    def triton_tutorial_flash_v2(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        return lambda: triton_tutorial_FA2(q, k, v, self.causal, self.sm_scale)

    @register_benchmark()
    def triton_op_flash_v2(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        return lambda: triton_op_FA2(q, k, v, self.causal, self.sm_scale)

    # Note that we hit "CUDA error: an illegal memory access was encountered"
    # for quite a few configs. It was known to work with, e.g.
    # --batch 1 --n-heads 4 --d-head 64
    def triton_op_flash_seq_v2(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        sequence_parallel = True
        return lambda: triton_op_FA2(
            q, k, v, self.causal, self.sm_scale, sequence_parallel
        )

    def xformers_preprocess(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> xformers_fmha.Inputs:
        q_1, k_1, v_1 = permute_qkv(q, k, v, perm=(0, 2, 1, 3))
        attn_bias = xformers.ops.LowerTriangularMask() if self.causal else None
        fhma_input = xformers_fmha.Inputs(
            query=q_1, key=k_1, value=v_1, attn_bias=attn_bias, scale=self.sm_scale
        )
        return fhma_input

    @register_benchmark()
    def xformers(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> Callable:
        fhma_input = self.xformers_preprocess(q, k, v)
        xformers_cutlass_fhma = xformers.ops.fmha.cutlass.FwOp
        return lambda: xformers_cutlass_fhma().apply(fhma_input, needs_gradient=False)

    @register_benchmark(enabled=False)
    def xformers_splitk(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ):
        fhma_input = self.xformers_preprocess(q, k, v)
        xformers_splitk_fhma = xformers_fmha.triton_splitk.FwOp
        return lambda: xformers_splitk_fhma().apply(fhma_input, needs_gradient=False)

    def colfax_cutlass_preprocess(self, q, k, v):
        return (
            torch.transpose(q, 1, 2),
            torch.transpose(k, 1, 2),
            torch.transpose(v, 1, 2),
        )

    @register_benchmark(enabled=False)
    def colfax_cutlass(self, q, k, v):
        default_scale = 1.0 / math.sqrt(float(self.D_HEAD))
        colfax_q, colfax_k, colfax_v = self.colfax_cutlass_preprocess(q, k, v)
        return lambda: colfax_cutlass_fmha(
            self.N_CTX, self.N_CTX, self.BATCH, colfax_q, colfax_k, colfax_v, default_scale
        )

    @register_benchmark(enabled=False)
    def tk(self, q, k, v):
        o = torch.zeros_like(v)
        def tk_dispatcher():
            if self.causal:
                tk_fwd_causal.attention_forward_causal(q, k, v, o)
            else:
                tk_fwd.attention_forward(q, k, v, o)
            return o
        return tk_dispatcher
    
    @register_benchmark(enabled=False, label=f"cudnn_{torch.backends.cudnn.version()}")
    def cudnn(self, q, k, v):
        os.environ["TORCH_CUDNN_SDPA_ENABLED"] = "1"
        def sdpa_flash_attention(q, k, v):
            with sdpa_kernel([SDPBackend.CUDNN_ATTENTION]):
                return sdpa(
                    q,
                    k,
                    v,
                    is_causal=self.causal,
                    scale=self.sm_scale,
                )
        return lambda: sdpa_flash_attention(
            q,
            k,
            v,
        )


    @register_metric()
    def tflops(
        self, fn_name: str, example_inputs: Any, metrics: BenchmarkOperatorMetrics
    ) -> float:
        flops_per_matmul = (
            2.0 * self.BATCH * self.H * self.N_CTX * self.N_CTX * self.D_HEAD
        )
        tflops = 2 * flops_per_matmul
        if self.causal:
            tflops *= 0.5
        if self.mode == BenchmarkMode.BWD:
            tflops *= 2.5  # 2.0(bwd) + 0.5(recompute)
        elif self.mode == BenchmarkMode.FWD_BWD:
            tflops *= 3.5  # 1.0(fwd) + 2.0(bwd) + 0.5(recompute)
        return tflops / metrics.latency * 1e-9

    def get_bwd_fn(self, fwd_fn: Callable) -> Callable:
        o = fwd_fn()
        o_tensor = input_filter(
            lambda x: isinstance(x, torch.Tensor),
            o,
        )
        do = torch.rand_like(o_tensor)
        fn = lambda: o_tensor.backward(do, retain_graph=True)
        return fn

    def get_input_iter(self) -> Generator:
        BATCH = self.BATCH
        H = self.H
        D_HEAD = self.D_HEAD
        ctx_vals = [2**i for i in range(7, 15)]
        requires_grad = True
        for N_CTX in ctx_vals:
            q = torch.randn(
                (BATCH, H, N_CTX, D_HEAD),
                dtype=self.dtype,
                device=self.device,
                requires_grad=requires_grad,
            )
            k = torch.randn(
                (BATCH, H, N_CTX, D_HEAD),
                dtype=self.dtype,
                device=self.device,
                requires_grad=requires_grad,
            )
            v = torch.randn(
                (BATCH, H, N_CTX, D_HEAD),
                dtype=self.dtype,
                device=self.device,
                requires_grad=requires_grad,
            )
            self.N_CTX = N_CTX
            yield (q, k, v)

    @register_x_val(label="SeqLen")
    def get_x_val(self, example_inputs) -> float:
        return self.N_CTX

    def plot(self):
        y_metric_name = "tflops"

        @triton.testing.perf_report(
            triton.testing.Benchmark(
                x_names=["N_CTX"],  # argument names to use as an x-axis for the plot
                x_vals=self.output.x_vals,  # different possible values for `x_name`
                line_arg="provider",  # argument name whose value corresponds to a different line in the plot
                line_vals=[
                    "aten",
                    "sdpa",
                    "flash_v2",
                    "triton_tutorial_flash_v2",
                    "triton_op_flash_v2",
                    # FIXME: cuda illegal meory failure with default config
                    # "triton_op_flash_seq_v2",
                    "xformers",
                    "hw_roofline",
                ],  # possible values for `line_arg``
                line_names=[
                    "ATen",
                    "SDPA",
                    "Flash V2",
                    "Triton Tutorial Flash V2",
                    "Triton Op Flash V2",
                    # FIXME: cuda illegal meory failure with default config
                    # "Triton Op Flash (Seq Parallel) V2",
                    "XFormers",
                    "Hardware Roofline",
                ],  # label name for the lines
                styles=[
                    ("blue", "-"),
                    ("yellow", "-"),
                    ("green", "-"),
                    ("red", "-"),
                    ("brown", "-"),
                    # FIXME: for "Triton Op Flash (Seq Parallel) V2", which had
                    # cuda illegal meory failure with default config
                    # ("orange", "-"),
                    ("purple", "-"),
                    ("black", "dashed"),
                ],  # line styles
                ylabel=y_metric_name,  # label name for the y-axis
                plot_name="flashattention-tflops",  # name for the plot. Used also as a file name for saving the plot.
                args={},  # values for function arguments not in `x_names` and `y_name`
            )
        )
        def _plot(N_CTX, provider):
            tflops = self.output.get_y_vals(N_CTX, provider, y_metric_name)
            return tflops

        _plot.run(
            show_plots=True, print_data=False, save_path="/tmp/test_flashattention"
        )
