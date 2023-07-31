# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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
import paddle
from paddle import nn
from paddle.distributed.fleet.meta_parallel import (
    ColumnParallelLinear,
    RowParallelLinear,
)
from paddle.quantization import PTQ, QAT, QuantConfig
from paddle.quantization.quanters.abs_max import FakeQuanterWithAbsMaxObserverLayer
from paddleslim.quant.advanced import (
    EMASampler,
    MultiStepSampler,
    PieceWiseSearch,
    Shift,
    Smooth,
)
from paddleslim.quant.layers import (
    QuantizedColumnParallelLinear,
    QuantizedRowParallelLinear,
)
from paddleslim.quant.observers import AbsMaxChannelWiseWeightObserver, AbsmaxObserver
from paddleslim.quant.quanters import PACTQuanter

from paddlenlp.peft import PrefixModelForCausalLM
from paddlenlp.peft.lora import LoRALinear
from paddlenlp.peft.lora.lora_quant_layers import QuantedLoRALinear


def create_qat_model(quant_args, model, dtype):
    # FakeQuanterChannelWiseAbsMaxObserver not yet merge in Paddle develop
    from paddle.quantization.quanters import FakeQuanterChannelWiseAbsMaxObserver

    q_config = QuantConfig(activation=None, weight=None)
    q_config.add_qat_layer_mapping(LoRALinear, QuantedLoRALinear)
    if quant_args.qat_type == "A8W8":
        activation = PACTQuanter(quanter=FakeQuanterWithAbsMaxObserverLayer, init_value=20, dtype=dtype)
        weight = FakeQuanterChannelWiseAbsMaxObserver(bit_length=8, dtype="float32")
    elif quant_args.qat_type == "W4":
        activation = None
        weight = FakeQuanterChannelWiseAbsMaxObserver(bit_length=4, dtype="float32")
    elif quant_args.qat_type == "A8W4":
        activation = PACTQuanter(quanter=FakeQuanterWithAbsMaxObserverLayer, init_value=20, dtype=dtype)
        weight = FakeQuanterChannelWiseAbsMaxObserver(bit_length=4, dtype="float32")
    else:
        raise ValueError("qat_type should be one of ['A8W8', 'W4', 'A8W4']")
    q_config.add_type_config(LoRALinear, weight=weight, activation=activation)
    q_config.add_type_config(nn.Linear, weight=weight, activation=activation)

    qat = QAT(q_config)
    model = qat.quantize(model, inplace=True)
    return model


def apply_shift(quant_args, trainer, ptq_dataloader, ptq_model_config):
    shift_sampler = EMASampler() if quant_args.shift_sampler == "ema" else None
    shift = Shift(
        model=trainer.model,
        model_config=ptq_model_config,
        sample_function=shift_sampler,
        shift_all_linears=quant_args.shift_all_linears,
    )

    trainer.ptq_loop(
        ptq_dataloader,
        description="Shift",
        max_eval_iters=quant_args.shift_step,
    )
    shift.update_weight()
    del shift, shift_sampler


def apply_smooth(quant_args, trainer, ptq_dataloader, ptq_model_config):
    smooth_sampler = MultiStepSampler() if quant_args.smooth_sampler == "multi_step" else None
    if quant_args.smooth_piecewise_search:
        search_func = PieceWiseSearch(
            k_piece=quant_args.smooth_k_piece,
            bits_length=8,
            search_piece=quant_args.smooth_search_piece,
            search_alpha_min=0.2,
            search_alpha_max=0.8,
            search_scale_min=1.0,
            search_scale_max=5.0,
            weight_quant_method="abs_max_channel_wise",
            act_quant_method="abs_max",
        )
    else:
        search_func = None
    smooth = Smooth(
        trainer.model,
        ptq_model_config,
        alpha=0.5,
        smooth_all_linears=quant_args.smooth_all_linears,
        sample_function=smooth_sampler,
        search_function=search_func,
    )
    trainer.ptq_loop(
        ptq_dataloader,
        description="Smooth",
        max_eval_iters=quant_args.smooth_step,
    )

    smooth.update_weight()
    del smooth, smooth_sampler, search_func


def apply_ptq(quant_args, trainer, ptq_dataloader):
    q_config = QuantConfig(activation=None, weight=None)
    act_quanter = AbsmaxObserver()
    weight_quanter = AbsMaxChannelWiseWeightObserver()
    q_config.add_qat_layer_mapping(ColumnParallelLinear, QuantizedColumnParallelLinear)
    q_config.add_qat_layer_mapping(RowParallelLinear, QuantizedRowParallelLinear)
    q_config.add_type_config(
        [paddle.nn.Linear, ColumnParallelLinear, RowParallelLinear, QuantedLoRALinear],
        activation=act_quanter,
        weight=weight_quanter,
    )

    ptq = PTQ(q_config)
    trainer.model = ptq.quantize(trainer.model, inplace=True)
    trainer.ptq_loop(
        ptq_dataloader,
        description="PTQ",
        max_eval_iters=quant_args.ptq_step,
    )
    trainer.model = ptq.convert(trainer.model, inplace=True)


def get_ptq_model_config(model):
    if isinstance(model, PrefixModelForCausalLM):
        base_model_prefix = model.model.base_model_prefix
    else:
        base_model_prefix = model.base_model_prefix

    if base_model_prefix in ["chatglm"]:
        raise NotImplementedError(f"{model} does not support Shift or Smooth.")
    elif base_model_prefix == "chatglm_v2":
        model_config = {"fused_qkv": False, "parallel_ffn": False}
    elif base_model_prefix == "bloom":
        model_config = {"fused_qkv": True, "parallel_ffn": False}
    elif base_model_prefix == "llama":
        model_config = {"fused_qkv": False, "parallel_ffn": True}
    else:
        raise ValueError(
            f"Unknown base_model_prefix: {model.base_model_prefix}. Supported base_model_prefix list: chatglm, bloom, llama."
        )
    return model_config